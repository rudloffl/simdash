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

from . import store
from .pcars2 import TERRAINS
from .recorder import Recorder

log = logging.getLogger("simdash")
STATIC = Path(__file__).resolve().parent / "static"
LIVE_PERIOD = 0.05  # s between WebSocket pushes

SAMPLE_COLS = ["ts", "lap", "lap_dist", "lap_time", "x", "z", "speed", "rpm", "gear", "throttle",
               "brake", "steer", "clutch", "g_lat", "g_long", "slip_angle", "slip_fl", "slip_fr",
               "slip_rl", "slip_rr", "abs", "pos"]

# Surface class of each terrain code: 0 track, 1 kerb, 2 off track (grass, gravel, runoff, walls...)
KERBS = {"rumble strips", "exit rumble strips", "b1 rumbles", "b2 rumbles", "drains", "paint concrete"}
TRACK = {"road", "low grip road", "bumpy road 1", "bumpy road 2", "bumpy road 3", "marbles", "pavement",
         "cobbles", "bumpy cobbles", "damaged road 1", "train track road", "rally tarmac"}
SURFACE = [0 if t in TRACK else 1 if t in KERBS else 2 for t in TERRAINS]


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

    @app.get("/api/sessions/{ref}/lap/{lap}")
    def lap_samples(ref: str, lap: int):
        """Samples of one completed lap. Lap N is the lap that ended when laps_completed reached N."""
        con, sid = open_ref(ref)
        with con:
            rows = con.execute(f"SELECT {','.join(SAMPLE_COLS)}, tick_id FROM v_samples WHERE session_id=? AND lap=? "
                               "ORDER BY ts", (sid, lap)).fetchall()
            terrain = {}
            if rows:
                for tick, wheel, t in con.execute("SELECT tick_id, wheel, terrain FROM wheel WHERE tick_id BETWEEN ? AND ?",
                                                  (rows[0]["tick_id"], rows[-1]["tick_id"])):
                    terrain.setdefault(tick, [None] * 4)[wheel] = t
        con.close()
        out = {c: [r[i] for r in rows] for i, c in enumerate(SAMPLE_COLS)}

        # worst surface under the left (fl, rl) and right (fr, rr) tyres
        def side(tick, wheels):
            cls = [SURFACE[t] for w in wheels if (t := terrain.get(tick, [None] * 4)[w]) is not None and 0 <= t < len(SURFACE)]
            return max(cls) if cls else None
        out["surf_l"] = [side(r["tick_id"], (0, 2)) for r in rows]
        out["surf_r"] = [side(r["tick_id"], (1, 3)) for r in rows]
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

    return app
