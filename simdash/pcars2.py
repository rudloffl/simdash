"""Read the AMS2 / Project CARS 2 shared memory ($pcars2$) that pcars2bridge
copies into /dev/shm.

Field offsets come from pcars2_offsets.json, generated from simapi's
pcars2data.h by gen_offsets.c, so they match exactly what simd/monocoque read.
"""

import json
import math
import mmap
import os
import struct
import time
from pathlib import Path

SHM_PATH = os.environ.get("SIMDASH_SHM", "/dev/shm/$pcars2$")
OFF = json.loads((Path(__file__).resolve().parent.parent / "pcars2_offsets.json").read_text())

MAX_PARTICIPANTS = 64
STR_LEN = 64

GAME_STATES = ["exited", "front end", "playing", "paused", "menu (clock running)",
               "restarting", "replay", "front end replay"]
SESSION_TYPES = ["Invalid", "Practice", "Test", "Qualify", "Formation lap", "Race", "Time attack"]
RACE_STATES = ["invalid", "not started", "racing", "finished", "disqualified", "retired", "DNF"]
PIT_MODES = ["", "pit entry", "in pit", "pit exit", "garage", "garage exit"]
PIT_SCHEDULES = ["none", "player requested", "engineer requested", "damage requested", "mandatory",
                 "drive through", "stop go", "pitspot occupied"]
FLAG_COLOURS = ["none", "green", "blue", "white (slow car)", "white (final lap)", "red", "yellow",
                "double yellow", "black and white", "black orange circle", "black", "chequered"]
FLAG_REASONS = ["none", "solo crash", "vehicle crash", "vehicle obstruction"]
CRASH_STATES = ["none", "off track", "large prop", "spinning", "rolling"]
TERRAINS = ["road", "low grip road", "bumpy road 1", "bumpy road 2", "bumpy road 3", "marbles",
            "grassy berms", "grass", "gravel", "bumpy gravel", "rumble strips", "drains", "tyrewalls",
            "cement walls", "guardrails", "sand", "bumpy sand", "dirt", "bumpy dirt", "dirt road",
            "bumpy dirt road", "pavement", "dirt bank", "wood", "dry verge", "exit rumble strips",
            "grasscrete", "long grass", "slope grass", "cobbles", "sand road", "baked clay", "astroturf",
            "snow half", "snow full", "damaged road 1", "train track road", "bumpy cobbles", "aries only",
            "orion only", "b1 rumbles", "b2 rumbles", "rough sand medium", "rough sand heavy",
            "snow walls", "ice road", "runoff road", "illegal strip", "paint concrete",
            "paint concrete illegal", "rally tarmac"]
ERS_MODES = ["none", "off", "build", "balanced", "attack", "qual"]
YELLOW_FLAG_STATES = {-1: "invalid", 0: "none", 1: "pending", 2: "pits closed", 3: "pit lead lap",
                      4: "pits open", 5: "pits open 2", 6: "last lap", 7: "resume", 8: "race halt"}

# Every enum by the name the database's enum_name table uses
ENUMS = {
    "game_state": GAME_STATES, "session_state": SESSION_TYPES, "race_state": RACE_STATES,
    "pit_mode": PIT_MODES, "pit_schedule": PIT_SCHEDULES, "flag_colour": FLAG_COLOURS,
    "flag_reason": FLAG_REASONS, "crash_state": CRASH_STATES, "terrain": TERRAINS,
    "ers_mode": ERS_MODES, "yellow_flag_state": YELLOW_FLAG_STATES,
}

GAME_PLAYING = 2
GAME_PAUSED = 3
GAME_MENU_TICKING = 4
GAME_RESTARTING = 5
PIT_IN_GARAGE = 4

G = 9.80665

# Every field of pcars2APIStruct after mParticipantInfo, in struct order:
# (name, kind, count, per_car). kind: f float, i int, I uint, ? bool, s 64-char
# string, c 40-char tyre compound. per_car arrays hold one value per participant
# (count values each); the others are scalars (count 1) or vectors / wheels.
FIELDS = [
    ("mVersion", "I", 1, False), ("mBuildVersionNumber", "I", 1, False),
    ("mGameState", "I", 1, False), ("mSessionState", "I", 1, False), ("mRaceState", "I", 1, False),
    ("mViewedParticipantIndex", "i", 1, False), ("mNumParticipants", "i", 1, False),
    ("mUnfilteredThrottle", "f", 1, False), ("mUnfilteredBrake", "f", 1, False),
    ("mUnfilteredSteering", "f", 1, False), ("mUnfilteredClutch", "f", 1, False),
    ("mCarName", "s", 1, False), ("mCarClassName", "s", 1, False), ("mLapsInEvent", "I", 1, False),
    ("mTrackLocation", "s", 1, False), ("mTrackVariation", "s", 1, False), ("mTrackLength", "f", 1, False),
    ("mNumSectors", "i", 1, False), ("mLapInvalidated", "?", 1, False),
    ("mBestLapTime", "f", 1, False), ("mLastLapTime", "f", 1, False), ("mCurrentTime", "f", 1, False),
    ("mSplitTimeAhead", "f", 1, False), ("mSplitTimeBehind", "f", 1, False), ("mSplitTime", "f", 1, False),
    ("mEventTimeRemaining", "f", 1, False),
    ("mPersonalFastestLapTime", "f", 1, False), ("mWorldFastestLapTime", "f", 1, False),
    ("mCurrentSector1Time", "f", 1, False), ("mCurrentSector2Time", "f", 1, False),
    ("mCurrentSector3Time", "f", 1, False),
    ("mFastestSector1Time", "f", 1, False), ("mFastestSector2Time", "f", 1, False),
    ("mFastestSector3Time", "f", 1, False),
    ("mPersonalFastestSector1Time", "f", 1, False), ("mPersonalFastestSector2Time", "f", 1, False),
    ("mPersonalFastestSector3Time", "f", 1, False),
    ("mWorldFastestSector1Time", "f", 1, False), ("mWorldFastestSector2Time", "f", 1, False),
    ("mWorldFastestSector3Time", "f", 1, False),
    ("mHighestFlagColour", "I", 1, False), ("mHighestFlagReason", "I", 1, False),
    ("mPitMode", "I", 1, False), ("mPitSchedule", "I", 1, False),
    ("mCarFlags", "I", 1, False), ("mOilTempCelsius", "f", 1, False), ("mOilPressureKPa", "f", 1, False),
    ("mWaterTempCelsius", "f", 1, False), ("mWaterPressureKPa", "f", 1, False),
    ("mFuelPressureKPa", "f", 1, False), ("mFuelLevel", "f", 1, False), ("mFuelCapacity", "f", 1, False),
    ("mSpeed", "f", 1, False), ("mRpm", "f", 1, False), ("mMaxRPM", "f", 1, False),
    ("mBrake", "f", 1, False), ("mThrottle", "f", 1, False), ("mClutch", "f", 1, False),
    ("mSteering", "f", 1, False), ("mGear", "i", 1, False), ("mNumGears", "i", 1, False),
    ("mOdometerKM", "f", 1, False), ("mAntiLockActive", "?", 1, False),
    ("mLastOpponentCollisionIndex", "i", 1, False), ("mLastOpponentCollisionMagnitude", "f", 1, False),
    ("mBoostActive", "?", 1, False), ("mBoostAmount", "f", 1, False),
    ("mOrientation", "f", 3, False), ("mLocalVelocity", "f", 3, False), ("mWorldVelocity", "f", 3, False),
    ("mAngularVelocity", "f", 3, False), ("mLocalAcceleration", "f", 3, False),
    ("mWorldAcceleration", "f", 3, False), ("mExtentsCentre", "f", 3, False),
    ("mTyreFlags", "I", 4, False), ("mTerrain", "I", 4, False), ("mTyreY", "f", 4, False),
    ("mTyreRPS", "f", 4, False), ("mTyreSlipSpeed", "f", 4, False), ("mTyreTemp", "f", 4, False),
    ("mTyreGrip", "f", 4, False), ("mTyreHeightAboveGround", "f", 4, False),
    ("mTyreLateralStiffness", "f", 4, False), ("mTyreWear", "f", 4, False),
    ("mBrakeDamage", "f", 4, False), ("mSuspensionDamage", "f", 4, False),
    ("mBrakeTempCelsius", "f", 4, False), ("mTyreTreadTemp", "f", 4, False),
    ("mTyreLayerTemp", "f", 4, False), ("mTyreCarcassTemp", "f", 4, False),
    ("mTyreRimTemp", "f", 4, False), ("mTyreInternalAirTemp", "f", 4, False),
    ("mCrashState", "I", 1, False), ("mAeroDamage", "f", 1, False), ("mEngineDamage", "f", 1, False),
    ("mAmbientTemperature", "f", 1, False), ("mTrackTemperature", "f", 1, False),
    ("mRainDensity", "f", 1, False), ("mWindSpeed", "f", 1, False), ("mWindDirectionX", "f", 1, False),
    ("mWindDirectionY", "f", 1, False), ("mCloudBrightness", "f", 1, False),
    ("mSequenceNumber", "I", 1, False),
    ("mWheelLocalPositionY", "f", 4, False), ("mSuspensionTravel", "f", 4, False),
    ("mSuspensionVelocity", "f", 4, False), ("mAirPressure", "f", 4, False),
    ("mEngineSpeed", "f", 1, False), ("mEngineTorque", "f", 1, False), ("mWings", "f", 2, False),
    ("mHandBrake", "f", 1, False),
    ("mCurrentSector1Times", "f", 1, True), ("mCurrentSector2Times", "f", 1, True),
    ("mCurrentSector3Times", "f", 1, True),
    ("mFastestSector1Times", "f", 1, True), ("mFastestSector2Times", "f", 1, True),
    ("mFastestSector3Times", "f", 1, True),
    ("mFastestLapTimes", "f", 1, True), ("mLastLapTimes", "f", 1, True),
    ("mLapsInvalidated", "?", 1, True), ("mRaceStates", "I", 1, True), ("mPitModes", "I", 1, True),
    ("mOrientations", "f", 3, True), ("mSpeeds", "f", 1, True),
    ("mCarNames", "s", 1, True), ("mCarClassNames", "s", 1, True),
    ("mEnforcedPitStopLap", "i", 1, False),
    ("mTranslatedTrackLocation", "s", 1, False), ("mTranslatedTrackVariation", "s", 1, False),
    ("mBrakeBias", "f", 1, False), ("mTurboBoostPressure", "f", 1, False), ("mTyreCompound", "c", 4, False),
    ("mPitSchedules", "I", 1, True), ("mHighestFlagColours", "I", 1, True),
    ("mHighestFlagReasons", "I", 1, True), ("mNationalities", "I", 1, True),
    ("mSnowDensity", "f", 1, False),
    ("mSessionDuration", "f", 1, False), ("mSessionAdditionalLaps", "i", 1, False),
    ("mTyreTempLeft", "f", 4, False), ("mTyreTempCenter", "f", 4, False), ("mTyreTempRight", "f", 4, False),
    ("mDrsState", "I", 1, False), ("mRideHeight", "f", 4, False),
    ("mJoyPad0", "I", 1, False), ("mDPad", "I", 1, False),
    ("mAntiLockSetting", "i", 1, False), ("mTractionControlSetting", "i", 1, False),
    ("mErsDeploymentMode", "i", 1, False), ("mErsAutoModeEnabled", "?", 1, False),
    ("mClutchTemp", "f", 1, False), ("mClutchWear", "f", 1, False),
    ("mClutchOverheated", "?", 1, False), ("mClutchSlipping", "?", 1, False),
    ("mYellowFlagState", "i", 1, False),
]
# ParticipantInfo struct fields (offsets are p_<name> in the JSON)
PARTICIPANT_FIELDS = [
    ("mIsActive", "?", 1), ("mName", "s", 1), ("mWorldPosition", "f", 3),
    ("mCurrentLapDistance", "f", 1), ("mRacePosition", "I", 1), ("mLapsCompleted", "I", 1),
    ("mCurrentLap", "I", 1), ("mCurrentSector", "i", 1),
]
KIND_SIZE = {"f": 4, "i": 4, "I": 4, "?": 1, "s": STR_LEN, "c": 40}
# field name -> kind, for the recorder's column types
KINDS = {name: kind for name, kind, *_ in FIELDS + PARTICIPANT_FIELDS}

missing = [n for n, *_ in FIELDS if n not in OFF] + [n for n, *_ in PARTICIPANT_FIELDS if "p_" + n not in OFF]
if missing:
    raise RuntimeError("pcars2_offsets.json lacks %s: run `make offsets`" % ", ".join(missing))


def _name(lst, i):
    if isinstance(lst, dict):
        return lst.get(i, str(i))
    return lst[i] if 0 <= i < len(lst) else str(i)


class Pcars2Reader:
    """Memory-maps $pcars2$ and returns decoded snapshots.

    The bridge can recreate the file when AMS2 restarts, so the inode is
    checked every couple of seconds and the mapping reopened if it changed.
    """

    def __init__(self, path=SHM_PATH):
        self.path = path
        self.mm = None
        self.inode = None
        self.last_check = 0.0
        self.size = OFF["total_size"]

    def _open(self):
        self.close()
        try:
            with open(self.path, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_size < self.size:
                    return False
                self.mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                self.inode = st.st_ino
            return True
        except OSError:
            return False

    def close(self):
        if self.mm is not None:
            self.mm.close()
        self.mm = None
        self.inode = None

    def _raw(self):
        now = time.monotonic()
        if self.mm is None or now - self.last_check > 2.0:
            self.last_check = now
            try:
                ino = os.stat(self.path).st_ino
            except OSError:
                self.close()
                return None
            if ino != self.inode and not self._open():
                return None
        # mSequenceNumber is odd while the game is writing; retry for a consistent copy
        seq_off = OFF["mSequenceNumber"]
        buf = None
        for _ in range(8):
            s1 = struct.unpack_from("<I", self.mm, seq_off)[0]
            if s1 & 1:
                time.sleep(0.0005)
                continue
            buf = self.mm[: self.size]
            if struct.unpack_from("<I", buf, seq_off)[0] == s1:
                return buf
        return buf if buf is not None else self.mm[: self.size]

    def read(self):
        """Return a snapshot dict, or None if the shared memory isn't there."""
        buf = self._raw()
        if buf is None:
            return None
        return decode(buf)


def _str(buf, off, n=STR_LEN):
    return buf[off: off + n].split(b"\0", 1)[0].decode("utf-8", "replace")


def _unpack(buf, off, kind, count):
    """count values of one kind at off; a scalar when count is 1."""
    if kind in "sc":
        n = KIND_SIZE[kind]
        vals = [_str(buf, off + k * n, n) for k in range(count)]
    elif kind == "?":
        vals = [b != 0 for b in buf[off: off + count]]
    else:
        vals = list(struct.unpack_from("<%d%s" % (count, kind), buf, off))
    return vals[0] if count == 1 else vals


def decode_raw(buf):
    """Every field, by its pcars2data.h name.

    Returns (raw, cars): raw maps each non per-car field to a value or list;
    cars is one dict per participant (< mNumParticipants) holding its
    ParticipantInfo fields plus its entry of every per-car array.
    """
    raw = {}
    per_car = []
    for name, kind, count, pc in FIELDS:
        if pc:
            per_car.append((name, kind, count))
        else:
            raw[name] = _unpack(buf, OFF[name], kind, count)
    num = max(0, min(raw["mNumParticipants"], MAX_PARTICIPANTS))
    psize = OFF["participant_size"]
    base = OFF["mParticipantInfo"]
    cars = []
    for k in range(num):
        p = base + k * psize
        car = {name: _unpack(buf, p + OFF["p_" + name], kind, count) for name, kind, count in PARTICIPANT_FIELDS}
        for name, kind, count in per_car:
            car[name] = _unpack(buf, OFF[name] + k * count * KIND_SIZE[kind], kind, count)
        cars.append(car)
    return raw, cars


def decode(buf):
    """Snapshot for the live view and the recorder: friendly keys, plus the
    untouched fields under "raw" and each car's under "raw" in that car."""
    r, raw_cars = decode_raw(buf)
    num = len(raw_cars)
    viewed = r["mViewedParticipantIndex"]
    if not 0 <= viewed < num:
        viewed = 0

    cars = []
    for k, c in enumerate(raw_cars):
        x, y, z = c["mWorldPosition"]
        cars.append({
            "idx": k,
            "active": c["mIsActive"],
            "name": c["mName"],
            "x": x, "y": y, "z": z,
            "lap_dist": c["mCurrentLapDistance"],
            "pos": c["mRacePosition"],
            "laps_completed": c["mLapsCompleted"],
            "lap": c["mCurrentLap"],
            "sector": c["mCurrentSector"],
            "best_lap": c["mFastestLapTimes"],
            "last_lap": c["mLastLapTimes"],
            "lap_invalid": c["mLapsInvalidated"],
            "race_state": c["mRaceStates"],
            "pit_mode": c["mPitModes"],
            "speed": c["mSpeeds"],
            "car": c["mCarNames"],
            "car_class": c["mCarClassNames"],
            "raw": c,
        })

    return {
        "raw": r,
        "version": r["mVersion"],
        "game_state": r["mGameState"],
        "session_state": r["mSessionState"],
        "race_state": r["mRaceState"],
        "viewed": viewed,
        "num_cars": num,
        "cars": cars,
        "car": r["mCarName"],
        "car_class": r["mCarClassName"],
        "track": r["mTranslatedTrackLocation"] or r["mTrackLocation"],
        "variation": r["mTranslatedTrackVariation"] or r["mTrackVariation"],
        "track_length": r["mTrackLength"],
        "laps_in_event": r["mLapsInEvent"],
        "session_duration": r["mSessionDuration"],
        "event_time_remaining": r["mEventTimeRemaining"],
        "lap_invalid": r["mLapInvalidated"],
        "best_lap": r["mBestLapTime"],
        "last_lap": r["mLastLapTime"],
        "current_time": r["mCurrentTime"],
        "s1": r["mCurrentSector1Time"],
        "s2": r["mCurrentSector2Time"],
        "s3": r["mCurrentSector3Time"],
        "flag": r["mHighestFlagColour"],
        "pit_mode": r["mPitMode"],
        "car_flags": r["mCarFlags"],
        "fuel_level": r["mFuelLevel"],
        "fuel_capacity": r["mFuelCapacity"],
        "speed": r["mSpeed"],
        "rpm": r["mRpm"],
        "max_rpm": r["mMaxRPM"],
        "brake": r["mBrake"],
        "throttle": r["mThrottle"],
        "clutch": r["mClutch"],
        "steering": r["mSteering"],
        "gear": r["mGear"],
        "abs": r["mAntiLockActive"],
        "orientation": r["mOrientation"],
        "local_vel": r["mLocalVelocity"],
        "ang_vel": r["mAngularVelocity"],
        "local_acc": r["mLocalAcceleration"],
        "terrain": r["mTerrain"],
        "tyre_rps": r["mTyreRPS"],
        "tyre_slip_speed": r["mTyreSlipSpeed"],
        "tyre_wear": r["mTyreWear"],
        "brake_temp": r["mBrakeTempCelsius"],
        "tyre_temp": [t - 273.15 if t > 0 else 0.0 for t in r["mTyreTreadTemp"]],
        "air_temp": r["mAmbientTemperature"],
        "track_temp": r["mTrackTemperature"],
        "rain": r["mRainDensity"],
    }


def derived(snap):
    """Car-frame G-forces and body slip angle.

    pCARS local frame: X right, Y up, -Z forward (simapi negates Z for forward
    speed). So g_long > 0 is accelerating, g_lat > 0 is accelerating to the right.
    """
    ax, ay, az = snap["local_acc"]
    vx, _, vz = snap["local_vel"]
    fwd = -vz
    slip_angle = math.degrees(math.atan2(vx, fwd)) if fwd > 5.0 else 0.0
    return {"g_lat": ax / G, "g_long": -az / G, "g_vert": ay / G, "slip_angle": slip_angle}
