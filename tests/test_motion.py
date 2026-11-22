import math
import os
import threading

import pytest

from simdash import motion

G = dict(motion.DEFAULTS["geometry"])


def test_home_is_all_horns_level():
    assert all(abs(r["alpha"]) < 1e-9 for r in motion.ik({}, G))


@pytest.mark.parametrize("pose", [{"x": 12}, {"y": -9, "z": 6}, {"roll": 10, "yaw": -20}, {"pitch": 15, "x": 5, "z": -4}])
def test_rods_keep_their_length(pose):
    for r in motion.ik(pose, G):
        assert r["alpha"] is not None
        assert math.dist(r["A"], r["P"]) == pytest.approx(G["rod"], abs=1e-6)


def test_pitch_nose_up_lifts_the_front_pair():
    a = [r["alpha"] for r in motion.ik({"pitch": 10}, G)]
    assert a[0] > 5 and a[1] > 5          # first pair sits under the nose
    assert a[0] == pytest.approx(a[1])    # left/right symmetric


def test_roll_is_symmetric_and_reach_is_sane():
    rc = motion.reach(G)
    assert rc["roll"][0] == pytest.approx(rc["roll"][1], abs=0.2)
    assert rc["y"][0] == pytest.approx(rc["y"][1], abs=0.2)
    assert rc["z"][0] > 5 and rc["pitch"][1] > 5


def test_out_of_reach_is_flagged():
    assert motion.solve({"pitch": 60}, motion.DEFAULTS)["ok"] is False


def test_pulses_follow_direction_and_clamp():
    sv = dict(motion.DEFAULTS["servos"])
    p = motion.pulses([10, 10, None, 0, 500, -500], sv)
    assert p[0] == 1600 and p[1] == 1400 and p[2] == 1500 and p[3] == 1500
    assert p[4] == sv["max_us"] and p[5] == sv["max_us"]  # servo 5 reversed: -500° -> long pulse, clamped


def test_pulse_defaults_stay_off_the_end_stops():
    sv = motion.DEFAULTS["servos"]
    assert (sv["min_us"], sv["max_us"]) == (800, 2200)
    assert motion.pulse_warnings(motion.DEFAULTS) == []        # ±60° at 10 µs/° fits in 800-2200
    wide = {"geometry": {**G, "servo_limit": 80.0}, "servos": sv}
    assert len(motion.pulse_warnings(wide)) == 6                # ±80° needs 700-2300: warned


def test_calibration_points_per_servo():
    sv = {**motion.DEFAULTS["servos"], "plus45_us": [2000] + motion.DEFAULTS["servos"]["plus45_us"][1:],
          "minus45_us": [1100] + motion.DEFAULTS["servos"]["minus45_us"][1:]}
    assert motion.raw_pulse(0, 45, sv) == 2000 and motion.raw_pulse(0, -45, sv) == 1100
    assert motion.raw_pulse(0, 22.5, sv) == 1750                # up: 500 µs per 45°
    assert motion.raw_pulse(0, -22.5, sv) == 1300               # down: 400 µs per 45° (non-linear servo)


def test_old_settings_become_three_points(tmp_path, monkeypatch):
    import json
    f = tmp_path / "motion.json"
    f.write_text(json.dumps({"servos": {"center_us": [1500, 1510, 1500, 1500, 1500, 1500], "us_per_deg": 11.0,
                                        "dir": [1, -1, 1, -1, 1, -1], "min_us": 600, "max_us": 2400}}))
    monkeypatch.setattr(motion, "SETTINGS", f)
    monkeypatch.setitem(motion._cache, "s", None)
    sv = motion.load()["servos"]
    assert sv["plus45_us"][0] == 1500 + 495 and sv["plus45_us"][1] == 1510 - 495 and sv["minus45_us"][1] == 1510 + 495


def test_settings_cache_is_invalidated_by_save(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    monkeypatch.setitem(motion._cache, "s", None)
    assert motion.settings()["geometry"]["horn"] == 18.0
    a = motion.settings()
    assert motion.settings() is a                               # cached: no file read per frame
    motion.save({"geometry": {"horn": 20.0}})
    assert motion.settings()["geometry"]["horn"] == 20.0


def test_near_toggle_and_joint_limits_are_rejected():
    # horn and rod nearly in line at z +17 mm (the review's case), other limits opened up to see it alone
    only = {**G, "joint_max_deg": 180.0, "servo_limit": 90.0}
    p = {"z": 17}
    assert "in line" in motion.why_not(motion.ik(p, only), only, p)
    assert motion.solve(p, motion.DEFAULTS)["ok"] is False
    loose = {**G, "transmission_min_deg": 0.0}
    p = {"yaw": 40}                                              # rods swing far from their home direction
    assert not motion.within(motion.ik(p, loose), loose, p)
    assert "swivels" in motion.why_not(motion.ik(p, loose), loose, p)
    assert motion.within(motion.ik({}, G), G, {})               # home is fine


def test_reach_respects_the_joint_limits():
    loose = {**G, "transmission_min_deg": 0.0, "joint_max_deg": 180.0}
    tight, free = motion.reach(G), motion.reach(loose)
    assert tight["roll"][1] < free["roll"][1] and tight["roll"][1] > 8


def test_template_svg_is_true_scale():
    svg = motion.template_svg("base", G)
    assert 'mm" height="' in svg and "50 mm" in svg


def _fake_board(monkeypatch, hello):
    """A fake pyboard on a pty: answers H with hello (or not at all), records every line."""
    master, slave = os.openpty()
    monkeypatch.setattr(motion, "find_port", lambda: os.ttyname(slave))
    got, stop = [], threading.Event()

    def run():
        import select
        buf = b""
        while not stop.is_set():
            if select.select([master], [], [], 0.02)[0]:
                buf += os.read(master, 1024)
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    got.append(ln.decode())
                    if ln == b"H" and hello:
                        os.write(master, hello)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return got, stop


def test_link_talks_the_firmware_protocol(monkeypatch):
    got, stop = _fake_board(monkeypatch, b"6DOF 2 ch=6\n")
    link = motion.Link()
    home = [1500, 1500, 1500, 1500, 1500, 1500]
    assert link.arm(True, home)
    link.send([1500, 1510, 1520, 1530, 1540, 1550])
    import time
    time.sleep(0.15)
    st = link.status()
    link.arm(False)
    time.sleep(0.05)
    stop.set()
    assert st["connected"] and st["armed"] and st["firmware"] == "6DOF 2 ch=6", st
    # park and home first, then the frames, and O when disarmed
    assert got[:4] == ["H", "A", "P 1500 1500 1500 1500 1500 1500", "S 1500 1500 1500 1500 1500 1500"], got
    assert "S 1500 1510 1520 1530 1540 1550" in got and got[-1] == "O"


def test_link_says_when_the_pca9685_is_missing(monkeypatch):
    got, stop = _fake_board(monkeypatch, b"6DOF 2 ch=6 pca=missing\n")
    link = motion.Link()
    assert link.arm(True, [1500] * 6) is False
    stop.set()
    assert "PCA9685" in link.status()["error"]
    assert not any(ln.startswith(("S", "P")) for ln in got)


@pytest.mark.parametrize("hello", [None, b"6DOF 1 ch=6\n", b">>> \n"])
def test_link_refuses_to_arm_without_a_valid_hello(monkeypatch, hello):
    got, stop = _fake_board(monkeypatch, hello)
    link = motion.Link()
    assert link.arm(True, [1500] * 6) is False
    st = link.status()
    stop.set()
    assert not st["armed"] and not st["connected"] and "firmware" in st["error"]
    assert not any(ln.startswith(("S", "P")) for ln in got)     # nothing reached the servos


def test_link_backs_off_when_the_board_is_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(motion, "find_port", lambda: calls.append(1))
    link = motion.Link()
    with link.lock:
        for _ in range(50):                                     # 50 frames in a second of replay
            link._connect_locked()
    assert len(calls) == 1 and "not found" in link.error


def test_cueing_braking_dips_the_nose_and_washes_out():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS))
    cue.fade = cue.fade_to = 1.0
    brake = {"fwd": -1.0 * motion.G, "left": 0.0, "up": 0.0, "yaw_rate": 0.0}
    for _ in range(50):                      # 1 s of 1 g braking at 50 Hz
        p = cue.step(brake, 0.02)
    assert p["pitch"] < -0.8 * motion.CUEING_DEFAULTS["pitch_per_g"]   # tilt holds
    assert abs(p["x"]) < 0.4 * motion.CUEING_DEFAULTS["surge_per_g"]   # the shift drifted back


def test_cueing_left_turn_leans_right():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS))
    cue.fade = cue.fade_to = 1.0
    for _ in range(50):
        p = cue.step({"fwd": 0.0, "left": 1.2 * motion.G, "up": 0.0, "yaw_rate": 0.0}, 0.02)
    assert p["roll"] > 5


def test_fit_pose_shrinks_into_reach():
    big = {"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 60, "yaw": 0}
    f, s = motion.fit_pose(big, G)
    assert 0 < s < 1 and motion.within(motion.ik(f, G), G, f)


def test_scale_guard_shrinks_fast_and_recovers_slowly():
    g, guard = G, motion.ScaleGuard()
    big = {"pitch": 40}
    _, s1 = guard.apply(big, g, 0.02)
    assert s1 == pytest.approx(motion.fit_scale(big, g))       # never above what fits
    _, s2 = guard.apply({"pitch": 1}, g, 0.02)                  # fits again: recovers, slowly
    assert s1 < s2 < 0.5


def _budget():
    return {ax: tuple(v) for ax, v in motion.reach(G).items()}


def test_soft_limit_keeps_each_axis_inside_its_budget():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS), _budget())
    cue.fade_to = cue.fade = 1.0
    for _ in range(100):
        p = cue.step({"fwd": -3 * motion.G, "left": 0.0, "up": 0.0, "yaw_rate": 0.0}, 0.02)
    b = motion.CUEING_DEFAULTS["budget"] * motion.reach(G)["pitch"][0]
    assert -b <= p["pitch"] < -0.8 * b                          # 3 g of braking: compressed into the budget
    assert motion.soft_limit(1.0, 10, 10) == pytest.approx(10 * math.tanh(0.1))


def test_no_spike_after_a_seek_or_gap_and_the_fade_in():
    """A jump in inputs (pits -> track) after reseed() gives no surge/yaw spike; the pose fades in."""
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS), _budget())
    calm = {"fwd": 0.0, "left": 0.0, "up": 0.0, "yaw_rate": 0.0}
    hard = {"fwd": -1.2 * motion.G, "left": 1.4 * motion.G, "up": 0.0, "yaw_rate": 0.8}
    cue.fade_to = 1.0
    for _ in range(50):
        cue.step(calm, 0.02)
    cue.reseed(hard)                                            # what Player does after a skipped gap / seek
    xs = [cue.step(hard, 0.02) for _ in range(5)]
    assert all(abs(p["x"]) < 0.5 and abs(p["yaw"]) < 0.5 for p in xs)
    # without the reseed the same jump comes out of the high-passes as a spike
    raw = motion.Cueing(dict(motion.CUEING_DEFAULTS), _budget())
    raw.fade_to = raw.fade = 1.0
    for _ in range(50):
        raw.step(calm, 0.02)
    spike = [raw.step(hard, 0.02) for _ in range(5)]
    assert max(abs(p["x"]) for p in spike) > 3
    # the fade: 0 right after the reseed, full after FADE_S
    assert cue.fade < 1
    for _ in range(int(motion.FADE_S / 0.02) + 2):
        cue.step(hard, 0.02)
    assert cue.fade == 1.0


def test_fade_out_glides_home():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS), _budget())
    cue.fade_to = cue.fade = 1.0
    hard = {"fwd": -1.0 * motion.G, "left": 1.0 * motion.G, "up": 0.0, "yaw_rate": 0.0}
    for _ in range(50):
        p = cue.step(hard, 0.02)
    assert abs(p["pitch"]) > 3
    cue.fade_to = 0.0                                           # pause
    for _ in range(40):
        p = cue.step(hard, 0.02)
    assert all(abs(v) < 0.05 for v in p.values())


def test_telemetry_signs_on_a_recorded_lap():
    """Item 5 of the review: the car-frame mapping, checked on a real lap (Brands Hatch, clockwise)."""
    import json
    import statistics as st
    fx = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "brands_hatch_lap.json")))
    rows = [dict(zip(fx["cols"], r)) for r in fx["rows"]]
    car = [motion.car_frame(r["acc_x"], 0.0, r["acc_z"], r["ang_vel_y"]) for r in rows]
    corner = [(r["speed"] * c[3], c[1]) for r, c in zip(rows, car) if r["speed"] > 15 and abs(c[3]) > 0.15]
    agree = sum(1 for vw, left in corner if (vw > 0) == (left > 0)) / len(corner)
    a, b = zip(*corner)
    ma, mb = st.mean(a), st.mean(b)
    corr = sum((x - ma) * (y - mb) for x, y in corner) / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    assert agree > 0.95 and corr > 0.7      # 0.77 measured: tyre slip, kerbs and camber, not a sign issue
    braking = [c[0] for r, c in zip(rows, car) if r["brake"] > 0.8 and r["speed"] > 10]
    assert sum(1 for f in braking if f < 0) / len(braking) > 0.95
    turned = sum(c[3] * (b["ts"] - a["ts"]) for a, b, c in zip(rows, rows[1:], car))
    assert math.degrees(turned) == pytest.approx(-360, abs=25)  # clockwise lap: yaw + = turning left


@pytest.mark.parametrize("layout", ["flat", "radial"])
def test_both_layouts_keep_rod_lengths_and_symmetry(layout):
    g = {**G, "layout": layout}
    assert all(abs(r["alpha"]) < 1e-9 for r in motion.ik({}, g))
    for r in motion.ik({"roll": 8, "pitch": -6, "x": 4}, g):
        assert math.dist(r["A"], r["P"]) == pytest.approx(g["rod"], abs=1e-6)
    rc = motion.reach(g)
    assert rc["roll"][0] == pytest.approx(rc["roll"][1], abs=0.2) and rc["roll"][0] > 10


def test_flat_layout_has_parallel_shafts_in_a_pair():
    L = motion.legs({**G, "layout": "flat"})
    (b0, u0, _), (b1, u1, _) = L[0], L[1]
    assert u0[0] == pytest.approx(-u1[0]) and u0[1] == pytest.approx(-u1[1])   # horns swing apart along one line
    assert math.dist(b0, b1) == pytest.approx(G["shaft_spacing"])


def test_settings_saved_before_layouts_stay_radial(tmp_path, monkeypatch):
    import json
    f = tmp_path / "motion.json"
    f.write_text(json.dumps({"geometry": {"base_r": 100.5, "base_half": 31.0}}))
    monkeypatch.setattr(motion, "SETTINGS", f)
    assert motion.load()["geometry"]["layout"] == "radial"


def _body(**over):
    cue = motion.Cueing({**motion.CUEING_DEFAULTS, "mode": "body", "smooth_ms": 0.0, **over}, {})
    cue.fade = cue.fade_to = 1.0
    return cue


def test_body_mode_from_suspension():
    cue = _body()
    calm = {"fwd": 0.0, "left": 0.0, "up": 0.0, "yaw_rate": 0.0}
    cue.step(calm, 0.02)
    # braking: front shorter than rear by 1 cm; left turn: right side shorter by 1 cm (travel = extension)
    p = cue.step({**calm, "susp": (-1.0, 1.0, -0.5)}, 0.02)
    assert p["pitch"] == pytest.approx(-motion.CUEING_DEFAULTS["body_pitch_per_cm"])   # nose down
    assert p["roll"] == pytest.approx(motion.CUEING_DEFAULTS["body_roll_per_cm"])      # leans right (outside)
    assert p["z"] < 0 and p["x"] == 0 and p["y"] == 0                                   # sits lower, no sliding


def test_body_mode_spring_damper_overshoots_then_settles():
    cue = _body()
    brake = {"fwd": -1.0 * motion.G, "left": 0.0, "up": 0.0, "yaw_rate": 0.0, "susp": None}
    cue.step(brake, 0.005)
    ps = [cue.step(brake, 0.005)["pitch"] for _ in range(600)]   # 3 s at 200 Hz
    target = -motion.CUEING_DEFAULTS["pitch_per_g"]
    assert min(ps) < target * 1.1                                 # dives past the target (ζ 0.5) ...
    assert ps[-1] == pytest.approx(target, rel=0.02)              # ... and settles on it


def test_body_mode_yaw_is_the_slip_angle():
    cue = _body()
    s = {"fwd": 0.0, "left": 0.0, "up": 0.0, "yaw_rate": 0.0, "slip": 8.0}
    cue.step(s, 0.02)
    # moving 8° to the left of where the nose points: the nose is 8° right of the travel direction
    assert cue.step(s, 0.02)["yaw"] == pytest.approx(-8.0)


def test_cueing_mode_is_saved_and_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    monkeypatch.setitem(motion._cache, "s", None)
    motion.save({"cueing": {"mode": "body", "slip_yaw": 1.5}})
    c = motion.cueing_settings()
    assert c["mode"] == "body" and c["slip_yaw"] == 1.5
    with pytest.raises(ValueError):
        motion.save({"cueing": {"mode": "rollercoaster"}})
