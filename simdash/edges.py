"""Track edges: where the drivable surface (track or kerb) ends, on each side of the track.

AMS2 gives no track boundaries, only the terrain under each tyre. In mapping mode the driver
runs along each edge with the outside wheels over it: every time a tyre goes between
track/kerb and off-track (grass, gravel, runoff...), that tyre is on the edge. Also, while
one tyre of an axle is off and the other is on, the edge lies between them (a looser guess,
weighted less).

Samples are kept raw (tracks/<key>.edges.json) so the edges can be rebuilt as the method
improves; edge points are binned by lap distance into a left and a right line. Edges can
also be harvested from recorded sessions: every off-track moment there is edge data too.

World frame, checked on recorded laps (2026-11-07): forward = (-sin yaw, -cos yaw) in (x, z)
and, with the left wheels off track, the car sits on the side (-fz, fx) of the racing line,
so left = (-fz, fx). Wheels are FL, FR, RL, RR.
"""
import json
import logging
import math
import subprocess
import threading
import time

from . import store
from .pcars2 import SURFACE, TERRAINS

log = logging.getLogger("simdash.edges")

HALF_TRACK = 0.8      # m, centre to tyre (cars differ; ~1.6 m track)
HALF_BASE = 1.3       # m, centre to axle
BIN = 3.0             # m of lap distance per edge point
MIN_SPEED = 3.0       # m/s
MAX_SLIP = 25.0       # degrees between heading and travel: beyond, the car is sliding, not mapping
MAX_FROM_LINE = 30.0  # m: edge points further from the racing line are dropped (pit lane, shortcuts)
STRADDLE_W = 0.3      # weight of a "between the two tyres" point; a crossing weighs 1
GAP_FILL = 5          # bins: shorter holes are interpolated on the drawn line
SIDES = ("left", "right")
OFF = 2
KERB = 1

# What lies beyond the edge, for the natural colours view (same grouping as the session page's surface map)
FAMILIES = [("wall", ("wall", "guardrail")), ("runoff", ("runoff", "illegal", "paint concrete")),
            ("grass", ("grass", "astroturf", "verge")),
            ("gravel", ("gravel", "sand", "dirt", "clay", "snow", "ice"))]


def family(code):
    name = TERRAINS[code] if 0 <= code < len(TERRAINS) else ""
    for fam, words in FAMILIES:
        if any(w in name for w in words):
            return fam
    return "other"


def _path(track, variation):
    return store.TRACKS_DIR / (store.track_key(track, variation) + ".edges.json")


def load(track, variation):
    try:
        return json.loads(_path(track, variation).read_text())
    except (OSError, ValueError):
        return None


def _save(data):
    store.TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(data["track"], data["variation"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(p)


def _empty(track, variation, length):
    return {"track": track, "variation": variation, "length": length, "runs": [], "sources": []}


# ---- edge points from samples ----
# A sample: [d, x, z, yaw, "ssss", y, [t, t, t, t]]: lap distance, position, heading, surface class
# of FL FR RL RR (0 track, 1 kerb, 2 off), then the car's altitude (centre of mass, ~0.3-0.5 m above
# the ground) and the raw AMS2 terrain codes. Samples from before 2026-11-07 22:30 stop at "ssss".
# A run: consecutive samples at the recorder's rate (crossings are found between neighbours).

def _wheels(x, z, yaw):
    fx, fz = -math.sin(yaw), -math.cos(yaw)
    lx, lz = -fz, fx
    out = []
    for along, side in ((HALF_BASE, 1), (HALF_BASE, -1), (-HALF_BASE, 1), (-HALF_BASE, -1)):
        out.append((x + fx * along + lx * HALF_TRACK * side, z + fz * along + lz * HALF_TRACK * side))
    return out


def _points(runs):
    """edge points [(side, d, x, z, weight, y or None)] and tyre surface observations
    [(d, x, z, class, terrain code or None)] for kerb / off-track tyres"""
    pts, obs = [], []
    for run in runs:
        prev = None
        for smp in run:
            d, x, z, yaw, surf = smp[:5]
            y = smp[5] if len(smp) > 5 else None
            codes = smp[6] if len(smp) > 6 else [None] * 4
            off = [s == str(OFF) for s in surf]
            wh = _wheels(x, z, yaw)
            for w in range(4):
                if surf[w] != "0":
                    obs.append((d + (HALF_BASE if w < 2 else -HALF_BASE), wh[w][0], wh[w][1], int(surf[w]), codes[w]))
            if prev is not None:
                pd, px, pz, pyaw, psurf = prev[:5]
                py = prev[5] if len(prev) > 5 else None
                pw = _wheels(px, pz, pyaw)
                for w in range(4):
                    if (psurf[w] == str(OFF)) != off[w]:
                        along = HALF_BASE if w < 2 else -HALF_BASE
                        pts.append((SIDES[w % 2], (d + pd) / 2 + along,
                                    (wh[w][0] + pw[w][0]) / 2, (wh[w][1] + pw[w][1]) / 2, 1.0,
                                    (y + py) / 2 if y is not None and py is not None else None))
            for a, b, along in ((0, 1, HALF_BASE), (2, 3, -HALF_BASE)):
                if off[a] != off[b]:
                    pts.append((SIDES[0] if off[a] else SIDES[1], d + along,
                                (wh[a][0] + wh[b][0]) / 2, (wh[a][1] + wh[b][1]) / 2, STRADDLE_W, y))
            prev = smp
    return pts, obs


class _Line:
    """the racing line with a grid index: signed distance (left > 0) of a point to it"""
    CELL = 32.0

    def __init__(self, points):
        self.p = [tuple(q) for q in points]
        self.grid = {}
        for i, (x, z) in enumerate(self.p):
            self.grid.setdefault((int(x // self.CELL), int(z // self.CELL)), []).append(i)

    def offset(self, x, z):
        cx, cz = int(x // self.CELL), int(z // self.CELL)
        best, bi = None, None
        for gx in (cx - 1, cx, cx + 1):
            for gz in (cz - 1, cz, cz + 1):
                for i in self.grid.get((gx, gz), ()):
                    dd = (self.p[i][0] - x) ** 2 + (self.p[i][1] - z) ** 2
                    if best is None or dd < best:
                        best, bi = dd, i
        if bi is None:
            return None
        line = self.p
        a, b = line[max(bi - 1, 0)], line[min(bi + 1, len(line) - 1)]
        fx, fz = b[0] - a[0], b[1] - a[1]
        n = math.hypot(fx, fz) or 1
        return (x - line[bi][0]) * (-fz / n) + (z - line[bi][1]) * (fx / n)


def build(data):
    """Left and right edge lines, coverage and gaps from the stored samples.

    Each line point: [x, z, d, y, kerb, beyond]: position, lap distance, altitude (None for old
    samples), 1 where tyres ran on a kerb by that edge, and what lies beyond the edge
    (grass, gravel, runoff, wall, other; None when unknown)."""
    length = data.get("length") or 0
    if length <= 0:
        return {"left": [], "right": [], "coverage": {"left": 0, "right": 0}, "gaps": {"left": [], "right": []}}
    nb = max(int(length // BIN), 1)
    outline = store.load_track(data["track"], data["variation"])
    line = _Line(outline["points"]) if outline else None
    acc = {s: [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(nb)] for s in SIDES}  # x, z, weight, y, y weight
    kerb = {s: [0] * nb for s in SIDES}
    beyond = {s: [{} for _ in range(nb)] for s in SIDES}
    kept = dropped = 0
    pts, obs = _points(data["runs"])
    for side, d, x, z, w, y in pts:
        if line:
            off = line.offset(x, z)
            if off is None or abs(off) > MAX_FROM_LINE or (off > 0) != (side == "left") and abs(off) > 2:
                dropped += 1
                continue
        a = acc[side][int((d % length) // BIN) % nb]
        a[0] += x * w
        a[1] += z * w
        a[2] += w
        if y is not None:
            a[3] += y * w
            a[4] += w
        kept += 1
    if line:
        for d, x, z, cls, code in obs:
            off = line.offset(x, z)
            if off is None or abs(off) > MAX_FROM_LINE + 10:
                continue
            side, i = ("left" if off > 0 else "right"), int((d % length) // BIN) % nb
            if cls == KERB:
                kerb[side][i] += 1
            elif code is not None:
                f = family(code)
                beyond[side][i][f] = beyond[side][i].get(f, 0) + 1
    out = {"bin": BIN, "kept": kept, "dropped": dropped, "coverage": {}, "gaps": {}}
    ys = []
    for side in SIDES:
        cells = [(c[0] / c[2], c[1] / c[2], c[3] / c[4] if c[4] else None) if c[2] else None for c in acc[side]]
        # light smoothing with the neighbours that have data
        sm = []
        for i, c in enumerate(cells):
            if c is None:
                sm.append(None)
                continue
            near = [cells[j % nb] for j in (i - 1, i, i + 1) if cells[j % nb] is not None]
            ny = [p[2] for p in near if p[2] is not None]
            fam = beyond[side][i]
            sm.append((sum(p[0] for p in near) / len(near), sum(p[1] for p in near) / len(near),
                       sum(ny) / len(ny) if ny else None,
                       int(any(kerb[side][j % nb] for j in (i - 1, i, i + 1))),
                       max(fam, key=fam.get) if fam else None))
            if ny:
                ys.append(sm[-1][2])
        have = sum(c is not None for c in sm)
        out["coverage"][side] = round(have / nb, 3)
        out["gaps"][side] = _gaps(sm, length)
        out[side] = _segments(sm)
    out["altitude"] = [round(min(ys), 1), round(max(ys), 1)] if ys else None
    return out


def _gaps(cells, length):
    """missing stretches longer than GAP_FILL bins, as [from_m, to_m]"""
    nb, gaps, i = len(cells), [], 0
    if all(c is None for c in cells):
        return [[0, round(length)]]
    while i < nb:
        if cells[i] is None:
            j = i
            while j < nb and cells[j] is None:
                j += 1
            if j - i > GAP_FILL:
                gaps.append([round(i * BIN), round(min(j * BIN, length))])
            i = j
        else:
            i += 1
    return gaps


def _segments(cells):
    """drawable pieces [[[x, z, d, y, kerb, beyond], ...], ...]: short holes bridged, long ones break the line."""
    segs, cur, hole = [], [], 0
    for i, c in enumerate(cells):
        if c is None:
            hole += 1
            continue
        if cur and hole > GAP_FILL:
            segs.append(cur)
            cur = []
        cur.append([round(c[0], 2), round(c[1], 2), round(i * BIN),
                    None if c[2] is None else round(c[2], 2), c[3], c[4]])
        hole = 0
    if cur:
        segs.append(cur)
    # join the last piece to the first across the start line
    if len(segs) > 1 and cells[0] is not None and cells[-1] is not None:
        segs[0] = segs.pop() + segs[0]
    return [s for s in segs if len(s) > 1]


# ---- API helpers ----

_cache = {}


def summary(track, variation, with_lines=True):
    data = load(track, variation)
    if not data:
        return None
    key = (track, variation, data.get("updated"))
    b = _cache.get(key)
    if b is None:
        b = build(data)
        _cache.clear()
        _cache[key] = b
    out = {"track": track, "variation": variation, "length": data["length"], "updated": data.get("updated"),
           "samples": sum(len(r) for r in data["runs"]), "sources": data.get("sources", []),
           **{k: v for k, v in b.items() if with_lines or k not in SIDES}}
    return out


def all_tracks():
    out = []
    for p in sorted(store.TRACKS_DIR.glob("*.json")):
        if p.name.endswith((".corners.json", ".edges.json")):
            continue
        try:
            t = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        s = summary(t["track"], t["variation"], with_lines=False) or {}
        out.append({"track": t["track"], "variation": t["variation"], "length": t.get("length"),
                    "coverage": s.get("coverage"), "samples": s.get("samples", 0), "updated": s.get("updated")})
    return out


def clear(track, variation, source=None):
    """forget everything, or only what one source (\"mapping\" or a session ref) added"""
    data = load(track, variation)
    if not data:
        return
    if source is None:
        _path(track, variation).unlink(missing_ok=True)
        return
    data["runs"] = [r for r, src in zip(data["runs"], data.get("run_src", [])) if src != source]
    data["run_src"] = [src for src in data.get("run_src", []) if src != source]
    data["sources"] = [s for s in data.get("sources", []) if s != source]
    data["updated"] = time.time()
    _save(data)


def _add_runs(track, variation, length, runs, source):
    data = load(track, variation) or _empty(track, variation, length)
    data["length"] = length or data["length"]
    data.setdefault("run_src", [None] * len(data["runs"]))
    if source != "mapping" and source in data["sources"]:  # a session imported twice
        return data
    data["runs"] += runs
    data["run_src"] += [source] * len(runs)
    if source not in data["sources"]:
        data["sources"].append(source)
    data["updated"] = time.time()
    _save(data)
    return data


def _interesting(run):
    """keep the parts of a run near a kerb or off-track tyre (a crossing needs its neighbours)"""
    keep = sorted({j for i, smp in enumerate(run) if smp[4] != "0000"
                   for j in (i - 1, i, i + 1) if 0 <= j < len(run)})
    out, cur, last = [], [], None
    for j in keep:
        if cur and j != last + 1:
            out.append(cur)
            cur = []
        cur.append(run[j])
        last = j
    if cur:
        out.append(cur)
    return [r for r in out if len(r) > 1]


def _usable(speed, slip_deg, pit_mode):
    return speed >= MIN_SPEED and abs(slip_deg) <= MAX_SLIP and not pit_mode


# ---- live mapping ----

STATE_FILE = store.DATA_DIR / "mapping.json"
OFF_AFTER = 30  # s without AMS2's shared memory before mapping switches itself off


def say(text):
    """Spoken update through speech-dispatcher, so the driver can stay in AMS2."""
    try:
        subprocess.Popen(["spd-say", "-l", "en", "-r", "5", text],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.warning("spd-say not available: %s", text)


def _where(track, variation, d):
    names = store.load_corner_names(track, variation)
    near = min(names, key=lambda c: abs(c["apex"] - d), default=None)
    if near and abs(near["apex"] - d) < 300:
        return ("before " if d < near["apex"] - 40 else "after " if d > near["apex"] + 40 else "at ") + near["name"]
    return f"at {d / 1000:.1f} kilometres" if d >= 1000 else f"at {round(d, -1):.0f} metres"


def spoken_summary(track, variation):
    s = summary(track, variation, with_lines=False)
    if not s:
        return "No edges yet."
    parts = []
    for side in SIDES:
        pct = round(s["coverage"][side] * 100)
        gaps = s["gaps"][side]
        if not gaps:
            parts.append(f"{side} edge complete.")
        else:
            big = max(gaps, key=lambda g: g[1] - g[0])
            parts.append(f"{side} {pct} percent, missing {_where(track, variation, gaps[0][0])}"
                         + (f", biggest hole {_where(track, variation, big[0])}." if big is not gaps[0] else "."))
    return " ".join(parts)


class Mapper:
    """Collects samples while armed; the recorder feeds it every recorded tick.

    Armed from the Track edges tab or before AMS2 starts (start-race --map); the armed state is
    kept in mapping.json so a simdash restart doesn't lose it, and it switches itself off once
    AMS2 has gone away. Spoken updates (lap by lap) let the driver map without leaving the game."""

    def __init__(self):
        self.lock = threading.Lock()
        self.armed = False
        self.speak = True
        self.key = None           # (track, variation, length)
        self.run = []             # current run of samples
        self.pending = []         # finished runs not saved yet
        self.last_save = 0.0
        self.last = None          # last sample, for the page
        self.count = 0
        self.crossings = 0
        self.last_feed = 0.0      # when AMS2 last gave a sample while armed
        self.announced = None     # track the "mapping on" message was spoken for
        try:
            st = json.loads(STATE_FILE.read_text())
            self.armed, self.speak = bool(st.get("armed")), bool(st.get("speak", True))
        except (OSError, ValueError):
            pass

    def _save_state(self):
        try:
            STATE_FILE.write_text(json.dumps({"armed": self.armed, "speak": self.speak}))
        except OSError:
            log.exception("saving mapping state")

    def state(self):
        with self.lock:
            return {"armed": self.armed, "speak": self.speak, "track": self.key[0] if self.key else None,
                    "variation": self.key[1] if self.key else None, "samples": self.count,
                    "crossings": self.crossings, "wheels": self.last[4] if self.last else None}

    def arm(self, on, speak=None):
        with self.lock:
            was = self.armed
            self.armed = on
            if speak is not None:
                self.speak = speak
            if on and not was:
                self.count = self.crossings = 0
                self.last = None
                self.last_feed = 0.0
                self.announced = None
            self._save_state()
        if was and not on:
            self.flush(force=True)
            if self.speak and self.key:
                say("Edge mapping off.")
        log.info("track edge mapping %s", "on" if on else "off")

    def no_game(self, now):
        """The recorder calls this while AMS2's shared memory is missing."""
        if self.armed and self.last_feed and now - self.last_feed > OFF_AFTER:
            log.info("AMS2 gone: track edge mapping off")
            self.arm(False)

    def _announce(self, track, variation, first):
        try:
            self.flush()
            text = spoken_summary(track, variation)
            say((f"Edge mapping on. {track}. " if first else "Lap done. ") + text)
        except Exception:
            log.exception("edge mapping announcement")

    def feed(self, snap, player, slip_deg, now):
        if not self.armed:
            return
        key = (snap["track"], snap["variation"], snap["track_length"])
        surf = "".join(str(SURFACE[t] if 0 <= t < len(SURFACE) else 0) for t in snap["terrain"])
        smp = [round(player["lap_dist"], 1), round(player["x"], 2), round(player["z"], 2),
               round(snap["orientation"][1], 4), surf, round(player["raw"]["mWorldPosition"][1], 2),
               [int(t) for t in snap["terrain"]]]
        ok = _usable(snap["speed"], slip_deg, snap["pit_mode"])
        speak = None
        with self.lock:
            self.last_feed = now
            if key[:2] != (self.key or (None, None))[:2]:
                self._end_run()
                self._flush_locked()
                self.key = key
            if self.announced != key[:2]:
                self.announced = key[:2]
                speak = True
            elif self.last and key[2] > 0 and self.last[0] > 0.8 * key[2] and smp[0] < 0.2 * key[2]:
                speak = False  # crossed the start line
            if not ok:
                self._end_run()
            else:
                if self.last and self.run and any((a == str(OFF)) != (b == str(OFF)) for a, b in zip(self.last[4], surf)):
                    self.crossings += 1
                self.run.append(smp)
                self.count += 1
            self.last = smp
        if speak is not None and self.speak:
            threading.Thread(target=self._announce, args=(key[0], key[1], speak), daemon=True).start()
        if now - self.last_save > 5:
            self.flush()

    def _end_run(self):
        if len(self.run) > 1:
            self.pending += _interesting(self.run)
        self.run = []

    def _flush_locked(self):
        if self.pending and self.key:
            _add_runs(self.key[0], self.key[1], self.key[2], self.pending, "mapping")
        self.pending = []

    def flush(self, force=False):
        with self.lock:
            self.last_save = time.time()
            if force:
                self._end_run()
            else:
                # save the finished part of the current run, keep its tail for the next crossing
                if len(self.run) > 2:
                    tail = self.run[-1:]
                    self.run = self.run[:-1]
                    self._end_run()
                    self.run = tail
            try:
                self._flush_locked()
            except OSError:
                log.exception("saving track edges")


# ---- from recorded sessions ----

def harvest(track, variation):
    """Add the off-track moments of every recorded session on this track layout. Returns sessions added."""
    added = 0
    for day in store.list_days():
        try:
            con = store.read_day_summary(day)
            sids = [r["id"] for r in con.execute(
                "SELECT id FROM v_sessions WHERE track=? AND variation=?", (track, variation))]
            con.close()
        except Exception:
            continue
        for sid in sids:
            ref = f"{day}:{sid}"
            existing = load(track, variation)
            if existing and ref in existing.get("sources", []):
                runs = [r for r, src in zip(existing["runs"], existing.get("run_src", [])) if src == ref]
                if all(len(smp) > 6 for r in runs for smp in r):
                    continue
                clear(track, variation, ref)  # imported before altitude and terrain codes were kept
            try:
                runs, length = _session_runs(day, sid)
            except Exception:
                log.exception("edges from %s", ref)
                continue
            _add_runs(track, variation, length, runs, ref)
            added += 1
    return added


def _session_runs(day, sid):
    con = store.read_day(day)
    try:
        t = con.execute("SELECT track, variation FROM v_sessions WHERE id=?", (sid,)).fetchone()
        outline = store.load_track(t["track"], t["variation"])
        length = outline["length"] if outline else None
        pits = con.execute("SELECT ps.tick_id, ps.pit_mode FROM player_state ps JOIN tick t ON t.id = ps.tick_id "
                           "WHERE t.session_id=? ORDER BY ps.tick_id", (sid,)).fetchall()
        rows = con.execute(
            """SELECT t.id, t.ts, te.lap_dist, te.x, te.y, te.z, te.orientation_y, te.local_vel_x, te.local_vel_z,
                      (SELECT group_concat(terrain, ',') FROM (SELECT terrain FROM wheel w
                        WHERE w.tick_id = t.id ORDER BY w.wheel)) AS ter
               FROM tick t JOIN telemetry te ON te.tick_id = t.id
               WHERE t.session_id=? ORDER BY t.id""", (sid,)).fetchall()
    finally:
        con.close()
    if not length:
        length = max((r["lap_dist"] for r in rows), default=0)
    runs, run, prev_ts, pi, pit = [], [], None, 0, 0
    for r in rows:
        while pi < len(pits) and pits[pi][0] <= r["id"]:
            pit = pits[pi][1] or 0
            pi += 1
        ter = (r["ter"] or "").split(",")
        fwd = -(r["local_vel_z"] or 0)
        speed = math.hypot(r["local_vel_x"] or 0, fwd)
        slip = math.degrees(math.atan2(r["local_vel_x"] or 0, fwd)) if fwd > 0.1 else 90
        if len(ter) != 4 or not _usable(speed, slip, pit) or (prev_ts and r["ts"] - prev_ts > 0.2):
            if len(run) > 1:
                runs += _interesting(run)
            run = []
        if len(ter) == 4 and _usable(speed, slip, pit):
            surf = "".join(str(SURFACE[int(t)] if 0 <= int(t) < len(SURFACE) else 0) for t in ter)
            run.append([round(r["lap_dist"], 1), round(r["x"], 2), round(r["z"], 2), round(r["orientation_y"], 4), surf,
                        round(r["y"], 2), [int(t) for t in ter]])
        prev_ts = r["ts"]
    if len(run) > 1:
        runs += _interesting(run)
    return runs, length


# ---- altitude profile ----

_profiles = {}


def profile(track, variation):
    """Altitude along the lap, from the fastest recorded lap on this layout (valid first, any car):
    {"d": [m], "y": [m], "ref", "lap", "lap_time", "car", "valid"} every BIN m, or None.
    y is the car's centre of mass, ~0.3-0.5 m above the ground."""
    best = None
    for day in store.list_days():
        try:
            con = store.read_day_summary(day)
            row = con.execute(
                """SELECT l.session_id, l.lap, l.lap_time, l.valid, s.car FROM lap l JOIN v_sessions s ON s.id = l.session_id
                   WHERE s.track=? AND s.variation=? AND l.lap_time > 0
                   ORDER BY l.valid DESC, l.lap_time LIMIT 1""", (track, variation)).fetchone()
            con.close()
        except Exception:
            continue
        if row and (best is None or (row["valid"], -row["lap_time"]) > (best[1]["valid"], -best[1]["lap_time"])):
            best = (day, dict(row))
    if best is None:
        return None
    day, lap = best
    key = (track, variation, day, lap["session_id"], lap["lap"])
    if key in _profiles:
        return _profiles[key]
    con = store.read_day(day)
    try:
        rows = con.execute("""SELECT e.lap_dist, e.y FROM telemetry e JOIN tick t ON t.id = e.tick_id
                              WHERE t.session_id=? AND e.lap=? ORDER BY t.ts""", (lap["session_id"], lap["lap"])).fetchall()
    finally:
        con.close()
    bins = {}
    for d, y in rows:
        if d is None or y is None or d < 0:
            continue
        b = bins.setdefault(int(d // BIN), [0.0, 0])
        b[0] += y
        b[1] += 1
    if not bins:
        return None
    ks = sorted(bins)
    out = {"d": [round(k * BIN + BIN / 2, 1) for k in ks], "y": [round(bins[k][0] / bins[k][1], 2) for k in ks],
           "ref": f"{day}:{lap['session_id']}", "lap": lap["lap"], "lap_time": lap["lap_time"],
           "car": lap["car"], "valid": bool(lap["valid"])}
    _profiles.clear()
    _profiles[key] = out
    return out
