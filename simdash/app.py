"""FastAPI app: live telemetry over a WebSocket, recorded sessions over REST."""

import asyncio
import csv
import io
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import leds, replay, shakers, store, system
from .pcars2 import SURFACE, TERRAINS
from .recorder import Recorder

log = logging.getLogger("simdash")
STATIC = Path(__file__).resolve().parent / "static"
LIVE_PERIOD = 0.05  # s between WebSocket pushes

SAMPLE_COLS = ["ts", "lap", "lap_dist", "lap_time", "x", "z", "speed", "rpm", "gear", "throttle",
               "brake", "steer", "clutch", "g_lat", "g_long", "slip_angle", "slip_fl", "slip_fr",
               "slip_rl", "slip_rr", "abs", "pos"]



class ShakerConfig(BaseModel):
    devid: str
    effects: list[dict]


class ShakerTest(BaseModel):
    devid: str
    effect: dict
    mode: str = "effect"


class ReplayLoad(BaseModel):
    ref: str
    devid: str = ""
    effects: list[dict]


class ReplayControl(BaseModel):
    action: str  # play, pause, seek
    ts: float | None = None
    speed: float = 1.0
    devid: str = ""


class LedDriver(BaseModel):
    driver: str


class LedTest(BaseModel):
    mode: str  # pedal | sweep | stop


class CornerName(BaseModel):
    track: str
    variation: str
    apex: float
    name: str = ""


def create_app(recorder=None):
    rec = recorder or Recorder()

    @asynccontextmanager
    async def lifespan(app):
        rec.start()
        yield
        rec.stop()
        await asyncio.to_thread(rec.join, 5)

    app = FastAPI(title="simdash", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # revalidate the page and its scripts on every load (cheap 304s), so an update shows on reload
    @app.middleware("http")
    async def no_stale_static(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ---- live ----

    @app.websocket("/ws/live")
    async def live_ws(ws: WebSocket):
        await ws.accept()
        backlog = rec.trace_backlog()
        last_ts = backlog[-1][0] if backlog else 0
        await ws.send_json({"type": "backlog", "trace": backlog})
        try:
            while True:
                live = rec.snapshot()
                rows = [r for r in rec.trace_backlog()[-10:] if r[0] > last_ts]
                if rows:
                    last_ts = rows[-1][0]
                await ws.send_json({"type": "live", "live": live, "trace": rows})
                await asyncio.sleep(LIVE_PERIOD)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/api/live/track")
    def live_track():
        return rec.track_outline()

    # ---- history ----

    def open_ref(ref):
        try:
            day, sid = store.parse_ref(ref)
            return store.read_day(day), sid
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "no such session")

    @app.get("/api/sessions")
    def sessions():
        return store.all_sessions()

    @app.get("/api/sessions/{ref}")
    def session(ref: str):
        con, sid = open_ref(ref)
        with con:
            row = con.execute("SELECT * FROM v_sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise HTTPException(404, "no such session")
            laps = con.execute("SELECT * FROM lap WHERE session_id=? ORDER BY lap", (sid,)).fetchall()
            parts = con.execute("SELECT * FROM v_results WHERE session_id=? ORDER BY pos IS NULL, pos",
                                (sid,)).fetchall()
            span = con.execute("SELECT MIN(ts), MAX(ts) FROM tick WHERE session_id=?", (sid,)).fetchone()
        con.close()
        return {"session": {**dict(row), "ref": ref}, "laps": [dict(r) for r in laps],
                "participants": [dict(r) for r in parts], "t0": span[0], "t1": span[1],
                "live": rec.snapshot().get("session", {}).get("ref") == ref}

    @app.get("/api/corners")
    def corner_names(track: str, variation: str):
        return store.load_corner_names(track, variation)

    @app.post("/api/corners")
    def set_corner_name(c: CornerName):
        return store.save_corner_name(c.track, c.variation, c.apex, c.name.strip()[:40])

    @app.get("/api/live/reference")
    def live_reference():
        """Best lap on the live track layout with the live car, from any session (this one included)."""
        s = rec.snapshot().get("session") or {}
        if not s.get("track"):
            return {}
        best = store.best_laps(s["track"], s["variation"], s["car"], limit=1)
        if not best:
            return {}
        return {**best[0], "samples": read_lap(best[0]["ref"], best[0]["lap"])}

    @app.get("/api/sessions/{ref}/references")
    def references(ref: str):
        """Best laps from other sessions (any day) on the same track layout with the same car, to compare against."""
        con, sid = open_ref(ref)
        with con:
            row = con.execute("SELECT track, variation, car FROM v_sessions WHERE id=?", (sid,)).fetchone()
        con.close()
        if row is None:
            raise HTTPException(404, "no such session")
        return store.best_laps(row["track"], row["variation"], row["car"], exclude_ref=ref)

    @app.get("/api/sessions/{ref}/lap/{lap}")
    def lap_samples(ref: str, lap: int):
        return read_lap(ref, lap)

    def read_lap(ref, lap):
        """Samples of one completed lap. Lap N is the lap that ended when laps_completed reached N."""
        con, sid = open_ref(ref)
        with con:
            rows = con.execute(f"SELECT {','.join(SAMPLE_COLS)}, tick_id FROM v_samples WHERE session_id=? AND lap=? "
                               "ORDER BY ts", (sid, lap)).fetchall()
            # lap distance where sector 2 and sector 3 start (mCurrentSector is 0-based)
            sectors = [r[0] for r in con.execute(
                "SELECT MIN(e.lap_dist) FROM telemetry e JOIN tick t ON t.id = e.tick_id "
                "WHERE t.session_id=? AND e.lap=? AND e.sector > 0 GROUP BY e.sector ORDER BY e.sector", (sid, lap))]
            terrain = {}
            if rows:
                for tick, wheel, t in con.execute("SELECT tick_id, wheel, terrain FROM wheel WHERE tick_id BETWEEN ? AND ?",
                                                  (rows[0]["tick_id"], rows[-1]["tick_id"])):
                    terrain.setdefault(tick, [None] * 4)[wheel] = t
        con.close()
        out = {c: [r[i] for r in rows] for i, c in enumerate(SAMPLE_COLS)}

        # worst surface under the left (fl, rl) and right (fr, rr) tyres: its class (surf_*) and
        # its raw AMS2 terrain code (terr_*, names in "terrains"), the front tyre's on a tie
        def side(tick, wheels):
            codes = [t for w in wheels if (t := terrain.get(tick, [None] * 4)[w]) is not None and 0 <= t < len(SURFACE)]
            return max(codes, key=lambda t: SURFACE[t]) if codes else None
        for name, wheels in (("l", (0, 2)), ("r", (1, 3))):
            raw = [side(r["tick_id"], wheels) for r in rows]
            out["terr_" + name] = raw
            out["surf_" + name] = [None if t is None else SURFACE[t] for t in raw]
        out["terrains"] = TERRAINS
        out["sectors"] = sectors
        return out

    @app.get("/api/sessions/{ref}/path")
    def session_path(ref: str):
        """Whole-session player path, thinned to about 1 point per 0.25 s, for the replay map."""
        con, sid = open_ref(ref)
        with con:
            rows = con.execute("SELECT t.ts, e.x, e.z FROM tick t JOIN telemetry e ON e.tick_id=t.id "
                               "WHERE t.session_id=? AND t.id % 5 = 0 ORDER BY t.ts", (sid,)).fetchall()
        con.close()
        return {"ts": [r[0] for r in rows], "x": [r[1] for r in rows], "z": [r[2] for r in rows]}

    @app.get("/api/sessions/{ref}/positions")
    def session_positions(ref: str):
        """Race position of every car over the session: a point on each change and at least every 5 s
        (so hovering anywhere on a step finds one), with the lap, plus each pit stop's entry time."""
        con, sid = open_ref(ref)
        with con:
            t_lo, t_hi = con.execute("SELECT MIN(id), MAX(id) FROM tick WHERE session_id=?", (sid,)).fetchone()
            rows = con.execute(
                """SELECT m.idx, t.ts, m.race_pos, m.lap FROM car_motion m JOIN tick t ON t.id=m.tick_id
                   WHERE t.session_id=? AND m.race_pos > 0 ORDER BY m.idx, t.ts""", (sid,)).fetchall()
            names = {r[0]: tuple(r[1:]) for r in con.execute(
                """SELECT p.idx, d.name, v.name, p.is_player FROM participant p JOIN driver d ON d.id=p.driver_id
                   JOIN vehicle v ON v.id=p.vehicle_id WHERE p.session_id=?""", (sid,))}
            pit_rows = con.execute(
                """SELECT cs.idx, t.ts, cs.pit_mode FROM car_status cs JOIN tick t ON t.id=cs.tick_id
                   WHERE cs.tick_id BETWEEN ? AND ? ORDER BY cs.idx, cs.tick_id""", (t_lo or 0, t_hi or 0)).fetchall()
        con.close()
        cars, skip = {}, set()
        for idx, ts, pos, lap in rows:
            if idx in skip:
                continue
            c = cars.get(idx)
            if c is None:
                name, car, player = names.get(idx, (f"Car {idx}", "", 0))
                if name == "Safety Car":  # AMS2 counts it as the last car
                    skip.add(idx)
                    continue
                c = cars[idx] = {"idx": idx, "name": name, "car": car, "player": bool(player),
                                 "ts": [], "pos": [], "lap": [], "pits": [], "_last": None}
            ts = round(ts, 2)
            if not c["pos"] or c["pos"][-1] != pos or ts - c["ts"][-1] >= 5:
                c["ts"].append(ts)
                c["pos"].append(pos)
                c["lap"].append(lap)
            c["_last"] = (ts, pos, lap)
        for c in cars.values():  # close each step line at the car's last sample
            ts, pos, lap = c.pop("_last")
            if c["ts"][-1] != ts:
                c["ts"].append(ts)
                c["pos"].append(pos)
                c["lap"].append(lap)
        prev = {}
        for idx, ts, mode in pit_rows:  # pit_mode is a change log: a stop starts when it leaves 0
            if idx in cars and mode in (1, 2) and not prev.get(idx):
                cars[idx]["pits"].append(round(ts, 2))
            prev[idx] = mode
        return list(cars.values())

    @app.get("/api/sessions/{ref}/shakers")
    def session_shakers(ref: str):
        """What each shaker played, per tick, with the monocoque settings in force when the session started."""
        con, sid = open_ref(ref)
        with con:
            row = con.execute("SELECT started_at FROM session WHERE id=?", (sid,)).fetchone()
        con.close()
        if row is None:
            raise HTTPException(404, "no such session")
        devices, path = shakers.devices_at(row[0])
        devices += shakers.simdash_devices()  # simdash's own effects: today's settings, no history
        try:
            tl = replay.Timeline(ref, devices=devices)
        except LookupError:
            return {"ts": [], "channels": {}, "config": path}
        return {"ts": [round(t, 3) for t in tl.data["ts"]], "channels": tl.channels(), "config": path}

    @app.get("/api/sessions/{ref}/cars_at")
    def cars_at(ref: str, ts: float):
        con, sid = open_ref(ref)
        with con:
            has_cars = "EXISTS (SELECT 1 FROM car_motion m WHERE m.tick_id=t.id)"
            near = con.execute(f"SELECT id, ts FROM tick t WHERE session_id=? AND ts<=? AND {has_cars} "
                               "ORDER BY ts DESC LIMIT 1", (sid, ts)).fetchone()
            if near is None:
                near = con.execute(f"SELECT id, ts FROM tick t WHERE session_id=? AND {has_cars} "
                                   "ORDER BY ts LIMIT 1", (sid,)).fetchone()
            rows = con.execute(
                """SELECT m.idx, m.race_pos AS pos, m.lap, m.x, m.z, m.speed * 3.6 AS speed,
                          (SELECT pit_mode FROM car_status s WHERE s.idx=m.idx AND s.tick_id<=m.tick_id
                           ORDER BY s.tick_id DESC LIMIT 1) AS pit,
                          d.name, p.is_player
                   FROM car_motion m
                   LEFT JOIN participant p ON p.session_id=? AND p.idx=m.idx
                   LEFT JOIN driver d ON d.id=p.driver_id
                   WHERE m.tick_id=?""", (sid, near[0])).fetchall() if near else []
        con.close()
        return {"ts": near[1] if near else None, "cars": [dict(r) for r in rows]}

    @app.get("/api/sessions/{ref}/samples.csv")
    def export_csv(ref: str):
        con, sid = open_ref(ref)

        def gen():
            with con:
                cur = con.execute(f"SELECT {','.join(SAMPLE_COLS)} FROM v_samples WHERE session_id=? "
                                  "ORDER BY ts", (sid,))
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(SAMPLE_COLS)
                for i, r in enumerate(cur):
                    w.writerow(tuple(r))
                    if i % 2000 == 0:
                        yield buf.getvalue()
                        buf.seek(0)
                        buf.truncate()
                yield buf.getvalue()
            con.close()

        name = ref.replace(":", "_session")
        return StreamingResponse(gen(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

    @app.delete("/api/sessions/{ref}")
    def delete_session(ref: str):
        if rec.snapshot().get("session", {}).get("ref") == ref:
            raise HTTPException(409, "session is being recorded")
        con, sid = open_ref(ref)
        try:
            with con:
                store.delete_session(con, sid)
        except sqlite3.Error as e:
            raise HTTPException(500, str(e))
        finally:
            con.close()
        return {"deleted": ref}

    # ---- bass shakers (monocoque Sound effects) ----

    @app.get("/api/shakers")
    def shaker_setup():
        try:
            cfg = shakers.load()
        except (OSError, ValueError) as e:
            raise HTTPException(500, str(e))
        return {**cfg, "layout": shakers.load_layout(), "sinks": shakers.sinks(), "running": shakers.monocoque_running(),
                "options": {"effects": shakers.EFFECTS, "wheel_effects": sorted(shakers.WHEEL_EFFECTS),
                            "wheels": shakers.WHEELS, "group_offset": shakers.GROUP_OFFSET, "modulations": shakers.MODULATIONS, "spots": shakers.SPOTS,
                            "channels": shakers.CHANNEL_SHORT}}

    @app.put("/api/shakers")
    def save_shakers(c: ShakerConfig):
        try:
            return {**shakers.save(c.effects, c.devid), "running": shakers.monocoque_running()}
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, str(e))

    @app.put("/api/shakers/layout")
    def save_shaker_layout(layout: dict[str, dict]):
        return shakers.save_layout(layout)

    @app.post("/api/shakers/test")
    async def test_shaker(t: ShakerTest):
        try:
            secs = await asyncio.to_thread(shakers.play, t.effect, t.devid, t.mode)
        except LookupError as e:
            raise HTTPException(409, str(e))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {"seconds": secs}

    @app.post("/api/shakers/stop")
    def stop_shaker():
        shakers.stop()
        return {}

    # ---- shaker replay: a recorded session played on the shakers ----

    @app.post("/api/shakers/replay")
    async def replay_load(r: ReplayLoad):
        """Load a session with these (possibly unsaved) effects; returns what the page draws."""
        try:
            tl = await asyncio.to_thread(replay.player.load, r.ref, r.effects, r.devid)
        except (LookupError, FileNotFoundError):
            raise HTTPException(404, "no such session")
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return {**await asyncio.to_thread(tl.display), "terrains": TERRAINS, "surface": SURFACE,
                "state": replay.player.state()}

    @app.get("/api/shakers/replay")
    def replay_state():
        return replay.player.state()

    @app.post("/api/shakers/replay/control")
    def replay_control(c: ReplayControl):
        p = replay.player
        try:
            if c.action == "play":
                p.play(c.ts if c.ts is not None else p.now(), max(0.1, min(2.0, c.speed)), c.devid)
            elif c.action == "pause":
                p.pause()
            elif c.action == "seek" and c.ts is not None:
                p.seek(c.ts)
            else:
                raise HTTPException(400, "unknown action")
        except LookupError as e:
            raise HTTPException(409, str(e))
        return p.state()

    # ---- wheel RPM LEDs (Moza) ----

    @app.get("/api/leds")
    async def led_setup():
        return await asyncio.to_thread(leds.setup)

    @app.put("/api/leds")
    def led_save(patch: dict):
        try:
            s = leds.save_settings(patch)
        except (TypeError, ValueError, KeyError) as e:
            raise HTTPException(400, f"bad setting: {e}")
        leds.driver_thread.reload()
        return s

    @app.post("/api/leds/driver")
    def led_driver(d: LedDriver):
        try:
            leds.set_driver(d.driver)
        except (OSError, ValueError) as e:
            raise HTTPException(400, str(e))
        leds.driver_thread.reload()
        return leds.setup()

    @app.post("/api/leds/test")
    def led_test(t: LedTest):
        try:
            if t.mode == "stop":
                leds.driver_thread.stop_test()
            else:
                leds.driver_thread.start_test(t.mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.get("/api/leds/state")
    def led_state():
        st = leds.driver_thread.snapshot()
        live = rec.snapshot()
        st["live_car"] = (live.get("session") or {}).get("car")
        st["live_max_rpm"] = (live.get("car") or {}).get("max_rpm")
        return st

    # ---- system: telemetry chain status and restarts ----

    @app.get("/api/system")
    async def system_status():
        return await asyncio.to_thread(system.status, rec.snapshot())

    @app.get("/api/system/log/{name}")
    def system_log(name: str):
        if name not in system.LOGS:
            raise HTTPException(404, "no such log")
        return {"lines": system._tail(system.LOGS[name], 40)}

    @app.post("/api/system/{component}/{action}")
    async def system_action(component: str, action: str):
        fn = system.ACTIONS.get(f"{component}/{action}")
        if fn is None:
            raise HTTPException(404, "no such action")
        try:
            await asyncio.to_thread(fn)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return await asyncio.to_thread(system.status, rec.snapshot())

    return app
