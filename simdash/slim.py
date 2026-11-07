"""Make old days smaller: slim their sessions, then compress the day file.

Slimming keeps every detail of the good laps and a light trace of the rest:
- good laps: each session's best valid lap, every valid lap within GOOD_RATIO of it, and every lap
  that was the best ever on its track layout with its car when it was driven (across all days);
- other laps: telemetry at about 2 Hz (every TICK_STEP-th sample), no per-wheel rows;
- other cars: positions at 1 Hz instead of 2 Hz, and their status only when something other than
  the running sector clocks changed (results and lap times stay);
- change logs: rows where only the always-moving columns changed are thinned the way the recorder
  now records them (see recorder.VOLATILE).
Then VACUUM. Compression packs the day file with zstd next to a small summary database (sessions
and laps, no samples), so lists, Progress and best-lap lookups stay fast; opening a session
unpacks the day into a size-limited cache.
"""

import logging
import os
import shutil
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import zstandard

from . import store

log = logging.getLogger("simdash.slim")

GOOD_RATIO = 1.01   # valid laps within 1 % of the session's best keep every detail
TICK_STEP = 10      # light laps keep every 10th sample (20 Hz -> 2 Hz)
OLDER_THAN = 7      # days: older days are slimmed and compressed automatically
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "simdash" / "days"
CACHE_MAX = 800 * 1024 * 1024
ZSTD_LEVEL = 9

# columns that move on every sample, per change-log table, and how often they may write a row alone
# (None: never alone); mirrors recorder.VOLATILE
VOLATILE = {"player_timing": ({"split_ahead", "split_behind", "s1", "s2", "s3"}, 0.5),
            "player_state": ({"engine_damage"}, 5.0),
            "car_status": ({"s1", "s2", "s3"}, None)}


# ---------- file names ----------

def zst_path(day):
    return store.DAYS_DIR / f"{day}.sqlite.zst"


def summary_path(day):
    return store.DAYS_DIR / f"{day}.summary.sqlite"


def cache_path(day):
    return CACHE_DIR / f"{day}.sqlite"


def state(day):
    """'full', 'slim' (slimmed, not compressed yet) or 'compressed'."""
    if store.day_path(day).exists():
        con = sqlite3.connect(store.day_path(day))
        try:
            done = con.execute("SELECT 1 FROM sqlite_master WHERE name='slimmed'").fetchone()
        finally:
            con.close()
        return "slim" if done else "full"
    return "compressed" if zst_path(day).exists() else "missing"


def day_files(day):
    return [p for p in (store.day_path(day), store.day_path(day).with_name(f"{day}.sqlite-wal"),
                        store.day_path(day).with_name(f"{day}.sqlite-shm"), zst_path(day), summary_path(day))
            if p.exists()]


# ---------- reading compressed days ----------

def cached_day(day):
    """Path of an uncompressed copy of a compressed day, unpacked into the cache on first use."""
    src, dst = zst_path(day), cache_path(day)
    if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".tmp")
        with open(src, "rb") as f, open(tmp, "wb") as g:
            zstandard.ZstdDecompressor().copy_stream(f, g)
        tmp.replace(dst)
        _evict(keep=dst)
    os.utime(dst)  # most recently used
    return dst


def _evict(keep):
    files = sorted(CACHE_DIR.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    total = 0
    for p in files:
        total += p.stat().st_size
        if total > CACHE_MAX and p != keep:
            p.unlink(missing_ok=True)


def unpack(day):
    """Turn a compressed day back into a normal day file (before changing it, e.g. deleting a session)."""
    if store.day_path(day).exists() or not zst_path(day).exists():
        return
    tmp = store.day_path(day).with_suffix(".tmp")
    with open(zst_path(day), "rb") as f, open(tmp, "wb") as g:
        zstandard.ZstdDecompressor().copy_stream(f, g)
    tmp.replace(store.day_path(day))
    zst_path(day).unlink()
    summary_path(day).unlink(missing_ok=True)
    cache_path(day).unlink(missing_ok=True)


# ---------- which laps are good ----------

def personal_bests():
    """{(day, session id): {lap, ...}} of the laps that were the best ever on their track layout with
    their car when driven, from every day (oldest first)."""
    sessions = []
    for day in store.list_days():
        try:
            con = store.read_day_summary(day)
            for r in con.execute("SELECT id, started_at, track, variation, car FROM v_sessions"):
                laps = con.execute("SELECT lap, lap_time FROM lap WHERE session_id=? AND valid AND lap_time > 0 "
                                   "ORDER BY lap", (r["id"],)).fetchall()
                sessions.append((r["started_at"], day, r["id"], (r["track"], r["variation"], r["car"]),
                                 [(l["lap"], l["lap_time"]) for l in laps]))
            con.close()
        except sqlite3.Error:
            continue
    best, out = {}, {}
    for _, day, sid, combo, laps in sorted(sessions, key=lambda x: x[0]):
        for lap, t in laps:
            if t < best.get(combo, float("inf")):
                best[combo] = t
                out.setdefault((day, sid), set()).add(lap)
    return out


def good_laps(con, sid, pbs):
    laps = con.execute("SELECT lap, lap_time, valid FROM lap WHERE session_id=? AND lap_time > 0", (sid,)).fetchall()
    valid = [t for _, t, v in laps if v]
    keep = set(pbs)
    if valid:
        top = min(valid) * GOOD_RATIO
        keep |= {lap for lap, t, v in laps if v and t <= top}
    return keep


# ---------- slimming one day ----------

def _thin_log(con, table, ts_of):
    """Drop change-log rows where only the always-moving columns changed (see VOLATILE)."""
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    vol, period = VOLATILE[table]
    keycols = ["idx"] if table == "car_status" else []
    data = [c for c in cols if c not in ("tick_id", "idx")]
    vi = {i for i, c in enumerate(data) if c in vol}
    drop, last = [], {}
    order = ", ".join(keycols + ["tick_id"])
    for row in con.execute(f"SELECT {', '.join(keycols + ['tick_id'] + data)} FROM {table} ORDER BY {order}"):
        key = row[0] if keycols else None
        tick = row[len(keycols)]
        vals = row[len(keycols) + 1:]
        prev = last.get(key)
        if prev is not None:
            pvals, pts = prev
            stable = any(a != b for i, (a, b) in enumerate(zip(vals, pvals)) if i not in vi)
            moved_ok = period is not None and ts_of(tick) - pts >= period
            if not stable and not moved_ok:
                drop.append((key, tick) if keycols else (tick,))
                continue
        last[key] = (vals, ts_of(tick))
    where = "idx=? AND tick_id=?" if keycols else "tick_id=?"
    con.executemany(f"DELETE FROM {table} WHERE {where}", drop)
    return len(drop)


def slim_day(day, pbs=None):
    """Slim a (not compressed) day file in place. Returns (bytes before, bytes after)."""
    path = store.day_path(day)
    before = sum(p.stat().st_size for p in day_files(day))
    pbs = personal_bests() if pbs is None else pbs
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='slimmed'").fetchone():
            return before, before
        ts = dict(con.execute("SELECT id, ts FROM tick"))
        with con:
            for (sid,) in con.execute("SELECT id FROM session").fetchall():
                keep = good_laps(con, sid, pbs.get((day, sid), set()))
                lo, hi = con.execute("SELECT MIN(id), MAX(id) FROM tick WHERE session_id=?", (sid,)).fetchone()
                if lo is None:
                    continue
                marks = ",".join("?" * len(keep)) or "NULL"
                light = (f"SELECT e.tick_id FROM telemetry e WHERE e.tick_id BETWEEN ? AND ? "
                         f"AND e.lap NOT IN ({marks})")
                args = (lo, hi, *keep)
                con.execute(f"DELETE FROM wheel WHERE tick_id IN ({light})", args)
                con.execute(f"DELETE FROM telemetry WHERE tick_id % {TICK_STEP} != 0 AND tick_id IN ({light})", args)
            # other cars: positions at 1 Hz, every other 2 Hz sample, chosen by moment (not per car) so
            # every moment kept still has all the cars
            con.execute("""DELETE FROM car_motion WHERE tick_id IN (
                SELECT tick_id FROM (SELECT tick_id, ROW_NUMBER() OVER (ORDER BY tick_id) AS n
                                     FROM (SELECT DISTINCT tick_id FROM car_motion)) WHERE n % 2 = 0)""")
            for table in VOLATILE:
                _thin_log(con, table, lambda t: ts.get(t, 0.0))
            # ticks nothing refers to any more
            refs = " UNION ".join(f"SELECT tick_id FROM {t}" for t in store.TICK_TABLES)
            con.execute(f"DELETE FROM tick WHERE id NOT IN ({refs}) "
                        "AND id NOT IN (SELECT MIN(id) FROM tick GROUP BY session_id)")
            con.execute("CREATE TABLE slimmed (done_at REAL, good_ratio REAL, tick_step INTEGER)")
            con.execute("INSERT INTO slimmed VALUES (?, ?, ?)", (time.time(), GOOD_RATIO, TICK_STEP))
        con.execute("VACUUM")
    finally:
        con.close()
    for p in (path.with_name(f"{day}.sqlite-wal"), path.with_name(f"{day}.sqlite-shm")):
        p.unlink(missing_ok=True)
    return before, path.stat().st_size


# ---------- compressing one day ----------

def _write_summary(day):
    """Sessions, laps, participants and the first tick of each session (what v_sessions reads), no samples."""
    dst = summary_path(day)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(store.day_path(day))
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    con = sqlite3.connect(tmp)
    try:
        with con:
            con.execute("CREATE TEMP TABLE first_tick AS SELECT MIN(id) AS id FROM tick GROUP BY session_id")
            for t in ("telemetry", "wheel", "car_motion", "car_status", "player_timing", "player_state"):
                con.execute(f"DELETE FROM {t}")
            for t in ("event_state", "weather"):
                con.execute(f"DELETE FROM {t} WHERE tick_id NOT IN (SELECT id FROM first_tick)")
            con.execute("DELETE FROM tick WHERE id NOT IN (SELECT id FROM first_tick)")
        con.execute("VACUUM")
    finally:
        con.close()
    tmp.replace(dst)


def compress_day(day):
    """Pack a day file with zstd next to its summary. Returns (bytes before, bytes after)."""
    path = store.day_path(day)
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    before = sum(p.stat().st_size for p in day_files(day))
    _write_summary(day)
    tmp = zst_path(day).with_name(zst_path(day).name + ".tmp")
    with open(path, "rb") as f, open(tmp, "wb") as g:
        zstandard.ZstdCompressor(level=ZSTD_LEVEL, threads=-1).copy_stream(f, g)
    # check it unpacks to the same bytes before dropping the original
    with open(tmp, "rb") as g:
        size = 0
        reader = zstandard.ZstdDecompressor().stream_reader(g)
        while chunk := reader.read(1 << 20):
            size += len(chunk)
    if size != path.stat().st_size:
        tmp.unlink()
        raise OSError(f"compressed {day} does not unpack to the original size")
    tmp.replace(zst_path(day))
    for p in (path, path.with_name(f"{day}.sqlite-wal"), path.with_name(f"{day}.sqlite-shm")):
        p.unlink(missing_ok=True)
    return before, sum(p.stat().st_size for p in day_files(day))


# ---------- the whole store ----------

def eligible(day, today=None):
    today = today or date.today()
    try:
        return date.fromisoformat(day) <= today - timedelta(days=OLDER_THAN)
    except ValueError:
        return False


def run(days=None, busy=lambda: False):
    """Slim and compress the given days (default: every day old enough). Stops early if busy() turns true."""
    days = [d for d in store.list_days() if eligible(d)] if days is None else days
    todo = [d for d in sorted(days) if state(d) in ("full", "slim")]
    if not todo:
        return []
    pbs = personal_bests()
    done = []
    for day in todo:
        if busy():
            break
        t0 = time.time()
        before = sum(p.stat().st_size for p in day_files(day))
        if state(day) == "full":
            slim_day(day, pbs)
        compress_day(day)
        after = sum(p.stat().st_size for p in day_files(day))
        log.info("slimmed and compressed %s: %.1f MB -> %.1f MB in %.0f s", day, before / 1e6, after / 1e6, time.time() - t0)
        done.append({"day": day, "before": before, "after": after})
    return done


def overview():
    """Every day with its size and state, for the Storage card."""
    out = []
    for day in store.list_days():
        files = day_files(day)
        out.append({"day": day, "state": state(day), "bytes": sum(p.stat().st_size for p in files),
                    "eligible": eligible(day)})
    cache = sum(p.stat().st_size for p in CACHE_DIR.glob("*.sqlite")) if CACHE_DIR.exists() else 0
    return {"days": out, "older_than": OLDER_THAN, "good_ratio": GOOD_RATIO, "cache_bytes": cache}
