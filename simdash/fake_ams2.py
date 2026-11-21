"""Fake AMS2 shared memory for testing simdash without the game.

    SIMDASH_SHM=/dev/shm/simdash-fake python -m simdash.fake_ams2 [--laps 3] [--cars 6]

Writes a $pcars2$-layout file (never the real /dev/shm/$pcars2$ unless you
point it there) with a few cars lapping a made-up circuit. After --laps laps
the race finishes and the game goes back to the front end.

For the haptics, every lap of the player has (see SCRIPT): sustained cornering g in the bends (the pace is set
for 1.6 g), a kerb on the left wheels, an ABS stop into the slowest bend and a wall hit on the left; the
suspension moves a little all the time (mSuspensionVelocity) and a lot on the kerb.
"""

import argparse
import math
import mmap
import os
import random
import struct
import time

from .pcars2 import OFF, STR_LEN

SIZE = 102288
TICK = 1 / 60
G = 9.81
# where on each lap (fraction of the lap) the scripted haptics events happen, for the player
SCRIPT = {"kerb": (0.20, 0.215), "wall": 0.60}
KERB = 10        # AMS2 terrain: rumble strips
JERK = 60.0      # m/s³: how fast the cars change from braking to accelerating


def track_point(u):
    a = 2 * math.pi * u
    return 420 * math.cos(a) + 90 * math.cos(3 * a), 260 * math.sin(a) + 40 * math.sin(2 * a)


def build_track(n=4000):
    pts = [track_point(i / n) for i in range(n + 1)]
    dist = [0.0]
    for i in range(1, len(pts)):
        dist.append(dist[-1] + math.dist(pts[i - 1], pts[i]))
    return pts, dist


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laps", type=int, default=3)
    ap.add_argument("--cars", type=int, default=6)
    ap.add_argument("--edges", action="store_true",
                    help="the player weaves across a 12 m wide track (6 m each side), tyres going off: for track edge mapping")
    ap.add_argument("--speedup", type=float, default=1.0, help="run the clock faster than real time")
    args = ap.parse_args()
    path = os.environ.get("SIMDASH_SHM", "/dev/shm/simdash-fake")
    if path == "/dev/shm/$pcars2$":
        raise SystemExit("refusing to overwrite the real AMS2 shared memory")

    pts, dist = build_track()
    length = dist[-1]

    def at(d):
        d %= length
        lo, hi = 0, len(dist) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            lo, hi = (mid, hi) if dist[mid] <= d else (lo, mid)
        f = (d - dist[lo]) / (dist[hi] - dist[lo])
        (x0, z0), (x1, z1) = pts[lo], pts[hi]
        return x0 + f * (x1 - x0), z0 + f * (z1 - z0), math.atan2(z1 - z0, x1 - x0)

    def curvature(d):
        _, _, h0 = at(d - 15)
        _, _, h1 = at(d + 15)
        dh = (h1 - h0 + math.pi) % (2 * math.pi) - math.pi
        return dh / 30

    with open(path, "wb") as f:
        f.write(b"\0" * SIZE)
    fd = os.open(path, os.O_RDWR)
    mm = mmap.mmap(fd, SIZE)

    def put(name, fmt, *vals, idx=0):
        struct.pack_into("<" + fmt, mm, OFF[name] + idx * struct.calcsize("<" + fmt), *vals)

    def put_str(off, s):
        b = s.encode()[: STR_LEN - 1]
        mm[off: off + STR_LEN] = b + b"\0" * (STR_LEN - len(b))

    n = args.cars
    names = ["You"] + [f"AI Driver {i}" for i in range(1, n)]
    pace = [1.0] + [random.uniform(0.93, 1.04) for _ in range(1, n)]
    pos_d = [-(i * 12.0) for i in range(n)]  # grid behind the line
    laps_done = [0] * n
    lap_start = [0.0] * n
    best = [-1.0] * n
    last = [0.0] * n
    speed = [0.0] * n
    accel = [0.0] * n     # m/s², ramped: a real car does not go from full braking to full throttle in one sample
    seq = 0
    t = 0.0
    prev_v = 0.0
    hit_lap = -1          # the lap the wall was last hit on
    knock = 0.0           # s left of the sideways push after the hit
    k_radius = 0.33

    put("mVersion", "I", 13)
    put_str(OFF["mCarName"], "Fake GT3")
    put_str(OFF["mCarClassName"], "GT3")
    put_str(OFF["mTrackLocation"], "Fakering")
    put_str(OFF["mTrackVariation"], "GP")
    put("mTrackLength", "f", length)
    put("mLapsInEvent", "I", args.laps)
    put("mNumParticipants", "i", n)
    put("mViewedParticipantIndex", "i", 0)
    put("mFuelCapacity", "f", 100.0)
    put("mMaxRPM", "f", 8500.0)
    put("mAmbientTemperature", "f", 21.0)
    put("mTrackTemperature", "f", 29.0)
    for i in range(n):
        put_str(OFF["mCarNames"] + i * STR_LEN, "Fake GT3")
        put_str(OFF["mCarClassNames"] + i * STR_LEN, "GT3")
    put("mGameState", "I", 2)
    put("mSessionState", "I", 5)
    put("mRaceState", "I", 2)
    print(f"fake AMS2 at {path}: {n} cars, {args.laps} laps of {length:.0f} m")

    finished = False
    try:
        while not finished:
            dt = TICK * args.speedup
            t += dt
            for i in range(n):
                c = abs(curvature(pos_d[i]))
                target = pace[i] * min(75.0, math.sqrt(1.6 * 9.81 / max(c, 1e-4)))
                want = 9.0 if target > speed[i] + 0.5 else -14.0 if target < speed[i] - 0.5 else 0.0
                accel[i] += max(-JERK * dt, min(JERK * dt, want - accel[i]))
                a = accel[i]
                speed[i] = max(0.0, min(target, speed[i] + a * dt)) if a > 0 else max(target, speed[i] + a * dt)
                pos_d[i] += speed[i] * dt
                if pos_d[i] >= (laps_done[i] + 1) * length:
                    laps_done[i] += 1
                    lt = t - lap_start[i]
                    lap_start[i] = t
                    last[i] = lt
                    if laps_done[i] > 1 and (best[i] < 0 or lt < best[i]):
                        best[i] = lt
            if laps_done[0] >= args.laps:
                finished = True

            seq += 1
            put("mSequenceNumber", "I", seq)
            order = sorted(range(n), key=lambda i: -pos_d[i])
            for i in range(n):
                p = OFF["mParticipantInfo"] + i * OFF["participant_size"]
                x, z, _ = at(pos_d[i])
                mm[p + OFF["p_mIsActive"]] = 1
                put_str(p + OFF["p_mName"], names[i])
                struct.pack_into("<3f", mm, p + OFF["p_mWorldPosition"], x, 0.0, z)
                struct.pack_into("<f", mm, p + OFF["p_mCurrentLapDistance"], pos_d[i] % length if pos_d[i] >= 0 else length + pos_d[i])
                struct.pack_into("<I", mm, p + OFF["p_mRacePosition"], order.index(i) + 1)
                struct.pack_into("<I", mm, p + OFF["p_mLapsCompleted"], laps_done[i])
                struct.pack_into("<I", mm, p + OFF["p_mCurrentLap"], laps_done[i] + 1)
                put("mFastestLapTimes", "f", best[i], idx=i)
                put("mLastLapTimes", "f", last[i], idx=i)
                put("mSpeeds", "f", speed[i], idx=i)
                put("mRaceStates", "I", 2, idx=i)

            if args.edges:
                # weave from 7.5 m left to 7.5 m right of the centre line; tyres beyond 6 m are on the grass
                x0, z0, _ = at(pos_d[0])
                x1, z1, _ = at(pos_d[0] + 1)
                fl = math.hypot(x1 - x0, z1 - z0) or 1
                fx, fz = (x1 - x0) / fl, (z1 - z0) / fl
                lx, lz = -fz, fx          # AMS2: left of travel (see edges.py)
                lat = 7.5 * math.sin(pos_d[0] / 60)
                p = OFF["mParticipantInfo"]
                struct.pack_into("<3f", mm, p + OFF["p_mWorldPosition"], x0 + lx * lat, 0.0, z0 + lz * lat)
                put("mOrientation", "3f", 0.0, math.atan2(-fx, -fz), 0.0)
                terr = []
                for side in (1, -1, 1, -1):   # FL FR RL RR
                    w = abs(lat + 0.8 * side)
                    terr.append(7 if w > 6 else 10 if w > 5 else 0)  # grass, rumble strips, road
                put("mTerrain", "4I", *terr)

            # the wall hit: once a lap, the car is pushed right (the wall is on its left) and loses 15 % of its speed
            u = (pos_d[0] % length) / length if pos_d[0] >= 0 else 0.0
            wall_now = laps_done[0] != hit_lap and u >= SCRIPT["wall"] and speed[0] > 10
            if wall_now:
                hit_lap = laps_done[0]
                knock = 0.15
                speed[0] *= 0.85
            v = speed[0]
            acc = (v - prev_v) / dt
            prev_v = v
            c = curvature(pos_d[0])
            braking = acc < -1
            cruising = abs(acc) < 0.1 and abs(c) < 0.0008
            thr = 0.0 if braking or cruising else min(1.0, 0.35 + acc / 9.0)
            brk = min(1.0, -acc / 14.0) if braking else 0.0
            # some wheelspin on the rears when flat out at low speed, some lock-up under heavy braking
            spin = 0.12 if thr > 0.9 and v < 35 else 0.0
            lock = -0.15 if brk > 0.9 and random.random() < 0.3 else 0.0
            rps = [v / k_radius * (1 + lock), v / k_radius * (1 + lock),
                   v / k_radius * (1 + spin), v / k_radius * (1 + spin)]
            put("mSpeed", "f", v)
            put("mRpm", "f", 3000 + 5000 * (v % 20) / 20)
            put("mGear", "i", 1 + min(5, int(v / 14)))
            put("mThrottle", "f", thr)
            put("mBrake", "f", brk)
            put("mSteering", "f", max(-1.0, min(1.0, c * 40)))
            put("mFuelLevel", "f", 0.8 - 0.0001 * t)
            push = 3.0 * knock / 0.15 if knock > 0 else 0.0   # m/s to the right, fading
            knock = max(0.0, knock - dt)
            put("mLocalVelocity", "3f", 1.5 * c * v - push, 0.0, -v)
            if wall_now:  # one sample of the hit: 15 g to the right, and the speed lost; local x points left
                put("mLocalAcceleration", "3f", -15 * G, 0.0, 0.0)
            else:
                # as a car could: the grip caps the cornering (the made-up track's hairpins would give 10 g at the
                # fake's speeds), and the forward/backward part is the ramped acceleration, not the speed's jumps
                put("mLocalAcceleration", "3f", max(-1.8 * G, min(1.8 * G, v * v * c)), 0.0, -accel[0])
            # ABS: working while the brake is hard on (the stops into the slow bends)
            put("mAntiLockActive", "?", brk > 0.8)
            # suspension: a little road texture all the time, the kerb shakes the left wheels
            on_kerb = SCRIPT["kerb"][0] <= u < SCRIPT["kerb"][1]
            sv = [random.uniform(-0.03, 0.03) for _ in range(4)]
            if on_kerb:
                bump = 0.35 * math.sin(2 * math.pi * 12 * t)
                sv[0], sv[2] = bump, -bump
            put("mSuspensionVelocity", "4f", *sv)
            if not args.edges:
                put("mTerrain", "4I", KERB if on_kerb else 0, 0, KERB if on_kerb else 0, 0)
            put("mAngularVelocity", "3f", 0.0, v * c, 0.0)
            put("mTyreRPS", "4f", *rps)
            put("mTyreTreadTemp", "4f", 360.0, 361.0, 358.0, 359.0)
            put("mBrakeTempCelsius", "4f", 400.0, 405.0, 300.0, 302.0)
            put("mCurrentTime", "f", t - lap_start[0])
            put("mLastLapTime", "f", last[0])
            put("mBestLapTime", "f", best[0])
            ld = pos_d[0] % length
            put("mCurrentSector1Time", "f", (t - lap_start[0]) if ld > length / 3 else -1.0)
            put("mEventTimeRemaining", "f", -1.0)
            seq += 1
            put("mSequenceNumber", "I", seq)
            time.sleep(TICK)
        put("mRaceState", "I", 3)
        time.sleep(2)
        put("mGameState", "I", 1)
        print("race finished, back to front end")
        time.sleep(3)
    finally:
        mm.close()
        os.close(fd)


if __name__ == "__main__":
    main()
