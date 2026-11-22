"""Desk 6DOF: a small Stewart platform on six hobby servos, with a model car on top.

Hardware (see docs/6dof/): six MG90-class servos on a PCA9685 PWM board, driven by a MicroPython
pyboard v1.1 over USB serial. simdash does all the maths: pose -> inverse kinematics -> servo
angle -> pulse width (µs), and sends the six pulses as one text line. The pyboard only clamps
them, writes the PCA9685 and goes limp when nothing arrives for a second.

Frame: x forward (the car's nose), y left, z up, origin at the centre of the base plate at the
height of the servo shafts. Servos sit in three pairs around the base; each horn swings in a
vertical plane tangent to the base circle, pointing away from its pair's centre. Angle 0 = horn
horizontal; the platform height where all six horns are horizontal is the home height.

Leg k: shaft B, horn direction u, horn length a, rod length s, platform anchor P (world).
With l = P - B:  L = |l|² + a² - s²,  M = 2a·l_z,  N = 2a·(l·u)
                 alpha = asin(L / √(M²+N²)) - atan2(N, M)      (unreachable when |L| > √(M²+N²))
"""

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
    },
    "servos": {
        "center_us": [1500] * 6,       # pulse with the horn horizontal (trim it per servo)
        "us_per_deg": 10.0,            # MG90-class: about 1000 µs per 90°; measure yours
        "dir": [1, -1, 1, -1, 1, -1],  # +1 when a longer pulse lifts the horn; mirrored pairs alternate
        "min_us": 600,
        "max_us": 2400,
    },
    "car": {"length": 180.0, "width": 80.0, "height": 50.0},  # mm, the model on top (1:24 ≈ 180 mm)
    "cueing": {},  # overrides of CUEING_DEFAULTS (replay and, later, live)
}


# ---------- settings ----------

def load():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return s
    if isinstance(saved.get("geometry"), dict) and "layout" not in saved["geometry"]:
        saved["geometry"]["layout"] = "radial"  # saved before the flat layout existed: keep its meaning
    for k in s:
        if isinstance(saved.get(k), dict):
            s[k].update({kk: vv for kk, vv in saved[k].items() if kk in s[k] or k == "cueing"})
    return s


def save(patch):
    s = load()
    for k in ("geometry", "servos", "car"):
        for kk, v in (patch.get(k) or {}).items():
            if kk in s[k]:
                s[k][kk] = v
    for kk, v in (patch.get("cueing") or {}).items():
        if kk in CUEING_DEFAULTS:
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
    sv = s["servos"]
    sv["center_us"] = [int(v) for v in sv["center_us"]][:6]
    sv["dir"] = [1 if int(v) >= 0 else -1 for v in sv["dir"]][:6]
    if len(sv["center_us"]) != 6 or len(sv["dir"]) != 6:
        raise ValueError("six servos")
    sv["us_per_deg"] = float(sv["us_per_deg"])
    sv["min_us"], sv["max_us"] = int(sv["min_us"]), int(sv["max_us"])
    if not 400 <= sv["min_us"] < sv["max_us"] <= 2600:
        raise ValueError("pulse range must be within 400-2600 µs")
    s["car"] = {k: float(v) for k, v in s["car"].items()}
    h0(g)  # raises if the rods can't reach
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)
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


def within(res, g):
    return all(r["alpha"] is not None and abs(r["alpha"]) <= g["servo_limit"] for r in res)


def reach(g):
    """How far the platform goes along each axis alone, both ways: {axis: [neg, pos]} (mm / deg)."""
    L, H = legs(g), h0(g)
    out = {}
    for ax in AXES:
        out[ax] = []
        for sign in (-1, 1):
            lo, hi = 0.0, 90.0
            for _ in range(24):
                m = (lo + hi) / 2
                if within(ik({ax: sign * m}, g, L, H), g):
                    lo = m
                else:
                    hi = m
            out[ax].append(round(lo, 1))
    return out


def design(g):
    """Numbers for the person designing the parts."""
    L, H = legs(g), h0(g)
    lean = math.degrees(math.acos(H / g["rod"]))  # horn tips sit at shaft height at home
    spacing = g["shaft_spacing"] if g.get("layout") == "flat" else 2 * g["base_r"] * math.sin(math.radians(g["base_half"]))
    p_spacing = 2 * g["plat_r"] * math.sin(math.radians(g["plat_half"]))
    return {
        "home_height": round(H, 1),
        "layout": g.get("layout", "radial"),
        # flat: the two bodies sit back to back between the shafts
        # bodies plus the screw tabs that stick out past them, and 1 mm of air
        "min_spacing": round(2 * (MG90["len"] - MG90["shaft"] + (MG90["tabs"] - MG90["len"]) / 2) + 1, 1) if g.get("layout") == "flat" else None,
        "rod_lean": round(lean, 1),
        "shaft_spacing": round(spacing, 1),
        "joint_spacing": round(p_spacing, 1),
        "reach": reach(g),
        "legs": [{"shaft": [round(c, 2) for c in B], "horn_dir": [round(c, 4) for c in u],
                  "joint": [round(c, 2) for c in P]} for B, u, P in L],
    }


def pulses(alphas, sv):
    """Horn angles (deg) -> pulse widths (µs), clamped; None angles hold the centre."""
    out = []
    for i, a in enumerate(alphas):
        us = sv["center_us"][i] + sv["dir"][i] * (a or 0.0) * sv["us_per_deg"]
        out.append(int(round(min(sv["max_us"], max(sv["min_us"], us)))))
    return out


def solve(pose, s=None):
    """What the tab draws: legs, angles, pulses, and whether the pose is allowed."""
    s = s or load()
    g = s["geometry"]
    res = ik(pose, g)
    alphas = [r["alpha"] for r in res]
    return {
        "pose": {ax: float(pose.get(ax, 0)) for ax in AXES},
        "ok": within(res, g),
        "alpha": [None if a is None else round(a, 2) for a in alphas],
        "pulse": pulses(alphas, s["servos"]),
        "legs": [{k: [round(c, 2) for c in r[k]] for k in ("B", "A", "P")} for r in res],
        "home_height": round(h0(g), 2),
    }


# ---------- 1:1 drawings for the 3D printed parts ----------

# MG90-class body, approximate (measure yours): length along the swing, shaft centre from the outer end,
# horn plane to top face, top face to bottom, tab length, tabs below the top face
MG90 = {"len": 22.8, "wid": 12.2, "shaft": 5.9, "top": 4.5, "depth": 22.7, "tabs": 32.5,
        "tab_depth": 6.0}


def template_svg(part, g):
    """A true-scale SVG (mm) of the base or the platform: holes, numbers, directions. Import it into
    the CAD program as a sketch, or print it at 100 % to check against the servos."""
    L = legs(g)
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
            nx, ny = X(B[0] * 0.78), Y(B[1] * 0.78)
            e.append(f'<text x="{nx:.2f}" y="{ny+1.6:.2f}" font-size="4.5" text-anchor="middle" stroke="none" fill="#000">{k}</text>')
        pair = (f"pairs back to back on flat faces {g['base_r']:g} mm from the centre, shafts {g['shaft_spacing']:g} mm apart"
                if g.get("layout") == "flat" else f"servo shafts on r={g['base_r']:g} mm, {2*g['base_half']:g}° apart in a pair")
        note = (f"BASE 1:1  {pair}; "
                f"red = horn {g['horn']:g} mm at 0° (horizontal)|dashed = MG90 body, strip = its screw tabs "
                f"(approx., measure yours)" + ("" if g.get("layout") == "flat" else "; circle = horn plane"))
    else:
        e.append(f'<circle cx="{c}" cy="{c}" r="{g["plat_r"]:.2f}" stroke-dasharray="2 2"/>')
        for k, (B, u, P) in enumerate(L):
            px, py = X(P[0]), Y(P[1])
            e.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="1.5"/>'
                     f'<text x="{X(P[0]*0.8):.2f}" y="{Y(P[1]*0.8)+1.6:.2f}" font-size="4.5" text-anchor="middle" stroke="none" fill="#000">{k}</text>')
        note = (f"PLATFORM 1:1  ball joints on r={g['plat_r']:g} mm, {2*g['plat_half']:g}° apart in a pair; "
                f"joint k takes the rod from servo k")
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


class Link:
    """Pulses to the pyboard. While armed, the last pulses are re-sent every 0.3 s because the
    firmware lets go of the servos after 1 s of silence."""

    KEEP = 0.3

    def __init__(self):
        self.lock = threading.Lock()
        self.fd = None
        self.port = None
        self.armed = False
        self.last = None
        self.sent_at = 0.0
        self.firmware = None
        self.error = None
        self.thread = None

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

    def _write(self, data):
        end = time.monotonic() + 0.2
        while data:
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                if time.monotonic() > end:
                    raise OSError("the pyboard is not reading")
                select.select([], [self.fd], [], 0.02)

    def _close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None

    def _send_locked(self, line):
        try:
            if self.fd is None:
                self._open()
            self._write(line)
            self.sent_at = time.monotonic()
            self.error = None
        except OSError as e:
            self.error = str(e)
            self._close()

    def arm(self, on):
        with self.lock:
            self.armed = bool(on)
            if not on:
                if self.fd is not None:
                    self._send_locked(b"O\n")
                self._close()
                self.error = None
        if on and not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._keepalive, name="motion-link", daemon=True)
            self.thread.start()

    def send(self, us):
        with self.lock:
            self.last = list(us)
            if self.armed:
                self._send_locked(("S " + " ".join(str(int(v)) for v in us) + "\n").encode())

    def _keepalive(self):
        while self.armed:
            time.sleep(0.1)
            with self.lock:
                if self.armed and self.last and time.monotonic() - self.sent_at > self.KEEP:
                    self._send_locked(("S " + " ".join(str(int(v)) for v in self.last) + "\n").encode())

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
    return {"settings": s, "design": d, "link": link.status(), "defaults": DEFAULTS,
            "cueing": cueing_settings(), "cueing_defaults": CUEING_DEFAULTS, "replay": player.info()}


# ---------- motion cueing: telemetry -> pose ----------

G = 9.81
CUEING_DEFAULTS = {
    "pitch_per_g": 12.0,   # deg of nose-down per g of braking (and nose-up when accelerating)
    "roll_per_g": 10.0,    # deg of lean to the outside per g of cornering
    "tilt_ms": 150.0,      # smoothing of the tilts (low-pass time constant)
    "surge_per_g": 15.0,   # mm of quick shift per g of change in acceleration (onset, then back to centre)
    "sway_per_g": 12.0,
    "heave_per_g": 10.0,   # mm per g of vertical acceleration (bumps, crests, compressions)
    "yaw_per_rad": 12.0,   # deg per rad/s of yaw rate (a flick on turn-in)
    "washout_ms": 800.0,   # how fast the shifts and the yaw flick drift back to centre (high-pass)
    "smooth_ms": 40.0,     # final smoothing of everything (servo jitter)
    "master": 1.0,         # overall scale
}


def cueing_settings():
    return {**CUEING_DEFAULTS, **{k: float(v) for k, v in (load().get("cueing") or {}).items() if k in CUEING_DEFAULTS}}


class Cueing:
    """Classic washout, simplified for a desk toy: sustained acceleration becomes tilt (low-passed),
    changes in acceleration and yaw rate become short moves that drift back (high-passed)."""

    def __init__(self, c):
        self.c = c
        self.reset()

    def reset(self):
        self.prev = None
        self.hp = {k: 0.0 for k in ("x", "y", "z", "yaw")}
        self.lp = {k: 0.0 for k in ("pitch", "roll")}
        self.out = {ax: 0.0 for ax in AXES}

    def step(self, s, dt):
        """s: {fwd, left, up (m/s²), yaw_rate (rad/s)} in the car's frame; dt in seconds (sim time)."""
        c = self.c
        s = {**s, **{k: max(-3 * G, min(3 * G, s[k])) for k in ("fwd", "left", "up")}}  # kerb / crash spikes
        x = {"x": s["fwd"] / G * c["surge_per_g"], "y": s["left"] / G * c["sway_per_g"],
             "z": s["up"] / G * c["heave_per_g"], "yaw": s["yaw_rate"] * c["yaw_per_rad"]}
        if self.prev is None or dt <= 0:
            self.prev = x
            return dict(self.out)
        a = (c["washout_ms"] / 1000) / (c["washout_ms"] / 1000 + dt)
        for k in self.hp:
            self.hp[k] = a * (self.hp[k] + x[k] - self.prev[k])
        self.prev = x
        b = dt / (c["tilt_ms"] / 1000 + dt)
        # braking (fwd < 0) -> nose down (pitch < 0); acceleration to the left (left turn) -> lean right (roll > 0)
        self.lp["pitch"] += b * (s["fwd"] / G * c["pitch_per_g"] - self.lp["pitch"])
        self.lp["roll"] += b * (s["left"] / G * c["roll_per_g"] - self.lp["roll"])
        target = {"x": self.hp["x"], "y": self.hp["y"], "z": self.hp["z"], "yaw": self.hp["yaw"],
                  "pitch": self.lp["pitch"], "roll": self.lp["roll"]}
        m = dt / (c["smooth_ms"] / 1000 + dt)
        for k in AXES:
            self.out[k] += m * (c["master"] * target[k] - self.out[k])
        return dict(self.out)


def fit_pose(pose, g, L=None, H=None):
    """The pose, scaled down as a whole until every servo can reach it (keeps its shape)."""
    L, H = L or legs(g), h0(g) if H is None else H
    if within(ik(pose, g, L, H), g):
        return pose, 1.0
    lo, hi = 0.0, 1.0
    for _ in range(10):
        m = (lo + hi) / 2
        if within(ik({k: v * m for k, v in pose.items()}, g, L, H), g):
            lo = m
        else:
            hi = m
    return {k: v * lo for k, v in pose.items()}, lo


# ---------- replay a recorded session on the platform ----------

GAP = 2.0  # s without ticks (pause, menus): skipped


def read_course(ref):
    """The recorded inputs of a session, in the car's frame (pCARS local: X left, Y up, -Z forward)."""
    day, sid = store.parse_ref(ref)
    con = store.read_day(day)
    with con:
        s = con.execute("SELECT car, track, variation FROM v_sessions WHERE id=?", (sid,)).fetchone()
        if s is None:
            raise LookupError("no such session")
        rows = con.execute(
            "SELECT t.ts, e.lap, e.speed, e.local_acc_x, e.local_acc_y, e.local_acc_z, e.angular_vel_y, e.x, e.z "
            "FROM tick t JOIN telemetry e ON e.tick_id = t.id WHERE t.session_id=? ORDER BY t.ts", (sid,)).fetchall()
    con.close()
    if not rows:
        raise LookupError("this session has no recorded telemetry")
    d = {"car": s["car"], "track": s["track"], "variation": s["variation"],
         "ts": [], "lap": [], "kmh": [], "fwd": [], "left": [], "up": [], "yaw_rate": [], "x": [], "z": []}
    for ts, lap, sp, ax, ay, az, wy, x, z in rows:
        d["x"].append(x or 0.0); d["z"].append(z or 0.0)
        d["ts"].append(ts); d["lap"].append(lap or 0); d["kmh"].append(round((sp or 0) * 3.6))
        d["left"].append(ax or 0.0); d["up"].append(ay or 0.0); d["fwd"].append(-(az or 0.0)); d["yaw_rate"].append(wy or 0.0)
    laps, last = [], None
    for i, lap in enumerate(d["lap"]):
        if lap != last:
            laps.append({"lap": lap, "ts": d["ts"][i]})
            last = lap
    d["laps"] = laps
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
        s = {k: d[k][i] + (d[k][j] - d[k][i]) * u for k in ("fwd", "left", "up", "yaw_rate", "x", "z")}
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
        self.inputs = None
        self.thread = None

    def load(self, ref):
        c = Course(ref)
        with self.lock:
            self.course, self.t, self.playing = c, c.t0, False
            self.cue = Cueing(cueing_settings())
        self._step(0.0)
        return self.info()

    def info(self):
        c = self.course
        if not c:
            return {}
        return {"ref": c.ref, "car": c.d["car"], "track": c.d["track"], "variation": c.d["variation"],
                "t0": c.t0, "t1": c.t1, "laps": c.d["laps"], "path": c.d["path"]}

    def play(self, speed=1.0):
        with self.lock:
            if not self.course:
                raise LookupError("no session loaded")
            self.speed = max(0.25, min(4.0, float(speed)))
            if self.t >= self.course.t1:
                self.t = self.course.t0
                self.cue.reset()
            self.cue.c = cueing_settings()
            self.playing = True
        if not (self.thread and self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, name="motion-replay", daemon=True)
            self.thread.start()

    def pause(self):
        with self.lock:
            self.playing = False

    def seek(self, t):
        with self.lock:
            if not self.course:
                raise LookupError("no session loaded")
            self.t = max(self.course.t0, min(self.course.t1, float(t)))
            self.cue.reset()
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

    def _step(self, dt):
        with self.lock:
            c = self.course
            if not c:
                return
            s = c.sample(self.t)
            pose = self.cue.step(s, dt)
        st = load()
        fitted, self.scale = fit_pose(pose, st["geometry"])
        r = solve(fitted, st)
        self.last, self.inputs = r, s
        if r["ok"]:
            link.send(r["pulse"])

    def _run(self):
        period = 1 / self.RATE
        nxt = time.monotonic()
        while True:
            with self.lock:
                if not self.playing or not self.course:
                    break
                dt = period * self.speed
                self.t = self.course.skip_gap(self.t + dt)
                if self.t >= self.course.t1:
                    self.t, self.playing = self.course.t1, False
            self._step(dt)
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))

    def state(self):
        s = self.inputs or {}
        return {"loaded": self.course is not None, "playing": self.playing, "t": self.t, "speed": self.speed,
                "lap": s.get("lap"), "kmh": s.get("kmh"),
                "pos": [round(s["x"], 1), round(s["z"], 1)] if s else None,
                "g": {k: round(s[k] / G, 2) for k in ("fwd", "left", "up")} if s else None,
                "scale": round(self.scale, 2), "solve": self.last, "link": link.status()}


player = Player()
