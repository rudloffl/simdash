"""Desk 6DOF: a small Stewart platform on six hobby servos, with a model car on top.

Hardware (see docs/6dof/): six MG90-class servos on a PCA9685 PWM board, driven by a MicroPython
pyboard v1.1 over USB serial. simdash does all the maths: pose -> inverse kinematics -> servo
angle -> pulse width (µs), and sends the six pulses as one text line. The pyboard clamps them,
limits how fast each servo moves, and when nothing arrives for a second glides to the park pulses
before going limp (firmware/6dof).

Frame: x forward (the car's nose), y left, z up, origin at the centre of the base plate at the
height of the servo shafts. Servos sit in three pairs around the base; each shaft's rotation axis
is set, on its own, by `shaft_angle` from the base centre -> shaft line (see legs()). Angle 0 = horn
horizontal; the platform height where all six horns are horizontal is the home height.

Leg k: shaft B, horn direction u, horn length a, rod length s, platform anchor P (world).
With l = P - B:  L = |l|² + a² - s²,  M = 2a·l_z,  N = 2a·(l·u)
                 alpha = asin(L / √(M²+N²)) - atan2(N, M)      (unreachable when |L| > √(M²+N²))
"""

import copy
import functools
import glob
import json
import logging
import math
import os
import re
import select
import termios
import threading
import time
from pathlib import Path

from . import store

log = logging.getLogger("simdash.motion")

SETTINGS = store.DATA_DIR / "motion.json"
AXES = ("x", "y", "z", "roll", "pitch", "yaw")
USB_VENDOR = "f055"  # MicroPython (pyboard: f055:9800 and friends)

DEFAULTS = {
    "geometry": {
        # each shaft's rotation axis, on its own, from perpendicular to the base centre -> shaft line (0°)
        # towards that line (90°, parallel): 0-90° tips the horn in, over the base (closer to its own rod,
        # less rod lean); 90° is the classic arrangement (each shaft radial, horn tangent to the base circle);
        # 90-180° tips it out, past the base edge
        "shaft_angle": 90.0,
        # "out": each horn points away from its pair's centre (horn-end ball joints far apart);
        # "in": the two horns of a pair point at each other, their ball joints close together
        "horns": "out",
        # the servo bodies (shafts horizontal either way, so the motion is the same): "lying", their length
        # along the horn's swing; "upright", standing on end, shaft near the top: compact, but the shafts sit higher
        "mount": "lying",
        # as drawn in CAD: a radius and the angle (seen from the centre) between the two points of a pair
        "base_r": 62.4,       # mm, base centre -> each servo shaft (in the horns' plane)
        "base_angle": 43.3,   # deg, between the two shafts of a pair (flat, MG90 bodies + tabs: >= ~41.8 at 62.4)
        "plat_r": 46.0,       # mm, platform centre -> rod ball joint
        "plat_angle": 48.0,   # deg, between the two joints of a platform pair (pairs sit 60° from the base pairs)
        "rod": 90.0,          # mm, ball centre to ball centre
        "horn": 18.0,         # mm, shaft -> rod hole on the horn
        "first_pair": 0.0,    # deg, where the first servo pair sits (0 = under the car's nose)
        # the servos: each horn sits at home_angle when the platform is at home (+ = horn tip above horizontal)
        # and may turn stroke degrees each way from there
        "home_angle": 0.0,    # deg
        "stroke": 60.0,       # deg, ± from home_angle
        # horn-rod angle kept within [min, 180 - min]: near 0 or 180 the leg toggles (the horn turns,
        # the platform barely moves, loads spike)
        "transmission_min_deg": 35.0,
        # the rod's ends: "stud", a ball stud aimed along the rod at home (it swivels joint_max_deg any way
        # from there: a cone); "rod_end", a rod end (heim joint) on a bolt: it turns freely about the bolt and
        # tilts at most joint_max_deg off it (its misalignment). On the horn the bolt is parallel to the servo
        # shaft; under the platform it is fixed square to the rod at home, as near parallel to that shaft as can be
        "joints": "stud",
        "joint_max_deg": 30.0,
    },
    "servos": {
        # three calibration points per servo: the pulse with the horn level, at +45° (up) and at -45°.
        # Defaults: 10 µs/° (MG90-class), mirrored pairs turning opposite ways; measure yours.
        "center_us": [1500] * 6,
        "plus45_us": [1950, 1050, 1950, 1050, 1950, 1050],
        "minus45_us": [1050, 1950, 1050, 1950, 1050, 1950],
        "min_us": 800,   # never beyond: MG90s hit their end stops near 600 / 2400 (stall, stripped gears)
        "max_us": 2200,
    },
    "car": {"length": 180.0, "width": 80.0, "height": 50.0},  # mm, the model on top (1:24 ≈ 180 mm)
    "cueing": {},  # overrides of CUEING_DEFAULTS (replay and, later, live)
}


# ---------- settings ----------

_cache = {"key": None, "s": None}


def _read():
    s = copy.deepcopy(DEFAULTS)
    s["enabled"] = True   # the 6DOF in use (System tab); off: the board is left alone and the tab hidden
    try:
        saved = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return s
    if isinstance(saved.get("geometry"), dict):
        _old_geometry(saved["geometry"])
    sv = saved.get("servos")
    if isinstance(sv, dict) and "plus45_us" not in sv and "us_per_deg" in sv:
        # one µs/° for all and a direction each (before 2026-11-22): as three points per servo
        c, k = sv.get("center_us", DEFAULTS["servos"]["center_us"]), float(sv["us_per_deg"])
        d = sv.get("dir", [1, -1, 1, -1, 1, -1])
        sv["plus45_us"] = [round(c[i] + d[i] * 45 * k) for i in range(6)]
        sv["minus45_us"] = [round(c[i] - d[i] * 45 * k) for i in range(6)]
    for k in s:
        if isinstance(saved.get(k), dict):
            s[k].update({kk: vv for kk, vv in saved[k].items() if kk in s[k] or k == "cueing"})
    s["enabled"] = saved.get("enabled") is not False
    return s


def _old_geometry(g):
    """Saved before 2026-11-22 (base_r to the pair's face + shaft_spacing, or half-angles): the same platform
    in today's terms (radius to the shafts, full angles within a pair). Saved before the continuous
    `shaft_angle` existed (2026-11-26): the discrete "flat" / "radial" / "aimed" `layout` is dropped, each
    shaft's own radial direction (90°, same as the old "radial") is the closest fit. Changes g in place."""
    if "base_angle" not in g and ("base_half" in g or "shaft_spacing" in g or "base_r" in g):
        r = float(g.get("base_r", 58.0))
        if g.get("layout") == "flat":
            h = float(g.get("shaft_spacing", 46.0)) / 2
            g["base_r"], g["base_angle"] = round(math.hypot(r, h), 2), round(2 * math.degrees(math.atan2(h, r)), 2)
        else:
            g["base_angle"] = 2 * float(g.get("base_half", 20.0))
    if "plat_angle" not in g and "plat_half" in g:
        g["plat_angle"] = 2 * float(g["plat_half"])
    if "stroke" not in g and "servo_limit" in g:   # travel ± from horizontal: the same with home level
        g["stroke"] = float(g["servo_limit"])
    if "shaft_angle" not in g:
        g["shaft_angle"] = 90.0
    for k in ("shaft_spacing", "base_half", "plat_half", "servo_limit", "layout"):
        g.pop(k, None)


def settings():
    """The settings, cached (read-only: don't change the result). Re-read when the file changes."""
    try:
        st = SETTINGS.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if _cache["s"] is None or _cache["key"] != key:
        _cache["s"], _cache["key"] = _read(), key
    return _cache["s"]


def load():
    """A copy of the settings, free to change."""
    return copy.deepcopy(settings())


def save(patch):
    s = load()
    for k in ("geometry", "servos", "car"):
        for kk, v in (patch.get(k) or {}).items():
            if kk in s[k]:
                s[k][kk] = v
    for kk, v in (patch.get("cueing") or {}).items():
        if kk == "mode":
            if v not in CUEING_MODES:
                raise ValueError("cueing mode is washout or body")
            s["cueing"][kk] = v
        elif kk in CUEING_DEFAULTS:
            s["cueing"][kk] = float(v)
    g = s["geometry"]
    for k in g:
        if k not in ("horns", "mount", "joints"):
            g[k] = float(g[k])
    if not 0 <= g["shaft_angle"] <= 180:
        raise ValueError("shaft angle must be within 0-180°")
    if g["horns"] not in ("out", "in"):
        raise ValueError("horns point out or in")
    if g["mount"] not in ("lying", "upright"):
        raise ValueError("servo bodies lying or upright")
    if g["joints"] not in ("stud", "rod_end"):
        raise ValueError("joints are ball studs or rod ends")
    if not 0 < g["base_angle"] < 120 or not 0 <= g["plat_angle"] < 120:
        raise ValueError("angles within a pair: base 0-120°, platform 0-120°")
    if not (g["horn"] > 0 and g["rod"] > g["horn"] and g["base_r"] > 0 and g["plat_r"] > 0):
        raise ValueError("lengths must be positive and the rod longer than the horn")
    if not 0 < g["stroke"] <= 90:
        raise ValueError("stroke must be within 0-90°")
    if not -60 <= g["home_angle"] <= 60:
        raise ValueError("home angle must be within ±60°")
    if not 0 <= g["transmission_min_deg"] < 90 or not 0 < g["joint_max_deg"] <= 90:
        raise ValueError("joint limits: horn-rod angle 0-90°, ball joint swivel 0-90°")
    sv = s["servos"]
    for k in ("center_us", "plus45_us", "minus45_us"):
        sv[k] = [int(v) for v in sv[k]][:6]
        if len(sv[k]) != 6:
            raise ValueError("six servos")
    for i in range(6):
        up, down = sv["plus45_us"][i] - sv["center_us"][i], sv["center_us"][i] - sv["minus45_us"][i]
        if up == 0 or down == 0 or (up > 0) != (down > 0):
            raise ValueError(f"servo {i}: +45° and -45° must lie on opposite sides of the level pulse")
    sv["min_us"], sv["max_us"] = int(sv["min_us"]), int(sv["max_us"])
    if not 500 <= sv["min_us"] < sv["max_us"] <= 2500:
        raise ValueError("pulse range must be within 500-2500 µs")
    s["car"] = {k: float(v) for k, v in s["car"].items()}
    h0(g)  # raises if the rods can't reach
    return _write(s)


def _write(s):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)
    _cache["s"] = None  # invalidate
    return s


def enabled():
    return settings()["enabled"]


def set_enabled(on):
    """The 6DOF on or off (System tab). Off: a playing course stops, the servos let go, and simdash leaves
    the board alone (no arming, no calibration moves, no demo writes) and hides the 6DOF tab."""
    on = bool(on)
    if not on:
        player.stop()
        link.arm(False)
    s = load()
    s["enabled"] = on
    _write(s)


def status():
    """The System tab's 6DOF card: whether it is in use, the board, the link and a course."""
    st, c = link.status(), player.course
    on = enabled()
    return {"enabled": on, "port": find_port(), "board": link.query() if on else None,
            "firmware_here": firmware_version(), "connected": st["connected"], "armed": st["armed"],
            "firmware": st["firmware"], "error": st["error"], "last": st["last"], "playing": player.playing,
            "course": {"ref": c.ref, "car": c.d["car"], "track": c.d["track"], "t": player.t - c.t0,
                       "length": c.t1 - c.t0} if c else None}


# ---------- geometry ----------

def legs(g):
    """[(B, u, P)] per servo 0-5: shaft (x, y), horn direction (x, y), platform anchor (x, y) at home.

    Each shaft's rotation axis is set, on its own, by `shaft_angle` (deg) from the tangent to the base
    circle at B, turning towards the base centre -> B line as the angle grows: 90° is that line (the
    classic "radial" shaft, horn tangent to the base circle). The horn direction u is square to it: at 0°
    u points straight at the centre (its tip lands inside the base); between 0° and 90° it still leans in;
    at 90° u is tangent (neither in nor out); past 90°, up to 180°, it leans the other way, out past the
    base edge. That lean is the same for both shafts of a pair (shaft_angle doesn't depend on which of the
    pair it is); "side" and horns "in"/"out" only flip the tangential part, as before, to keep the pair
    symmetric and the horns pointing away from (or towards) their own pair's centre."""
    flip = -1 if g.get("horns", "out") == "in" else 1   # in: towards the pair's centre
    theta = math.radians(g.get("shaft_angle", 90.0))
    out = []
    for k in range(6):
        side = 1 if k % 2 else -1          # 0: clockwise servo of the pair, 1: counter-clockwise
        cb = math.radians(g["first_pair"] + 120 * (k // 2))
        # shafts on the base_r circle, base_angle apart in a pair
        b = cb + side * math.radians(g["base_angle"] / 2)
        B = (g["base_r"] * math.cos(b), g["base_r"] * math.sin(b))
        p = cb + side * math.radians(60) - side * math.radians(g["plat_angle"] / 2)
        P = (g["plat_r"] * math.cos(p), g["plat_r"] * math.sin(p))
        et, er = (-math.sin(b), math.cos(b)), (math.cos(b), math.sin(b))      # tangent, radial (outward) at B
        tang, rad = side * math.sin(theta), -math.cos(theta)                  # side only flips the tangential part
        u = (flip * (tang * et[0] + rad * er[0]), flip * (tang * et[1] + rad * er[1]))
        out.append((B, u, P))
    return out


def tip(B, u, a, alpha):
    """The horn's ball (x, y, z) for shaft B, horn direction u, horn length a, at alpha (deg, + = up)."""
    al = math.radians(alpha)
    return (B[0] + a * math.cos(al) * u[0], B[1] + a * math.cos(al) * u[1], a * math.sin(al))


def h0(g):
    """Home height of the platform joints above the shafts (all horns at home_angle)."""
    B, u, P = legs(g)[0]
    A = tip(B, u, g["horn"], g.get("home_angle", 0.0))
    d2 = (P[0] - A[0]) ** 2 + (P[1] - A[1]) ** 2
    if d2 >= g["rod"] ** 2:
        raise ValueError("rods too short to reach the platform anchors")
    return A[2] + math.sqrt(g["rod"] ** 2 - d2)


def _gkey(g):
    return tuple(sorted((k, v) for k, v in g.items()))


@functools.lru_cache(maxsize=16)
def _geo(key):
    g = dict(key)
    L, H = legs(g), h0(g)
    home = ik({}, g, L, H)
    rods = [tuple(r["P"][i] - r["A"][i] for i in range(3)) for r in home]
    # rod-end bolts: on the horn along the shaft (w = u x z, it doesn't move as the horn turns); under the
    # platform (in the platform's frame) that same direction made square to the rod at home
    shaft = [(u[1], -u[0], 0.0) for _, u, _ in L]
    plat = []
    for w, r in zip(shaft, rods):
        n = math.sqrt(sum(c * c for c in r))
        rh = tuple(c / n for c in r)
        d = sum(w[i] * rh[i] for i in range(3))
        b = tuple(w[i] - d * rh[i] for i in range(3))
        nb = math.sqrt(sum(c * c for c in b))
        plat.append(tuple(c / nb for c in b))
    return {"L": L, "H": H, "home_rod": rods, "bolt_horn": shaft, "bolt_plat": plat}


def geo(g):
    """legs, home height and the home rod directions of a geometry, computed once."""
    return _geo(_gkey(g))


def _angle(a, b):
    c = sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def _tilt(v, axis):
    """How far (deg) direction v is off the plane square to axis: a rod end's misalignment on its bolt."""
    return abs(90.0 - _angle(v, axis))


def _turn(v, w, th):
    """v rotated by th (rad) about the unit axis w (Rodrigues)."""
    c, s = math.cos(th), math.sin(th)
    d = w[0] * v[0] + w[1] * v[1] + w[2] * v[2]
    cr = (w[1] * v[2] - w[2] * v[1], w[2] * v[0] - w[0] * v[2], w[0] * v[1] - w[1] * v[0])
    return tuple(v[i] * c + cr[i] * s + w[i] * d * (1 - c) for i in range(3))


def rotation(roll, pitch, yaw):
    """Degrees. roll + = leans right (left side up), pitch + = nose up, yaw + = nose left."""
    r, p, y = math.radians(roll), math.radians(-pitch), math.radians(yaw)
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return ((cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
            (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
            (-sp, cp * sr, cp * cr))


def ik(pose, g, legs_=None, home=None):
    """pose = {x, y, z (mm), roll, pitch, yaw (deg)} -> per leg dict with alpha (deg, None when out of
    reach), shaft B, horn tip A, platform joint P (3D world points)."""
    legs_ = legs_ or legs(g)
    H = home if home is not None else h0(g)
    R = rotation(pose.get("roll", 0), pose.get("pitch", 0), pose.get("yaw", 0))
    t = (pose.get("x", 0), pose.get("y", 0), H + pose.get("z", 0))
    a, s = g["horn"], g["rod"]
    out = []
    for B, u, P0 in legs_:
        P = tuple(t[i] + R[i][0] * P0[0] + R[i][1] * P0[1] for i in range(3))
        l = (P[0] - B[0], P[1] - B[1], P[2])
        L = l[0] ** 2 + l[1] ** 2 + l[2] ** 2 + a * a - s * s
        M, N = 2 * a * l[2], 2 * a * (l[0] * u[0] + l[1] * u[1])
        r = math.hypot(M, N)
        alpha = None if r == 0 or abs(L) > r else math.degrees(math.asin(L / r) - math.atan2(N, M))
        A = tip(B, u, a, g.get("home_angle", 0.0) if alpha is None else alpha)
        out.append({"alpha": alpha, "B": (B[0], B[1], 0.0), "A": A, "P": P})
    return out


def why_not(res, g, pose):
    """None when the platform can take this pose, else the first reason it can't:
    a servo out of reach or past its travel, a horn and rod near toggle, or a ball joint past its swivel."""
    gm = geo(g)
    R = rotation(pose.get("roll", 0), pose.get("pitch", 0), pose.get("yaw", 0))
    tmin, jmax = g.get("transmission_min_deg", 0.0), g.get("joint_max_deg", 180.0)
    for k, r in enumerate(res):
        a = r["alpha"]
        if a is None:
            return f"servo {k} can't reach"
        if abs(a - g.get("home_angle", 0.0)) > g["stroke"]:
            return f"servo {k} past its stroke ({a - g.get('home_angle', 0.0):+.0f}° from home)"
        horn = tuple(r["A"][i] - r["B"][i] for i in range(3))
        rod = tuple(r["P"][i] - r["A"][i] for i in range(3))
        tr = _angle(horn, rod)
        if tr < tmin or tr > 180 - tmin:
            return f"servo {k}: horn and rod nearly in line ({tr:.0f}°)"
        home = gm["home_rod"][k]
        # the platform's stud (or bolt) turns with the platform: the rod seen from the platform
        plat = tuple(sum(R[j][i] * rod[j] for j in range(3)) for i in range(3))
        if g.get("joints", "stud") == "rod_end":
            # free about the bolts; only the tilt off each bolt counts. The horn's bolt (along the shaft)
            # doesn't move as the horn turns
            t = _tilt(rod, gm["bolt_horn"][k])
            if t > jmax:
                return f"joint {k} on the horn tilts {t:.0f}° off its bolt"
            t = _tilt(plat, gm["bolt_plat"][k])
            if t > jmax:
                return f"joint {k} on the platform tilts {t:.0f}° off its bolt"
            continue
        if _angle(plat, home) > jmax:
            return f"joint {k} on the platform swivels {_angle(plat, home):.0f}°"
        # the horn's stud turns with the horn, about w = u × z, by alpha - home: undo it
        u = gm["L"][k][1]
        horn_view = _turn(rod, (u[1], -u[0], 0.0), -math.radians(a - g.get("home_angle", 0.0)))
        if _angle(horn_view, home) > jmax:
            return f"joint {k} on the horn swivels {_angle(horn_view, home):.0f}°"
    return None


def within(res, g, pose):
    return why_not(res, g, pose) is None


def reach(g):
    """How far the platform goes along each axis alone, both ways: {axis: [neg, pos]} (mm / deg)."""
    return copy.deepcopy(_reach(_gkey(g)))


@functools.lru_cache(maxsize=16)
def _reach(key):
    g = dict(key)
    gm = geo(g)
    L, H = gm["L"], gm["H"]
    out = {}
    for ax in AXES:
        out[ax] = []
        for sign in (-1, 1):
            lo, hi = 0.0, 90.0
            for _ in range(24):
                m = (lo + hi) / 2
                p = {ax: sign * m}
                if within(ik(p, g, L, H), g, p):
                    lo = m
                else:
                    hi = m
            out[ax].append(round(lo, 1))
    return out


def design(g):
    """Numbers for the person designing the parts."""
    gm = geo(g)
    L, H = gm["L"], gm["H"]
    home = ik({}, g, L, H)
    lean = _angle(gm["home_rod"][0], (0, 0, 1))
    trans = [_angle(tuple(r["A"][i] - r["B"][i] for i in range(3)), gm["home_rod"][k]) for k, r in enumerate(home)]
    spacing = 2 * g["base_r"] * math.sin(math.radians(g["base_angle"] / 2))
    p_spacing = 2 * g["plat_r"] * math.sin(math.radians(g["plat_angle"] / 2))
    gap_home, gap_min, cross = _ball_gaps(g, L)
    h, st = g.get("home_angle", 0.0), g["stroke"]
    drop = max(0.0, -math.sin(math.radians(h - st))) * g["horn"]   # how far the horn's ball goes below the shaft
    return {
        "home_height": round(H, 1),
        "shaft_angle": g.get("shaft_angle", 90.0),
        "mount": g.get("mount", "lying"),
        # shaft axis above the base plate: the body (and its tabs) below the shaft, or the horn swung down
        "shaft_height": round(max(_below_shaft(g) + 1, drop + 4), 1),
        "home_angle": h, "stroke": st,
        "horn_range": [round(h - st, 1), round(h + st, 1)],   # from horizontal, + = up
        "rod_lean": round(lean, 1),
        "transmission_home": round(trans[0], 1),   # horn-rod angle at home (90 is ideal)
        "shaft_spacing": round(spacing, 1),
        "joint_spacing": round(p_spacing, 1),
        "horns": g.get("horns", "out"),
        "joints": g.get("joints", "stud"),
        # rod-end bolt axes (unit vectors): on the horn in the world (fixed, along the shaft), under the
        # platform in the platform's frame (turn them with the pose)
        "bolts": {"horn": [[round(c, 4) for c in v] for v in gm["bolt_horn"]],
                  "plat": [[round(c, 4) for c in v] for v in gm["bolt_plat"]]},
        # rod end on the horn: how far the rod already sits off square to its bolt at home (its sideways lean)
        "bolt_tilt_home": round(_tilt(gm["home_rod"][0], gm["bolt_horn"][0]), 1),
        # under the platform: the bolt's slope from horizontal
        "plat_bolt_slope": round(abs(math.degrees(math.asin(max(-1.0, min(1.0, gm["bolt_plat"][0][2]))))), 1),
        # horn balls of a pair, centre to centre: at home, and the closest anywhere in the stroke (each servo
        # on its own); 0 = the horns cross
        "ball_gap": 0.0 if cross else round(gap_home, 1),
        "ball_gap_min": 0.0 if cross else round(gap_min, 1),
        "reach": reach(g),
        "leg_side": _leg_side(g, L, H),
        "legs": [{"shaft": [round(c, 2) for c in B], "horn_dir": [round(c, 4) for c in u],
                  "joint": [round(c, 2) for c in P]} for B, u, P in L],
    }


def _leg_side(g, L, H):
    """Leg 0 seen along its shaft at home and at both ends of pure heave (every leg does the same in heave).
    2D points in the horn's swing plane, mm: s along the horn's horizontal direction, h up, shaft at (0, 0).
    The rod leans out of that plane, so it is drawn turned about the horn's line into the plane: its length
    and its angle to the horn stay true (the joint's height on the drawing is then close, not exact)."""
    u = L[0][1]
    lo, hi = reach(g)["z"]
    out = []
    for name, z, beyond in (("low", -lo, -lo - 0.3), ("home", 0.0, None), ("high", hi, hi + 0.3)):
        r = ik({"z": z}, g, L, H)[0]
        if r["alpha"] is None:
            continue
        al = math.radians(r["alpha"])
        hd = (math.cos(al), math.sin(al))     # horn direction in the plane
        n = (-math.sin(al), math.cos(al))     # in-plane, square to the horn
        horn = tuple(r["A"][i] - r["B"][i] for i in range(3))
        rod = tuple(r["P"][i] - r["A"][i] for i in range(3))
        hu = tuple(c / g["horn"] for c in horn)
        along = sum(rod[i] * hu[i] for i in range(3))
        perp = math.sqrt(max(0.0, g["rod"] ** 2 - along ** 2))
        n3 = (n[0] * u[0], n[0] * u[1], n[1])
        if sum(rod[i] * n3[i] for i in range(3)) < 0:
            perp = -perp
        A = (g["horn"] * hd[0], g["horn"] * hd[1])
        P = (A[0] + along * hd[0] + perp * n[0], A[1] + along * hd[1] + perp * n[1])
        why = None
        if beyond is not None:
            p = {"z": beyond}
            why = why_not(ik(p, g, L, H), g, p)
        out.append({"name": name, "z": round(z, 1), "alpha": round(r["alpha"], 1),
                    "trans": round(_angle(horn, rod), 1), "A": [round(c, 2) for c in A],
                    "P": [round(c, 2) for c in P], "why": why})
    return out


def _ball_gaps(g, L):
    """Pair 0's horn balls: (distance at home, the smallest over the stroke with each servo anywhere in it,
    whether the two horns meet somewhere). Horns are taken as thin bars from the shaft to the ball."""
    h, st, a = g.get("home_angle", 0.0), g["stroke"], g["horn"]
    (B0, u0, _), (B1, u1, _) = L[0], L[1]
    angles = [h - st + 2 * st * i / 16 for i in range(17)]
    home = math.dist(tip(B0, u0, a, h), tip(B1, u1, a, h))
    gap, cross = home, False
    for a0 in angles:
        T0 = tip(B0, u0, a, a0)
        bar0 = [tuple(b + (t - b) * f / 8 for b, t in zip((B0[0], B0[1], 0.0), T0)) for f in range(9)]
        for a1 in angles:
            T1 = tip(B1, u1, a, a1)
            gap = min(gap, math.dist(T0, T1))
            if not cross:
                bar1 = [tuple(b + (t - b) * f / 8 for b, t in zip((B1[0], B1[1], 0.0), T1)) for f in range(9)]
                cross = min(math.dist(p, q) for p in bar0 for q in bar1) < 1.0
    return home, gap, cross


def raw_pulse(i, a, sv):
    """Horn angle (deg) -> pulse (µs) for servo i, from its three calibration points, not clamped."""
    c = sv["center_us"][i]
    slope = (sv["plus45_us"][i] - c) / 45 if a >= 0 else (c - sv["minus45_us"][i]) / 45
    return c + a * slope


def pulses(alphas, sv, home=0.0):
    """Horn angles (deg) -> pulse widths (µs), clamped; None angles hold the home angle."""
    return [int(round(min(sv["max_us"], max(sv["min_us"], raw_pulse(i, home if a is None else a, sv)))))
            for i, a in enumerate(alphas)]


def pulse_warnings(s):
    """Servos whose full stroke (home ± stroke) needs pulses outside min-max: the clamp would cut it."""
    sv, g = s["servos"], s["geometry"]
    h, st = g.get("home_angle", 0.0), g["stroke"]
    out = []
    for i in range(6):
        lo, hi = sorted((raw_pulse(i, h - st, sv), raw_pulse(i, h + st, sv)))
        if lo < sv["min_us"] or hi > sv["max_us"]:
            out.append(f"servo {i}: {h - st:g}° to {h + st:g}° needs {lo:.0f}–{hi:.0f} µs, outside {sv['min_us']}–{sv['max_us']}")
    return out


def solve(pose, s=None):
    """What the tab draws: legs, angles, pulses, and whether the pose is allowed."""
    s = s or settings()
    g = s["geometry"]
    gm = geo(g)
    res = ik(pose, g, gm["L"], gm["H"])
    alphas = [r["alpha"] for r in res]
    why = why_not(res, g, pose)
    return {
        "pose": {ax: float(pose.get(ax, 0)) for ax in AXES},
        "ok": why is None,
        "why": why,
        "alpha": [None if a is None else round(a, 2) for a in alphas],
        "pulse": pulses(alphas, s["servos"], g.get("home_angle", 0.0)),
        "legs": [{k: [round(c, 2) for c in r[k]] for k in ("B", "A", "P")} for r in res],
        "home_height": round(gm["H"], 2),
    }


def fk(alphas, g, guess=None):
    """Horn angles (deg, from horizontal) -> the pose the platform takes, or None (see _fk). A big jump from
    the guess is walked in small steps, each one starting from the last."""
    p = _fk(alphas, g, guess)
    if p is not None:
        return p
    gm = geo(g)
    start = guess or {}
    a0 = [r["alpha"] for r in ik(start, g, gm["L"], gm["H"])]
    if any(a is None for a in a0):
        start, a0 = {}, [g.get("home_angle", 0.0)] * 6
    n = 16
    for i in range(1, n + 1):
        start = _fk([a + (b - a) * i / n for a, b in zip(a0, alphas)], g, start)
        if start is None:
            return None
    return start


def _fk(alphas, g, guess=None):
    """Horn angles (deg, from horizontal) -> the pose the platform takes ({x, y, z, roll, pitch, yaw}), found by
    Newton steps on the inverse kinematics from guess (default home); None when it doesn't settle (the rods
    can't all reach). Unique near the guess, which is all the page needs."""
    gm = geo(g)
    L, H = gm["L"], gm["H"]
    p = [float((guess or {}).get(ax, 0.0)) for ax in AXES]

    def err(q):
        r = ik(dict(zip(AXES, q)), g, L, H)
        if any(x["alpha"] is None for x in r):
            return None
        return [x["alpha"] - t for x, t in zip(r, alphas)]
    e = err(p)
    if e is None:
        p, e = [0.0] * 6, err([0.0] * 6)
    for _ in range(40):
        if max(abs(v) for v in e) < 1e-4:
            return dict(zip(AXES, p))
        J = []                                          # d alpha_k / d pose_j, by small steps
        for j in range(6):
            col = [0.0] * 6
            for hstep in (1e-3, -1e-3):                 # forward, or backward at the edge of reach
                q = list(p)
                q[j] += hstep
                ej = err(q)
                if ej is not None:
                    col = [(a - b) / hstep for a, b in zip(ej, e)]
                    break
            J.append(col)
        # the Newton step d: sum_j J[j][k] * d_j = -e_k
        M = [[J[j][k] for j in range(6)] + [-e[k]] for k in range(6)]
        try:
            d = _gauss(M)
        except ZeroDivisionError:
            return None
        n = max(abs(v) for v in d)
        step = 1.0 if n < 5 else 5 / n                  # mm / deg: small steps, the solution is near
        while step > 1e-3:
            q = [a + step * b for a, b in zip(p, d)]
            eq = err(q)
            if eq is not None and max(abs(v) for v in eq) < max(abs(v) for v in e):
                p, e = q, eq
                break
            step /= 2
        else:
            return None
    return dict(zip(AXES, p)) if max(abs(v) for v in e) < 1e-3 else None


def _gauss(M):
    """Solve the n x (n+1) augmented system M by Gaussian elimination with partial pivoting."""
    n = len(M)
    M = [list(r) for r in M]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            raise ZeroDivisionError
        M[c], M[piv] = M[piv], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x


def solve_servos(offsets, s=None, guess=None):
    """Servos driven directly: offsets (deg from home, one per servo) -> what the tab draws, like solve(), with
    the pose the platform takes. ok only when the angles are within the stroke and the platform can sit
    there within every limit; the pulses are those of the asked angles."""
    s = s or settings()
    g = s["geometry"]
    h, st = g.get("home_angle", 0.0), g["stroke"]
    offsets = [float(v) for v in offsets][:6]
    if len(offsets) != 6:
        raise ValueError("six servo angles")
    alphas = [h + v for v in offsets]
    pose = fk(alphas, g, guess)
    r = solve(pose or guess or {}, s)
    why = next((f"servo {k} past its stroke ({v:+.0f}° from home)" for k, v in enumerate(offsets) if abs(v) > st), None)
    if why is None and pose is None:
        why = "the platform can't sit there (the rods can't all reach)"
    why = why or r["why"]
    r.update({"ok": why is None, "why": why, "pulse": pulses(alphas, s["servos"], h), "target": alphas,
              "fk": pose is not None})
    if pose is not None:
        r["alpha"] = [round(a, 2) for a in alphas]
    return r


# ---------- 1:1 drawings for the 3D printed parts ----------

# MG90-class body, approximate (measure yours): length along the swing, shaft centre from the outer end,
# horn plane to top face, top face to bottom, tab length, tabs below the top face
MG90 = {"len": 22.8, "wid": 12.2, "shaft": 5.9, "top": 4.5, "depth": 22.7, "tabs": 32.5,
        "tab_depth": 6.0}


def _below_shaft(g):
    """How far the servo body (with its tabs) reaches below the shaft axis (mm)."""
    if g.get("mount", "lying") == "upright":   # standing on end, shaft near the top
        return MG90["len"] - MG90["shaft"] + (MG90["tabs"] - MG90["len"]) / 2
    return MG90["wid"] / 2                     # lying: the shaft sits halfway across the body's width


def template_svg(part, g):
    """A true-scale SVG (mm) of the base or the platform, dimensioned the way the geometry is set (radius,
    angle within a pair, horn), with every point's coordinates listed below. Import it into the CAD program
    as a sketch, or print it at 100 % to check against the servos."""
    gm = geo(g)
    L = gm["L"]
    lean = _angle(gm["home_rod"][0], (0, 0, 1))
    ha_home, st = g.get("home_angle", 0.0), g["stroke"]
    horns_in = g.get("horns", "out") == "in"
    upright = g.get("mount", "lying") == "upright"
    sh = max(_below_shaft(g) + 1, max(0.0, -math.sin(math.radians(ha_home - st))) * g["horn"] + 4)
    DIM = "#080"   # dimensions
    # the horn ball's distance from the base centre (top view) at rest (home) and at each end of the
    # stroke: a guide for plat_r (every leg reaches the same radii, by symmetry, so leg 0 alone gives them)
    B0, u0 = L[0][0], L[0][1]
    home_r = math.hypot(*tip(B0, u0, g["horn"], ha_home)[:2])
    stroke_ends = [ha_home - st, ha_home + st]
    stroke_r = [math.hypot(*tip(B0, u0, g["horn"], a)[:2]) for a in stroke_ends]
    rc = reach(g)   # the range achievable, each axis alone: drawn on the platform, sizes both canvases

    def stud(x, y, rod, sign):
        """An arrow along the home rod's direction seen from above (sign -1: the other end's view)."""
        dx, dy = sign * rod[0], sign * rod[1]
        n = math.hypot(dx, dy) or 1
        ex, ey = x + dx / n * 7, y + dy / n * 7
        return (f'<line x1="{X(x):.2f}" y1="{Y(y):.2f}" x2="{X(ex):.2f}" y2="{Y(ey):.2f}" stroke="#06c" stroke-width="0.4"/>'
                f'<circle cx="{X(ex):.2f}" cy="{Y(ey):.2f}" r="0.6" fill="#06c" stroke="none"/>')

    rod_end = g.get("joints", "stud") == "rod_end"

    def bolt(x, y, b):
        """A rod end's bolt seen from above: a blue bar through the joint along the bolt's axis."""
        n = math.hypot(b[0], b[1]) or 1
        dx, dy = b[0] / n * 6, b[1] / n * 6
        return (f'<line x1="{X(x - dx):.2f}" y1="{Y(y - dy):.2f}" x2="{X(x + dx):.2f}" y2="{Y(y + dy):.2f}" '
                f'stroke="#06c" stroke-width="0.8" stroke-linecap="round"/>')

    def at(r, deg):
        return (r * math.cos(math.radians(deg)), r * math.sin(math.radians(deg)))

    def text(x, y, s, size=3.2, color="#000", anchor="middle", along=None):
        """Text centred on (x, y); along = a direction (deg, world) to write it along, kept readable."""
        rot = ""
        if along is not None:
            a = -along % 360                      # paper angle (y flipped)
            a = a - 180 if 90 < a <= 270 else a
            rot = f' transform="rotate({a:.2f} {X(x):.2f} {Y(y):.2f})"'
        return (f'<text x="{X(x):.2f}" y="{Y(y) + size * 0.35:.2f}" font-size="{size}" text-anchor="{anchor}" '
                f'stroke="none" fill="{color}"{rot}>{s}</text>')

    def line(a, b, extra=""):
        return f'<line x1="{X(a[0]):.2f}" y1="{Y(a[1]):.2f}" x2="{X(b[0]):.2f}" y2="{Y(b[1]):.2f}" {extra}/>'

    def radius_dim(deg, r, label):
        """A radius from the centre to (r, deg), labelled halfway, beside the line."""
        m = at(r * 0.6, deg)
        off = at(4, deg + 90)
        return (line((0, 0), at(r, deg), f'stroke="{DIM}" stroke-width="0.3"')
                + text(m[0] + off[0], m[1] + off[1], label, color=DIM))

    def angle_dim(a0, a1, r, label):
        """An arc between two directions (deg, a0 < a1) at radius r, labelled outside its middle."""
        p0, p1 = at(r, a0), at(r, a1)
        lab = at(r + 5, (a0 + a1) / 2)
        # world counter-clockwise = clockwise on paper (y flipped): sweep 1
        return (f'<path d="M{X(p0[0]):.2f} {Y(p0[1]):.2f} A{r:.2f} {r:.2f} 0 0 1 {X(p1[0]):.2f} {Y(p1[1]):.2f}" '
                f'stroke="{DIM}" stroke-width="0.3"/>' + text(lab[0], lab[1], label, color=DIM))

    def chord_dim(a, b, out, label):
        """The straight distance a-b, drawn offset by out (mm) away from the centre, with extension lines."""
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        n = math.hypot(mx, my) or 1
        o = (mx / n * out, my / n * out)
        a2, b2 = (a[0] + o[0], a[1] + o[1]), (b[0] + o[0], b[1] + o[1])
        lab = (mx + o[0] * (1 + 2.2 / out), my + o[1] * (1 + 2.2 / out))   # just outside the line, along it
        thin = f'stroke="{DIM}" stroke-width="0.15"'
        return (line(a, a2, thin) + line(b, b2, thin) + line(a2, b2, f'stroke="{DIM}" stroke-width="0.3"')
                + text(lab[0], lab[1], label, color=DIM, along=math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))))

    def bolt_plan(k):
        """Platform joint k's bolt seen from above: (azimuth of its rising end, deg CCW from x; angle from the
        radial line through the joint, signed CCW, -90..90; slope from horizontal, deg). The axis is a line:
        the rising end (towards the platform) is the one given."""
        b = gm["bolt_plat"][k]
        if b[2] < 0:
            b = tuple(-v for v in b)
        az = math.degrees(math.atan2(b[1], b[0]))
        P = L[k][2]
        rel = (az - math.degrees(math.atan2(P[1], P[0])) + 180) % 360 - 180
        if rel > 90:
            rel -= 180
        elif rel < -90:
            rel += 180
        return az % 360, rel, math.degrees(math.asin(max(-1.0, min(1.0, b[2]))))

    def point_angle(Pc, d0, d1, r, label):
        """An arc at point Pc from direction d0 to d1 (deg, world, the short way), labelled beyond its middle."""
        sw = (d1 - d0 + 180) % 360 - 180
        p0 = (Pc[0] + r * math.cos(math.radians(d0)), Pc[1] + r * math.sin(math.radians(d0)))
        p1 = (Pc[0] + r * math.cos(math.radians(d0 + sw)), Pc[1] + r * math.sin(math.radians(d0 + sw)))
        m = math.radians(d0 + sw / 2)
        lab = (Pc[0] + (r + 4) * math.cos(m), Pc[1] + (r + 4) * math.sin(m))
        return (f'<path d="M{X(p0[0]):.2f} {Y(p0[1]):.2f} A{r:.2f} {r:.2f} 0 0 {1 if sw > 0 else 0} {X(p1[0]):.2f} {Y(p1[1]):.2f}" '
                f'stroke="#06c" stroke-width="0.3"/>' + text(lab[0], lab[1], label, size=2.6, color="#06c"))

    def centre_line(deg, r, label):
        return (line((0, 0), at(r, deg), 'stroke="#999" stroke-width="0.2" stroke-dasharray="4 1 1 1"')
                + text(*at(r + 6, deg), label, size=2.8, color="#666", along=deg))

    pad = 20
    R = max((g["base_r"] + g["horn"] + MG90["len"]) if part == "base" else g["plat_r"] + 20,
            home_r, *stroke_r, *rc["x"], *rc["y"])
    size = 2 * (R + pad)
    rows = 10                                   # coordinate table under the drawing
    height = size + 4.2 * rows + 6
    c = size / 2

    def X(x): return c + x                                       # x forward -> right on paper
    def Y(y): return c - y                                       # y left -> up on paper
    e = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size:.1f}mm" height="{height:.1f}mm" '
         f'viewBox="0 0 {size:.1f} {height:.1f}" font-family="sans-serif">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<g stroke="#000" fill="none" stroke-width="0.25">',
         f'<line x1="{c-6}" y1="{c}" x2="{c+6}" y2="{c}"/><line x1="{c}" y1="{c-6}" x2="{c}" y2="{c+6}"/>']
    fp = g["first_pair"]
    # horn reach: where the horn ball sits (top view, distance from the centre) at rest and at each end
    # of the stroke
    e.append(f'<circle cx="{c}" cy="{c}" r="{home_r:.2f}" stroke="#c00" stroke-width="0.25" stroke-dasharray="1 2"/>')
    e.append(text(*at(home_r, fp + 180), f"horn reach home: R{home_r:.1f}", size=2.6, color="#c00", along=fp + 180))
    for a, r in zip(stroke_ends, stroke_r):
        e.append(f'<circle cx="{c}" cy="{c}" r="{r:.2f}" stroke="#c80" stroke-width="0.25" stroke-dasharray="1 2"/>')
    for a, r, deg in zip(stroke_ends, stroke_r, (fp + 170, fp + 190)):
        e.append(text(*at(r, deg), f"horn reach {a:+g}°: R{r:.1f}", size=2.6, color="#c80", along=deg))
    table = []
    if part == "base":
        ha = g["base_angle"] / 2
        e.append(f'<circle cx="{c}" cy="{c}" r="{g["base_r"]:.2f}" stroke-dasharray="2 2"/>')
        for j in range(3):
            cb = fp + 120 * j
            e.append(centre_line(cb, g["base_r"] + 26, f"pair {j}"))
        for k, (B, u, P) in enumerate(L):
            bx, by = X(B[0]), Y(B[1])
            tp = tip(B, u, g["horn"], ha_home)   # the horn's ball at home
            t = (-u[0], -u[1]) if horns_in else u   # along the swing, away from the pair's centre
            # servo body seen from above: the shaft's rotation axis (square to the horn's own swing, pointing
            # away from the base centre) is n; the body's length lies along the swing (t). At shaft_angle
            # 90° (the classic radial shaft) this runs the body from its shaft towards the next pair, so a
            # tight base_angle has the two bodies of a pair meeting; away from 90° it tilts, which can bring
            # the bodies of a pair closer still (check the drawing). The tabs (screws parallel to the shaft)
            # need a wall at the tab depth: drawn as a strip. Upright: the body stands on end, so from above
            # it is only its width along the swing, centred on the shaft; its tabs point up and down (screw
            # them into a wall at the tab depth).
            n = (u[1], -u[0])   # square to the horn's own swing direction
            if n[0] * B[0] + n[1] * B[1] < 0:   # the outward-pointing one of the two
                n = (-n[0], -n[1])
            c0 = 0.0 if upright else MG90["len"] / 2 - MG90["shaft"]   # body centre along t, from the shaft
            along, tabs = (MG90["wid"], MG90["wid"]) if upright else (MG90["len"], MG90["tabs"])

            def box(t0, t1, r0, r1, style):
                pts = [(B[0] + tt * t[0] + rr * n[0], B[1] + tt * t[1] + rr * n[1])
                       for tt, rr in ((t0, r0), (t1, r0), (t1, r1), (t0, r1))]
                e.append('<polygon points="' + " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in pts) + f'" {style}/>')
            box(c0 - along / 2, c0 + along / 2, -MG90["top"] - MG90["depth"], -MG90["top"],
                'stroke="#888" stroke-dasharray="1 1"')
            box(c0 - tabs / 2, c0 + tabs / 2, -MG90["top"] - MG90["tab_depth"] - 1.2,
                -MG90["top"] - MG90["tab_depth"] + 1.2, 'stroke="#888"')
            e.append(f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="1.5"/>'
                     + line(B, tp, 'stroke="#c00"') + f'<circle cx="{X(tp[0]):.2f}" cy="{Y(tp[1]):.2f}" r="1" stroke="#c00"/>')
            e.append(bolt(tp[0], tp[1], gm["bolt_horn"][k]) if rod_end else stud(tp[0], tp[1], gm["home_rod"][k], 1))
            e.append(text(B[0] * 0.78, B[1] * 0.78, str(k), size=4.5))
            table.append(f"{k}   shaft ({B[0]:7.2f}, {B[1]:7.2f})   horn ball ({tp[0]:7.2f}, {tp[1]:7.2f}, {tp[2]:6.2f})   "
                         f"horn towards ({u[0]:+.3f}, {u[1]:+.3f})")
        # dimensions, on pair 0 (the others are the same, 120° round)
        (B0, u0, _), (B1, u1, _) = L[0], L[1]
        t0, t1 = tip(B0, u0, g["horn"], ha_home), tip(B1, u1, g["horn"], ha_home)
        e.append(radius_dim(fp - ha, g["base_r"], f"R{g['base_r']:g}"))
        e.append(angle_dim(fp - ha, fp + ha, g["base_r"] * 0.45, f"{g['base_angle']:g}°"))
        e.append(angle_dim(fp, fp + 120, 14, "120°"))
        e.append(chord_dim(B0, B1, 8, f"shafts {math.dist(B0, B1):.1f}"))
        crossed = _ball_gaps(g, L)[2]
        e.append(chord_dim(t0, t1, 17, "HORNS CROSS" if crossed else f"balls {math.dist(t0, t1):.1f}"))
        lo = stroke_ends[0]   # the lowest position (most below horizontal): often where a pair's balls sit closest
        tlo0, tlo1 = tip(B0, u0, g["horn"], lo), tip(B1, u1, g["horn"], lo)
        e.append(chord_dim(tlo0, tlo1, 27, f"balls at {lo:+g}°: {math.dist(tlo0, tlo1):.1f}"))
        hm = ((B0[0] + t0[0]) / 2, (B0[1] + t0[1]) / 2)
        no = at(5, fp)   # beside the horn, away from the centre
        e.append(text(hm[0] + no[0], hm[1] + no[1], f"horn {g['horn']:g}" + (f" at {ha_home:+g}°" if ha_home else ""), size=2.8, color="#c00",
                      along=math.degrees(math.atan2(u0[1], u0[0]))))
        # shaft_angle on shaft 0: 0° reference is the tangent (perpendicular to the centre -> shaft line,
        # dashed), 90° is that line itself (dashed); the arc between the 0° reference and the actual axis
        # is the angle itself
        sa = g.get("shaft_angle", 90.0)
        b0deg = math.degrees(math.atan2(B0[1], B0[0]))
        tang0, rad0, axis0 = b0deg + 90, b0deg, b0deg + 90 - sa
        rr = 10
        for d in (tang0, rad0):
            e.append(line(B0, (B0[0] + rr * 1.8 * math.cos(math.radians(d)), B0[1] + rr * 1.8 * math.sin(math.radians(d))),
                          'stroke="#06c" stroke-width="0.2" stroke-dasharray="1 0.8"'))
        e.append(point_angle(B0, tang0, axis0, rr, f"shaft_angle {sa:g}°"))
        pair = (f"shaft_angle {sa:g}°: each shaft's own rotation axis, from perpendicular to the base centre "
                f"→ shaft line (0°) towards that line (90°, parallel: the classic radial shaft, "
                f"horn tangent to the circle); 0-90° tips the horn in, over the base, 90-180° tips it "
                f"out, past the base edge")
        pair += (f"|servos upright (shaft near the top), shaft axis {sh:.1f} mm above the base plate" if upright
                 else f"|servos lying, shaft axis {sh:.1f} mm above the base plate")
        note = (f"BASE 1:1 (seen from above)  servo shafts on R{g['base_r']:g}, {g['base_angle']:g}° apart in a pair; "
                f"pairs 120° apart, pair 0 at {fp:g}°; {pair}"
                f"|on shaft 0: dashed blue lines are 0° (tangent) and 90° (centre -> shaft) for shaft_angle, "
                f"the blue arc between them is the angle itself"
                f"|red/orange = where the horn ball sits (top view) at rest, at each end of the stroke: a guide for plat_r"
                f"|red = horn {g['horn']:g} mm at home: {ha_home:+g}° from horizontal (stroke ±{st:g}°: {ha_home - st:+g}° to {ha_home + st:+g}°)"
                + (f", {g['horn'] * math.cos(math.radians(ha_home)):.1f} mm seen from above" if ha_home else "") + f"|horns pointing {'at their neighbour' if horns_in else 'away from their pair'}; "
                f"green = dimensions|dashed = MG90 body seen from above, strip = its screw tabs{' (pointing up and down)' if upright else ''} (approx., measure yours)"
                + (f"|blue = rod-end bolt on the horn, parallel to the servo shaft (the rod sits "
                   f"{_tilt(gm['home_rod'][0], gm['bolt_horn'][0]):.0f}° off square to it at home)" if rod_end else
                   f"|blue = ball stud on the horn: aim it along the rod at home, {lean:.0f}° from vertical, leaning this way"))
        head = f"Coordinates (mm, x forward, y left, z up from the shafts' axis height; rod {g['rod']:g}, home height {gm['H']:.2f})"
    else:
        ha = g["plat_angle"] / 2
        e.append(f'<circle cx="{c}" cy="{c}" r="{g["plat_r"]:.2f}" stroke-dasharray="2 2"/>')
        for j in range(3):
            e.append(centre_line(fp + 60 + 120 * j, g["plat_r"] + 15, f"pair {j}"))
        e.append(centre_line(fp, g["plat_r"] * 0.5, "base pair 0"))
        for k, (B, u, P) in enumerate(L):
            e.append(f'<circle cx="{X(P[0]):.2f}" cy="{Y(P[1]):.2f}" r="1.5"/>' + text(P[0] * 0.8, P[1] * 0.8, str(k), size=4.5))
            e.append(bolt(P[0], P[1], gm["bolt_plat"][k]) if rod_end else stud(P[0], P[1], gm["home_rod"][k], -1))
            row = f"{k}   ball joint ({P[0]:7.2f}, {P[1]:7.2f}, {gm['H']:.2f})"
            if rod_end:
                az, rel, sl = bolt_plan(k)
                row += f"   bolt towards {az:5.1f}° ({rel:+.1f}° from radial), rising {sl:.1f}°"
                # the rising end marked by a dot
                q = (P[0] + 6 * math.cos(math.radians(az)), P[1] + 6 * math.sin(math.radians(az)))
                e.append(f'<circle cx="{X(q[0]):.2f}" cy="{Y(q[1]):.2f}" r="0.7" fill="#06c" stroke="none"/>')
            else:
                row += f"   takes the rod from servo {k}"
            table.append(row)
        P1, P2 = L[1][2], L[2][2]   # platform pair 0 (centred 60° from base pair 0)
        e.append(radius_dim(fp + 60 - ha, g["plat_r"], f"R{g['plat_r']:g}"))
        e.append(angle_dim(fp + 60 - ha, fp + 60 + ha, g["plat_r"] * 0.45, f"{g['plat_angle']:g}°"))
        e.append(angle_dim(fp, fp + 60, 12, "60°"))
        e.append(chord_dim(P1, P2, 8, f"{math.dist(P1, P2):.1f}"))
        # the range achievable, each axis alone (surge/sway drawn; heave/roll/pitch/yaw in the note below)
        rx, ry = rc["x"], rc["y"]
        magenta = 'stroke="#c0c" stroke-width="0.3" stroke-dasharray="3 1.5"'
        e.append(line((0, 0), (rx[1], 0), magenta) + line((0, 0), (-rx[0], 0), magenta))
        e.append(line((0, 0), (0, ry[1]), magenta) + line((0, 0), (0, -ry[0]), magenta))
        e.append(text(rx[1] / 2, 3.5, f"+{rx[1]:g}", size=2.6, color="#c0c")
                 + text(-rx[0] / 2, 3.5, f"-{rx[0]:g}", size=2.6, color="#c0c")
                 + text(3.5, ry[1] / 2, f"+{ry[1]:g}", size=2.6, color="#c0c", along=90)
                 + text(3.5, -ry[0] / 2, f"-{ry[0]:g}", size=2.6, color="#c0c", along=90))
        if rod_end:
            # on platform pair 0: each bolt's angle from the radial line through its joint (dashed, outwards)
            for k in (1, 2):
                P = L[k][2]
                rad = math.degrees(math.atan2(P[1], P[0]))
                e.append(line(P, (P[0] + 10 * math.cos(math.radians(rad)), P[1] + 10 * math.sin(math.radians(rad))),
                              'stroke="#06c" stroke-width="0.2" stroke-dasharray="1 0.8"'))
                az, rel, _ = bolt_plan(k)
                e.append(point_angle(P, rad, rad + rel, 8, f"{abs(rel):.1f}°"))
        note = (f"PLATFORM 1:1 (seen from above)  ball joints on R{g['plat_r']:g}, {g['plat_angle']:g}° apart in a pair; "
                f"pairs 120° apart, 60° from the base pairs|joint k takes the rod from servo k; green = dimensions"
                f"|red/orange = where the horn ball sits (top view) at rest, at each end of the stroke: a guide for plat_r"
                f"|magenta = range achievable, each axis alone, within every limit: surge/sway (drawn) "
                f"+{rx[1]:g}/-{rx[0]:g} / +{ry[1]:g}/-{ry[0]:g} mm; heave +{rc['z'][1]:g}/-{rc['z'][0]:g} mm; "
                f"roll +{rc['roll'][1]:g}/-{rc['roll'][0]:g}°, pitch +{rc['pitch'][1]:g}/-{rc['pitch'][0]:g}°, "
                f"yaw +{rc['yaw'][1]:g}/-{rc['yaw'][0]:g}°"
                + (f"|blue = rod-end bolt under the platform, seen from above, square to the rod at home"
                   f"|dot = the bolt's end that rises {bolt_plan(0)[2]:.1f}° towards the platform; on pair 0: its angle from the radial (dashed)" if rod_end else
                   f"|blue = ball stud under the platform: aim it down along the rod at home, {lean:.0f}° from vertical, its tip leaning this way"))
        head = f"Coordinates (mm, x forward, y left, z up from the servo shafts' axis height; rod {g['rod']:g})"
    # the car's nose
    e.append(f'<path d="M{c+12} {c} l-4 -2 v4 z" fill="#000"/><text x="{c+6}" y="{c+7}" font-size="3.5" stroke="none" fill="#000">x (nose)</text>')
    # a 50 mm scale bar to check the print
    e.append(f'<line x1="{pad/2}" y1="{size-pad/2}" x2="{pad/2+50}" y2="{size-pad/2}" stroke-width="0.5"/>'
             f'<text x="{pad/2}" y="{size-pad/2-2}" font-size="3.5" stroke="none" fill="#000">50 mm</text>')
    for i, ln in enumerate(note.split("|")):
        e.append(f'<text x="{pad/2}" y="{pad/2 + 4.5 * i}" font-size="3.2" stroke="none" fill="#000">{ln}</text>')
    for i, ln in enumerate([head] + table):
        e.append(f'<text x="{pad/2}" y="{size + 2 + 4.2 * i}" font-size="3" font-family="monospace" '
                 f'xml:space="preserve" stroke="none" fill="#000">{ln}</text>')
    e.append('</g></svg>')
    return "\n".join(e)


# ---------- the pyboard ----------

def find_port():
    """The pyboard's USB serial port, by MicroPython's USB vendor id."""
    for tty in sorted(glob.glob("/sys/class/tty/ttyACM*")):
        dev = os.path.realpath(os.path.join(tty, "device"))
        try:
            vid = open(os.path.join(os.path.dirname(dev), "idVendor")).read().strip()
        except OSError:
            continue
        if vid == USB_VENDOR:
            return "/dev/" + os.path.basename(tty)
    return None


FIRMWARE_MIN = 2  # firmware/6dof/main.py VERSION with slew limiting and the P (park) command


def _frame(cmd, us):
    return (cmd + " " + " ".join(str(int(v)) for v in us) + "\n").encode()


class Link:
    """Pulses to the pyboard.

    Arming needs the firmware's hello ("6DOF <version>", version >= FIRMWARE_MIN): with the board in
    the REPL, a crashed main.py or another board, arming is refused. On each (re)connect the park
    pulses (P) and the home pose (S) go first: after going limp the board doesn't know where the horns
    are and takes the first S as its starting point, then slews from there.
    A sender thread writes only the newest frame (stale ones are dropped, a slow port never stalls the
    replay), re-sends it every KEEP s (the firmware glides to park after 1 s of silence), and retries a
    missing board at most once per RETRY s."""

    KEEP = 0.3
    RETRY = 1.0

    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.fd = None
        self.port = None
        self.armed = False
        self.home = None      # pulses sent first on each connect, and stored as park
        self.last = None      # the newest pulses
        self.pending = None   # the frame waiting for the sender thread
        self.sent_at = 0.0
        self.next_try = 0.0
        self.firmware = None
        self.error = None
        self.thread = None
        self.prev_report = None   # (monotonic, report) of the last Q, for rates

    # -- port

    def _open(self):
        port = find_port()
        if not port:
            raise OSError("pyboard not found (MicroPython USB device f055)")
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._raw(fd)
        self.fd, self.port = fd, port
        self.firmware = self._ask(b"H\n")
        ver = None
        if self.firmware:
            try:
                ver = int(self.firmware.split()[1])
            except (IndexError, ValueError):
                pass
        if ver is None or ver < FIRMWARE_MIN:
            self._close()
            raise OSError(f"no 6DOF firmware answering on {port} (version {FIRMWARE_MIN}+ needed"
                          + (f", got \"{self.firmware}\")" if self.firmware else "): is main.py running?"))
        if "pca=missing" in self.firmware:
            self._close()
            raise OSError(f"the pyboard answers but its PCA9685 doesn't (I2C X9/X10, 3V3, GND): check the wiring, then reset the board")
        self._write(b"A\n")   # you armed it here: clears a stop latched with the board's USR switch (fw v3)
        if self.home:
            self._write(_frame("P", self.home))
            self._write(_frame("S", self.home))

    def _ask(self, line, wait=0.5, prefix="6DOF", fd=None):
        fd = self.fd if fd is None else fd
        try:
            os.read(fd, 4096)
        except (BlockingIOError, OSError):
            pass
        self._write(line, fd=fd)
        buf, end = b"", time.monotonic() + wait
        while time.monotonic() < end and not any(ln.startswith(prefix.encode()) for ln in buf.split(b"\n")[:-1]):
            select.select([fd], [], [], max(0.0, end - time.monotonic()))
            try:
                buf += os.read(fd, 512)
            except BlockingIOError:
                pass
        for ln in buf.decode(errors="replace").splitlines():
            if ln.startswith(prefix):
                return ln.strip()
        return None

    def query(self):
        """The board's own state (firmware 4+: the Q line), with rates from the previous query. Read-only:
        Q changes nothing on the board, not even a playing demo. None when no board answers."""
        if board_busy.is_set():
            return {"answered": False, "busy": True}
        with self.lock:
            try:
                if self.fd is not None:
                    line = self._ask(b"Q\n", wait=0.3, prefix="STATE")
                else:
                    port = find_port()
                    if not port:
                        return None
                    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                    try:
                        self._raw(fd)
                        line = self._ask(b"Q\n", wait=0.3, prefix="STATE", fd=fd)
                    finally:
                        os.close(fd)
            except OSError:
                return None
            return self._report(line)

    def _report(self, line):
        if not line:
            return {"answered": False}
        r = {"answered": True}
        for kv in line.split()[1:]:
            k, _, v = kv.partition("=")
            r[k] = int(v) if v.lstrip("-").isdigit() else v
        now, prev = time.monotonic(), self.prev_report
        self.prev_report = (now, r)
        if prev and "ms" in r and "ms" in prev[1]:
            dt = now - prev[0]
            board_dt = ((r["ms"] - prev[1]["ms"]) % (1 << 30)) / 1000   # ticks_ms wraps at 2^30
            if 0.5 < dt < 60 and abs(board_dt - dt) < 1.0:              # the same board run, no reset between
                r["loop_hz"] = round(((r["loops"] - prev[1]["loops"]) % (1 << 30)) / board_dt)
                r["tick_hz"] = round((r["ticks"] - prev[1]["ticks"]) / board_dt)
                r["clock_ok"] = abs((r["clock"] - prev[1]["clock"]) / 1000 - board_dt) < 0.2
        return r

    @staticmethod
    def _raw(fd):
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, a)

    def _write(self, data, wait=0.05, fd=None):
        fd = self.fd if fd is None else fd
        end = time.monotonic() + wait
        while data:
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                if time.monotonic() > end:
                    raise OSError("the pyboard is not reading")
                select.select([], [fd], [], 0.01)

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _connect_locked(self):
        """Open the port unless open, or retried less than RETRY s ago. True when connected."""
        if self.fd is not None:
            return True
        now = time.monotonic()
        if now < self.next_try:
            return False
        try:
            self._open()
            self.error = None
            return True
        except OSError as e:
            self.error = str(e)
            self.next_try = now + self.RETRY
            self._close()
            return False

    # -- use

    def arm(self, on, home=None):
        """on: connect, check the firmware, send park + home; refused (False) without a valid hello.
        home: the pulses of the home pose (park position too)."""
        with self.lock:
            if not on:
                self.armed = False
                if self.fd is not None:
                    try:
                        self._write(b"O\n")
                    except OSError:
                        pass
                self._close()
                self.error = None
                return True
            if home is not None:
                self.home = list(home)
            self.next_try = 0.0
            if not self._connect_locked():
                self.armed = False
                return False
            self.armed = True
            self.last = self.home and list(self.home)
            self.sent_at = time.monotonic()
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._sender, name="motion-link", daemon=True)
            self.thread.start()
        return True

    def send(self, us):
        with self.lock:
            self.last = list(us)
            if self.armed:
                self.pending = _frame("S", us)
                self.wake.set()

    def _sender(self):
        while True:
            self.wake.wait(self.KEEP / 3)
            self.wake.clear()
            with self.lock:
                if not self.armed:
                    return
                frame = self.pending
                self.pending = None
                if frame is None and self.last and time.monotonic() - self.sent_at > self.KEEP:
                    frame = _frame("S", self.last)  # keepalive
                if frame is None:
                    continue
                if not self._connect_locked():
                    continue
                try:
                    self._write(frame)
                    self.sent_at = time.monotonic()
                except OSError as e:
                    self.error = str(e)
                    self._close()
                    self.next_try = time.monotonic() + self.RETRY

    def status(self):
        return {"port": self.port if self.fd is not None else find_port(), "connected": self.fd is not None,
                "armed": self.armed, "firmware": self.firmware, "error": self.error, "last": self.last}


link = Link()
board_busy = threading.Event()   # simdash is copying files to the board (raw REPL): leave the port alone


def setup():
    s = load()
    try:
        d = design(s["geometry"])
    except ValueError as e:
        d = {"error": str(e)}
    return {"settings": s, "design": d, "link": link.status(), "defaults": DEFAULTS, "warnings": pulse_warnings(s),
            "cueing": cueing_settings(), "cueing_defaults": CUEING_DEFAULTS, "replay": player.info()}


# ---------- motion cueing: telemetry -> pose ----------

G = 9.81
CUEING_MODES = ("washout", "body")
CUEING_DEFAULTS = {
    # washout: tuned to fool an inner ear (tilt for sustained g, quick shifts that drift back);
    # body: what a car body does on its suspension, as seen from outside (for a model car on the desk)
    "mode": "washout",
    "pitch_per_g": 12.0,   # deg of nose-down per g of braking (and nose-up when accelerating)
    "roll_per_g": 10.0,    # deg of lean to the outside per g of cornering
    "tilt_ms": 150.0,      # smoothing of the tilts (low-pass time constant)
    "surge_per_g": 15.0,   # mm of quick shift per g of change in acceleration (onset, then back to centre)
    "sway_per_g": 12.0,
    "heave_per_g": 10.0,   # mm per g of vertical acceleration (bumps, crests, compressions)
    "yaw_per_rad": 12.0,   # deg per rad/s of yaw rate (a flick on turn-in)
    "washout_ms": 800.0,   # how fast the shifts and the yaw flick drift back to centre (high-pass)
    "smooth_ms": 40.0,     # final smoothing of everything (servo jitter)
    "budget": 0.5,         # each axis is compressed (tanh) towards this share of its single-axis reach (0.5: combined
                           # poses rarely need the whole-pose scale: 2-3 % of frames on a Brands Hatch race)
    "master": 1.0,         # overall scale
    # body mode
    "body_pitch_per_cm": 5.0,   # deg per cm of front-minus-rear suspension (from the session's median)
    "body_roll_per_cm": 5.0,    # deg per cm of left-minus-right suspension
    "body_heave_per_cm": 4.0,   # mm per cm of mean suspension
    "kerb_per_g": 6.0,          # mm per g of vertical acceleration, high-passed (kerbs, bumps)
    "body_hz": 1.8,             # no suspension recorded: pitch/roll from a spring-damper on the g-forces
    "body_zeta": 0.5,
    "slip_yaw": 1.0,            # deg of yaw per deg of slip angle: the nose shows where the car points vs where it goes
    "slope_gain": 0.4,          # share of the track's slope (pitch) and banking (roll)
}
FADE_S = 0.5               # fade in after load / seek / gap / play, out to home on pause


def cueing_settings():
    out = dict(CUEING_DEFAULTS)
    for k, v in (settings().get("cueing") or {}).items():
        if k == "mode":
            out[k] = v if v in CUEING_MODES else "washout"
        elif k in CUEING_DEFAULTS:
            out[k] = float(v)
    return out


def soft_limit(v, lo, hi):
    """Compress v smoothly towards the budget [-lo, hi] (tanh): about linear well inside, never past it."""
    b = hi if v >= 0 else lo
    return b * math.tanh(v / b) if b > 0 else 0.0


class Cueing:
    """Classic washout, simplified for a desk toy: sustained acceleration becomes tilt (low-passed),
    changes in acceleration and yaw rate become short moves that drift back (high-passed)."""

    def __init__(self, c, budget=None):
        self.c = c
        self.budget = budget or {}   # {axis: (neg, pos)} single-axis reach, from reach()
        self.fade, self.fade_to = 0.0, 0.0
        self.reset()

    def reset(self):
        self.prev = None
        self.hp = {k: 0.0 for k in ("x", "y", "z", "yaw")}
        self.lp = {k: 0.0 for k in ("pitch", "roll")}
        self.out = {ax: 0.0 for ax in AXES}
        self.body = {"pitch": 0.0, "roll": 0.0, "dpitch": 0.0, "droll": 0.0, "kerb": 0.0, "up": None,
                     "slope": 0.0, "bank": 0.0}

    def reseed(self, s):
        """Start again from these inputs (after a seek or a skipped gap): the jump in inputs must not come
        out of the high-passes as a spike; the tilts start at their steady value, faded in from home."""
        c = self.c
        s = self._clip(s)
        self.prev = self._shifts(s)
        self.hp = {k: 0.0 for k in self.hp}
        self.lp = {"pitch": s["fwd"] / G * c["pitch_per_g"], "roll": s["left"] / G * c["roll_per_g"]}
        b = self.body
        b.update(pitch=self.lp["pitch"], roll=self.lp["roll"], dpitch=0.0, droll=0.0, kerb=0.0, up=s["up"],
                 slope=s.get("slope", 0.0), bank=s.get("bank", 0.0))
        self.fade = 0.0

    @staticmethod
    def _clip(s):
        return {**s, **{k: max(-3 * G, min(3 * G, s[k])) for k in ("fwd", "left", "up")}}  # kerb / crash spikes

    def _shifts(self, s):
        c = self.c
        return {"x": s["fwd"] / G * c["surge_per_g"], "y": s["left"] / G * c["sway_per_g"],
                "z": s["up"] / G * c["heave_per_g"], "yaw": s["yaw_rate"] * c["yaw_per_rad"]}

    def step(self, s, dt):
        """s: {fwd, left, up (m/s²), yaw_rate (rad/s)} in the car's frame; dt in seconds."""
        c = self.c
        s = self._clip(s)
        x = self._shifts(s)
        if dt > 0:  # fade in / out
            d = dt / FADE_S
            self.fade = min(self.fade_to, self.fade + d) if self.fade < self.fade_to else max(self.fade_to, self.fade - d)
        if self.prev is None or dt <= 0:
            self.prev = x
            return dict(self.out)
        target = self._body(s, dt) if c.get("mode") == "body" else self._washout(s, x, dt)
        m = dt / (c["smooth_ms"] / 1000 + dt)
        for k in AXES:
            v = c["master"] * target[k]
            if k in self.budget:  # soft limit before the smoothing, so the limit is smooth too
                lo, hi = self.budget[k]
                v = soft_limit(v, c["budget"] * lo, c["budget"] * hi)
            self.out[k] += m * (self.fade * v - self.out[k])
        return dict(self.out)

    def _body(self, s, dt):
        """A car body on its suspension: pitch/roll from the suspension (or a spring-damper on the g-forces
        when none was recorded), heave from the mean suspension plus kerb hits, yaw = slip angle,
        plus a share of the track's slope and banking."""
        c, b = self.c, self.body
        if s.get("susp") is not None:   # deviations from the session's median, cm; + = extended
            fr, lr, mean = s["susp"]
            pitch, roll = c["body_pitch_per_cm"] * fr, c["body_roll_per_cm"] * lr
            heave = c["body_heave_per_cm"] * mean
        else:
            w = 2 * math.pi * c["body_hz"]
            for k, target in (("pitch", s["fwd"] / G * c["pitch_per_g"]), ("roll", s["left"] / G * c["roll_per_g"])):
                acc = w * w * (target - b[k]) - 2 * c["body_zeta"] * w * b["d" + k]
                b["d" + k] += acc * dt
                b[k] += b["d" + k] * dt
            pitch, roll, heave = b["pitch"], b["roll"], 0.0
        # kerbs: vertical acceleration, high-passed
        a = 0.15 / (0.15 + dt)
        up = s["up"] / G * c["kerb_per_g"]
        b["kerb"] = a * (b["kerb"] + up - (b["up"] if b["up"] is not None else up))
        b["up"] = up
        # track slope and banking, slow
        k = dt / (0.5 + dt)
        b["slope"] += k * (s.get("slope", 0.0) - b["slope"])
        b["bank"] += k * (s.get("bank", 0.0) - b["bank"])
        return {"x": 0.0, "y": 0.0, "z": heave + b["kerb"],
                "pitch": pitch + c["slope_gain"] * b["slope"], "roll": roll + c["slope_gain"] * b["bank"],
                "yaw": -c["slip_yaw"] * s.get("slip", 0.0)}

    def _washout(self, s, x, dt):
        c = self.c
        a = (c["washout_ms"] / 1000) / (c["washout_ms"] / 1000 + dt)
        for k in self.hp:
            self.hp[k] = a * (self.hp[k] + x[k] - self.prev[k])
        self.prev = x
        b = dt / (c["tilt_ms"] / 1000 + dt)
        # braking (fwd < 0) -> nose down (pitch < 0); acceleration to the left (left turn) -> lean right (roll > 0)
        self.lp["pitch"] += b * (s["fwd"] / G * c["pitch_per_g"] - self.lp["pitch"])
        self.lp["roll"] += b * (s["left"] / G * c["roll_per_g"] - self.lp["roll"])
        return {"x": self.hp["x"], "y": self.hp["y"], "z": self.hp["z"], "yaw": self.hp["yaw"],
                "pitch": self.lp["pitch"], "roll": self.lp["roll"]}


def fit_scale(pose, g):
    """The largest share (0-1) of the pose the platform can take, whole (keeps its shape)."""
    gm = geo(g)
    L, H = gm["L"], gm["H"]
    if within(ik(pose, g, L, H), g, pose):
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(10):
        m = (lo + hi) / 2
        p = {k: v * m for k, v in pose.items()}
        if within(ik(p, g, L, H), g, p):
            lo = m
        else:
            hi = m
    return lo


def fit_pose(pose, g, L=None, H=None):
    """The pose, scaled down as a whole until the platform can take it."""
    s = fit_scale(pose, g)
    return ({k: v * s for k, v in pose.items()} if s < 1 else pose), s


class ScaleGuard:
    """Last resort after the soft limits: the whole-pose scale, smoothed (fast to shrink, slow to recover)
    so its changes don't reach the servos as steps; never above what fits."""

    ATTACK, RELEASE = 0.05, 0.5

    def __init__(self):
        self.s = 1.0

    def apply(self, pose, g, dt):
        need = fit_scale(pose, g)
        tau = self.ATTACK if need < self.s else self.RELEASE
        self.s += (need - self.s) * (dt / (tau + dt) if dt > 0 else 1.0)
        self.s = s = min(self.s, need)  # remember what was used: recover from there
        return ({k: v * s for k, v in pose.items()} if s < 1 else pose), s


# ---------- replay a recorded session on the platform ----------

GAP = 2.0  # s without ticks (pause, menus): skipped


def car_frame(acc_x, acc_y, acc_z, ang_vel_y):
    """Raw pCARS local values -> the car's frame used by the cueing: (fwd, left, up) in m/s², yaw rate in
    rad/s (+ = turning left). pCARS local frame: X left, Y up, Z back. Checked on recorded laps
    (tests/fixtures): lateral g has the sign of speed x yaw rate, braking gives fwd < 0, and a lap of a
    clockwise track adds up to -360° of yaw."""
    return -(acc_z or 0.0), acc_x or 0.0, acc_y or 0.0, ang_vel_y or 0.0


def read_course(ref):
    """The recorded inputs of a session, in the car's frame (pCARS local: X left, Y up, -Z forward)."""
    day, sid = store.parse_ref(ref)
    con = store.read_day(day)
    with con:
        s = con.execute("SELECT car, track, variation FROM v_sessions WHERE id=?", (sid,)).fetchone()
        if s is None:
            raise LookupError("no such session")
        rows = con.execute(
            "SELECT t.ts, e.lap, e.speed, e.local_acc_x, e.local_acc_y, e.local_acc_z, e.angular_vel_y, e.x, e.z, "
            "t.id, e.local_vel_x, e.local_vel_z, e.orientation_x, e.orientation_z "
            "FROM tick t JOIN telemetry e ON e.tick_id = t.id WHERE t.session_id=? ORDER BY t.ts", (sid,)).fetchall()
        susp = {}
        if rows:  # suspension per wheel (FL FR RL RR); slimmed days keep none
            for tid, w, tr in con.execute("SELECT tick_id, wheel, suspension_travel FROM wheel WHERE tick_id BETWEEN ? AND ?",
                                          (rows[0][9], rows[-1][9])):
                if tr is not None:
                    susp.setdefault(tid, [None] * 4)[w] = tr
    con.close()
    if not rows:
        raise LookupError("this session has no recorded telemetry")
    d = {"car": s["car"], "track": s["track"], "variation": s["variation"],
         "ts": [], "lap": [], "kmh": [], "fwd": [], "left": [], "up": [], "yaw_rate": [], "x": [], "z": [],
         "slip": [], "slope": [], "bank": [], "susp": []}
    for ts, lap, sp, ax, ay, az, wy, x, z, tid, vx, vz, ox, oz in rows:
        # slip angle (deg, + = moving to the left of where the nose points), above 5 m/s
        fwd_v = -(vz or 0.0)
        d["slip"].append(math.degrees(math.atan2(vx or 0.0, fwd_v)) if fwd_v > 5 else 0.0)
        # orientation: x = nose up (checked: climb rate = speed·sin(x)); z: - = leaning right
        d["slope"].append(math.degrees(ox or 0.0)); d["bank"].append(-math.degrees(oz or 0.0))
        w = susp.get(tid)
        d["susp"].append(w if w and None not in w else None)
        d["x"].append(x or 0.0); d["z"].append(z or 0.0)
        d["ts"].append(ts); d["lap"].append(lap or 0); d["kmh"].append(round((sp or 0) * 3.6))
        fwd, left, up, yaw_rate = car_frame(ax, ay, az, wy)
        d["fwd"].append(fwd); d["left"].append(left); d["up"].append(up); d["yaw_rate"].append(yaw_rate)
    laps, last = [], None
    for i, lap in enumerate(d["lap"]):
        if lap != last:
            laps.append({"lap": lap, "ts": d["ts"][i]})
            last = lap
    d["laps"] = laps
    # suspension (travel grows as the wheel extends: checked on recorded laps) as front-rear, left-right and
    # mean in cm, from the session's median (static rake and ride height removed); None when not recorded
    full = [w for w in d["susp"] if w]
    if len(full) > 0.5 * len(d["ts"]):
        def fr(w): return ((w[0] + w[1]) - (w[2] + w[3])) / 2 * 100
        def lr(w): return ((w[0] + w[2]) - (w[1] + w[3])) / 2 * 100
        def mean(w): return sum(w) / 4 * 100
        base = [sorted(f(w) for w in full)[len(full) // 2] for f in (fr, lr, mean)]
        d["susp"] = [(fr(w) - base[0], lr(w) - base[1], mean(w) - base[2]) if w else None for w in d["susp"]]
    else:
        d["susp"] = [None] * len(d["ts"])
    d["has_susp"] = len(full) > 0.5 * len(d["ts"])
    # the path for the map: a point every 3 m
    path = []
    for x, z in zip(d["x"], d["z"]):
        if not path or math.hypot(x - path[-1][0], z - path[-1][1]) > 3:
            path.append([round(x, 1), round(z, 1)])
    d["path"] = path
    return d


class Course:
    def __init__(self, ref):
        self.ref = ref
        self.d = read_course(ref)
        self.t0, self.t1 = self.d["ts"][0], self.d["ts"][-1]

    def index(self, t):
        ts = self.d["ts"]
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

    def sample(self, t):
        """Inputs at time t, interpolated between the 20 Hz ticks."""
        d, i = self.d, self.index(t)
        ts = d["ts"]
        j = min(i + 1, len(ts) - 1)
        u = 0.0 if j == i or ts[j] - ts[i] > GAP else max(0.0, min(1.0, (t - ts[i]) / (ts[j] - ts[i])))
        s = {k: d[k][i] + (d[k][j] - d[k][i]) * u for k in ("fwd", "left", "up", "yaw_rate", "x", "z", "slip", "slope", "bank")}
        a, b = d["susp"][i], d["susp"][j]
        s["susp"] = None if a is None else a if b is None else tuple(a[k] + (b[k] - a[k]) * u for k in range(3))
        s.update(lap=d["lap"][i], kmh=d["kmh"][i])
        return s

    def skip_gap(self, t):
        ts, i = self.d["ts"], self.index(t)
        if i + 1 < len(ts) and ts[i + 1] - ts[i] > GAP and t > ts[i] + 0.2:
            return ts[i + 1]
        return t


class Player:
    """Plays a course: 50 Hz, cueing -> fit inside the reach -> IK -> pulses to the pyboard (when armed)."""

    RATE = 50

    def __init__(self):
        self.lock = threading.Lock()
        self.course = None
        self.playing = False
        self.t = 0.0
        self.speed = 1.0
        self.cue = None
        self.last = None      # solve() of the last pose, for the page
        self.scale = 1.0      # how much the last pose had to be shrunk to fit
        self.guard = ScaleGuard()
        self.inputs = None
        self.thread = None

    def _budget(self):
        return {ax: tuple(v) for ax, v in reach(settings()["geometry"]).items()}

    def load(self, ref):
        c = Course(ref)
        with self.lock:
            self.course, self.t, self.playing = c, c.t0, False
            self.cue = Cueing(cueing_settings(), self._budget())
            self.cue.reseed(c.sample(c.t0))
            self.guard = ScaleGuard()
        self._step(0.0)
        return self.info()

    def info(self):
        c = self.course
        if not c:
            return {}
        return {"ref": c.ref, "car": c.d["car"], "track": c.d["track"], "variation": c.d["variation"],
                "t0": c.t0, "t1": c.t1, "laps": c.d["laps"], "path": c.d["path"], "has_susp": c.d["has_susp"]}

    def play(self, speed=1.0):
        with self.lock:
            if not self.course:
                raise LookupError("no session loaded")
            self.speed = max(0.25, min(4.0, float(speed)))
            if self.t >= self.course.t1:
                self.t = self.course.t0
                self.cue.reseed(self.course.sample(self.t))
            self.cue.c = cueing_settings()
            self.cue.fade_to = 1.0     # fade in from wherever it is (home after a pause)
            self.playing = True
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, name="motion-replay", daemon=True)
            self.thread.start()

    def pause(self):
        """Stops the clock; the platform glides home (fades out), then the thread ends."""
        with self.lock:
            self.playing = False
            if self.cue:
                self.cue.fade_to = 0.0

    def seek(self, t):
        with self.lock:
            if not self.course:
                raise LookupError("no session loaded")
            self.t = max(self.course.t0, min(self.course.t1, float(t)))
            self.cue.reseed(self.course.sample(self.t))   # fades in again from home
        if not (self.thread and self.thread.is_alive()):
            self._step(0.0)

    def stop(self):
        """Unload and park the platform at home."""
        with self.lock:
            self.playing, self.course = False, None
        r = solve({})
        if r["ok"]:
            link.send(r["pulse"])
        self.last = r

    def reload_cueing(self):
        with self.lock:
            if self.cue:
                self.cue.c = cueing_settings()
                self.cue.budget = self._budget()

    def _step(self, dt):
        with self.lock:
            c = self.course
            if not c:
                return
            s = c.sample(self.t)
            pose = self.cue.step(s, dt)
        st = settings()
        fitted, self.scale = self.guard.apply(pose, st["geometry"], dt)
        r = solve(fitted, st)
        self.last, self.inputs = r, s
        if r["ok"]:
            link.send(r["pulse"])

    def _settled(self):
        return self.cue.fade == 0.0 and all(abs(v) < 0.05 for v in self.cue.out.values())

    def _run(self):
        period = 1 / self.RATE
        nxt = time.monotonic()
        while True:
            with self.lock:
                if not self.course or (not self.playing and self._settled()):
                    break
                dt = period * self.speed if self.playing else period
                if self.playing:
                    t = self.t + dt
                    self.t = self.course.skip_gap(t)
                    if self.t > t + 1e-6:  # a gap was skipped: restart the filters on the new inputs
                        self.cue.reseed(self.course.sample(self.t))
                    if self.t >= self.course.t1:
                        self.t, self.playing = self.course.t1, False
                        self.cue.fade_to = 0.0
            self._step(dt)
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))

    def state(self):
        s = self.inputs or {}
        return {"loaded": self.course is not None, "playing": self.playing, "t": self.t, "speed": self.speed,
                "moving": bool(self.thread and self.thread.is_alive()),   # playing, or gliding home after a pause
                "lap": s.get("lap"), "kmh": s.get("kmh"),
                "pos": [round(s["x"], 1), round(s["z"], 1)] if s else None,
                "g": {k: round(s[k] / G, 2) for k in ("fwd", "left", "up")} if s else None,
                "scale": round(self.scale, 2), "solve": self.last, "link": link.status()}


player = Player()


# ---------- standalone demo: a recorded race baked into a file the board plays with no PC ----------

DEMO_RATE = 25
DEMO_STEP = 4                 # µs per int8 step: ±127 x 4 = ±508 µs around the level pulse
DEMO_GLIDE_S = 1.0
DEMO_FILE = store.DATA_DIR / "demo.bin"


def demo_frames(course, t0, t1, s=None):
    """The pulses of a course from t0 to t1 at DEMO_RATE in sim time, through the same pipeline as
    Player._step (cueing -> whole-pose guard -> IK -> calibrated pulses), with a glide in from park and out
    to park of DEMO_GLIDE_S each. Returns (frames, park)."""
    s = s or settings()
    g = s["geometry"]
    park = solve({}, s)["pulse"]
    cue = Cueing(cueing_settings(), {ax: tuple(v) for ax, v in reach(g).items()})
    cue.reseed(course.sample(t0))
    cue.fade_to = 1.0
    guard = ScaleGuard()
    dt = 1.0 / DEMO_RATE
    frames, last, t = [], list(park), t0
    while t <= t1 + 1e-6:              # both ends included (float steps would lose the last frame)
        pose = cue.step(course.sample(t), dt)
        fitted, _ = guard.apply(pose, g, dt)
        r = solve(fitted, s)
        if r["ok"]:
            last = r["pulse"]
        frames.append(list(last))
        nt = course.skip_gap(t + dt)
        if nt > t + dt + 1e-6:          # a gap in the recording: restart the filters on the new inputs
            cue.reseed(course.sample(nt))
        t = nt

    def glide(a, b):
        n = int(DEMO_GLIDE_S * DEMO_RATE)
        out = []
        for k in range(1, n + 1):
            w = (1 - math.cos(math.pi * k / n)) / 2      # eased
            out.append([round(x + (y - x) * w) for x, y in zip(a, b)])
        return out
    if not frames:
        frames = [list(park)]
    return [list(park)] + glide(park, frames[0]) + frames + glide(frames[-1], park), park


def demo_encode(frames, center, park, step=DEMO_STEP, rate=DEMO_RATE):
    """demo.bin bytes, and how many frames had a pulse clipped to ±127 steps."""
    import struct
    if len(frames) > 65535:
        raise ValueError(f"{len(frames)} frames: at most 65535 ({65535 / rate / 60:.0f} min at {rate} Hz)")
    head = b"6DOFDEMO" + struct.pack("<BBH6H6HB", 1, rate, len(frames), *center, *park, step)
    body, clipped = bytearray(), 0
    for f in frames:
        cut = False
        for i, us in enumerate(f):
            v = round((us - center[i]) / step)
            if v > 127 or v < -127:
                v, cut = max(-127, min(127, v)), True
            body.append(v & 0xFF)
        clipped += cut
    return head + bytes(body), clipped


def demo_range(course, what="session", lap=None, from_s=None, to_s=None):
    """(t0, t1) in the course's clock for the whole session, one lap, or seconds from its start."""
    t0, t1 = course.t0, course.t1
    if what == "lap":
        laps = course.d["laps"]
        k = next((i for i, x in enumerate(laps) if x["lap"] == int(lap)), None)
        if k is None:
            raise ValueError(f"no lap {lap}")
        return laps[k]["ts"], laps[k + 1]["ts"] if k + 1 < len(laps) else t1
    if what == "time":
        a, b = course.t0 + float(from_s or 0), course.t0 + float(to_s if to_s is not None else t1 - t0)
        if not a < b:
            raise ValueError("the range must end after it starts")
        return max(t0, a), min(t1, b)
    return t0, t1


def export_demo(ref, what="session", lap=None, from_s=None, to_s=None):
    """Bake a recorded course into DEMO_FILE; returns what the tab shows."""
    course = Course(ref)
    s = settings()
    t0, t1 = demo_range(course, what, lap, from_s, to_s)
    frames, park = demo_frames(course, t0, t1, s)
    data, clipped = demo_encode(frames, park, park)   # steps counted from home: the stroke fits ±127
    DEMO_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEMO_FILE.write_bytes(data)
    return {"bytes": len(data), "frames": len(frames), "seconds": round(len(frames) / DEMO_RATE, 1),
            "clipped": clipped, "ref": ref, "track": course.d["track"], "car": course.d["car"],
            "what": what, "lap": lap, "from_s": round(t0 - course.t0, 1), "to_s": round(t1 - course.t0, 1)}


def write_demo():
    """Put DEMO_FILE on the board: /sd/demo.bin with an SD card, else /flash/demo.bin. mpremote when
    installed, else simdash's own raw-REPL copy. The servos are let go first; the board restarts after."""
    from . import pyboard
    if not DEMO_FILE.exists():
        raise ValueError("build the demo first")
    data = DEMO_FILE.read_bytes()
    port = find_port()
    if not port:
        raise OSError("pyboard not found (MicroPython USB device f055)")
    link.arm(False)                       # sends O and lets go of the port
    board_busy.set()                      # no status Q lines into the raw REPL meanwhile
    try:
        return _write_demo(port, data, pyboard)
    finally:
        board_busy.clear()


def _write_demo(port, data, pyboard):
    import shutil
    import subprocess
    with pyboard.Board(port) as b:
        info = b.info()
        dest = "/sd/demo.bin" if info["sd"] else "/flash/demo.bin"
        room = (info["sd_free"] if info["sd"] else info["flash_free"]) + info["demo"].get(dest, 0) - 2048
        if len(data) > room:
            raise ValueError(f"the demo is {len(data) // 1024} kB, the board has {max(0, room) // 1024} kB free on "
                             f"{'its SD card' if info['sd'] else 'its flash (no SD card)'}: pick a lap or a shorter range")
        tool = shutil.which("mpremote")
        if not tool:
            b.put(data, dest)
    if tool:  # outside the raw REPL session (it soft-reset the board on exit)
        r = subprocess.run([tool, "connect", port, "cp", str(DEMO_FILE), ":" + dest, "+", "reset"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise OSError("mpremote: " + (r.stderr or r.stdout).strip()[-300:])
    return {"dest": dest, "bytes": len(data), "with": "mpremote" if tool else "simdash", "port": port}


# ---------- the firmware itself, installed from simdash's copy ----------

FIRMWARE_DIR = Path(__file__).resolve().parent.parent / "firmware" / "6dof"
FIRMWARE_FILES = ("pca9685.py", "board.py", "demo.py", "fw.py", "main.py")


def firmware_version():
    """VERSION in simdash's copy of board.py (what the button installs)."""
    m = re.search(r'^VERSION = "(\d+)"', (FIRMWARE_DIR / "board.py").read_text(), re.M)
    return m.group(1) if m else None


def write_firmware():
    """Install simdash's firmware/6dof on the pyboard's /flash: every file copied as <name>.new first
    (checked by size), then all renamed at once, so a failed copy leaves the old firmware whole, plus an
    empty /flash/SKIPSD so a micro-SD card never replaces /flash at boot (main.py mounts it at /sd). The
    servos let go first; the board restarts into the new firmware after (demo.bin and other files kept).
    Also the way back for a board that booted off a card: the copy goes by absolute /flash paths."""
    from . import pyboard
    files = {f: (FIRMWARE_DIR / f).read_bytes() for f in FIRMWARE_FILES}
    port = find_port()
    if not port:
        raise OSError("pyboard not found (MicroPython USB device f055)")
    player.stop()
    link.arm(False)
    board_busy.set()
    try:
        with pyboard.Board(port) as b:
            have = b.exec("import os\nprint(os.listdir('/flash'))\ns = os.statvfs('/flash')\nprint(s[0] * s[3])")
            free = int(have.strip().splitlines()[-1])
            need = sum(len(d) for d in files.values()) + 2048
            if need > free:                       # old and new side by side until the renames
                raise ValueError(f"the firmware needs {need // 1024} kB, the board's flash has {free // 1024} kB free")
            for f, data in files.items():
                b.put(data, "/flash/" + f + ".new")
            b.exec("import os\nfor f in %r:\n"
                   "    try:\n        os.remove('/flash/' + f)\n    except OSError:\n        pass\n"
                   "    os.rename('/flash/' + f + '.new', '/flash/' + f)\n"
                   # never boot off an SD card: the firmware is on /flash and mounts a card itself
                   "open('/flash/SKIPSD', 'w').close()\n" % (list(files),))
            sizes = b.exec("import os\nprint([os.stat('/flash/' + f)[6] for f in %r])" % (list(files),))
            if sizes.strip() != str([len(d) for d in files.values()]):
                raise pyboard.BoardError("the files on the board don't match: " + sizes.strip())
    finally:
        board_busy.clear()
    r = None                              # it restarts into the new main.py: see it answer
    for _ in range(10):
        time.sleep(0.5)
        r = link.query()
        if r and r.get("answered"):
            break
    return {"version": firmware_version(), "files": {f: len(d) for f, d in files.items()}, "port": port,
            "running": bool(r and r.get("answered")), "state": r}
