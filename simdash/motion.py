"""Desk 6DOF: a small Stewart platform on six hobby servos, with a model car on top.

Hardware (see docs/6dof/): six MG90-class servos on a PCA9685 PWM board, driven by a MicroPython
pyboard v1.1 over USB serial. simdash does all the maths: pose -> inverse kinematics -> servo
angle -> pulse width (µs), and sends the six pulses as one text line. The pyboard clamps them,
limits how fast each servo moves, and when nothing arrives for a second glides to the park pulses
before going limp (firmware/6dof).

Frame: x forward (the car's nose), y left, z up, origin at the centre of the base plate at the
height of the servo shafts. Servos sit in three pairs around the base; each horn swings in a
vertical plane tangent to the base circle, pointing away from its pair's centre. Angle 0 = horn
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
import select
import termios
import threading
import time

from . import store

log = logging.getLogger("simdash.motion")

SETTINGS = store.DATA_DIR / "motion.json"
AXES = ("x", "y", "z", "roll", "pitch", "yaw")
USB_VENDOR = "f055"  # MicroPython (pyboard: f055:9800 and friends)

DEFAULTS = {
    "geometry": {
        # "flat": the two servos of a pair side by side on one flat face, back to back, shafts parallel,
        #         horns swinging outwards (like most kits); "radial": each shaft points away from the centre
        "layout": "flat",
        "base_r": 58.0,       # mm, base centre -> the horns' plane (flat: the pair's face; radial: each shaft)
        "shaft_spacing": 46.0,  # mm, flat layout: between the two shafts of a pair (MG90 bodies + tabs need ~44.5)
        "base_half": 20.0,    # deg, radial layout: half the angle between the two shafts of a pair
        "plat_r": 46.0,       # mm, platform centre -> rod ball joint
        "plat_half": 24.0,    # deg, half the angle between the two joints of a platform pair
        "horn": 18.0,         # mm, shaft -> rod hole on the horn
        "rod": 90.0,          # mm, ball centre to ball centre
        "first_pair": 0.0,    # deg, where the first servo pair sits (0 = under the car's nose)
        "servo_limit": 60.0,  # deg, horn travel allowed each way from horizontal
        # horn-rod angle kept within [min, 180 - min]: near 0 or 180 the leg toggles (the horn turns,
        # the platform barely moves, loads spike)
        "transmission_min_deg": 35.0,
        # each ball joint's swivel from its home direction (studs printed along the home rod direction)
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
    try:
        saved = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return s
    if isinstance(saved.get("geometry"), dict) and "layout" not in saved["geometry"]:
        saved["geometry"]["layout"] = "radial"  # saved before the flat layout existed: keep its meaning
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
    return s


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
        if k != "layout":
            g[k] = float(g[k])
    if g["layout"] not in ("flat", "radial"):
        raise ValueError("layout is flat or radial")
    if not (g["horn"] > 0 and g["rod"] > g["horn"] and g["base_r"] > 0 and g["plat_r"] > 0):
        raise ValueError("lengths must be positive and the rod longer than the horn")
    if not 0 < g["servo_limit"] <= 90:
        raise ValueError("servo limit must be within 0-90°")
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
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)
    _cache["s"] = None  # invalidate
    return s


# ---------- geometry ----------

def legs(g):
    """[(B, u, P)] per servo 0-5: shaft (x, y), horn direction (x, y), platform anchor (x, y) at home."""
    out = []
    for k in range(6):
        side = 1 if k % 2 else -1          # 0: clockwise servo of the pair, 1: counter-clockwise
        cb = math.radians(g["first_pair"] + 120 * (k // 2))
        if g.get("layout", "radial") == "flat":
            # both shafts on the pair's face, parallel (pointing outwards), horns along the face
            n, tc = (math.cos(cb), math.sin(cb)), (-math.sin(cb), math.cos(cb))
            h = g["shaft_spacing"] / 2
            B = (g["base_r"] * n[0] + side * h * tc[0], g["base_r"] * n[1] + side * h * tc[1])
            u = (side * tc[0], side * tc[1])
        else:
            b = cb + side * math.radians(g["base_half"])
            B = (g["base_r"] * math.cos(b), g["base_r"] * math.sin(b))
            u = (-math.sin(b) * side, math.cos(b) * side)   # tangent, away from the pair's centre
        p = cb + side * math.radians(60) - side * math.radians(g["plat_half"])
        P = (g["plat_r"] * math.cos(p), g["plat_r"] * math.sin(p))
        out.append((B, u, P))
    return out


def h0(g):
    """Home height of the platform joints above the shafts (all horns horizontal)."""
    B, u, P = legs(g)[0]
    ax, ay = B[0] + g["horn"] * u[0], B[1] + g["horn"] * u[1]
    d2 = (P[0] - ax) ** 2 + (P[1] - ay) ** 2
    if d2 >= g["rod"] ** 2:
        raise ValueError("rods too short to reach the platform anchors")
    return math.sqrt(g["rod"] ** 2 - d2)


def _gkey(g):
    return tuple(sorted((k, v) for k, v in g.items()))


@functools.lru_cache(maxsize=16)
def _geo(key):
    g = dict(key)
    L, H = legs(g), h0(g)
    home = ik({}, g, L, H)
    rods = [tuple(r["P"][i] - r["A"][i] for i in range(3)) for r in home]
    return {"L": L, "H": H, "home_rod": rods}


def geo(g):
    """legs, home height and the home rod directions of a geometry, computed once."""
    return _geo(_gkey(g))


def _angle(a, b):
    c = sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


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
        al = math.radians(alpha or 0)
        A = (B[0] + a * math.cos(al) * u[0], B[1] + a * math.cos(al) * u[1], a * math.sin(al))
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
        if abs(a) > g["servo_limit"]:
            return f"servo {k} past its travel ({a:.0f}°)"
        horn = tuple(r["A"][i] - r["B"][i] for i in range(3))
        rod = tuple(r["P"][i] - r["A"][i] for i in range(3))
        tr = _angle(horn, rod)
        if tr < tmin or tr > 180 - tmin:
            return f"servo {k}: horn and rod nearly in line ({tr:.0f}°)"
        home = gm["home_rod"][k]
        # the platform's stud turns with the platform: the rod seen from the platform
        plat = tuple(sum(R[j][i] * rod[j] for j in range(3)) for i in range(3))
        if _angle(plat, home) > jmax:
            return f"joint {k} on the platform swivels {_angle(plat, home):.0f}°"
        # the horn's stud turns with the horn, about w = u × z by alpha: undo it
        u = gm["L"][k][1]
        horn_view = _turn(rod, (u[1], -u[0], 0.0), -math.radians(a))
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
    lean = math.degrees(math.acos(H / g["rod"]))  # horn tips sit at shaft height at home
    home = ik({}, g, L, H)
    trans = [_angle(tuple(r["A"][i] - r["B"][i] for i in range(3)), gm["home_rod"][k]) for k, r in enumerate(home)]
    spacing = g["shaft_spacing"] if g.get("layout") == "flat" else 2 * g["base_r"] * math.sin(math.radians(g["base_half"]))
    p_spacing = 2 * g["plat_r"] * math.sin(math.radians(g["plat_half"]))
    return {
        "home_height": round(H, 1),
        "layout": g.get("layout", "radial"),
        # flat: the two bodies sit back to back between the shafts
        # bodies plus the screw tabs that stick out past them, and 1 mm of air
        "min_spacing": round(2 * (MG90["len"] - MG90["shaft"] + (MG90["tabs"] - MG90["len"]) / 2) + 1, 1) if g.get("layout") == "flat" else None,
        "rod_lean": round(lean, 1),
        "transmission_home": round(trans[0], 1),   # horn-rod angle at home (90 is ideal)
        "shaft_spacing": round(spacing, 1),
        "joint_spacing": round(p_spacing, 1),
        "reach": reach(g),
        "legs": [{"shaft": [round(c, 2) for c in B], "horn_dir": [round(c, 4) for c in u],
                  "joint": [round(c, 2) for c in P]} for B, u, P in L],
    }


def raw_pulse(i, a, sv):
    """Horn angle (deg) -> pulse (µs) for servo i, from its three calibration points, not clamped."""
    c = sv["center_us"][i]
    slope = (sv["plus45_us"][i] - c) / 45 if a >= 0 else (c - sv["minus45_us"][i]) / 45
    return c + a * slope


def pulses(alphas, sv):
    """Horn angles (deg) -> pulse widths (µs), clamped; None angles hold the centre."""
    return [int(round(min(sv["max_us"], max(sv["min_us"], raw_pulse(i, a or 0.0, sv))))) for i, a in enumerate(alphas)]


def pulse_warnings(s):
    """Servos whose full travel (± servo_limit) needs pulses outside min-max: the clamp would cut it."""
    sv, lim = s["servos"], s["geometry"]["servo_limit"]
    out = []
    for i in range(6):
        lo, hi = sorted((raw_pulse(i, -lim, sv), raw_pulse(i, lim, sv)))
        if lo < sv["min_us"] or hi > sv["max_us"]:
            out.append(f"servo {i}: ±{lim:g}° needs {lo:.0f}–{hi:.0f} µs, outside {sv['min_us']}–{sv['max_us']}")
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
        "pulse": pulses(alphas, s["servos"]),
        "legs": [{k: [round(c, 2) for c in r[k]] for k in ("B", "A", "P")} for r in res],
        "home_height": round(gm["H"], 2),
    }


# ---------- 1:1 drawings for the 3D printed parts ----------

# MG90-class body, approximate (measure yours): length along the swing, shaft centre from the outer end,
# horn plane to top face, top face to bottom, tab length, tabs below the top face
MG90 = {"len": 22.8, "wid": 12.2, "shaft": 5.9, "top": 4.5, "depth": 22.7, "tabs": 32.5,
        "tab_depth": 6.0}


def template_svg(part, g):
    """A true-scale SVG (mm) of the base or the platform: holes, numbers, directions. Import it into
    the CAD program as a sketch, or print it at 100 % to check against the servos."""
    gm = geo(g)
    L = gm["L"]
    lean = math.degrees(math.acos(gm["H"] / g["rod"]))

    def stud(x, y, rod, sign):
        """An arrow along the home rod's direction seen from above (sign -1: the other end's view)."""
        dx, dy = sign * rod[0], sign * rod[1]
        n = math.hypot(dx, dy) or 1
        ex, ey = x + dx / n * 7, y + dy / n * 7
        return (f'<line x1="{X(x):.2f}" y1="{Y(y):.2f}" x2="{X(ex):.2f}" y2="{Y(ey):.2f}" stroke="#06c" stroke-width="0.4"/>'
                f'<circle cx="{X(ex):.2f}" cy="{Y(ey):.2f}" r="0.6" fill="#06c" stroke="none"/>')
    pad = 20
    reach_r = math.hypot(g["base_r"], g.get("shaft_spacing", 0) / 2 + g["horn"]) if g.get("layout") == "flat" else g["base_r"] + g["horn"]
    R = (reach_r + MG90["len"]) if part == "base" else g["plat_r"] + 12
    size = 2 * (R + pad)
    c = size / 2

    def X(x): return c + x                                       # x forward -> right on paper
    def Y(y): return c - y                                       # y left -> up on paper
    e = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{size:.1f}mm" height="{size:.1f}mm" '
         f'viewBox="0 0 {size:.1f} {size:.1f}" font-family="sans-serif">',
         '<rect width="100%" height="100%" fill="#fff"/>',
         f'<g stroke="#000" fill="none" stroke-width="0.25">',
         f'<line x1="{c-6}" y1="{c}" x2="{c+6}" y2="{c}"/><line x1="{c}" y1="{c-6}" x2="{c}" y2="{c+6}"/>']
    if part == "base":
        if g.get("layout") == "flat":  # each pair's mounting face (where the horns swing)
            for j in range(3):
                cb = math.radians(g["first_pair"] + 120 * j)
                n, tc = (math.cos(cb), math.sin(cb)), (-math.sin(cb), math.cos(cb))
                L2 = g["shaft_spacing"] / 2 + g["horn"] + 4
                a = (g["base_r"] * n[0] - L2 * tc[0], g["base_r"] * n[1] - L2 * tc[1])
                b = (g["base_r"] * n[0] + L2 * tc[0], g["base_r"] * n[1] + L2 * tc[1])
                e.append(f'<line x1="{X(a[0]):.2f}" y1="{Y(a[1]):.2f}" x2="{X(b[0]):.2f}" y2="{Y(b[1]):.2f}" stroke-dasharray="2 2"/>')
        else:
            e.append(f'<circle cx="{c}" cy="{c}" r="{g["base_r"]:.2f}" stroke-dasharray="2 2"/>')
        for k, (B, u, P) in enumerate(L):
            bx, by = X(B[0]), Y(B[1])
            tx, ty = X(B[0] + g["horn"] * u[0]), Y(B[1] + g["horn"] * u[1])
            # servo body seen from above: the shaft points outwards, so the body reaches inwards from
            # the horn's plane; its length lies along the horn's swing, the shaft at the end nearest the
            # pair's centre and the body running towards the next pair (towards each other they collide).
            # The tabs (screws parallel to the shaft) need a wall at the tab depth: drawn as a strip.
            flat = g.get("layout") == "flat"
            if flat:  # bodies back to back between the shafts, all facing the pair's outward direction
                cb = math.radians(g["first_pair"] + 120 * (k // 2))
                n = (math.cos(cb), math.sin(cb))
                c0 = MG90["shaft"] - MG90["len"] / 2
            else:     # each body radial, running from its shaft towards the next pair
                n = (B[0] / math.hypot(*B), B[1] / math.hypot(*B))
                c0 = MG90["len"] / 2 - MG90["shaft"]   # body centre along u, from the shaft

            def box(t0, t1, r0, r1, style):
                pts = [(B[0] + tt * u[0] + rr * n[0], B[1] + tt * u[1] + rr * n[1])
                       for tt, rr in ((t0, r0), (t1, r0), (t1, r1), (t0, r1))]
                e.append('<polygon points="' + " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in pts) + f'" {style}/>')
            box(c0 - MG90["len"] / 2, c0 + MG90["len"] / 2, -MG90["top"] - MG90["depth"], -MG90["top"],
                'stroke="#888" stroke-dasharray="1 1"')
            box(c0 - MG90["tabs"] / 2, c0 + MG90["tabs"] / 2, -MG90["top"] - MG90["tab_depth"] - 1.2,
                -MG90["top"] - MG90["tab_depth"] + 1.2, 'stroke="#888"')
            e.append(f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="1.5"/>'
                     f'<line x1="{bx:.2f}" y1="{by:.2f}" x2="{tx:.2f}" y2="{ty:.2f}" stroke="#c00"/>'
                     f'<circle cx="{tx:.2f}" cy="{ty:.2f}" r="1" stroke="#c00"/>')
            e.append(stud(B[0] + g["horn"] * u[0], B[1] + g["horn"] * u[1], gm["home_rod"][k], 1))
            nx, ny = X(B[0] * 0.78), Y(B[1] * 0.78)
            e.append(f'<text x="{nx:.2f}" y="{ny+1.6:.2f}" font-size="4.5" text-anchor="middle" stroke="none" fill="#000">{k}</text>')
        pair = (f"pairs back to back on flat faces {g['base_r']:g} mm from the centre, shafts {g['shaft_spacing']:g} mm apart"
                if g.get("layout") == "flat" else f"servo shafts on r={g['base_r']:g} mm, {2*g['base_half']:g}° apart in a pair")
        note = (f"BASE 1:1  {pair}; "
                f"red = horn {g['horn']:g} mm at 0° (horizontal)|dashed = MG90 body, strip = its screw tabs "
                f"(approx., measure yours)" + ("" if g.get("layout") == "flat" else "; circle = horn plane")
                + f"|blue = ball stud on the horn: aim it along the rod at home, {lean:.0f}° from vertical, leaning this way")
    else:
        e.append(f'<circle cx="{c}" cy="{c}" r="{g["plat_r"]:.2f}" stroke-dasharray="2 2"/>')
        for k, (B, u, P) in enumerate(L):
            px, py = X(P[0]), Y(P[1])
            e.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="1.5"/>'
                     f'<text x="{X(P[0]*0.8):.2f}" y="{Y(P[1]*0.8)+1.6:.2f}" font-size="4.5" text-anchor="middle" stroke="none" fill="#000">{k}</text>')
            e.append(stud(P[0], P[1], gm["home_rod"][k], -1))
        note = (f"PLATFORM 1:1 (seen from above)  ball joints on r={g['plat_r']:g} mm, {2*g['plat_half']:g}° apart in a pair; "
                f"joint k takes the rod from servo k|blue = ball stud under the platform: aim it down along the rod at home, "
                f"{lean:.0f}° from vertical, its tip leaning this way")
    # the car's nose
    e.append(f'<path d="M{c+12} {c} l-4 -2 v4 z" fill="#000"/><text x="{c+14}" y="{c+1.5}" font-size="4" stroke="none" fill="#000">x (nose)</text>')
    # a 50 mm scale bar to check the print
    e.append(f'<line x1="{pad/2}" y1="{size-pad/2}" x2="{pad/2+50}" y2="{size-pad/2}" stroke-width="0.5"/>'
             f'<text x="{pad/2}" y="{size-pad/2-2}" font-size="3.5" stroke="none" fill="#000">50 mm</text>')
    for i, ln in enumerate(note.split("|")):
        e.append(f'<text x="{pad/2}" y="{pad/2 + 4.5 * i}" font-size="3.2" stroke="none" fill="#000">{ln}</text>')
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

    # -- port

    def _open(self):
        port = find_port()
        if not port:
            raise OSError("pyboard not found (MicroPython USB device f055)")
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(fd)
        a[0] = a[1] = a[3] = 0
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        a[4] = a[5] = termios.B115200
        termios.tcsetattr(fd, termios.TCSANOW, a)
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
        if self.home:
            self._write(_frame("P", self.home))
            self._write(_frame("S", self.home))

    def _ask(self, line, wait=0.5):
        try:
            os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            pass
        self._write(line)
        buf, end = b"", time.monotonic() + wait
        while time.monotonic() < end and b"\n" not in buf:
            select.select([self.fd], [], [], max(0.0, end - time.monotonic()))
            try:
                buf += os.read(self.fd, 256)
            except BlockingIOError:
                pass
        for ln in buf.decode(errors="replace").splitlines():
            if ln.startswith("6DOF"):
                return ln.strip()
        return None

    def _write(self, data, wait=0.05):
        end = time.monotonic() + wait
        while data:
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                if time.monotonic() > end:
                    raise OSError("the pyboard is not reading")
                select.select([], [self.fd], [], 0.01)

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
