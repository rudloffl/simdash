"""Shaker replay: play a recorded session on the bass shakers as monocoque would have.

Each effect's frequency and strength is recomputed from the recorded telemetry with
monocoque's own formulas (devices/sounddevice.c, hapticeffect.c slipeffect(), and simapi's
pcars2 mapping for the units), then the streams are mixed into one 8-channel signal and
played with pacat on the shaker card. The replay clock runs here, the page follows it.
"""

import math
import random
import subprocess
import threading
import time
from array import array

from . import shakers, store

RATE = 8000           # Hz; shaker frequencies stay far below 4 kHz, and it keeps the synth cheap
CHUNK = 0.05          # s of audio per synthesis step
AHEAD = 0.12          # s of audio generated ahead of the clock
LATENCY = 0.1         # s between generating a sample and feeling it (pacat + PulseAudio)
GAP = 2.0             # s without ticks (pause, menus) skipped during playback
GEAR_FADE = 0.005     # monocoque's gear pulse fade-out
DIAMETERS = shakers.CONFIG.with_name("diameters.config")

TYRE_IDX = {"FrontLeft": [0], "FrontRight": [1], "RearLeft": [2], "RearRight": [3],
            "Fronts": [0, 1], "Rears": [2, 3], "All": [0, 1, 2, 3]}


# ---------- recorded data ----------

def read_session(ref):
    day, sid = store.parse_ref(ref)
    con = store.read_day(day)
    with con:
        s = con.execute("SELECT car, track, variation FROM v_sessions WHERE id=?", (sid,)).fetchone()
        if s is None:
            raise LookupError("no such session")
        rows = con.execute(
            "SELECT t.id, t.ts, e.x, e.z, e.lap, e.speed, e.rpm, e.gear, e.throttle, e.brake, "
            "e.local_vel_y, e.local_vel_z, e.abs_active FROM tick t JOIN telemetry e ON e.tick_id = t.id "
            "WHERE t.session_id=? ORDER BY t.ts", (sid,)).fetchall()
        rps, terrain, max_rpm = {}, {}, []
        if rows:
            lo, hi = rows[0][0], rows[-1][0]
            for tick, wheel, v, ter in con.execute("SELECT tick_id, wheel, rps, terrain FROM wheel WHERE tick_id BETWEEN ? AND ?",
                                                   (lo, hi)):
                rps.setdefault(tick, [0.0] * 4)[wheel] = v or 0.0
                terrain.setdefault(tick, [None] * 4)[wheel] = ter
            max_rpm = con.execute(
                "SELECT tick_id, max_rpm FROM player_state WHERE tick_id <= ? AND tick_id >= "
                "COALESCE((SELECT MAX(tick_id) FROM player_state WHERE tick_id <= ?), 0) ORDER BY tick_id",
                (hi, lo)).fetchall()
    con.close()
    d = {k: [] for k in ("ts", "x", "z", "lap", "speed", "rpm", "maxrpm", "gear", "throttle", "brake",
                         "vel_fwd", "vel_up", "abs", "rps", "terrain")}
    k, cur_max = 0, 0
    for r in rows:
        while k < len(max_rpm) and max_rpm[k][0] <= r[0]:
            cur_max = max_rpm[k][1] or 0
            k += 1
        d["ts"].append(r[1]); d["x"].append(r[2]); d["z"].append(r[3]); d["lap"].append(r[4])
        d["speed"].append(r[5] or 0.0); d["rpm"].append(r[6] or 0.0); d["maxrpm"].append(cur_max)
        d["gear"].append(r[7] or 0); d["throttle"].append(r[8] or 0.0); d["brake"].append(r[9] or 0.0)
        d["vel_up"].append(r[10] or 0.0); d["vel_fwd"].append(r[11] or 0.0); d["abs"].append(r[12] or 0)
        d["rps"].append(rps.get(r[0], [0.0] * 4))
        d["terrain"].append(terrain.get(r[0], [None] * 4))
    return {"car": s["car"], "track": s["track"], "variation": s["variation"], **d}


def tyre_diameters(car):
    """(diameters, source): monocoque's learned ones if it has this car, else simdash's (2 x radius)."""
    try:
        cfg = shakers.parse(DIAMETERS.read_text())
        for c in cfg.get("cars", []):
            if str(c.get("car", "")).lower() == str(car).lower():
                dia = [float(c.get(f"tyre{i}", 0)) for i in range(4)]
                if all(x > 0 for x in dia):
                    return dia, "monocoque"
    except (OSError, ValueError):
        pass
    radii = store.load_wheels().get(car)
    if radii and all(r > 0 for r in radii):
        return [2 * r for r in radii], "simdash"
    return None, None


# ---------- monocoque's effects, tick by tick ----------

def wheel_slip(data, i, dia):
    """monocoque's wheelslip[]: > 0 the wheel turns slower than the car (lock), < 0 faster (spin)."""
    v = 0.277778 * round(3.6 * data["speed"][i])        # simapi rounds the speed to whole km/h
    if not dia or v <= 0.5:
        return [0.0] * 4
    return [(v - dia[w] * -data["rps"][i][w] / 2) / v for w in range(4)]   # simapi negates mTyreRPS


def slip_play(dev, data, i, slips):
    """slipeffect(): how far past the threshold the effect's wheels are, summed."""
    # simapi: Yvelocity = -mLocalVelocity[2] (forwards), Zvelocity = -mLocalVelocity[1] (up)
    if -data["vel_fwd"][i] <= 0 or abs(data["vel_up"][i]) > 1:
        return 0.0
    th = float(dev.get("threshold", 0) or 0)
    eff = dev["effect"]
    play = 0.0
    for w in TYRE_IDX.get(dev.get("tyre", "All"), TYRE_IDX["All"]):
        s = slips[w]
        if eff == "TyreSlip" and s < -th:
            play += abs(s) - abs(th)
        elif eff in ("TyreLock", "ABS") and s > th:
            play += s - th
    if eff == "ABS" and not data["abs"][i]:
        play = 0.0
    if eff == "Suspension":
        play = 0.0  # simapi does not map AMS2's suspension
    return play


def effect_track(dev, data, dia):
    """Per tick (frequency, amplitude %) of one continuous effect; amplitude 0 = silent."""
    out = []
    for i in range(len(data["ts"])):
        if dev["effect"] == "Engine":
            mx = round(data["maxrpm"][i])
            x = round(data["rpm"][i]) / mx if mx > 0 else 0.0
        else:
            x = slip_play(dev, data, i, wheel_slip(data, i, dia))
            if x <= 0:
                out.append((0.0, 0.0))
                continue
        out.append(shakers._modulated(dev, x))
    return out


def gear_events(data):
    """Times of monocoque's gear kicks: the gear changed and simapi's gear (mGear + 1) is above 1."""
    out = []
    for i in range(1, len(data["ts"])):
        g = data["gear"][i]
        if g != data["gear"][i - 1] and g + 1 > 1:
            out.append(data["ts"][i])
    return out


class Timeline:
    def __init__(self, ref, effects=None, devid="", devices=None):
        """effects: simdash's (Shakers tab) effects; or devices: monocoque's as in a config file."""
        self.ref = ref
        self.data = read_session(ref)
        if not self.data["ts"]:
            raise LookupError("this session has no recorded telemetry")
        if devices is not None:
            self.devices, self.source = devices, list(range(len(devices)))
        else:
            self.devices, self.source = shakers.expand(effects, devid)
        self.dia, self.dia_source = tyre_diameters(self.data["car"])
        self.gears = gear_events(self.data)
        self.tracks = [None if d["effect"] == "Gear" else effect_track(d, self.data, self.dia) for d in self.devices]
        self.t0, self.t1 = self.data["ts"][0], self.data["ts"][-1]

    def index(self, t):
        """Last tick at or before t (bisect)."""
        ts = self.data["ts"]
        lo, hi = 0, len(ts) - 1
        if t <= ts[0]:
            return 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if ts[mid] <= t:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def state(self, k, t):
        """(frequency, amplitude) of continuous device k at time t, interpolated between ticks."""
        ts, tr = self.data["ts"], self.tracks[k]
        i = self.index(t)
        if i + 1 >= len(ts) or ts[i + 1] - ts[i] > GAP:
            return tr[i]
        (f0, a0), (f1, a1) = tr[i], tr[i + 1]
        if a0 == 0 or a1 == 0:  # an effect starting or stopping does not glide in
            return tr[i]
        u = (t - ts[i]) / (ts[i + 1] - ts[i])
        return f0 + (f1 - f0) * u, a0 + (a1 - a0) * u

    def skip_gap(self, t):
        """If t falls in a long gap between ticks, the time of the next tick."""
        ts = self.data["ts"]
        i = self.index(t)
        if i + 1 < len(ts) and ts[i + 1] - ts[i] > GAP and t > ts[i] + 0.2:
            return ts[i + 1]
        return t

    def channels(self):
        """Per tick, what each shaker plays: {channel: {"a": [strength %], "fx": [effect or None]}}."""
        d = self.data
        n = len(d["ts"])
        out = {}
        for k, dev in enumerate(self.devices):
            ch = out.setdefault(dev["pan"], {"a": [0] * n, "fx": [None] * n})
            if dev["effect"] == "Gear":
                dur, j = float(dev.get("duration", 0.125)), 0
                for i, t in enumerate(d["ts"]):
                    while j < len(self.gears) and self.gears[j] + dur < t:
                        j += 1
                    a = int(dev.get("amplitude", 50))
                    if j < len(self.gears) and self.gears[j] <= t + 0.05 and a > ch["a"][i]:
                        ch["a"][i], ch["fx"][i] = a, "Gear"
                continue
            for i, (f, a) in enumerate(self.tracks[k]):
                if a > ch["a"][i]:
                    ch["a"][i], ch["fx"][i] = int(round(min(a, 100))), dev["effect"]
        return out

    def display(self):
        """What the page shows: the path, a few channels, and each simdash effect's level per tick."""
        d = self.data
        n = len(d["ts"])
        n_fx = max(self.source, default=-1) + 1
        level = [[0] * n for _ in range(n_fx)]
        freq = [[0] * n for _ in range(n_fx)]
        for k, dev in enumerate(self.devices):
            i_fx = self.source[k]
            if dev["effect"] == "Gear":
                dur = float(dev.get("duration", 0.125))
                j = 0
                for i, t in enumerate(d["ts"]):
                    while j < len(self.gears) and self.gears[j] + dur < t:
                        j += 1
                    if j < len(self.gears) and self.gears[j] <= t + 0.05:
                        level[i_fx][i] = max(level[i_fx][i], int(dev.get("amplitude", 50)))
                        freq[i_fx][i] = dev.get("frequency", 0)
                continue
            for i, (f, a) in enumerate(self.tracks[k]):
                if a > level[i_fx][i]:
                    level[i_fx][i] = int(round(min(a, 100)))
                    freq[i_fx][i] = round(f, 1)
        r = lambda xs, nd: [round(v, nd) for v in xs]
        slips = [wheel_slip(d, i, self.dia) for i in range(n)]
        return {
            "ref": self.ref, "car": d["car"], "track": d["track"], "variation": d["variation"],
            "t0": self.t0, "t1": self.t1, "ts": r(d["ts"], 3), "x": r(d["x"], 1), "z": r(d["z"], 1),
            "lap": d["lap"], "speed": r([v * 3.6 for v in d["speed"]], 0), "gear": d["gear"],
            "rpm": r(d["rpm"], 0), "maxrpm": r(d["maxrpm"], 0), "throttle": r(d["throttle"], 2), "brake": r(d["brake"], 2),
            "level": level, "freq": freq, "diameters": self.dia_source,
            # per wheel (FL, FR, RL, RR): raw AMS2 terrain code, and monocoque's slip (> 0 lock, < 0 spin)
            "terrain": [[t[w] for t in d["terrain"]] for w in range(4)],
            "slip": [[round(sl[w], 2) for sl in slips] for w in range(4)],
        }


# ---------- live: what monocoque is playing now ----------

class Live:
    """Shaker output for the live dashboard, from each shared-memory sample, with the settings
    monocoque started with (monocoque reads its config once, at start)."""

    REFRESH = 5.0  # s between checks for a restarted monocoque or new tyre sizes

    def __init__(self):
        self.devices, self.dia, self.car = [], None, None
        self.checked = 0.0
        self.gear = None
        self.kicks = {}  # device index -> time its gear kick ends

    def _refresh(self, car, now):
        from . import system
        pids = system._pids("monocoque")
        since = system._started(pids[0]) if pids else None
        self.devices = shakers.devices_at(since)[0] if since else []
        if car != self.car or self.dia is None:
            self.car = car
        self.dia = tyre_diameters(car)[0]
        self.checked = now

    def update(self, snap, now):
        """{channel: [strength %, effect]} for the shakers playing now; {} without monocoque."""
        if now - self.checked > self.REFRESH or snap["car"] != self.car:
            self._refresh(snap["car"], now)
        lv = snap["local_vel"]
        data = {"speed": [snap["speed"]], "rpm": [snap["rpm"]], "maxrpm": [snap["max_rpm"]],
                "rps": [list(snap["tyre_rps"])], "vel_fwd": [lv[2]], "vel_up": [lv[1]], "abs": [snap["abs"]]}
        shifted = self.gear is not None and snap["gear"] != self.gear and snap["gear"] + 1 > 1
        self.gear = snap["gear"]
        slips = wheel_slip(data, 0, self.dia)
        out = {}
        for k, dev in enumerate(self.devices):
            if dev["effect"] == "Gear":
                if shifted:
                    self.kicks[k] = now + float(dev.get("duration", 0.125))
                a = dev.get("amplitude", 50) if self.kicks.get(k, 0) > now else 0
            elif dev["effect"] == "Engine":
                mx = round(snap["max_rpm"])
                a = shakers._modulated(dev, round(snap["rpm"]) / mx if mx > 0 else 0.0)[1]
            else:
                x = slip_play(dev, data, 0, slips)
                a = shakers._modulated(dev, x)[1] if x > 0 else 0
            if a > out.get(dev["pan"], [0])[0]:
                out[dev["pan"]] = [int(round(min(a, 100))), dev["effect"]]
        return out


# ---------- playback ----------

class _Stream:
    """One monocoque PulseAudio stream: a phase-continuous sine on one channel."""

    def __init__(self, dev):
        self.dev = dev
        self.pan = int(dev.get("pan", 0))
        self.gain = (max(0, min(100, int(dev.get("volume", 100)))) / 100) ** 3   # PulseAudio's cubic volume
        self.noise = float(dev.get("noise", 0) or 0)
        self.phase = 0.0
        self.pulse = None  # gear: seconds into the current kick


class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.tl = None
        self.playing = False
        self.speed = 1.0
        self.pos = 0.0         # replay time at wall0
        self.wall0 = 0.0
        self.audio = False     # sound going to the card (False: the clock runs for the page only)
        self.thread = None
        self.proc = None
        self.error = ""

    # -- clock --
    def now(self):
        if not self.playing:
            return self.pos
        return self.pos + (time.monotonic() - self.wall0) * self.speed

    def state(self):
        with self.lock:
            tl = self.tl
            t = self.now()
            if self.playing and self.audio:
                t -= LATENCY * self.speed  # what is being felt, not what is being generated
            return {"ref": tl.ref if tl else None, "playing": self.playing, "ts": t, "speed": self.speed,
                    "audio": self.audio, "error": self.error, "t0": tl.t0 if tl else None, "t1": tl.t1 if tl else None}

    # -- control --
    def load(self, ref, effects, devid):
        tl = Timeline(ref, effects, devid)
        with self.lock:
            keep = self.tl is not None and self.tl.ref == ref
            t = self.now() if keep else tl.t0
            self.tl = tl
            self.pos, self.wall0 = min(max(t, tl.t0), tl.t1), time.monotonic()
            self.streams = [_Stream(d) for d in tl.devices]
        return tl

    def play(self, ts, speed, devid):
        with self.lock:
            if self.tl is None:
                raise LookupError("load a session first")
            self.pos = min(max(ts, self.tl.t0), self.tl.t1)
            self.wall0 = time.monotonic()
            self.speed = speed
            self.playing = True
            self.error = ""
            for s in self.streams:
                s.pulse = None
            start = self.thread is None
            if start:
                self.thread = threading.Thread(target=self._run, args=(devid,), daemon=True)
        shakers.stop()  # a test still playing
        if start:
            self.thread.start()

    def pause(self):
        with self.lock:
            self.pos = self.now()
            self.playing = False

    def seek(self, ts):
        with self.lock:
            if self.tl is None:
                return
            self.pos = min(max(ts, self.tl.t0), self.tl.t1)
            self.wall0 = time.monotonic()
            for s in self.streams:
                s.pulse = None

    # -- audio thread --
    def _open(self, devid):
        if not devid or devid not in {s["name"] for s in shakers.sinks()}:
            return None  # never let PulseAudio fall back to the speakers
        return subprocess.Popen(
            ["pacat", "--playback", f"--device={devid}", "--format=s16le", f"--rate={RATE}", "--channels=8",
             "--channel-map=" + ",".join(shakers.CHANNEL_MAP), "--volume=65536", "--client-name=simdash",
             "--stream-name=shaker replay", "--latency-msec=60"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _run(self, devid):
        self.proc = self._open(devid)
        self.audio = self.proc is not None
        if self.proc is None and devid:
            self.error = "shaker card not connected: showing only"
        gen = None  # replay time the next chunk starts at
        try:
            while True:
                with self.lock:
                    if self.playing and self.now() >= self.tl.t1:
                        self.pos, self.playing = self.tl.t1, False
                    if not self.playing:
                        self.thread = None  # play() starts a new one from here on
                        break
                    tl, speed = self.tl, self.speed
                    t = self.now()
                    jump = tl.skip_gap(t)
                    if jump != t:
                        self.pos, self.wall0 = jump, time.monotonic()
                        t = jump
                        gen = None
                    if gen is None or gen < t - 0.05 or gen > t + 2 * AHEAD * speed:  # started or seeked
                        gen = t
                    chunks = []
                    while gen < t + AHEAD * speed:
                        chunks.append(self._synth(tl, gen, speed))
                        gen += CHUNK * speed
                if self.proc and chunks:
                    try:
                        self.proc.stdin.write(b"".join(chunks))
                        self.proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        self.error = "the shaker card went away"
                        self.proc.kill()
                        self.proc, self.audio = None, False
                time.sleep(0.02)
        finally:
            if self.proc:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
                self.proc.terminate()
            self.proc = None

    def _synth(self, tl, t, speed):
        """CHUNK seconds of 8-channel s16le starting at replay time t."""
        n = int(CHUNK * RATE)
        mix = [0.0] * (8 * n)
        dt = speed / RATE                 # replay seconds per sample
        t_end = t + n * dt
        new_kick = any(t <= g < t_end for g in tl.gears)
        for k, s in enumerate(self.streams):
            dev = s.dev
            if dev["effect"] == "Gear":
                dur = float(dev.get("duration", 0.125))
                if new_kick:
                    s.pulse, s.phase = 0.0, 0.0
                if s.pulse is None:
                    continue
                f, a = dev.get("frequency", 0), min(100, dev.get("amplitude", 50))
                for j in range(n):
                    left = dur - s.pulse
                    if left <= 0:
                        s.pulse, s.phase = None, 0.0
                        break
                    amp = a / 100 * (min(1.0, left / GEAR_FADE))
                    mix[8 * j + s.pan] += s.gain * amp * math.sin(2 * math.pi * s.phase)
                    ff = f + (random.uniform(-1, 1) * s.noise if s.noise else 0)
                    s.phase = (s.phase + ff / RATE) % 1.0
                    s.pulse += 1 / RATE   # a kick lasts its duration in real time, whatever the replay speed
                continue
            f0, a0 = tl.state(k, t)
            f1, a1 = tl.state(k, t_end)
            if a0 <= 0 and a1 <= 0:
                s.phase = 0.0
                continue
            for j in range(n):
                u = j / n
                f, a = f0 + (f1 - f0) * u, min(100.0, a0 + (a1 - a0) * u)
                mix[8 * j + s.pan] += s.gain * a / 100 * math.sin(2 * math.pi * s.phase)
                ff = f + (random.uniform(-1, 1) * s.noise if s.noise else 0)
                s.phase = (s.phase + ff / RATE) % 1.0
        return array("h", (int(max(-1.0, min(1.0, v)) * 32767) for v in mix)).tobytes()


player = Player()
