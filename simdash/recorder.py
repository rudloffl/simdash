"""Background recorder: polls $pcars2$, keeps the live state for the web UI
and writes every driving session to the day's SQLite database, with every
field of the shared memory (see store.py for the layout).

A new session starts when track, car or session type changes, when AMS2
restarts the session, or after returning from the main menu.
"""

import logging
import math
import sqlite3
import threading
import time
from collections import defaultdict, deque

from . import store
from .pcars2 import (GAME_MENU_TICKING, GAME_PAUSED, GAME_PLAYING, GAME_STATES, PIT_IN_GARAGE,
                     PIT_MODES, RACE_STATES, SESSION_TYPES, Pcars2Reader, _name, derived)

log = logging.getLogger("simdash.recorder")

HZ = 20
CARS_EVERY = 0.5        # s between car_motion rows (all cars)
FLUSH_EVERY = 1.0       # s between DB commits
MIN_DRIVING_S = 15      # sessions with less driving and no laps are dropped
TRACE_SECONDS = 60


def _getter(field, idx):
    if field.startswith("car."):
        f = field[4:]
        return (lambda raw, car: car[f]) if idx is None else (lambda raw, car: car[f][idx])
    return (lambda raw, car: raw[field]) if idx is None else (lambda raw, car: raw[field][idx])


def row_maker(spec):
    """(raw, car) -> tuple of the spec's column values."""
    getters = [_getter(field, idx) for _, field, idx in spec]
    return lambda raw, car: tuple(g(raw, car) for g in getters)


def insert_sql(table, n):
    return f"INSERT INTO {table} VALUES ({','.join('?' * n)})"


TELEMETRY_ROW = row_maker(store.TELEMETRY)
CAR_MOTION_ROW = row_maker(store.CAR_MOTION)
CAR_STATUS_ROW = row_maker(store.CAR_STATUS)
CHANGE_LOG_ROWS = {table: row_maker(spec) for table, spec in store.CHANGE_LOGS.items()}
WHEEL_FIELDS = [field for _, field in store.WHEEL]
# insert order: ticks before the rows that point to them
FLUSH_ORDER = ("tick", "telemetry", "wheel", *store.CHANGE_LOGS, "car_motion", "car_status")


def gear_str(g):
    return "R" if g < 0 else "N" if g == 0 else str(g)


def clean_time(t):
    return t if t is not None and t > 0 else None


class WheelSlip:
    """Longitudinal slip per wheel, without knowing the tyre size.

    While coasting straight (off throttle and brake, no steering, no yaw) every wheel rolls
    freely, so k = speed / |wheel rps| is that wheel's effective radius in
    whatever unit AMS2 uses. Afterwards slip = (k * |rps| - speed) / speed:
    > 0 wheelspin, < 0 locking (-1 = fully locked). k is saved per car.
    """

    LEARN_SAMPLES = 20

    def __init__(self):
        self.data = store.load_wheels()
        self.car = None
        self.k = None
        self.sums = [0.0] * 4
        self.n = 0
        self.dirty = False
        self.last_save = 0.0

    def set_car(self, car):
        if car == self.car:
            return
        self.save()
        self.car = car
        self.k = self.data.get(car)
        self.sums = [0.0] * 4
        self.n = 0

    def update(self, snap):
        v = snap["speed"]
        rps = [abs(r) for r in snap["tyre_rps"]]
        # Brake tolerance is loose: pedals often read a few % at rest.
        coasting = (v > 14 and snap["throttle"] < 0.05 and snap["brake"] < 0.1
                    and abs(snap["steering"]) < 0.05 and abs(snap["ang_vel"][1]) < 0.05
                    and all(r > 1 for r in rps))
        ratios = [v / r for r in rps] if coasting else None
        # Left and right of an axle must agree, else a wheel is on a kerb or in the air.
        if ratios and all(abs(ratios[a] - ratios[b]) < 0.02 * ratios[a] for a, b in ((0, 1), (2, 3))):
            if self.k is None:
                self.sums = [s + r for s, r in zip(self.sums, ratios)]
                self.n += 1
                if self.n >= self.LEARN_SAMPLES:
                    self.k = [s / self.n for s in self.sums]
                    self.dirty = True
                    log.info("learned wheel radii for %s: %s", self.car, self.k)
            else:
                self.k = [0.995 * k + 0.005 * r for k, r in zip(self.k, ratios)]
                self.dirty = True
        if self.dirty and time.monotonic() - self.last_save > 30:
            self.save()
        if self.k is None or v < 3:
            return None
        return [max(-1.0, min(1.0, (k * r - v) / v)) for k, r in zip(self.k, rps)]

    def save(self):
        if self.dirty and self.car:
            self.data[self.car] = self.k
            try:
                store.save_wheels(self.data)
            except OSError as e:
                log.warning("cannot save wheels: %s", e)
        self.dirty = False
        self.last_save = time.monotonic()


class Session:
    def __init__(self, sid, day, key, snap, player, now):
        self.id = sid
        self.day = day
        self.key = key
        self.ref = f"{day}:{sid}"
        self.started_at = now
        self.laps_completed = player["laps_completed"] if player else 0
        self.lap_count = 0
        self.best_lap = None
        self.driving_s = 0.0
        self.last_ts = now
        self.last_cars = 0.0
        self.last_flush = now
        self.last_last_lap = snap["last_lap"]
        self.pending = None
        self.rows = defaultdict(list)   # table -> rows waiting for the next flush
        self.changes = {}               # change-log key -> last values written
        self.idents = {}                # car idx -> participant identity written
        self.tick_id = None
        self.reset_lap(snap)

    def reset_lap(self, snap):
        self.lap_invalid = False
        self.lap_s1 = None
        self.lap_s2 = None
        self.lap_top = 0.0
        self.lap_fuel = snap["fuel_level"]
        self.lap_points = []


class Recorder(threading.Thread):
    def __init__(self, reader=None):
        super().__init__(daemon=True, name="recorder")
        self.reader = reader or Pcars2Reader()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wheels = WheelSlip()
        self.sess = None
        self.con = None
        self.con_day = None
        self.next_tick = 1
        self.dims = {}
        self.live = {"status": "starting"}
        self.trace = deque(maxlen=HZ * TRACE_SECONDS)
        self.track = None          # cached outline for the current track
        self.track_id = None       # (track, variation)
        self.track_rev = 0         # bumped whenever the outline changes
        self.trail = deque(maxlen=6000)

    # ---- public, called from the web threads ----

    def snapshot(self):
        with self.lock:
            return self.live

    def trace_backlog(self):
        with self.lock:
            return list(self.trace)

    def track_outline(self):
        with self.lock:
            if self.track:
                return {"rev": self.track_rev, "source": "lap", **self.track}
            return {"rev": self.track_rev, "source": "trail", "points": list(self.trail)}

    def stop(self):
        self.stop_event.set()

    # ---- thread ----

    def run(self):
        period = 1.0 / HZ
        next_t = time.monotonic()
        while not self.stop_event.is_set():
            try:
                active = self.tick()
            except Exception:
                log.exception("recorder tick failed")
                active = False
            next_t += period if active else 0.5
            delay = next_t - time.monotonic()
            if delay < -1:
                next_t = time.monotonic()
            elif delay > 0:
                self.stop_event.wait(delay)
        self.close_session()
        self.wheels.save()

    def set_live(self, live, trace_row=None):
        with self.lock:
            self.live = live
            if trace_row is not None:
                self.trace.append(trace_row)

    def tick(self):
        now = time.time()
        snap = self.reader.read()
        if snap is None:
            self.close_session()
            self.set_live({"status": "no shared memory", "detail":
                           "/dev/shm/$pcars2$ not found: is simd running and AMS2 started with the bridge?"})
            return False

        gs = snap["game_state"]
        if gs not in (GAME_PLAYING, GAME_PAUSED, GAME_MENU_TICKING):
            self.close_session()
            self.set_live({"status": "idle", "detail": "AMS2: " + _name(GAME_STATES, gs)})
            return False

        player = snap["cars"][snap["viewed"]] if snap["cars"] else None
        if not snap["track"] or not snap["session_state"] or player is None:
            self.set_live({"status": "idle", "detail": "AMS2 loading a session"})
            return False

        key = (snap["track"], snap["variation"], snap["car"], snap["session_state"])
        if self.sess and (self.sess.key != key or player["laps_completed"] < self.sess.laps_completed):
            self.close_session()
        if self.sess is None:
            self.open_session(snap, player, key, now)
        self.load_track(snap)

        der = derived(snap)
        self.wheels.set_car(snap["car"])
        slips = self.wheels.update(snap) if gs == GAME_PLAYING else None
        speed_kmh = snap["speed"] * 3.6

        trace_row = None
        if gs == GAME_PLAYING:
            self.record(snap, player, der, slips, now)
            trace_row = [round(now, 3), round(speed_kmh, 1), round(snap["throttle"], 3),
                         round(snap["brake"], 3), round(snap["steering"], 3), round(der["g_lat"], 3),
                         round(der["g_long"], 3), round(der["slip_angle"], 2),
                         round(max(slips), 3) if slips else None,
                         round(min(slips), 3) if slips else None]

        self.set_live(self.build_live(snap, player, der, slips, gs), trace_row)
        return True

    # ---- sessions ----

    def db(self, day):
        if self.con_day != day:
            if self.con:
                self.con.close()
            self.con = store.open_day(day)
            self.con_day = day
            self.next_tick = self.con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM tick").fetchone()[0]
            self.dims = {}
        return self.con

    def dim(self, table, cols, vals):
        """id of the vehicle / driver row with these values, inserted if new."""
        key = (table, vals)
        if key not in self.dims:
            where = " AND ".join(f"{c}=?" for c in cols)
            self.con.execute(f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES "
                             f"({','.join('?' * len(cols))})", vals)
            self.dims[key] = self.con.execute(f"SELECT id FROM {table} WHERE {where}", vals).fetchone()[0]
        return self.dims[key]

    def track_row(self, raw):
        con = self.con
        con.execute("""INSERT INTO track (location, variation, translated_location, translated_variation, length)
                       VALUES (?,?,?,?,?) ON CONFLICT (location, variation) DO UPDATE SET
                       translated_location=excluded.translated_location,
                       translated_variation=excluded.translated_variation,
                       length=COALESCE(NULLIF(excluded.length, 0), length)""",
                    (raw["mTrackLocation"], raw["mTrackVariation"], raw["mTranslatedTrackLocation"],
                     raw["mTranslatedTrackVariation"], raw["mTrackLength"]))
        return con.execute("SELECT id FROM track WHERE location=? AND variation=?",
                           (raw["mTrackLocation"], raw["mTrackVariation"])).fetchone()[0]

    def open_session(self, snap, player, key, now):
        day = store.day_of(now)
        con = self.db(day)
        raw = snap["raw"]
        cur = con.execute(
            """INSERT INTO session (started_at, session_state, track_id, vehicle_id, shm_version,
               build_version, start_pos) VALUES (?,?,?,?,?,?,?)""",
            (now, snap["session_state"], self.track_row(raw),
             self.dim("vehicle", ("name", "class"), (snap["car"], snap["car_class"])),
             raw["mVersion"], raw["mBuildVersionNumber"], player["pos"] or None))
        con.commit()
        self.sess = Session(cur.lastrowid, day, key, snap, player, now)
        self.trail.clear()
        with self.lock:
            self.track_rev += 1
        self.last_snap = snap
        log.info("session %s started: %s at %s %s in %s", self.sess.ref, key[3], snap["track"],
                 snap["variation"], snap["car"])

    def close_session(self):
        s = self.sess
        if s is None:
            return
        self.sess = None
        con = self.db(s.day)
        snap = getattr(self, "last_snap", None)
        try:
            if s.pending:
                self.write_lap(s, s.pending, snap["last_lap"] if snap else None)
            self.flush(s)
            if s.driving_s < MIN_DRIVING_S and s.lap_count == 0:
                store.delete_session(con, s.id)
                log.info("session %s dropped (%.0f s driving, no laps)", s.ref, s.driving_s)
            else:
                player = snap["cars"][snap["viewed"]] if snap and snap["cars"] else None
                con.execute(
                    """UPDATE session SET ended_at=?, laps=?, best_lap=?, final_pos=?, result=?,
                       driving_s=? WHERE id=?""",
                    (s.last_ts, s.lap_count, s.best_lap, player["pos"] if player else None,
                     player["race_state"] if player else None,
                     round(s.driving_s, 1), s.id))
                log.info("session %s closed: %d laps", s.ref, s.lap_count)
            con.commit()
        except Exception:
            log.exception("closing session %s failed", s.ref)
        self.wheels.save()

    # ---- per tick recording ----

    def record(self, snap, player, der, slips, now):
        s = self.sess
        self.last_snap = snap
        dt_s = min(now - s.last_ts, 0.5)
        s.last_ts = now
        if snap["speed"] > 1:
            s.driving_s += dt_s

        # Lap bookkeeping. mLastLapTime may update a few frames after the line.
        lc = player["laps_completed"]
        if s.pending and (snap["last_lap"] != s.pending["prev_last"] or now - s.pending["t"] > 2):
            self.write_lap(s, s.pending, snap["last_lap"])
            s.pending = None
        if lc > s.laps_completed:
            if s.pending:
                self.write_lap(s, s.pending, snap["last_lap"])
            s.pending = {
                "lap": lc, "t": now, "prev_last": s.last_last_lap, "invalid": s.lap_invalid,
                "s1": s.lap_s1, "s2": s.lap_s2, "top": s.lap_top, "pos": player["pos"],
                "fuel": (s.lap_fuel - snap["fuel_level"]) * snap["fuel_capacity"],
                "points": s.lap_points, "length": snap["track_length"],
            }
            s.laps_completed = lc
            s.reset_lap(snap)
        s.last_last_lap = snap["last_lap"]
        s.lap_invalid = s.lap_invalid or snap["lap_invalid"]
        if snap["s1"] > 0:
            s.lap_s1 = snap["s1"]
        if snap["s2"] > 0:
            s.lap_s2 = snap["s2"]
        speed_kmh = snap["speed"] * 3.6
        s.lap_top = max(s.lap_top, speed_kmh)

        in_garage = snap["pit_mode"] == PIT_IN_GARAGE and snap["speed"] < 0.5
        raw, praw = snap["raw"], player["raw"]
        s.tick_id = None
        if not in_garage:
            x, z = player["x"], player["z"]
            s.lap_points.append((round(x, 2), round(z, 2), round(player["lap_dist"], 1)))
            if not self.trail or math.hypot(x - self.trail[-1][0], z - self.trail[-1][1]) > 4:
                with self.lock:
                    self.trail.append((round(x, 1), round(z, 1)))
            tid = self.tick_row(s, now)
            s.rows["telemetry"].append((tid, *TELEMETRY_ROW(raw, praw)))
            sl = slips or [None] * 4
            for w in range(4):
                s.rows["wheel"].append((tid, w, *[raw[f][w] for f in WHEEL_FIELDS], sl[w]))

        # change logs: a row only when something differs from the last one written
        for table, make in CHANGE_LOG_ROWS.items():
            vals = make(raw, praw)
            if s.changes.get(table) != vals:
                s.changes[table] = vals
                s.rows[table].append((self.tick_row(s, now), *vals))
        for c in snap["cars"]:
            vals = CAR_STATUS_ROW(raw, c["raw"])
            if s.changes.get(c["idx"]) != vals:
                s.changes[c["idx"]] = vals
                s.rows["car_status"].append((c["idx"], self.tick_row(s, now), *vals))
        self.sync_participants(s, snap)

        if now - s.last_cars >= CARS_EVERY:
            s.last_cars = now
            for c in snap["cars"]:
                if c["active"]:
                    s.rows["car_motion"].append((self.tick_row(s, now), c["idx"], *CAR_MOTION_ROW(raw, c["raw"])))
        if now - s.last_flush >= FLUSH_EVERY:
            s.last_flush = now
            self.flush(s)

    def tick_row(self, s, now):
        """id of this tick's row, created on first use so idle ticks cost nothing."""
        if s.tick_id is None:
            s.tick_id = self.next_tick
            self.next_tick += 1
            s.rows["tick"].append((s.tick_id, s.id, now))
        return s.tick_id

    def flush(self, s):
        con = self.db(s.day)
        rows, s.rows = s.rows, defaultdict(list)
        try:
            for table in FLUSH_ORDER:
                if rows[table]:
                    con.executemany(insert_sql(table, len(rows[table][0])), rows[table])
            con.commit()
        except sqlite3.Error as e:
            con.rollback()
            log.error("dropped %d ticks for session %s: %s", len(rows["tick"]), s.ref, e)

    def sync_participants(self, s, snap):
        """participant rows for active cars, rewritten when a car's identity changes."""
        for c in snap["cars"]:
            if not c["active"]:
                continue
            ident = (c["name"], c["raw"]["mNationalities"], c["car"], c["car_class"], c["idx"] == snap["viewed"])
            if s.idents.get(c["idx"]) == ident:
                continue
            s.idents[c["idx"]] = ident
            self.db(s.day).execute(
                """INSERT INTO participant VALUES (?,?,?,?,?) ON CONFLICT (session_id, idx) DO UPDATE SET
                   driver_id=excluded.driver_id, vehicle_id=excluded.vehicle_id, is_player=excluded.is_player""",
                (s.id, c["idx"], self.dim("driver", ("name", "nationality"), ident[:2]),
                 self.dim("vehicle", ("name", "class"), ident[2:4]), int(ident[4])))

    def write_lap(self, s, p, last_lap):
        lap_time = clean_time(last_lap)
        s1, s2 = p["s1"], p["s2"]
        s3 = lap_time - s1 - s2 if lap_time and s1 and s2 and lap_time > s1 + s2 else None
        valid = not p["invalid"]
        con = self.db(s.day)
        con.execute("INSERT OR REPLACE INTO lap VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (s.id, p["lap"], lap_time, int(valid), s1, s2, s3, p["pos"],
                     round(p["top"], 1), round(p["fuel"], 3) if p["fuel"] > 0 else None, time.time()))
        s.lap_count += 1
        if valid and lap_time and (s.best_lap is None or lap_time < s.best_lap):
            s.best_lap = lap_time
        con.execute("UPDATE session SET laps=?, best_lap=?, ended_at=?, driving_s=? WHERE id=?",
                    (s.lap_count, s.best_lap, s.last_ts, round(s.driving_s, 1), s.id))
        con.commit()
        log.info("session %s lap %d: %s%s", s.ref, p["lap"], lap_time, "" if valid else " (invalid)")
        self.maybe_save_outline(s, p, lap_time, valid)

    def maybe_save_outline(self, s, p, lap_time, valid):
        pts, length = p["points"], p["length"]
        if not lap_time or len(pts) < 100 or length <= 0:
            return
        dists = [q[2] for q in pts]
        if min(dists[:20]) > 0.1 * length or max(dists[-20:]) < 0.9 * length:
            return  # lap doesn't cover the whole track (pit exit, reset...)
        track, variation = s.key[0], s.key[1]
        old = store.load_track(track, variation)
        if old and (old.get("valid") and not valid or
                    old.get("valid") == valid and old.get("lap_time", 1e9) <= lap_time):
            return
        outline = [pts[0][:2]]
        for q in pts[1:]:
            if math.hypot(q[0] - outline[-1][0], q[1] - outline[-1][1]) >= 3:
                outline.append(q[:2])
        data = {"track": track, "variation": variation, "length": length,
                "lap_time": lap_time, "valid": valid, "points": outline}
        store.save_track(track, variation, data)
        with self.lock:
            self.track = data
            self.track_rev += 1
        log.info("saved track outline for %s %s (%d points)", track, variation, len(outline))

    def load_track(self, snap):
        tid = (snap["track"], snap["variation"])
        if tid != self.track_id:
            self.track_id = tid
            data = store.load_track(*tid)
            with self.lock:
                self.track = data
                self.track_rev += 1

    # ---- live state for the UI ----

    def build_live(self, snap, player, der, slips, gs):
        length = snap["track_length"] or 1
        pdist = player["laps_completed"] * length + player["lap_dist"]
        pspeed = max(snap["speed"], 10)
        cars = []
        for c in snap["cars"]:
            if not c["active"]:
                continue
            gap_m = c["laps_completed"] * length + c["lap_dist"] - pdist
            cars.append({
                "idx": c["idx"], "name": c["name"], "pos": c["pos"], "lap": c["lap"],
                "x": round(c["x"], 1), "z": round(c["z"], 1),
                "pit": PIT_MODES[c["pit_mode"]] if c["pit_mode"] < len(PIT_MODES) else "",
                "best": clean_time(c["best_lap"]), "last": clean_time(c["last_lap"]),
                "gap": round(-gap_m / pspeed, 1), "player": c["idx"] == snap["viewed"],
                "state": _name(RACE_STATES, c["race_state"]),
            })
        cars.sort(key=lambda c: c["pos"] or 999)
        s = self.sess
        return {
            "status": "recording" if gs == GAME_PLAYING else "paused",
            "detail": _name(GAME_STATES, gs),
            "track_rev": self.track_rev,
            "session": {
                "ref": s.ref if s else None,
                "type": _name(SESSION_TYPES, snap["session_state"]),
                "track": snap["track"], "variation": snap["variation"],
                "car": snap["car"], "car_class": snap["car_class"],
                "laps_in_event": snap["laps_in_event"], "num_cars": snap["num_cars"],
                "time_left": snap["event_time_remaining"] if snap["event_time_remaining"] > 0 else None,
                "air_temp": round(snap["air_temp"], 1), "track_temp": round(snap["track_temp"], 1),
                "rain": round(snap["rain"], 2),
            },
            "car": {
                "speed": round(snap["speed"] * 3.6, 1), "rpm": round(snap["rpm"]),
                "max_rpm": round(snap["max_rpm"]), "gear": gear_str(snap["gear"]),
                "throttle": round(snap["throttle"], 3), "brake": round(snap["brake"], 3),
                "clutch": round(snap["clutch"], 3), "steer": round(snap["steering"], 3),
                "abs": snap["abs"],
                "fuel_l": round(snap["fuel_level"] * snap["fuel_capacity"], 1),
                "g_lat": round(der["g_lat"], 3), "g_long": round(der["g_long"], 3),
                "slip_angle": round(der["slip_angle"], 2),
                "slip": [round(v, 3) for v in slips] if slips else None,
                "tyre_temp": [round(t) for t in snap["tyre_temp"]],
                "brake_temp": [round(t) for t in snap["brake_temp"]],
                "wear": [round(w, 3) for w in snap["tyre_wear"]],
                "x": round(player["x"], 1), "z": round(player["z"], 1),
            },
            "lap": {
                "lap": player["lap"], "completed": player["laps_completed"], "pos": player["pos"],
                "current": clean_time(snap["current_time"]), "last": clean_time(snap["last_lap"]),
                "best": clean_time(snap["best_lap"]), "invalid": snap["lap_invalid"],
                "dist": round(player["lap_dist"]), "length": round(snap["track_length"]),
            },
            "cars": cars,
        }
