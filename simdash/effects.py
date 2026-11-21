"""The haptic effects' formulas: from telemetry (one tick of a recording, or one live sample as a one-tick
recording, see tick_data) to each effect's strength. Shared by the haptics engine (engine.py), the replay and the
live/session views (replay.py), so the shakers, the page and the recording all compute an effect the same way.

monocoque's effects (Engine, TyreSlip, TyreLock, ABS) follow monocoque's own code; the others are simdash's.
"""

import math
import time

from . import shakers
from .pcars2 import SURFACE, rear_slide_deg

TYRE_IDX = {"FrontLeft": [0], "FrontRight": [1], "RearLeft": [2], "RearRight": [3],
            "Fronts": [0, 1], "Rears": [2, 3], "All": [0, 1, 2, 3]}


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


SURFACE_FULL = 200 / 3.6  # m/s at which a surface effect reaches its max pitch or strength
WHEEL_IDX = {"FL": 0, "FR": 1, "RL": 2, "RR": 3}


def surface_play(dev, data, i):
    """simdash's Kerb / OffTrack: one of the effect's wheels is on that surface (AMS2's raw terrain),
    as strong as the car is fast (full at 200 km/h)."""
    want = shakers.SURFACE_CLASS[dev["effect"]]
    v = data["speed"][i]
    if v < 1.5:
        return 0.0
    for w in dev["wheels"]:
        t = data["terrain"][i][WHEEL_IDX[w]]
        if t is not None and 0 <= t < len(SURFACE) and SURFACE[t] == want:
            return max(0.01, min(1.0, v / SURFACE_FULL))
    return 0.0


def oversteer_play(dev, data, i):
    """simdash's Oversteer: the rear slip angle past the threshold, full strength at "full" degrees,
    for the side the rear slides to (wheels RL: to the left, RR: to the right)."""
    a = rear_slide_deg(data["vel_x"][i], -data["vel_fwd"][i], data["yaw"][i])
    side = "RL" if a > 0 else "RR"  # > 0: the rear slides to the left
    if side not in dev.get("wheels", ("RL", "RR")):
        return 0.0
    a = abs(a)
    th, full = float(dev.get("threshold", 5)), float(dev.get("full", 10))
    if a <= th:
        return 0.0
    return max(0.01, min(1.0, (a - th) / max(0.5, full - th)))


def suspension_play(dev, data, i):
    """simdash's Suspension: the fastest-moving suspension of the effect's wheels (m/s, up or down) past the
    threshold, full at "full": bumps, kerbs and landings, not the slow roll and pitch of cornering and braking."""
    v = max((abs(data["susp_vel"][i][WHEEL_IDX[w]]) for w in dev.get("wheels", ())), default=0.0)
    th = float(dev.get("threshold", shakers.SUSPENSION_DEFAULTS["threshold"]))
    full = float(dev.get("full", shakers.SUSPENSION_DEFAULTS["full"]))
    if v <= th:
        return 0.0
    return max(0.01, min(1.0, (v - th) / max(0.01, full - th)))


class AccBase:
    """The car's recent horizontal acceleration (local x, z, m/s²): an average over the last ~TAU s, before the
    current sample. Impact measures how far a sample jumps from it."""

    TAU = 0.25

    def __init__(self):
        self.v, self.t = None, None
        self.vel = None      # (local x, z velocity) of the previous sample, and its time
        self.glitch = False  # whether the last sample was a glitch
        self.key, self.last = None, None

    def update(self, acc, t, vel=None, key=None):
        """The baseline the sample acc (at time t) is compared with; then acc joins it.
        vel: the sample's local (x, z) velocity, to tell a glitch from a real crash (see is_glitch).
        key: the game's sample number; the same sample read again (the engine reads faster than AMS2 writes)
        gets the same answer, and does not count twice."""
        if key is not None and key == self.key:
            return self.last
        self.key = key
        self.last = self._update(acc, t, vel)
        return self.last

    def _update(self, acc, t, vel):
        if self.v is None or t - self.t > 1.0:  # first sample, or after a pause
            self.v = acc
        prev = self.v
        jump = math.hypot(acc[0] - prev[0], acc[1] - prev[1])
        self.glitch = jump / G > IMPACT_GLITCH and self.is_glitch(jump, t, vel)
        if vel is not None:
            self.vel = (tuple(vel), t)
        if self.glitch:
            return prev  # compared (and ignored by impact_play), never averaged in
        k = min(1.0, max(0.0, t - self.t) / self.TAU) if self.t is not None else 1.0
        self.v = (prev[0] + (acc[0] - prev[0]) * k, prev[1] + (acc[1] - prev[1]) * k)
        self.t = t
        return prev

    def is_glitch(self, jump, t, vel):
        """A huge reading is real when the velocity changed with it: a crash at 190 km/h read 52 g at 20 Hz and over
        60 g at 100 Hz, losing 30 m/s in 50 ms. The 401 g glitch left the speed as it was. Without the velocity
        (or the previous sample), any huge reading is a glitch."""
        if vel is None or self.vel is None:
            return True
        (pvx, pvz), pt = self.vel
        dt = t - pt
        if not 0 < dt < 0.2:
            return True
        dv = math.hypot(vel[0] - pvx, vel[1] - pvz) / dt   # m/s² the velocity really changed by
        return dv < 0.25 * jump


G = 9.80665
IMPACT_GLITCH = 60.0  # g: past this a reading must come with a matching velocity change (AccBase.is_glitch): AMS2
                      # sometimes reports one absurd sample (401 g at 59 km/h, speed unchanged)
# where each wheel is, as (forward, left)
WHEEL_DIR = {"FL": (1, 1), "FR": (1, -1), "RL": (-1, 1), "RR": (-1, -1)}


def impact_play(dev, data, i):
    """simdash's Impact: the jump of the horizontal acceleration from its recent level past the threshold (g), full
    at "full". The car is pushed away from what it hits, so the obstacle lies opposite the jump: an effect on some
    wheels plays when the obstacle is on their side (all four: any hit)."""
    (ax, az), (bx, bz) = data["acc"][i], data["acc_base"][i]
    dx, dz = (ax - bx) / G, (az - bz) / G
    m = math.hypot(dx, dz)
    th = float(dev.get("threshold", shakers.IMPACT_DEFAULTS["threshold"]))
    full = float(dev.get("full", shakers.IMPACT_DEFAULTS["full"]))
    if m <= th or (m > IMPACT_GLITCH and data.get("acc_glitch", [True] * (i + 1))[i]):
        return 0.0
    x = max(0.01, min(1.0, (m - th) / max(0.5, full - th)))
    wheels = dev.get("wheels") or list(WHEEL_DIR)
    if len(wheels) == 4:
        return x
    # local x points left and -z forward; the obstacle is opposite the push: forward dz, left -dx
    of, ol = dz / m, -dx / m
    cos = max((of * f + ol * l) / math.sqrt(2) for f, l in (WHEEL_DIR[w] for w in wheels))
    return x * max(0.0, min(1.0, cos / math.sqrt(0.5)))


def gforce_play(dev, data, i):
    """simdash's GForce: the car's sustained horizontal acceleration (the 0.25 s average Impact compares with) past
    the threshold (g), full at "full", on the wheels the weight shifts onto: braking loads the fronts, accelerating
    the rears, a left corner the right side. All four wheels: any direction."""
    bx, bz = data["acc_base"][i]
    lf, ll = bz / G, -bx / G      # local x points left, z backward: the load goes where the car is pushed from
    m = math.hypot(lf, ll)
    th = float(dev.get("threshold", shakers.GFORCE_DEFAULTS["threshold"]))
    full = float(dev.get("full", shakers.GFORCE_DEFAULTS["full"]))
    if m <= th or m > 6.0:  # past 6 g it is a crash, Impact's
        return 0.0
    x = max(0.01, min(1.0, (m - th) / max(0.1, full - th)))
    wheels = dev.get("wheels") or list(WHEEL_DIR)
    if len(wheels) == 4:
        return x
    cos = max((lf * f + ll * l) / m / math.sqrt(2) for f, l in (WHEEL_DIR[w] for w in wheels))
    return x * max(0.0, min(1.0, cos / math.sqrt(0.5)))


def rev_play(dev, data, i):
    """simdash's RevLimiter: the RPM past "threshold" of the max, full at the max (a shift cue)."""
    mx = data["maxrpm"][i]
    if mx <= 0:
        return 0.0
    r, th = data["rpm"][i] / mx, float(dev.get("threshold", shakers.REV_DEFAULTS["threshold"]))
    if r < th:
        return 0.0
    return max(0.01, min(1.0, (r - th) / max(0.005, 1.0 - th)))


def speed_play(dev, data, i):
    """simdash's Speed: a rumble from "threshold" km/h, full at "full"."""
    kmh = data["speed"][i] * 3.6
    th = float(dev.get("threshold", shakers.SPEED_DEFAULTS["threshold"]))
    full = float(dev.get("full", shakers.SPEED_DEFAULTS["full"]))
    if kmh <= th:
        return 0.0
    return max(0.01, min(1.0, (kmh - th) / max(1.0, full - th)))


def effect_value(dev, data, i, slips):
    """(frequency, amplitude %) of a continuous effect at tick i; amplitude 0 = silent."""
    if dev["effect"] == "Engine":
        mx = round(data["maxrpm"][i])
        return shakers._modulated(dev, round(data["rpm"][i]) / mx if mx > 0 else 0.0)
    if dev["effect"] == "Oversteer":
        x = oversteer_play(dev, data, i)
    elif dev["effect"] == "Suspension":
        x = suspension_play(dev, data, i)
    elif dev["effect"] == "Impact":
        x = impact_play(dev, data, i)
    elif dev["effect"] == "GForce":
        x = gforce_play(dev, data, i)
    elif dev["effect"] == "RevLimiter":
        x = rev_play(dev, data, i)
    elif dev["effect"] == "Speed":
        x = speed_play(dev, data, i)
    elif dev["effect"] in shakers.SIMDASH_EFFECTS:
        x = surface_play(dev, data, i)
    else:
        x = slip_play(dev, data, i, slips)
    return shakers._modulated(dev, x) if x > 0 else (0.0, 0.0)


def knock_envelope(dev):
    """(hold, release) s for the effects that come as hits (a 10 ms spike alone is too short to feel), else None.
    Suspension: a bump fades over 60 ms. Impact: the knock holds for 30 % of its duration, then fades."""
    if dev["effect"] == "Suspension":
        return 0.0, 0.06
    if dev["effect"] == "Impact":
        d = float(dev.get("duration", shakers.IMPACT_DEFAULTS["duration"]))
        return 0.3 * d, 0.7 * d
    return None


class Knock:
    """A hit holds, then fades out linearly, unless a stronger one comes."""

    def __init__(self, hold, release):
        self.hold, self.release = hold, max(1e-3, release)
        self.f, self.a, self.t = 0.0, 0.0, None

    def level(self, t):
        if self.t is None:
            return 0.0
        dt = t - self.t - self.hold
        return self.a if dt <= 0 else self.a * max(0.0, 1 - dt / self.release)

    def update(self, f, a, t):
        """(f, a) to play at time t for a new value (f, a) of the effect."""
        cur = self.level(t)
        if a > 0 and a >= cur:
            self.f, self.a, self.t = f, a, t
            return f, a
        return (self.f, cur) if cur > 0 else (f, 0.0)


# ---------- a live sample as a one-tick recording ----------

def tick_data(snap, acc_base=None):
    """One live sample as a one-tick recording, the input of effect_value() and wheel_slip() (tick 0).
    acc_base: the AccBase following this stream of samples (Impact), updated here."""
    lv = snap["local_vel"]
    la = snap.get("local_acc") or (0.0, 0.0, 0.0)
    acc = (la[0], la[2])
    base = acc_base.update(acc, time.monotonic(), (lv[0], lv[2]), snap.get("seq")) if acc_base is not None else acc
    glitch = acc_base.glitch if acc_base is not None else True
    return {"speed": [snap["speed"]], "rpm": [snap["rpm"]], "maxrpm": [snap["max_rpm"]],
            "rps": [list(snap["tyre_rps"])], "vel_fwd": [lv[2]], "vel_up": [lv[1]], "abs": [snap["abs"]],
            "terrain": [list(snap["terrain"])], "vel_x": [lv[0]], "yaw": [snap["ang_vel"][1]],
            "susp_vel": [list(snap.get("susp_vel") or (0.0,) * 4)], "acc": [acc], "acc_base": [base], "acc_glitch": [glitch]}
