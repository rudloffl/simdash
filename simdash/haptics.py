"""Moza pedal haptics: the Pedal Haptic Feedback module (USB 346e:002f) driven from AMS2's telemetry.

The module keeps no effects: the host streams them while driving. Protocol (from AZOM's
description, checked against Pit House traffic by crmne/omarchy-moza), on the module's own USB
serial port, Moza framing with the usual checksum:

    7e 0b 4d 12  01 SUB PEDAL SLOT EN  DUR(2) FREQ(2) STRENGTH(2)  checksum

SUB 01 = set (EN 00 with zero values stops that slot), PEDAL 01 throttle / 02 brake / 03 clutch,
DUR in ms (the effect plays that long after the last frame, so the host keeps resending),
FREQ in Hz (10-100; for road texture a suspension position 0-100), STRENGTH a fraction of 65535,
all big-endian.

Plugged behind a wheelbase, the module has no port of its own: the same payload goes to the base's
serial port with the extended address (AZOM, from a capture behind an R16/R21 base), one byte longer:

    7e 0c 4d 1f 1e  01 SUB PEDAL ...  checksum      (replies: cd f1)

0x1f means "an extended device", 0x1e is the module. simdash uses the module's own USB port when it
is there, else asks the base (a query frame) whether a module answers behind it.
"""

import glob
import json
import logging
import os
import select
import termios
import threading
import time

from . import store

log = logging.getLogger("simdash.haptics")

SETTINGS = store.DATA_DIR / "haptics.json"
USB_ID = ("346e", "002f")
PEDALS = {"throttle": 1, "brake": 2, "clutch": 3}
SLOTS = {"tc": 0, "abs": 1, "lock": 2, "brake_point": 3, "engine": 4, "bite": 5, "gear": 6, "spin": 7, "road": 8}
EFFECT_NAMES = {"engine": "Engine", "gear": "Gear shift", "tc": "Traction control", "abs": "ABS",
                "spin": "Wheel spin", "lock": "Wheel lockup", "brake_point": "Brake threshold",
                "bite": "Clutch bite point", "road": "Road texture"}
STALE = 0.3           # s without game data: everything stops
CLOSE_AFTER = 10.0    # s of quiet before the port is let go
TEST_SECONDS = 3.0
MAX_FRAMES = 12       # per tick
PERIOD = 1 / 30
ROAD_STEP = 0.05      # s: the sample spacing the road texture scale was tuned on


def _fx(on, hz, pct, **extra):
    return {"on": on, "hz": hz, "strength": pct, **extra}


# Pit House's "PHF-Official" preset, plus the effects it leaves off
DEFAULTS = {
    "enabled": True,
    "engine_from": 0.2,    # engine vibration from this fraction of max RPM...
    "engine_to": 1.0,      # ...rising from 10 Hz to the effect's frequency at this one
    "fitted": {"throttle": True, "brake": True, "clutch": True},  # pedals with a motor on them
    "pedals": {
        "throttle": {"engine": _fx(True, 100, 10), "tc": _fx(True, 50, 80), "gear": _fx(True, 100, 80),
                     "spin": _fx(False, 60, 60)},
        "brake": {"engine": _fx(True, 100, 10), "abs": _fx(True, 30, 100), "gear": _fx(True, 100, 80),
                  "lock": _fx(False, 40, 80), "brake_point": _fx(False, 60, 50, trigger=0.9),
                  "road": _fx(False, 0, 40)},
        "clutch": {"engine": _fx(True, 100, 10), "bite": _fx(True, 50, 50, trigger=0.7)},
    },
}
# how long each frame plays (ms) and how often it is resent (s), as Pit House does
TIMING = {"abs": (66, 0.045), "tc": (200, 0.105), "lock": (50, 0.033), "brake_point": (50, 0.033),
          "bite": (200, 0.1), "spin": (100, 0.05), "road": (100, 0.05)}


# ---------- settings ----------

def _merge(base, patch):
    out = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def _clean(s):
    s["enabled"] = bool(s["enabled"])
    s["engine_from"] = min(0.95, max(0.0, float(s["engine_from"])))
    s["engine_to"] = min(1.0, max(s["engine_from"] + 0.05, float(s["engine_to"])))
    s["fitted"] = {p: bool(v) for p, v in s["fitted"].items()}
    for fxs in s["pedals"].values():
        for name, e in fxs.items():
            e["on"] = bool(e["on"])
            e["hz"] = int(min(100, max(0 if name == "road" else 10, float(e["hz"]))))
            e["strength"] = int(min(100, max(0, float(e["strength"]))))
            if "trigger" in e:
                e["trigger"] = min(1.0, max(0.05, float(e["trigger"])))
    return s


def load_settings():
    try:
        saved = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        saved = {}
    return _clean(_merge(DEFAULTS, saved))


def save_settings(patch):
    s = _clean(_merge(load_settings(), patch))
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)
    return s


# ---------- the device ----------

def find_port():
    """The module's serial port, by its USB id (its name looks like a wheelbase's)."""
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        dev = os.path.realpath(os.path.join(tty, "device"))
        usb = os.path.dirname(dev)  # the interface's parent is the USB device
        try:
            vid = open(os.path.join(usb, "idVendor")).read().strip()
            pid = open(os.path.join(usb, "idProduct")).read().strip()
        except OSError:
            continue
        if (vid, pid) == USB_ID:
            return "/dev/" + os.path.basename(tty)
    return None


def is_module_port(path):
    """True for the haptic module's port, so the wheel LEDs never write to it."""
    try:
        real = os.path.basename(os.path.realpath(path))
        usb = os.path.dirname(os.path.realpath(f"/sys/class/tty/{real}/device"))
        return (open(os.path.join(usb, "idVendor")).read().strip(),
                open(os.path.join(usb, "idProduct")).read().strip()) == USB_ID
    except OSError:
        return False


def frame(pedal, slot, on, dur_ms=0, hz=0, strength=0, routed=False, sub=1):
    """One set frame (sub 2: query); on=False with zeros stops the slot. strength is 0..1.
    routed: through a wheelbase, with the extended address."""
    st = int(65535 * max(0.0, min(1.0, strength))) if on else 0
    dur, hz = (int(dur_ms), int(hz)) if on else (0, 0)
    head = [0x7e, 0x0c, 0x4d, 0x1f, 0x1e] if routed else [0x7e, 0x0b, 0x4d, 0x12]
    b = head + [0x01, sub, pedal, slot, 1 if on else 0,
                *dur.to_bytes(2, "big"), *hz.to_bytes(2, "big"), *st.to_bytes(2, "big")]
    return bytes(b + [(0x0d + sum(b)) % 256])


def _open_tty(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def _send(fd, data, wait=0.25):
    """Write one frame. The port is non-blocking and the module reads slower than a burst of frames
    arrives (11 of 27 back-to-back frames got EAGAIN on the real module), so wait until it takes more."""
    end = time.monotonic() + wait
    while True:
        try:
            n = os.write(fd, data)
        except BlockingIOError:
            n = 0
        data = data[n:]
        if not data:
            return
        left = end - time.monotonic()
        if left <= 0:
            raise OSError("the haptic module is not taking data")
        select.select([], [fd], [], left)


def base_ports():
    return [p for p in sorted(glob.glob("/dev/serial/by-id/usb-Gudsen_MOZA*-if00")) if not is_module_port(p)]


def probe_base(port, wait=0.4):
    """True when a haptic module answers a query through this wheelbase (reply group cd, address f1)."""
    fd = _open_tty(port)
    try:
        try:
            os.read(fd, 65536)
        except (BlockingIOError, OSError):
            pass
        _send(fd, frame(PEDALS["brake"], 0, False, routed=True, sub=2))
        buf, end = b"", time.monotonic() + wait
        while time.monotonic() < end:
            time.sleep(0.02)
            try:
                buf += os.read(fd, 4096)
            except (BlockingIOError, OSError):
                pass
            i = buf.find(b"\xcd\xf1")
            if i >= 2 and buf[i - 2] == 0x7e:
                return True
        return False
    finally:
        os.close(fd)


_probe_cache = {}  # base port -> (monotonic time, answered)


def known_port():
    """The module's port without asking the base again: its USB port, or the last base that answered."""
    return find_port() or next((b for b, (_, ok) in _probe_cache.items() if ok), None)


def find_target(now=None):
    """(port, routed) of the module: its own USB port first, else a wheelbase it answers behind."""
    port = find_port()
    if port:
        return port, False
    now = now or time.monotonic()
    for base in base_ports():
        t, ok = _probe_cache.get(base, (0, False))
        if now - t > (60 if ok else 10):
            try:
                ok = probe_base(base)
            except OSError:
                ok = False
            _probe_cache[base] = (now, ok)
            log.info("haptic module behind %s: %s", base, "yes" if ok else "no answer")
        if ok:
            return base, True
    return None, False


# ---------- what should play ----------

class Effects:
    """From one telemetry sample to the wanted state of every (pedal, effect):
    {(pedal, effect): (duration ms, Hz, strength 0..1, resend every s)}."""

    def __init__(self):
        self.gear = None
        self.gear_until = 0.0
        self.gear_to = None
        self.susp = None
        self.susp_t = 0.0
        self.road = 0.0
        self.why = {}         # (pedal, effect) -> why it plays, for the page

    def reset(self):
        self.gear, self.gear_until, self.susp, self.road = None, 0.0, None, 0.0
        self.why = {}

    def compute(self, s, g, now):
        want, why = {}, {}
        self.why = why
        rpm, max_rpm = g["rpm"], g["max_rpm"]
        if g["gear"] != self.gear:
            if self.gear is not None and g["gear"] != 0:
                self.gear_until = now + 0.2
                self.gear_to = (self.gear, g["gear"])
            self.gear = g["gear"]
        pct = lambda v: f"{round(100 * v)} %"
        gear_name = lambda n: "R" if n < 0 else "N" if n == 0 else str(n)
        flags = g["car_flags"]
        abs_on = (g["abs"] or flags & 0x10) and g["brake"] > 0.06
        tc_on = flags & 0x40 and g["throttle"] > 0.06
        slips = [x for x in (g["slips"] or []) if x is not None]
        moving = g["speed"] > 2
        # road texture: how much the suspension moves from one sample to the next
        # per 20 Hz sample (the recorder's rate, which the scale below was tuned on), whatever the feed's rate:
        # the haptics engine feeds 100 Hz, and the loop here runs at 30 Hz over either
        susp = g["susp"]
        if susp and self.susp and susp != self.susp:
            k = min(4.0, max(0.05, (now - self.susp_t) / ROAD_STEP))
            move = max(abs(a - b) for a, b in zip(susp, self.susp)) / k
            self.road = max(move, self.road * 0.7 ** k)
        if susp != self.susp:
            self.susp, self.susp_t = susp, now
        for pedal, fxs in s["pedals"].items():
            if not s["fitted"].get(pedal, True):
                continue  # no motor on this pedal
            for name, e in fxs.items():
                if not e["on"] or not e["strength"]:
                    continue
                amp, hz = e["strength"] / 100, e["hz"]
                if name == "engine":
                    if not max_rpm or rpm < s["engine_from"] * max_rpm:
                        continue
                    k = min(1.0, (rpm / max_rpm - s["engine_from"]) / (s["engine_to"] - s["engine_from"]))
                    f = 10 + (hz - 10) * k
                    want[pedal, name] = (int(1000 / max(f, 10)) + 1, f, amp, PERIOD)
                    why[pedal, name] = f"{round(rpm)} rpm, {pct(rpm / max_rpm)} of max"
                    continue
                if name == "gear":
                    active = now < self.gear_until
                    timing = (200, 0.2)
                elif name == "abs":
                    active = abs_on
                elif name == "tc":
                    active = tc_on
                elif name == "spin":
                    active = bool(slips) and max(slips) > 0.15 and g["throttle"] > 0.1 and moving
                elif name == "lock":
                    active = bool(slips) and min(slips) < -0.2 and g["brake"] > 0.05 and moving
                elif name == "brake_point":
                    active = g["brake"] >= e["trigger"]
                elif name == "bite":
                    active = abs(g["clutch"] - e["trigger"]) <= 0.05
                elif name == "road":
                    # a position near 100 is still, lower is a bump (Pit House dips toward 37)
                    hz = int(100 - min(63, self.road * 3000))
                    active = moving and hz < 98
                else:
                    active = False
                if active:
                    dur, every = (200, 0.2) if name == "gear" else TIMING[name]
                    want[pedal, name] = (dur, hz, amp, every)
                    why[pedal, name] = _reason_text(self, name, g, e, slips, pct, gear_name)
        return want


def _reason_text(fx, name, g, e, slips, pct, gear_name):
    """Why a pedal effect plays, in words, for the page."""
    if name == "gear":
        a, b = fx.gear_to or (None, g["gear"])
        return f"shift {gear_name(a)} → {gear_name(b)}" if a is not None else f"shift to {gear_name(b)}"
    if name == "abs":
        return f"ABS working, brake {pct(g['brake'])}"
    if name == "tc":
        return f"traction control cutting, throttle {pct(g['throttle'])}"
    if name == "spin":
        return f"wheel spinning {pct(max(slips))} faster than the car, throttle {pct(g['throttle'])}"
    if name == "lock":
        return f"wheel {pct(-min(slips))} slower than the car, brake {pct(g['brake'])}"
    if name == "brake_point":
        return f"brake {pct(g['brake'])}, past the {pct(e['trigger'])} trigger"
    if name == "bite":
        return f"clutch {pct(g['clutch'])}, near the {pct(e['trigger'])} bite point"
    if name == "road":
        return f"suspension moving {fx.road * 1000:.1f} mm per sample"
    return ""



# ---------- the writer ----------

class Driver:
    """One thread (~30 Hz) writes the frames: a running test first, else the live game."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.fd = None
        self.port = None
        self.routed = False    # through a wheelbase
        self.game = None       # (monotonic time, telemetry dict)
        self.test = None       # (pedal, effect, until)
        self.settings = None
        self.fx = Effects()
        self.state = {"source": "idle", "playing": [], "error": None}
        self.on_start = None   # called in the thread as it starts (the haptics engine makes it real-time)

    def feed(self, snap, slips):
        susp = (snap.get("raw") or {}).get("mSuspensionTravel")
        g = {k: snap[k] for k in ("rpm", "max_rpm", "gear", "car_flags", "abs", "brake", "throttle", "clutch", "speed")}
        g["slips"], g["susp"] = slips, list(susp) if susp else None
        self.feed_values(g, "game")

    def feed_values(self, g, source="game"):
        """One sample, from the live game or from a race replay (the replay plays even when
        "Vibrate the pedals while driving" is off: it is there to try the effects)."""
        with self.lock:
            self.game = (time.monotonic(), g, source)
        self._ensure()

    def start_test(self, pedal, effect):
        if pedal not in PEDALS or effect not in SLOTS:
            raise ValueError("unknown pedal or effect")
        with self.lock:
            self.test = (pedal, effect, time.monotonic() + TEST_SECONDS)
            self.settings = None
        self._ensure()

    def reload(self):
        with self.lock:
            self.settings = None

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def _ensure(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="haptics")
                self.thread.start()

    def _settings(self):
        with self.lock:
            if self.settings is None:
                self.settings = load_settings()
            return self.settings

    def _wanted(self, now):
        s = self._settings()
        with self.lock:
            test, game = self.test, self.game
            if test and now > test[2]:
                self.test = test = None
        if test and not s["fitted"].get(test[0], True):
            test = None
        if test:
            pedal, name, _ = test
            e = s["pedals"].get(pedal, {}).get(name) or {"hz": 50, "strength": 50}
            hz = e["hz"] if name != "road" else 60
            self.fx.why = {(pedal, name): "Test button"}
            return "test", {(pedal, name): (200, hz, e["strength"] / 100, 0.1)}
        if game and (s["enabled"] or game[2] == "replay") and now - game[0] < STALE:
            return game[2], self.fx.compute(s, game[1], now)
        self.fx.reset()
        return "idle", {}

    def _open(self):
        port, routed = find_target()
        if not port:
            raise OSError("no Moza pedal haptic module, on USB or behind the wheelbase")
        fd = _open_tty(port)
        self.fd, self.port, self.routed = fd, port, routed
        for p in PEDALS.values():  # a fresh connection starts from silence
            for slot in SLOTS.values():
                _send(fd, frame(p, slot, False, routed=routed))

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _write(self, frames):
        if self.fd is None:
            self._open()
        for f in frames:
            _send(self.fd, frame(*f, routed=self.routed))
        try:
            os.read(self.fd, 4096)  # acknowledgements: drain so the buffer never fills
        except (BlockingIOError, OSError):
            pass

    def _run(self):
        if self.on_start:
            self.on_start("pedals")
        sent = {}              # (pedal, effect) -> (time sent, wanted tuple)
        quiet_since, retry_at, err = None, 0.0, None
        while True:
            now = time.monotonic()
            try:
                source, want = self._wanted(now)
            except Exception:
                log.exception("haptics source")
                source, want = "idle", {}
            frames = []
            for key in [k for k in sent if k not in want]:  # stops first
                frames.append((PEDALS[key[0]], SLOTS[key[1]], False))
                del sent[key]
            for key, w in want.items():
                dur, hz, amp, every = w
                last = sent.get(key)
                if last and now - last[0] < every and last[1][:3] == w[:3]:
                    continue
                frames.append((PEDALS[key[0]], SLOTS[key[1]], True, dur, hz, amp))
                sent[key] = (now, w)
            frames = frames[:MAX_FRAMES]
            if frames and now >= retry_at:
                try:
                    self._write(frames)
                    err = None
                except OSError as e:
                    err = e.strerror or str(e)
                    self._close()
                    sent.clear()
                    retry_at = now + 2.0
            quiet_since = None if want else (quiet_since or now)
            if quiet_since and now - quiet_since > 5:
                err = None  # an old error is not news once nothing wants to play
            if quiet_since and now - quiet_since > CLOSE_AFTER and self.fd is not None:
                self._close()
            with self.lock:
                self.state = {"source": source, "port": self.port if self.fd is not None else None,
                              "routed": self.routed,
                              "playing": sorted(f"{p}:{n}" for p, n in want), "error": err,
                              "detail": {f"{p}:{n}": {"hz": round(w[1]), "strength": round(w[2] * 100),
                                                      "why": self.fx.why.get((p, n), "")}
                                         for (p, n), w in want.items()}}
            time.sleep(PERIOD if want or source != "idle" else 0.1)


driver_thread = Driver()


def setup():
    port, routed = find_target()
    return {"settings": load_settings(), "port": port, "routed": routed, "pedals": list(PEDALS),
            "names": EFFECT_NAMES}
