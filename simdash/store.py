"""One SQLite database per day: <data dir>/days/YYYY-MM-DD.sqlite.

A session belongs to the day it started on, even if it runs past midnight.
Sessions are referenced across days as "YYYY-MM-DD:<id>".

Every field of the shared memory is stored, in its raw unit, split by how
often it changes:

- constant for a session: session (+ the track and vehicle it points to)
- continuous, at 20 Hz: telemetry (player car) and wheel (4 rows per tick)
- continuous, at 2 Hz: car_motion (every car)
- occasional, one row only when a value changes: player_state, player_timing,
  event_state, weather, car_status
- per participant: participant -> driver, vehicle

Every time-series row points to a tick (session, timestamp). A change-log
table's row holds from its tick until the next row for the same session
(and car, for car_status); the first tick of a session has a row in each.
The v_* views give the shapes the web app uses.
"""

import datetime as dt
import json
import math
import os
import re
import sqlite3
from pathlib import Path

from .pcars2 import ENUMS, KINDS

DATA_DIR = Path(os.environ.get("SIMDASH_DATA", Path.home() / ".local/share/simdash"))
DAYS_DIR = DATA_DIR / "days"
TRACKS_DIR = DATA_DIR / "tracks"
WHEELS_FILE = DATA_DIR / "wheels.json"

SCHEMA_VERSION = 2
WHEELS = ("fl", "fr", "rl", "rr")

# Column specs: (column, source field, index into the field or None).
# A "car." source is a participant field (the viewed car's for player tables).

TELEMETRY = [
    ("x", "car.mWorldPosition", 0), ("y", "car.mWorldPosition", 1), ("z", "car.mWorldPosition", 2),
    ("lap_dist", "car.mCurrentLapDistance", None), ("race_pos", "car.mRacePosition", None),
    ("lap", "car.mCurrentLap", None), ("laps_completed", "car.mLapsCompleted", None),
    ("sector", "car.mCurrentSector", None),
    ("current_time", "mCurrentTime", None), ("event_time_remaining", "mEventTimeRemaining", None),
    ("throttle", "mThrottle", None), ("brake", "mBrake", None), ("clutch", "mClutch", None),
    ("steering", "mSteering", None), ("handbrake", "mHandBrake", None),
    ("unfiltered_throttle", "mUnfilteredThrottle", None), ("unfiltered_brake", "mUnfilteredBrake", None),
    ("unfiltered_clutch", "mUnfilteredClutch", None), ("unfiltered_steering", "mUnfilteredSteering", None),
    ("speed", "mSpeed", None), ("rpm", "mRpm", None), ("gear", "mGear", None),
    ("engine_speed", "mEngineSpeed", None), ("engine_torque", "mEngineTorque", None),
    ("turbo_boost_pressure", "mTurboBoostPressure", None),
    ("boost_active", "mBoostActive", None), ("boost_amount", "mBoostAmount", None),
    ("abs_active", "mAntiLockActive", None), ("odometer_km", "mOdometerKM", None),
    ("fuel_level", "mFuelLevel", None), ("fuel_pressure_kpa", "mFuelPressureKPa", None),
    ("oil_temp_c", "mOilTempCelsius", None), ("oil_pressure_kpa", "mOilPressureKPa", None),
    ("water_temp_c", "mWaterTempCelsius", None), ("water_pressure_kpa", "mWaterPressureKPa", None),
    ("clutch_temp_k", "mClutchTemp", None), ("clutch_wear", "mClutchWear", None),
] + [(f"{col}_{a}", field, i)
     for col, field in (("orientation", "mOrientation"), ("local_vel", "mLocalVelocity"),
                        ("world_vel", "mWorldVelocity"), ("angular_vel", "mAngularVelocity"),
                        ("local_acc", "mLocalAcceleration"), ("world_acc", "mWorldAcceleration"))
     for i, a in enumerate("xyz")]

# One row per wheel (0 FL, 1 FR, 2 RL, 3 RR); the index is the wheel.
# mTyreSlipSpeed, mTyreGrip and mTyreLateralStiffness are obsolete and not stored.
WHEEL = [
    ("tyre_flags", "mTyreFlags"), ("terrain", "mTerrain"), ("rps", "mTyreRPS"),
    ("tyre_y", "mTyreY"), ("height_above_ground", "mTyreHeightAboveGround"),
    ("wheel_local_y", "mWheelLocalPositionY"), ("suspension_travel", "mSuspensionTravel"),
    ("suspension_velocity", "mSuspensionVelocity"), ("ride_height_cm", "mRideHeight"),
    ("air_pressure_psi", "mAirPressure"), ("wear", "mTyreWear"),
    ("brake_damage", "mBrakeDamage"), ("suspension_damage", "mSuspensionDamage"),
    ("brake_temp_c", "mBrakeTempCelsius"), ("tyre_temp_c", "mTyreTemp"),
    ("temp_left_c", "mTyreTempLeft"), ("temp_center_c", "mTyreTempCenter"),
    ("temp_right_c", "mTyreTempRight"), ("tread_temp_k", "mTyreTreadTemp"),
    ("layer_temp_k", "mTyreLayerTemp"), ("carcass_temp_k", "mTyreCarcassTemp"),
    ("rim_temp_k", "mTyreRimTemp"), ("internal_air_temp_k", "mTyreInternalAirTemp"),
]

PLAYER_STATE = [
    ("car_flags", "mCarFlags", None), ("pit_mode", "mPitMode", None), ("pit_schedule", "mPitSchedule", None),
    ("flag_colour", "mHighestFlagColour", None), ("flag_reason", "mHighestFlagReason", None),
    ("lap_invalidated", "mLapInvalidated", None), ("crash_state", "mCrashState", None),
    ("aero_damage", "mAeroDamage", None), ("engine_damage", "mEngineDamage", None),
    ("last_collision_idx", "mLastOpponentCollisionIndex", None),
    ("last_collision_magnitude", "mLastOpponentCollisionMagnitude", None),
    ("brake_bias", "mBrakeBias", None), ("abs_setting", "mAntiLockSetting", None),
    ("tc_setting", "mTractionControlSetting", None),
    ("wing_front", "mWings", 0), ("wing_rear", "mWings", 1), ("drs_state", "mDrsState", None),
    ("ers_mode", "mErsDeploymentMode", None), ("ers_auto", "mErsAutoModeEnabled", None),
    ("clutch_overheated", "mClutchOverheated", None), ("clutch_slipping", "mClutchSlipping", None),
    ("joypad", "mJoyPad0", None), ("dpad", "mDPad", None),
    ("fuel_capacity", "mFuelCapacity", None), ("max_rpm", "mMaxRPM", None), ("num_gears", "mNumGears", None),
    ("extents_x", "mExtentsCentre", 0), ("extents_y", "mExtentsCentre", 1), ("extents_z", "mExtentsCentre", 2),
] + [(f"compound_{w}", "mTyreCompound", i) for i, w in enumerate(WHEELS)]

PLAYER_TIMING = [
    ("best_lap", "mBestLapTime", None), ("last_lap", "mLastLapTime", None),
    ("personal_best_lap", "mPersonalFastestLapTime", None), ("world_best_lap", "mWorldFastestLapTime", None),
    ("split_time", "mSplitTime", None), ("split_ahead", "mSplitTimeAhead", None),
    ("split_behind", "mSplitTimeBehind", None),
] + [(f"{col}{n}", f"m{field}Sector{n}Time", None)
     for col, field in (("s", "Current"), ("best_s", "Fastest"), ("personal_best_s", "PersonalFastest"),
                        ("world_best_s", "WorldFastest"))
     for n in (1, 2, 3)]

EVENT_STATE = [
    ("viewed_idx", "mViewedParticipantIndex", None), ("num_participants", "mNumParticipants", None),
    ("race_state", "mRaceState", None), ("laps_in_event", "mLapsInEvent", None),
    ("session_duration_min", "mSessionDuration", None), ("additional_laps", "mSessionAdditionalLaps", None),
    ("num_sectors", "mNumSectors", None), ("enforced_pit_lap", "mEnforcedPitStopLap", None),
    ("yellow_flag_state", "mYellowFlagState", None),
]

WEATHER = [
    ("air_temp_c", "mAmbientTemperature", None), ("track_temp_c", "mTrackTemperature", None),
    ("rain", "mRainDensity", None), ("snow", "mSnowDensity", None), ("wind_speed", "mWindSpeed", None),
    ("wind_dir_x", "mWindDirectionX", None), ("wind_dir_y", "mWindDirectionY", None),
    ("cloud_brightness", "mCloudBrightness", None),
]

CAR_MOTION = [
    ("x", "car.mWorldPosition", 0), ("y", "car.mWorldPosition", 1), ("z", "car.mWorldPosition", 2),
    ("orientation_x", "car.mOrientations", 0), ("orientation_y", "car.mOrientations", 1),
    ("orientation_z", "car.mOrientations", 2), ("speed", "car.mSpeeds", None),
    ("lap_dist", "car.mCurrentLapDistance", None), ("race_pos", "car.mRacePosition", None),
    ("lap", "car.mCurrentLap", None), ("laps_completed", "car.mLapsCompleted", None),
    ("sector", "car.mCurrentSector", None),
]

CAR_STATUS = [
    ("active", "car.mIsActive", None), ("race_state", "car.mRaceStates", None),
    ("pit_mode", "car.mPitModes", None), ("pit_schedule", "car.mPitSchedules", None),
    ("flag_colour", "car.mHighestFlagColours", None), ("flag_reason", "car.mHighestFlagReasons", None),
    ("lap_invalidated", "car.mLapsInvalidated", None),
    ("best_lap", "car.mFastestLapTimes", None), ("last_lap", "car.mLastLapTimes", None),
] + [(f"{col}{n}", f"car.m{field}Sector{n}Times", None)
     for col, field in (("s", "Current"), ("best_s", "Fastest")) for n in (1, 2, 3)]

# change-log tables written from the shared fields, keyed by tick only
CHANGE_LOGS = {"player_state": PLAYER_STATE, "player_timing": PLAYER_TIMING,
               "event_state": EVENT_STATE, "weather": WEATHER}
# tables whose rows hang off a tick, deleted with their session
TICK_TABLES = ("telemetry", "wheel", "player_state", "player_timing", "event_state", "weather",
               "car_motion", "car_status")


def _type(field):
    kind = KINDS[field.removeprefix("car.")]
    return "REAL" if kind == "f" else "TEXT" if kind in "sc" else "INTEGER"


def _cols(spec):
    return ",\n    ".join(f"{c[0]} {_type(c[1])}" for c in spec)


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS enum_name (
    type TEXT NOT NULL, code INTEGER NOT NULL, name TEXT NOT NULL,
    PRIMARY KEY (type, code)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS track (
    id INTEGER PRIMARY KEY,
    location TEXT NOT NULL, variation TEXT NOT NULL,
    translated_location TEXT, translated_variation TEXT,
    length REAL,
    UNIQUE (location, variation)
);
CREATE TABLE IF NOT EXISTS vehicle (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, class TEXT NOT NULL,
    UNIQUE (name, class)
);
CREATE TABLE IF NOT EXISTS driver (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL, nationality INTEGER NOT NULL,
    UNIQUE (name, nationality)
);
CREATE TABLE IF NOT EXISTS session (
    id INTEGER PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    session_state INTEGER NOT NULL,
    track_id INTEGER NOT NULL REFERENCES track,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle,
    shm_version INTEGER,
    build_version INTEGER,
    -- summary, kept up to date by the recorder
    start_pos INTEGER,
    final_pos INTEGER,
    laps INTEGER DEFAULT 0,
    best_lap REAL,
    result INTEGER,
    driving_s REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS participant (
    session_id INTEGER NOT NULL REFERENCES session,
    idx INTEGER NOT NULL,
    driver_id INTEGER NOT NULL REFERENCES driver,
    vehicle_id INTEGER NOT NULL REFERENCES vehicle,
    is_player INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, idx)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS lap (
    session_id INTEGER NOT NULL REFERENCES session,
    lap INTEGER NOT NULL,
    lap_time REAL,
    valid INTEGER,
    s1 REAL, s2 REAL, s3 REAL,
    position INTEGER,
    top_speed REAL,
    fuel_used REAL,
    ended_at REAL,
    PRIMARY KEY (session_id, lap)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS tick (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tick_session ON tick (session_id, ts);
CREATE TABLE IF NOT EXISTS telemetry (
    tick_id INTEGER PRIMARY KEY REFERENCES tick,
    {_cols(TELEMETRY)}
);
CREATE TABLE IF NOT EXISTS wheel (
    tick_id INTEGER NOT NULL REFERENCES tick,
    wheel INTEGER NOT NULL,
    {_cols([(c, f) for c, f in WHEEL])},
    slip REAL,  -- derived: wheel surface speed vs car speed, from learned radii (wheels.json)
    PRIMARY KEY (tick_id, wheel)
) WITHOUT ROWID;
""" + "".join(f"""CREATE TABLE IF NOT EXISTS {table} (
    tick_id INTEGER PRIMARY KEY REFERENCES tick,
    {_cols(spec)}
);
""" for table, spec in CHANGE_LOGS.items()) + f"""CREATE TABLE IF NOT EXISTS car_motion (
    tick_id INTEGER NOT NULL REFERENCES tick,
    idx INTEGER NOT NULL,
    {_cols(CAR_MOTION)},
    PRIMARY KEY (tick_id, idx)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS car_motion_idx ON car_motion (idx, tick_id);
CREATE TABLE IF NOT EXISTS car_status (
    idx INTEGER NOT NULL,
    tick_id INTEGER NOT NULL REFERENCES tick,
    {_cols(CAR_STATUS)},
    PRIMARY KEY (idx, tick_id)
) WITHOUT ROWID;

CREATE VIEW IF NOT EXISTS v_sessions AS
SELECT s.id, s.started_at, s.ended_at,
    st.name AS session_type,
    COALESCE(NULLIF(t.translated_location, ''), t.location) AS track,
    COALESCE(NULLIF(t.translated_variation, ''), t.variation) AS variation,
    t.length AS track_length, v.name AS car, v.class AS car_class,
    e.laps_in_event, NULLIF(e.session_duration_min, 0) AS session_minutes,
    e.num_participants AS num_cars,
    s.start_pos, s.final_pos, s.laps, s.best_lap, rs.name AS result,
    w.air_temp_c AS air_temp, w.track_temp_c AS track_temp, w.rain,
    s.driving_s
FROM session s
JOIN track t ON t.id = s.track_id
JOIN vehicle v ON v.id = s.vehicle_id
LEFT JOIN enum_name st ON st.type = 'session_state' AND st.code = s.session_state
LEFT JOIN enum_name rs ON rs.type = 'race_state' AND rs.code = s.result
LEFT JOIN event_state e ON e.tick_id = (SELECT MIN(id) FROM tick WHERE session_id = s.id)
LEFT JOIN weather w ON w.tick_id = (SELECT MIN(id) FROM tick WHERE session_id = s.id);

-- player samples in the units the web app uses (km/h, G, degrees)
CREATE VIEW IF NOT EXISTS v_samples AS
SELECT t.session_id, t.id AS tick_id, t.ts, e.lap, e.lap_dist, e.current_time AS lap_time,
    e.x, e.z, e.speed * 3.6 AS speed, e.rpm, e.gear,
    e.throttle, e.brake, e.steering AS steer, e.clutch,
    e.local_acc_x / {9.80665} AS g_lat, -e.local_acc_z / {9.80665} AS g_long,
    CASE WHEN -e.local_vel_z > 5 THEN degrees(atan2(e.local_vel_x, -e.local_vel_z)) ELSE 0 END AS slip_angle,
    {", ".join(f"(SELECT slip FROM wheel w WHERE w.tick_id = t.id AND w.wheel = {i}) AS slip_{w}"
               for i, w in enumerate(WHEELS))},
    e.abs_active AS abs, e.race_pos AS pos
FROM tick t JOIN telemetry e ON e.tick_id = t.id;

-- each participant as last recorded in the session
CREATE VIEW IF NOT EXISTS v_results AS
SELECT p.session_id, p.idx, d.name, d.nationality, v.name AS car, v.class AS car_class,
    m.race_pos AS pos, m.laps_completed,
    CASE WHEN cs.best_lap > 0 THEN cs.best_lap END AS best_lap,
    CASE WHEN cs.last_lap > 0 THEN cs.last_lap END AS last_lap,
    rs.name AS race_state, p.is_player
FROM participant p
JOIN driver d ON d.id = p.driver_id
JOIN vehicle v ON v.id = p.vehicle_id
LEFT JOIN car_motion m ON m.idx = p.idx AND m.tick_id = (
    SELECT MAX(tick_id) FROM car_motion WHERE idx = p.idx
    AND tick_id BETWEEN (SELECT MIN(id) FROM tick WHERE session_id = p.session_id)
                    AND (SELECT MAX(id) FROM tick WHERE session_id = p.session_id))
LEFT JOIN car_status cs ON cs.idx = p.idx AND cs.tick_id = (
    SELECT MAX(tick_id) FROM car_status WHERE idx = p.idx
    AND tick_id BETWEEN (SELECT MIN(id) FROM tick WHERE session_id = p.session_id)
                    AND (SELECT MAX(id) FROM tick WHERE session_id = p.session_id))
LEFT JOIN enum_name rs ON rs.type = 'race_state' AND rs.code = cs.race_state;
"""

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_of(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_path(day):
    if not DAY_RE.match(day):
        raise ValueError("bad day %r" % day)
    return DAYS_DIR / f"{day}.sqlite"


def _math(con):
    """atan2/degrees for v_samples, in case SQLite was built without math functions."""
    con.create_function("atan2", 2, math.atan2, deterministic=True)
    con.create_function("degrees", 1, math.degrees, deterministic=True)


def _move_old_schema(path):
    """Databases from before the full-telemetry schema are renamed to
    YYYY-MM-DD.v1.sqlite (no longer listed) rather than mixed with new data."""
    if not path.exists():
        return
    con = sqlite3.connect(path)
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='samples' AND type='table'").fetchone():
            return
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    path.replace(path.with_name(path.stem + ".v1.sqlite"))


def open_day(day):
    """Read-write connection used by the recorder; creates the file."""
    DAYS_DIR.mkdir(parents=True, exist_ok=True)
    path = day_path(day)
    _move_old_schema(path)
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.executemany("INSERT OR IGNORE INTO enum_name VALUES (?,?,?)",
                    [(t, code, name) for t, names in ENUMS.items()
                     for code, name in (names.items() if isinstance(names, dict) else enumerate(names))])
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()
    _math(con)
    return con


def read_day(day):
    """Connection for the web app. Not opened read-only: WAL readers need the -shm file."""
    path = day_path(day)
    if not path.exists():
        raise FileNotFoundError(day)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    _math(con)
    return con


def delete_session(con, sid):
    for table in TICK_TABLES:
        con.execute(f"DELETE FROM {table} WHERE tick_id IN (SELECT id FROM tick WHERE session_id=?)", (sid,))
    for table in ("tick", "lap", "participant"):
        con.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
    con.execute("DELETE FROM session WHERE id=?", (sid,))


def list_days():
    if not DAYS_DIR.exists():
        return []
    return sorted((p.stem for p in DAYS_DIR.glob("*.sqlite") if DAY_RE.match(p.stem)), reverse=True)


def parse_ref(ref):
    day, _, sid = ref.partition(":")
    return day, int(sid)


def all_sessions():
    out = []
    for day in list_days():
        try:
            con = read_day(day)
            rows = con.execute("SELECT * FROM v_sessions ORDER BY started_at DESC").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for r in rows:
            d = dict(r)
            d["ref"] = f"{day}:{d['id']}"
            d["day"] = day
            out.append(d)
    return out


def track_key(track, variation):
    raw = f"{track}__{variation}".lower()
    return re.sub(r"[^a-z0-9_]+", "-", raw).strip("-") or "unknown"


def load_track(track, variation):
    p = TRACKS_DIR / (track_key(track, variation) + ".json")
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save_track(track, variation, outline):
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    p = TRACKS_DIR / (track_key(track, variation) + ".json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(outline))
    tmp.replace(p)


def load_wheels():
    try:
        return json.loads(WHEELS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_wheels(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WHEELS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(WHEELS_FILE)
