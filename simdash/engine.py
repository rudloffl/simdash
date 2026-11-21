"""simdash's haptics engine: plays every shaker effect and the Moza pedal haptics itself, in place of monocoque.

    python -m simdash.engine          (~/.local/bin/simdash-haptics; start-race and the System tab start it)

A process of its own, so the recorder, the database and the web server never hold it up. It reads
AMS2's shared memory ($pcars2$) directly, once per audio block (every BLOCK s, 100 Hz), turns it
into each effect's frequency and strength with the formulas replay.py already uses for monocoque's
effects and simdash's own, and writes the 8-channel signal to the shaker card through
libpulse-simple. Its writes block until the card has room, so the card's clock paces the loop:
no drift and a short, fixed buffer (LATENCY).

Settings are the same files as before: monocoque's Sound devices (~/.config/monocoque/monocoque.config)
and simdash's own effects (effects.json). They are read again whenever they change, so a saved
change or a preset applies at once, with no restart.

Every second it writes what it is doing to STATUS (the System tab shows it): telemetry rate seen,
synthesis time per block, the card's latency, and what each channel plays.
"""

import ctypes
import math
import ctypes.util
import fcntl
import json
import logging
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import felt, haptics, link, replay, shakers
from .pcars2 import FIELDS, GAME_PLAYING, OFF, Pcars2Reader, _unpack

log = logging.getLogger("simdash.engine")

RATE = replay.RATE      # Hz: 8000, the shakers feel nothing near 4 kHz
BLOCK = 0.01            # s of sound per step, i.e. a telemetry read every 10 ms
LATENCY = 0.03          # s of sound buffered ahead of the card (PulseAudio's target length), unless SETTINGS says
SETTINGS = Path.home() / ".local/share/simdash/engine.json"   # {"buffer_ms": ...}: the System tab's choice
BUFFERS_MS = (15, 20, 25, 30, 40)    # not under 15: the engine writes 10 ms blocks, a 10 ms buffer runs dry every block
TEST_BUFFERS_MS = (30, 20, 15)       # the latency test, in this order
TEST_SECONDS = 60.0                  # of playing (driving) per buffer
TEST_FILE = Path.home() / ".local/state/simdash-latency-test.json"
STALE = 0.25            # s without a new sample from the game before going silent
IDLE = 10.0             # s of silence before letting the card go
RELOAD = 1.0            # s between looks at the settings files
STATE = Path.home() / ".local/state"
STATUS = STATE / "simdash-haptics.json"
LAST = STATE / "simdash-haptics-last.json"  # the summary of the last run, kept after the engine stops
RT_PRIORITY = 10        # SCHED_RR priority asked of RTKit: below PipeWire's own threads (20), above everything else
LOG_EVERY = 30.0
LOCK = STATE / "simdash-haptics.lock"


# ---------- telemetry: only the fields the effects use ----------

_KINDS = {name: (kind, count) for name, kind, count, pc in FIELDS if not pc}
_WANT = {"game_state": "mGameState", "car": "mCarName", "speed": "mSpeed", "rpm": "mRpm", "max_rpm": "mMaxRPM",
         "gear": "mGear", "abs": "mAntiLockActive", "local_vel": "mLocalVelocity", "ang_vel": "mAngularVelocity",
         "terrain": "mTerrain", "tyre_rps": "mTyreRPS", "susp_vel": "mSuspensionVelocity",
         "local_acc": "mLocalAcceleration", "seq": "mSequenceNumber",
         # the pedal haptics' inputs
         "throttle": "mThrottle", "brake": "mBrake", "clutch": "mClutch", "car_flags": "mCarFlags",
         "susp": "mSuspensionTravel"}


class Telemetry(Pcars2Reader):
    """Pcars2Reader decoding a dozen fields instead of the whole block (no cars, no strings but the car)."""

    def sample(self):
        buf = self._raw()
        if buf is None:
            return None
        return {k: _unpack(buf, OFF[f], *_KINDS[f]) for k, f in _WANT.items()}


# ---------- output: libpulse-simple through ctypes ----------

class _Spec(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("rate", ctypes.c_uint32), ("channels", ctypes.c_uint8)]


class _Map(ctypes.Structure):
    _fields_ = [("channels", ctypes.c_uint8), ("map", ctypes.c_int * 32)]


class _Attr(ctypes.Structure):
    _fields_ = [(k, ctypes.c_uint32) for k in ("maxlength", "tlength", "prebuf", "minreq", "fragsize")]


PA_STREAM_PLAYBACK = 1
PA_SAMPLE_S16LE = 3
_ANY = 0xFFFFFFFF  # (uint32_t) -1: the server's default


class PulseOut:
    """One 8-channel playback stream on one sink; write() blocks until the stream has room."""

    _lib = _pulse = None

    def __init__(self, devid, latency=LATENCY):
        cls = type(self)
        if cls._lib is None:
            cls._lib = ctypes.CDLL(ctypes.util.find_library("pulse-simple") or "libpulse-simple.so.0")
            cls._pulse = ctypes.CDLL(ctypes.util.find_library("pulse") or "libpulse.so.0")
            cls._lib.pa_simple_new.restype = ctypes.c_void_p
            cls._lib.pa_simple_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t,
                                                 ctypes.POINTER(ctypes.c_int)]
            cls._lib.pa_simple_get_latency.restype = ctypes.c_uint64
            cls._lib.pa_simple_get_latency.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            cls._lib.pa_simple_free.argtypes = [ctypes.c_void_p]
            cls._lib.pa_simple_drain.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            cls._pulse.pa_strerror.restype = ctypes.c_char_p
        spec = _Spec(PA_SAMPLE_S16LE, RATE, 8)
        cmap = _Map()
        if not self._pulse.pa_channel_map_parse(ctypes.byref(cmap), ",".join(shakers.CHANNEL_MAP).encode()):
            raise OSError("bad channel map")
        frame = 2 * 8
        attr = _Attr(_ANY, int(latency * RATE) * frame, _ANY, int(BLOCK * RATE) * frame, _ANY)
        err = ctypes.c_int(0)
        self.s = self._lib.pa_simple_new(None, b"simdash", PA_STREAM_PLAYBACK, devid.encode(), b"simdash haptics",
                                         ctypes.byref(spec), ctypes.byref(cmap), ctypes.byref(attr), ctypes.byref(err))
        if not self.s:
            raise OSError(f"PulseAudio: {self._pulse.pa_strerror(err.value).decode()}")
        self.devid = devid

    def write(self, pcm):
        err = ctypes.c_int(0)
        if self._lib.pa_simple_write(self.s, pcm, len(pcm), ctypes.byref(err)) < 0:
            raise OSError(f"PulseAudio: {self._pulse.pa_strerror(err.value).decode()}")

    def latency(self):
        """Seconds between a sample written now and the card playing it."""
        err = ctypes.c_int(0)
        return self._lib.pa_simple_get_latency(self.s, ctypes.byref(err)) / 1e6

    def close(self):
        if self.s:
            self._lib.pa_simple_free(self.s)
            self.s = None


# ---------- output shaping: per-shaker volume, ducking, limiter ----------

class Shaper:
    """Applied to each block after the effects are summed (shakers.load_shaping() settings).

    Limiter: each shaker's sum is kept under the ceiling by a smooth gain instead of being clipped (a clipped
    sine turns into a harsh buzz and buries a knock under the rest). The block is computed before it is played,
    so the gain drops at the start of the block that needs it, and recovers over release_ms.
    Ducking (see Engine.synth): continuous effects give way while a knock or gear kick plays on the same shaker."""

    def __init__(self):
        self.g = [1.0] * 8          # limiter gain per channel
        self.reduction = {}         # channel -> % the limiter takes off (feedback, status)
        self.peak = [0.0] * 8       # highest block peak since the last status, before the limiter (x full scale)
        self.set(shakers.load_shaping())

    def set(self, s):
        self.s = s
        m = s["master"] / 100
        self.gains = [m * s["gains"].get(str(c), 100) / 100 for c in range(8)]

    def process(self, mix, n):
        s = self.s
        ceil = s["ceiling"] / 100
        rel = 1 - math.exp(-BLOCK / (s["release_ms"] / 1000))
        red = {}
        for c in range(8):
            gc = self.gains[c]
            peak = max(abs(mix[8 * j + c]) for j in range(n)) * gc
            if peak > self.peak[c]:
                self.peak[c] = peak
            g0 = self.g[c]
            if s["limiter"]:
                target = min(1.0, ceil / peak) if peak > 0 else 1.0
                g1 = target if target < g0 else min(target, g0 + (1.0 - g0) * rel)
            else:
                g1 = 1.0
            self.g[c] = g1
            if g1 < 0.995:
                red[str(c)] = round(100 * (1 - g1))
            if g1 >= g0:  # recovering: glide up across the block
                step = (g1 - g0) / n
                for j in range(n):
                    mix[8 * j + c] *= gc * (g0 + step * j)
            else:         # turning down: at once, from the block's first sample
                k = gc * g1
                for j in range(n):
                    mix[8 * j + c] *= k
        self.reduction = red


# ---------- the engine ----------

def _mtime(p):
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


class Engine:
    def __init__(self, reader=None, out_factory=PulseOut, pedals=None, sender=None, inbox=None):
        self.reader = reader or Telemetry()
        self.buffer = LATENCY   # s: the stream's buffer (load_settings, or the latency test's current one)
        self.test = None        # the latency test while it runs (start_test)
        self.spent = {}         # what else took time since the last write (s), for the dropout log
        self.inbox = inbox      # link.Inbox: replay and test sound and pedal commands from simdash
        self.shaper = Shaper()
        self.duck = [0.0] * 8   # how far each channel's continuous effects are ducked now (0..1)
        self.pedals = pedals    # haptics.Driver: the Moza pedals, fed from the same samples (main() passes it)
        self.sender = sender    # felt.Sender: what plays, back to simdash
        self.out_factory = out_factory
        self.out = None
        self.devices, self.streams, self.devid = [], [], ""
        self.mtimes = None
        self.checked = 0.0
        self.car, self.dia, self.dia_checked = None, None, 0.0
        self.gear = None
        self.prev = {}          # stream index -> (f, a) at the end of the last block
        self.held = {}          # Suspension / Impact stream index -> replay.Knock: its last hit, held then fading
        self.acc_base = replay.AccBase()
        self.seq, self.seq_t = None, 0.0
        self.quiet_since = time.monotonic()
        self.retry = 0.0
        self.stats = self._new_stats()
        self.levels = {}
        self.played = {}        # channel -> {effect: strength %}: everything that plays, for the feedback
        self.state = "starting"
        self.wrote = None       # when the last write returned
        self.run_t0 = time.time()
        self.total = self._new_total()   # over the whole run, seconds spent playing only
        self.window = self._new_total()  # the same since the last log line
        self.logged_state = None

    @staticmethod
    def _new_total():
        return {"seconds": 0.0, "samples": 0, "blocks": 0, "late": 0, "synth": 0.0, "synth_max": 0.0, "gap_max": 0.0,
                "lat_min": None, "lat_max": None, "lat_sum": 0.0, "lat_n": 0, "hz_min": None}

    def _add(self, tot, st, span, lat):
        hz = st["samples"] / span
        tot["seconds"] += span
        tot["samples"] += st["samples"]
        tot["blocks"] += st["blocks"]
        tot["late"] += st["late"]
        tot["synth"] += st["synth"]
        tot["synth_max"] = max(tot["synth_max"], st["synth_max"])
        tot["gap_max"] = max(tot["gap_max"], st["gap_max"])
        if span >= 0.5:  # a lowest rate over a sliver of a second means nothing
            tot["hz_min"] = hz if tot["hz_min"] is None else min(tot["hz_min"], hz)
        if lat is not None:
            tot["lat_min"] = lat if tot["lat_min"] is None else min(tot["lat_min"], lat)
            tot["lat_max"] = lat if tot["lat_max"] is None else max(tot["lat_max"], lat)
            tot["lat_sum"] += lat
            tot["lat_n"] += 1

    @staticmethod
    def summary(tot):
        """The timing over some playing seconds, for the log and the System tab."""
        if tot["seconds"] < 0.5:
            return None
        r = lambda v, nd=1: None if v is None else round(v, nd)
        return {"playing_s": round(tot["seconds"]), "telemetry_hz": r(tot["samples"] / tot["seconds"]),
                "telemetry_hz_min": r(tot["hz_min"]), "late_blocks": tot["late"],
                "synth_ms": r(1000 * tot["synth"] / tot["blocks"], 2) if tot["blocks"] else None,
                "synth_max_ms": r(1000 * tot["synth_max"], 2), "gap_max_ms": r(1000 * tot["gap_max"]),
                "priority": PRIORITY,
                "latency_ms": r(tot["lat_sum"] / tot["lat_n"]) if tot["lat_n"] else None,
                "latency_min_ms": r(tot["lat_min"]), "latency_max_ms": r(tot["lat_max"])}

    @staticmethod
    def summary_text(sm):
        return (f"{sm['playing_s']} s playing: telemetry {sm['telemetry_hz']} Hz (min {sm['telemetry_hz_min']}), "
                f"latency {sm['latency_ms']} ms ({sm['latency_min_ms']}-{sm['latency_max_ms']}), "
                f"synth {sm['synth_ms']} ms/block (max {sm['synth_max_ms']}), longest gap between writes {sm['gap_max_ms']} ms, "
                f"dropouts {sm['late_blocks']}")

    @staticmethod
    def _new_stats():
        return {"t0": time.monotonic(), "blocks": 0, "samples": 0, "synth": 0.0, "synth_max": 0.0, "late": 0,
                "gap_max": 0.0}

    # -- settings --

    def reload(self, now):
        self.checked = now
        m = (_mtime(shakers.CONFIG), _mtime(shakers.OWN_EFFECTS), _mtime(haptics.SETTINGS), _mtime(shakers.SHAPING),
             _mtime(SETTINGS))
        if m == self.mtimes:
            return
        if (self.mtimes is None or m[4] != self.mtimes[4]) and self.test is None:
            self.set_buffer(load_settings()["buffer_ms"] / 1000)
        if self.mtimes is None or m[3] != self.mtimes[3]:
            self.shaper.set(shakers.load_shaping())
        if self.mtimes is not None and m[:3] == self.mtimes[:3]:
            self.mtimes = m
            return  # only the shaping or the buffer changed: the effects keep playing as they are
        if self.pedals is not None and (self.mtimes is None or m[2] != self.mtimes[2]):
            self.pedals.reload()
        self.mtimes = m
        devices = shakers.devices_at(time.time())[0] + shakers.simdash_devices()
        try:
            devid = shakers.load()["devid"]
        except (OSError, ValueError):
            devid = ""
        self.devices = devices
        self.streams = [replay._Stream(d) for d in devices]
        self.prev, self.held = {}, {}
        if devid != self.devid and self.out is not None:
            self.out.close()
            self.out = None
        self.devid = devid
        log.info("%d effects on %s", len(devices), devid or "no card")

    def _diameters(self, car, now):
        if car != self.car or now - self.dia_checked > 5.0:
            self.car, self.dia_checked = car, now
            self.dia = replay.tyre_diameters(car)[0]
        return self.dia

    # -- one block --

    def targets(self, snap, now):
        """{stream index: (f, a)} for the continuous effects, and whether a gear kick starts now."""
        if snap is None:
            self.state = "no game"
            return {}, False
        if snap["seq"] != self.seq:
            self.seq, self.seq_t = snap["seq"], now
            self.stats["samples"] += 1
        if snap["game_state"] != GAME_PLAYING:
            self.state, self.gear = "idle", None
            return {}, False
        if now - self.seq_t > STALE:
            self.state = "frozen"
            return {}, False
        self.state = "playing"
        shifted = self.gear is not None and snap["gear"] != self.gear and snap["gear"] + 1 > 1
        self.gear = snap["gear"]
        data = replay.tick_data(snap, self.acc_base)
        slips = replay.wheel_slip(data, 0, self._diameters(snap["car"], now))
        if self.pedals is not None:
            self.pedals.feed_values(self.pedal_values(snap, slips), "game")
        out = {}
        for k, dev in enumerate(self.devices):
            env = replay.knock_envelope(dev)
            if env:
                knock = self.held.get(k) or self.held.setdefault(k, replay.Knock(*env))
                out[k] = knock.update(*replay.effect_value(dev, data, 0, slips), now)
            elif dev["effect"] != "Gear":
                out[k] = replay.effect_value(dev, data, 0, slips)
        return out, shifted

    @staticmethod
    def pedal_values(snap, slips):
        """haptics.Effects.compute's input. Its slips are the recorder's: > 0 spin, < 0 lock (monocoque's,
        here, are the other way round), and none until the tyre size is known."""
        g = {k: snap[k] for k in ("rpm", "max_rpm", "gear", "car_flags", "abs", "brake", "throttle", "clutch", "speed")}
        g["slips"] = [max(-1.0, min(1.0, -x)) for x in slips] if any(slips) else None
        g["susp"] = list(snap["susp"])
        return g

    def set_buffer(self, buffer):
        """A new buffer length (s): the stream is opened again with it at the next block."""
        if abs(buffer - self.buffer) < 1e-6:
            return
        self.buffer = buffer
        if self.out is not None:
            self.out.close()
            self.out = None
        log.info("buffer %.0f ms", buffer * 1000)

    # -- latency test: each buffer in turn for TEST_SECONDS of driving, counting dropouts --

    def start_test(self, buffers=TEST_BUFFERS_MS, seconds=None):
        seconds = seconds or TEST_SECONDS
        self.test = {"buffers": [b for b in buffers if b in BUFFERS_MS], "seconds": seconds, "i": 0,
                     "results": [], "started": time.time(), "done": False, "cur": self._new_test_run()}
        log.info("latency test: %s ms, %.0f s of driving each", self.test["buffers"], seconds)
        self.set_buffer(self.test["buffers"][0] / 1000)
        self._write_test()

    @staticmethod
    def _new_test_run():
        return {"playing_s": 0.0, "dropouts": 0, "gap_max_ms": 0.0, "lat_sum": 0.0, "lat_n": 0, "lat_max": None}

    def cancel_test(self):
        if self.test is not None:
            log.info("latency test cancelled")
            self.test["done"], self.test["cancelled"] = True, True
            self._write_test()
            self.test = None
            self.set_buffer(load_settings()["buffer_ms"] / 1000)

    def _test_tick(self, span, st, lat):
        """Once a second (status): add the playing second to the buffer being tested; move on when it has enough."""
        t = self.test
        if t is None or self.state != "playing":
            return
        c = t["cur"]
        c["playing_s"] += span
        c["dropouts"] += st["late"]
        c["gap_max_ms"] = max(c["gap_max_ms"], 1000 * st["gap_max"])
        if lat is not None:
            c["lat_sum"] += lat
            c["lat_n"] += 1
            c["lat_max"] = lat if c["lat_max"] is None else max(c["lat_max"], lat)
        if c["playing_s"] >= t["seconds"]:
            b = t["buffers"][t["i"]]
            t["results"].append({"buffer_ms": b, "playing_s": round(c["playing_s"]), "dropouts": c["dropouts"],
                                 "gap_max_ms": round(c["gap_max_ms"], 1),
                                 "latency_ms": round(c["lat_sum"] / c["lat_n"], 1) if c["lat_n"] else None,
                                 "latency_max_ms": round(c["lat_max"], 1) if c["lat_max"] is not None else None})
            log.info("latency test: %d ms buffer: %s", b, t["results"][-1])
            t["i"] += 1
            t["cur"] = self._new_test_run()
            if t["i"] >= len(t["buffers"]):
                t["done"] = True
                self._write_test()
                self.test = None
                self.set_buffer(load_settings()["buffer_ms"] / 1000)
                return
            self.set_buffer(t["buffers"][t["i"]] / 1000)
        self._write_test()

    def _write_test(self):
        t = self.test
        if t is None:
            return
        d = {k: t[k] for k in ("buffers", "seconds", "results", "started", "done")}
        d["cancelled"] = t.get("cancelled", False)
        d["current"] = None if t["done"] else {"buffer_ms": t["buffers"][t["i"]], "playing_s": round(t["cur"]["playing_s"]),
                                               "dropouts": t["cur"]["dropouts"]}
        try:
            tmp = TEST_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            tmp.replace(TEST_FILE)
        except OSError:
            pass

    def command(self, d):
        """A command from simdash (link.py): replay samples and Test buttons for the pedals."""
        if "latency_test" in d:
            if d["latency_test"] == "cancel":
                self.cancel_test()
            else:
                self.start_test()
            return
        if self.pedals is None:
            return
        if "pedal_test" in d:
            pedal, effect = d["pedal_test"]
            self.pedals.reload()  # the page saves its settings just before
            try:
                self.pedals.start_test(str(pedal), str(effect))
            except ValueError:
                pass
        elif "pedals" in d and self.state != "playing":  # the game wins over a replay
            g = d["pedals"]
            if isinstance(g, dict):
                self.pedals.feed_values(g, "replay")

    def feedback(self):
        """What plays now, to simdash (Live page, session recording), while there is something to tell."""
        if self.sender is None:
            return
        pd = {}
        if self.pedals is not None:
            pd = {k: v.get("strength", 0) for k, v in (self.pedals.snapshot().get("detail") or {}).items()}
        if self.state == "playing" or pd:
            self.sender.send(self.played, pd, self.shaper.reduction)

    DUCK_RELEASE = 0.1  # s for ducked effects to come back after a knock

    def _ducking(self, cur, kick):
        """(before, now) duck level per channel: the continuous effects give way while a knock or gear kick
        plays on their shaker (at once), and come back over DUCK_RELEASE."""
        target = self.shaper.s["duck"] / 100
        hit = set()
        for k, st in enumerate(self.streams):
            e = st.dev["effect"]
            if (e == "Gear" and (kick or st.pulse is not None)) or (e in shakers.DUCKERS and cur.get(k, (0, 0))[1] > 0):
                hit.add(st.pan)
        before = list(self.duck)
        rel = 1 - math.exp(-BLOCK / self.DUCK_RELEASE)
        self.duck = [target if c in hit else d - d * rel for c, d in enumerate(self.duck)]
        return before, self.duck

    def synth(self, cur, kick, n):
        mix = [0.0] * (8 * n)
        levels, played = {}, {}
        d0, d1 = self._ducking(cur, kick)
        for k, st in enumerate(self.streams):
            if st.dev["effect"] == "Gear":
                st.kick(mix, n, kick)
                a = st.dev.get("amplitude", 50) if st.pulse is not None else 0
            else:
                f, a = cur.get(k, (0.0, 0.0))
                f0, a0 = self.prev.get(k, (f, 0.0))
                if st.dev["effect"] in shakers.DUCKERS:
                    st.glide(mix, n, f0 if a0 > 0 else f, a0, f, a)
                else:
                    c = st.pan
                    st.glide(mix, n, f0 if a0 > 0 else f, a0 * (1 - d0[c]), f, a * (1 - d1[c]))
            if a > 0:
                p = played.setdefault(st.pan, {})
                p[st.dev["effect"]] = max(p.get(st.dev["effect"], 0), int(round(min(a, 100))))
            if a > levels.get(st.pan, [0])[0]:
                levels[st.pan] = [int(round(min(a, 100))), st.dev["effect"]]
        self.prev = cur
        self.levels, self.played = levels, played
        if self.inbox is not None:
            self.inbox.mix_into(mix, n)  # a replay or test from simdash, over the live effects
        self.shaper.process(mix, n)
        return replay._pcm(mix)

    def step(self):
        """Read the game, make one block, hand it to the card (blocks until it has room).
        Returns False when there is nothing to play and no stream open (the caller then waits)."""
        now = time.monotonic()
        c0 = time.perf_counter()
        if now - self.checked > RELOAD:
            self.reload(now)
            self._spent("settings", c0)
        if self.inbox is not None:
            c1 = time.perf_counter()
            self.inbox.drain(self.command)
            self._spent("commands", c1)
        t0 = time.perf_counter()
        snap = self.reader.sample()
        t_read = time.perf_counter() - t0
        cur, kick = self.targets(snap, now)
        loud = (kick or any(a > 0 for _, a in cur.values()) or any(s.pulse is not None for s in self.streams)
                or (self.inbox is not None and self.inbox.pending()))
        if loud:
            self.quiet_since = now
        elif self.out is not None and now - self.quiet_since > IDLE:
            self.out.close()
            self.out = None
        if self.out is None:
            self.wrote = None
            self.levels, self.played = {}, {}
            if not (loud and self.devid and now > self.retry):
                return False
            if self.devid not in {s["name"] for s in shakers.sinks()}:
                self.state, self.retry = "no card", now + 10.0  # never let PulseAudio pick the speakers
                return False
            try:
                self.out = self.out_factory(self.devid, self.buffer)
            except OSError as e:
                log.warning("cannot open %s: %s", self.devid, e)
                self.retry = now + 10.0
                return False
        n = int(BLOCK * RATE)
        pcm = self.synth(cur, kick, n)
        dt = time.perf_counter() - t0
        st = self.stats
        st["blocks"] += 1
        st["synth"] += dt
        st["synth_max"] = max(st["synth_max"], dt)
        gap = time.monotonic() - self.wrote if self.wrote is not None else 0.0
        st["gap_max"] = max(st["gap_max"], gap)
        if gap > self.buffer:
            # longer than the buffer since the last write: the card (likely) ran dry. Where the time went: the
            # read, the synthesis and the other work since the last write are timed, the rest is waiting for the CPU
            st["late"] += 1
            other = "".join(f", {k} {1000 * v:.2f} ms" for k, v in sorted(self.spent.items()) if v >= 0.0005)
            known = t_read + (dt - t_read) + sum(self.spent.values())
            log.warning("dropout: %.1f ms between writes (read %.2f ms, synthesis %.2f ms%s, the rest %.1f ms waiting)",
                        1000 * gap, 1000 * t_read, 1000 * (dt - t_read), other, 1000 * max(0.0, gap - known))
        try:
            self.out.write(pcm)
            self.wrote = time.monotonic()
            self.spent = {}
        except OSError as e:
            log.warning("%s", e)
            self.out.close()
            self.out = None
        return True

    def _spent(self, what, since):
        self.spent[what] = self.spent.get(what, 0.0) + time.perf_counter() - since

    def status(self):
        st, now = self.stats, time.monotonic()
        span = max(1e-6, now - st["t0"])
        out = {"pid": os.getpid(), "t": time.time(), "state": self.state, "devid": self.devid,
               "effects": len(self.devices), "card_open": self.out is not None,
               "telemetry_hz": round(st["samples"] / span, 1), "blocks_hz": round(st["blocks"] / span, 1),
               "synth_ms": round(1000 * st["synth"] / st["blocks"], 2) if st["blocks"] else None,
               "synth_max_ms": round(1000 * st["synth_max"], 2), "late_blocks": st["late"],
               "block_ms": BLOCK * 1000, "latency_ms": None, "levels": self.levels, "priority": PRIORITY}
        out["limiter"] = self.shaper.reduction
        out["peak"] = {str(c): round(p, 2) for c, p in enumerate(self.shaper.peak) if p > 0}
        self.shaper.peak = [0.0] * 8
        if self.inbox is not None:
            out["queued_s"] = round(len(self.inbox.buf) / link.FRAME / RATE, 2)
        if self.pedals is not None:
            p = self.pedals.snapshot()
            out["pedals"] = {"port": p.get("port"), "playing": p.get("playing", []), "error": p.get("error")}
        if self.out is not None:
            c0 = time.perf_counter()
            try:
                out["latency_ms"] = round(1000 * (self.out.latency() + BLOCK), 1)
            except OSError:
                pass
            self._spent("card latency query", c0)
        out["buffer_ms"] = round(self.buffer * 1000)
        self._test_tick(span, st, out["latency_ms"])
        if self.test is not None:
            out["latency_test"] = {"buffer_ms": round(self.buffer * 1000), "i": self.test["i"],
                                   "of": len(self.test["buffers"]), "playing_s": round(self.test["cur"]["playing_s"]),
                                   "seconds": self.test["seconds"]}
        if self.state != self.logged_state:
            log.info("state: %s", self.state)
            self.logged_state = self.state
        if self.state == "playing":
            for tot in (self.total, self.window):
                self._add(tot, st, span, out["latency_ms"])
        out["run"] = self.summary(self.total)
        self.stats = self._new_stats()
        return out

    def log_window(self):
        sm = self.summary(self.window)
        if sm:
            log.info("%s", self.summary_text(sm))
        self.window = self._new_total()

    def finish(self):
        """At exit: the whole run in the log, and in LAST for the System tab."""
        self.log_window()
        sm = self.summary(self.total)
        if sm:
            log.info("run total: %s", self.summary_text(sm))
        try:
            tmp = LAST.with_suffix(".tmp")
            tmp.write_text(json.dumps({"started": self.run_t0, "ended": time.time(), "summary": sm}))
            tmp.replace(LAST)
        except OSError:
            pass

    def run(self, stop=lambda: False):
        last_status = last_log = 0.0
        while not stop():
            try:
                played = self.step()
            except Exception:  # noqa: BLE001 - keep the shakers alive
                log.exception("engine step")
                played = False
            c0 = time.perf_counter()
            try:
                self.feedback()
            except Exception:  # noqa: BLE001
                log.exception("feedback")
            self._spent("feedback", c0)
            if not played:
                time.sleep(0.05 if self.state != "playing" else 0.01)  # no card: still feed the pedals often
            now = time.monotonic()
            if now - last_status >= 1.0:
                last_status = now
                q0 = self.spent.get("card latency query", 0.0)
                c0 = time.perf_counter()
                st = self.status()  # times its card latency query itself
                c1 = time.perf_counter()
                self.spent["status"] = self.spent.get("status", 0.0) + (c1 - c0) - (self.spent.get("card latency query", 0.0) - q0)
                _write_status(st)
                self._spent("status file", c1)
            if now - last_log >= LOG_EVERY:
                last_log = now
                c0 = time.perf_counter()
                self.log_window()
                self._spent("log", c0)


PRIORITY = "normal"


def _rtkit(method, *args):
    r = subprocess.run(["gdbus", "call", "--system", "--dest", "org.freedesktop.RealtimeKit1",
                        "--object-path", "/org/freedesktop/RealtimeKit1",
                        "--method", f"org.freedesktop.RealtimeKit1.{method}", *map(str, args)],
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0, r.stderr.strip()


def realtime():
    """Ask RTKit for real-time scheduling of this (the only) thread, as PipeWire does for its own: the game
    can then no longer keep the engine off the CPU past its buffer. RTKit wants RLIMIT_RTTIME set first
    (a thread that would hog the CPU for 200 ms straight is stopped; the engine blocks every 10 ms).
    Falls back to nice -15, then to normal priority."""
    # the *WithPID calls: RTKit would otherwise look the thread up in the caller, which is gdbus
    pid, tid = os.getpid(), threading.get_native_id()
    try:
        resource.setrlimit(resource.RLIMIT_RTTIME, (200000, 200000))
        ok, err = _rtkit("MakeThreadRealtimeWithPID", pid, tid, RT_PRIORITY)
        if ok:
            return f"real-time (SCHED_RR {RT_PRIORITY})"
        ok, err2 = _rtkit("MakeThreadHighPriorityWithPID", pid, tid, "int32 -15")  # typed: a bare -15 reads as an option
        if ok:
            log.warning("RTKit refused real-time (%s), got nice -15", err)
            return "nice -15"
        log.warning("RTKit refused real-time (%s) and nice (%s)", err, err2)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("no RTKit: %s", e)
    return "normal"


def load_settings():
    try:
        b = int(json.loads(SETTINGS.read_text()).get("buffer_ms", LATENCY * 1000))
    except (OSError, ValueError, TypeError, AttributeError):
        b = int(LATENCY * 1000)
    return {"buffer_ms": b if b in BUFFERS_MS else int(LATENCY * 1000)}


def save_settings(d):
    b = int(d.get("buffer_ms", LATENCY * 1000))
    if b not in BUFFERS_MS:
        raise ValueError(f"buffer must be one of {BUFFERS_MS} ms")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"buffer_ms": b}))
    tmp.replace(SETTINGS)
    return {"buffer_ms": b}


def read_test():
    """The last latency test (running or finished), or None."""
    try:
        return json.loads(TEST_FILE.read_text())
    except (OSError, ValueError):
        return None


def _write_status(d):
    try:
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(STATUS)
    except OSError:
        pass


def read_last():
    """The summary of the last finished run ({started, ended, summary}), or None."""
    try:
        return json.loads(LAST.read_text())
    except (OSError, ValueError):
        return None


def read_status():
    """The engine's last status, or None if it is not running (or has not written one for 3 s)."""
    try:
        d = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        return None
    if time.time() - d.get("t", 0) > 3 or not Path(f"/proc/{d.get('pid')}").exists():
        return None
    return d


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("simdash-haptics is already running")
    global PRIORITY
    PRIORITY = realtime()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # close the stream cleanly
    log.info("haptics engine: %d Hz, %.0f ms blocks, %.0f ms buffer, %s", RATE, BLOCK * 1000,
             load_settings()["buffer_ms"], PRIORITY)
    inbox = link.Inbox(RATE)
    if not inbox.open():
        log.warning("no command socket: replay and tests keep their own streams")
    eng = Engine(pedals=haptics.driver_thread, sender=felt.Sender(), inbox=inbox)
    try:
        eng.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        eng.finish()
        inbox.close()
        if eng.out is not None:
            eng.out.close()
        try:
            STATUS.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()
