"""Moza wheel RPM LEDs: a shift light driven by simdash, or by monocoque, or off.

The old R5 base with the ES rim takes monocoque's "MozaR5" frame on the base's serial port:
7e 06 41 13 fd de <4-byte big-endian mask> <checksum>, bits 0-9 = the 10 LEDs. The rim only
listens when boxflat's "RPM Indicator Mode" is "RPM" ("On" is a static rainbow).

monocoque's driver has fixed thresholds (10..92 %, blink from 94 %), so simdash can drive the
LEDs itself instead: a bar from a start point up to a shift point (per car if wanted), then all
LEDs flash until you change gear. Only one of them may write to the port: choosing simdash or
off removes monocoque's Moza device from monocoque.config, choosing monocoque adds it back.

Test sources need no game: a pedal read straight from the base's joystick device (each pedal is
an axis), or a sweep.
"""

import array
import fcntl
import glob
import json
import logging
import math
import os
import termios
import threading
import time

from . import shakers, store

log = logging.getLogger("simdash.leds")

SETTINGS = store.DATA_DIR / "leds.json"
N_LEDS = 10
DEFAULTS = {
    "driver": None,          # "simdash" | "monocoque" | "off"; None = guess from monocoque.config
    "device_id": 0x13,       # serial device the frame goes to: 19 = base (what works on the old R5)
    "start": 0.75,           # first LED at this fraction of the shift point
    "shift": 0.95,           # shift point as a fraction of the car's max RPM, unless set per car
    "blink_hz": 6.0,         # flashes per second past the shift point
    "top_gear_blink": False, # flash in top gear too (nothing to shift to)
    "cars": {},              # car name -> {"shift_rpm": int}
    "axis": 2,               # input axis code for the pedal test (Z = throttle on this rig)
    "axis_labels": {"2": "Throttle", "5": "Brake"},  # axis code -> pedal name
    "axis_inverts": {"5": True},                      # axis code -> reads 100 % when released
}
PORT_GLOB = "/dev/serial/by-id/usb-Gudsen_MOZA*-if00"
INPUT_GLOB = "/dev/input/by-id/usb-Gudsen_MOZA*-event-joystick"
AXIS_NAMES = {0: "X", 1: "Y", 2: "Z", 3: "RX", 4: "RY", 5: "RZ", 6: "Throttle axis", 7: "Rudder axis",
              8: "Wheel axis", 9: "Gas axis", 10: "Brake axis"}  # Linux's names, not necessarily what is plugged in
TEST_SECONDS = 120   # a test stops by itself after this
STALE = 0.5          # s without game data before the race source goes quiet
CLOSE_AFTER = 10.0   # s of quiet before the port is let go
KEEPALIVE = 0.25     # s between repeats of an unchanged frame


# ---------- settings ----------

def load_settings():
    s = dict(DEFAULTS)
    try:
        s.update(json.loads(SETTINGS.read_text()))
    except (OSError, ValueError):
        pass
    if "axis_invert" in s:  # older file: one reverse flag for the chosen axis
        if s.pop("axis_invert") and s.get("axis") is not None:
            s["axis_inverts"] = {**s["axis_inverts"], str(s["axis"]): True}
    if s["driver"] not in ("simdash", "monocoque", "off"):
        s["driver"] = "monocoque" if monocoque_entry() else "off"
    return s


def save_settings(patch):
    s = load_settings()
    for k, v in patch.items():
        if k not in DEFAULTS or k == "driver":
            continue
        s[k] = v
    s["start"] = min(0.99, max(0.0, float(s["start"])))
    s["shift"] = min(1.0, max(0.3, float(s["shift"])))
    s["blink_hz"] = min(15.0, max(1.0, float(s["blink_hz"])))
    s["axis_labels"] = {str(k): str(v) for k, v in (s["axis_labels"] or {}).items() if v}
    s["axis_inverts"] = {str(k): True for k, v in (s["axis_inverts"] or {}).items() if v}
    s["cars"] = {str(k): {"shift_rpm": int(v["shift_rpm"])} for k, v in (s["cars"] or {}).items()
                 if v and v.get("shift_rpm")}
    _write(s)
    return s


def _write(s):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)


def find_port():
    ports = sorted(glob.glob(PORT_GLOB))
    return ports[0] if ports else None


def find_input():
    devs = sorted(glob.glob(INPUT_GLOB))
    return devs[0] if devs else None


# ---------- monocoque.config: its Moza device ----------

def _is_moza(d):
    return (str(d.get("device", "")).lower() == "serial" and str(d.get("type", "")).lower() == "wheel"
            and str(d.get("subtype", "")).lower().startswith("moza"))


def monocoque_entry():
    try:
        cfg = shakers._read_cfg()
    except (OSError, ValueError):
        return None
    return next((d for d in shakers._default_entry(cfg).get("devices", []) if _is_moza(d)), None)


def set_driver(driver):
    """Who writes to the LEDs. monocoque gets its Moza device in monocoque.config only when it is the driver."""
    if driver not in ("simdash", "monocoque", "off"):
        raise ValueError("driver is simdash, monocoque or off")
    cfg = shakers._read_cfg()
    entry = shakers._default_entry(cfg)
    devices = entry.get("devices", [])
    has = any(_is_moza(d) for d in devices)
    if driver == "monocoque" and not has:
        port = find_port()
        if not port:
            raise ValueError("no Moza base found on USB")
        entry["devices"] = [{"//": "Moza wheel RPM LEDs (simdash Wheel LEDs tab)", "device": "Serial",
                             "type": "Wheel", "subtype": "MozaR5", "devpath": port, "baud": 115200}] + devices
        shakers.write_cfg(cfg)
    elif driver != "monocoque" and has:
        entry["devices"] = [d for d in devices if not _is_moza(d)]
        shakers.write_cfg(cfg)
    s = load_settings()
    s["driver"] = driver
    _write(s)
    if driver != "simdash":
        driver_thread.release()
    return s


# ---------- frames ----------

def frame(mask, device_id=0x13):
    b = [0x7e, 0x06, 0x41, device_id, 0xfd, 0xde, *int(mask).to_bytes(4, "big")]
    return bytes(b + [(0x0d + sum(b)) % 256])


def bar(n):
    return (1 << max(0, min(N_LEDS, n))) - 1


def shift_rpm(s, car, max_rpm):
    own = (s["cars"].get(car) or {}).get("shift_rpm") if car else None
    return float(own) if own else s["shift"] * max_rpm


def rpm_mask(s, rpm, max_rpm, gear, num_gears, car, t):
    """(LED mask, phase) for an engine speed: a bar up to the shift point, then flashing."""
    if not max_rpm or max_rpm <= 0:
        return 0, "idle"
    top = shift_rpm(s, car, max_rpm)
    start = s["start"] * top
    can_shift = gear is None or (gear > 0 and (s["top_gear_blink"] or not num_gears or gear < num_gears))
    if rpm >= top and can_shift:
        on = int(t * s["blink_hz"] * 2) % 2 == 0
        return (bar(N_LEDS) if on else 0), "shift"
    if rpm <= start:
        return 0, "bar"
    return bar(math.ceil(N_LEDS * (rpm - start) / max(1.0, top - start))), "bar"


# ---------- pedal axes from the base's joystick device ----------

def _ioc(nr, size):
    return (2 << 30) | (size << 16) | (ord("E") << 8) | nr


class Axes:
    """Current value of each absolute axis of the base (wheel and pedals), by ioctl: no event parsing."""

    def __init__(self):
        self.fd, self.path, self.codes = None, None, []

    def _open(self):
        path = find_input()
        if not path:
            return False
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.path = path
        bits = array.array("B", [0] * 8)
        fcntl.ioctl(self.fd, _ioc(0x20 + 3, 8), bits)  # EVIOCGBIT(EV_ABS)
        mask = int.from_bytes(bytes(bits), "little")
        self.codes = [c for c in range(16) if mask >> c & 1]  # hats (16+) left out
        return True

    def read(self):
        try:
            if self.fd is None and not self._open():
                return []
            out = []
            for c in self.codes:
                a = array.array("i", [0] * 6)
                fcntl.ioctl(self.fd, _ioc(0x40 + c, 24), a)  # EVIOCGABS: value, min, max, ...
                v, lo, hi = a[0], a[1], a[2]
                out.append({"code": c, "name": AXIS_NAMES.get(c, str(c)),
                            "value": (v - lo) / (hi - lo) if hi > lo else 0.0})
            return out
        except OSError:
            self.close()
            return []

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None


# ---------- the writer ----------

class Driver:
    """One thread writes the LED frames (~30 Hz): a running test first, else the live game when
    simdash is the driver. Quiet sources get one all-off frame; the port is let go after a while."""

    PERIOD = 1 / 30

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.fd = None
        self.port = None
        self.game = None          # (monotonic time, rpm, max_rpm, gear, num_gears, car)
        self.test = None          # (mode, until)
        self.axes = Axes()
        self.settings = None
        self.state = {"source": "idle", "mask": 0, "phase": "idle", "error": None}
        self.release_req = False

    # called by the recorder for every sample while driving
    def feed(self, snap):
        with self.lock:
            self.game = (time.monotonic(), snap["rpm"], snap["max_rpm"], snap["gear"],
                         snap.get("num_gears"), snap.get("car"))
        self._ensure()

    def start_test(self, mode):
        if mode not in ("pedal", "sweep"):
            raise ValueError("test is pedal or sweep")
        with self.lock:
            self.test = (mode, time.monotonic() + TEST_SECONDS, time.monotonic())
        self.reload()
        self._ensure()

    def stop_test(self):
        with self.lock:
            self.test = None

    def reload(self):
        with self.lock:
            self.settings = None

    def release(self):
        with self.lock:
            self.release_req = True

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
            st["test"] = self.test[0] if self.test else None
        st["axes"] = self.axes.read()
        return st

    def _ensure(self):
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name="leds")
                self.thread.start()

    def _settings(self):
        with self.lock:
            if self.settings is None:
                self.settings = load_settings()
            return self.settings

    def _compute(self, now):
        s = self._settings()
        with self.lock:
            test, game = self.test, self.game
            if test and now > test[1]:
                self.test = test = None
        t = time.monotonic()
        if test:
            mode, _, began = test
            if mode == "sweep":  # fill up over 2 s, then 1.5 s past the shift point
                k = (t - began) % 3.5
                frac = s["shift"] * (s["start"] + (1 - s["start"]) * k / 2.0 if k < 2.0 else 1.0)
                m, ph = rpm_mask(s, frac * 10000, 10000, None, None, None, t)
                return "sweep", m, ph, {"fraction": round(frac, 3)}
            axes = {a["code"]: a["value"] for a in self.axes.read()}
            v = axes.get(s["axis"])
            if v is None:
                return "pedal", 0, "idle", {"error": "choose the pedal (axis) to test with"}
            if s["axis_inverts"].get(str(s["axis"])):
                v = 1 - v
            m, ph = rpm_mask(s, v * 10000, 10000, None, None, None, t)  # pedal travel stands for RPM / max RPM
            return "pedal", m, ph, {"fraction": round(v, 3)}
        if game and s["driver"] == "simdash" and t - game[0] < STALE:
            _, rpm, max_rpm, gear, num_gears, car = game
            m, ph = rpm_mask(s, rpm, max_rpm, gear, num_gears, car, t)
            return "game", m, ph, {"rpm": round(rpm), "max_rpm": round(max_rpm), "car": car,
                                   "shift_rpm": round(shift_rpm(s, car, max_rpm)) if max_rpm else None}
        return "idle", 0, "idle", {}

    def _open(self, s):
        port = find_port()
        if not port:
            raise OSError("no Moza base found on USB")
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, a)
        self.fd, self.port = fd, port

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _send(self, s, mask):
        if self.fd is None:
            self._open(s)
        os.write(self.fd, frame(mask, int(s["device_id"])))
        try:
            os.read(self.fd, 512)  # the base answers; drain so its buffer never fills
        except (BlockingIOError, OSError):
            pass

    def _run(self):
        last_mask, last_sent, quiet_since, retry_at = None, 0.0, None, 0.0
        while True:
            now = time.monotonic()
            with self.lock:
                release, self.release_req = self.release_req, False
            if release:
                self._close()
                last_mask = None
            try:
                source, mask, phase, info = self._compute(now)
            except Exception:
                log.exception("LED source")
                source, mask, phase, info = "idle", 0, "idle", {}
            err = info.pop("error", None)
            if source == "idle":
                quiet_since = quiet_since or now
            else:
                quiet_since = None
            s = self._settings()
            want_send = source != "idle" or last_mask not in (None, 0)
            if want_send and now >= retry_at and (mask != last_mask or now - last_sent > KEEPALIVE):
                try:
                    self._send(s, mask)
                    last_mask, last_sent = (mask if source != "idle" else None), now
                except OSError as e:
                    err = e.strerror or str(e)
                    self._close()
                    last_mask, retry_at = None, now + 2.0
            if quiet_since and now - quiet_since > CLOSE_AFTER and self.fd is not None:
                self._close()
            with self.lock:
                self.state = {"source": source, "mask": mask, "phase": phase, "error": err,
                              "port": self.port, **info}
            time.sleep(self.PERIOD if source != "idle" else 0.1)


driver_thread = Driver()


def setup():
    """Everything the page shows about the LEDs."""
    s = load_settings()
    return {"settings": s, "port": find_port(), "input": find_input(),
            "monocoque_entry": monocoque_entry() is not None, "monocoque_running": shakers.monocoque_running(),
            "n_leds": N_LEDS}
