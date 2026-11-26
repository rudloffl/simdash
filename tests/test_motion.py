import math
import os
import threading

import pytest

from simdash import motion

G = dict(motion.DEFAULTS["geometry"])
SPACING = 2 * G["base_r"] * math.sin(math.radians(G["base_angle"] / 2))   # between the shafts of a pair


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
    wide = {"geometry": {**G, "stroke": 80.0}, "servos": sv}
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
    only = {**G, "joint_max_deg": 180.0, "stroke": 90.0}
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


@pytest.mark.parametrize("shaft_angle", [0.0, 45.0, 90.0, 135.0, 180.0])
def test_every_shaft_angle_keeps_rod_lengths_and_symmetry(shaft_angle):
    g = {**G, "shaft_angle": shaft_angle}
    assert all(abs(r["alpha"]) < 1e-9 for r in motion.ik({}, g))
    for r in motion.ik({"roll": 8, "pitch": -6, "x": 4}, g):
        assert math.dist(r["A"], r["P"]) == pytest.approx(g["rod"], abs=1e-6)
    rc = motion.reach(g)
    assert rc["roll"][0] == pytest.approx(rc["roll"][1], abs=0.2) and rc["roll"][0] > 5


def test_shaft_angle_90_is_the_classic_radial_shaft():
    # each shaft through the base centre, horn tangent to the base circle: today's only previous layout
    L = motion.legs({**G, "shaft_angle": 90.0})
    for B, u, _ in L:
        assert abs(B[0] * u[0] + B[1] * u[1]) < 1e-9       # u tangent: no radial component
    (b0, u0, _), (b1, u1, _) = L[0], L[1]
    assert math.hypot(*b0) == pytest.approx(G["base_r"]) and math.hypot(*b1) == pytest.approx(G["base_r"])


@pytest.mark.parametrize("shaft_angle", [0.0, 30.0, 60.0])
def test_shaft_angle_under_90_tips_the_horn_in(shaft_angle):
    # 0-90°: the horn's ball at home lands inside the base circle (closer to the centre than its own shaft)
    g = {**G, "shaft_angle": shaft_angle}
    for B, u, _ in motion.legs(g):
        assert B[0] * u[0] + B[1] * u[1] < 0


@pytest.mark.parametrize("shaft_angle", [120.0, 150.0, 180.0])
def test_shaft_angle_over_90_tips_the_horn_out(shaft_angle):
    g = {**G, "shaft_angle": shaft_angle}
    for B, u, _ in motion.legs(g):
        assert B[0] * u[0] + B[1] * u[1] > 0


def test_shaft_angle_leans_the_same_way_for_both_of_a_pair():
    # shaft_angle doesn't depend on which of the pair it is: both lean in (or out) together, not mirrored
    g = {**G, "shaft_angle": 30.0}
    (b0, u0, _), (b1, u1, _) = motion.legs(g)[:2]
    assert (b0[0] * u0[0] + b0[1] * u0[1] < 0) == (b1[0] * u1[0] + b1[1] * u1[1] < 0)


def test_shaft_angle_0_points_each_horn_at_the_base_centre():
    # 0°: n tangent, so u is purely radial (through the centre), pointing inward ("horns": "out")
    L = motion.legs({**G, "shaft_angle": 0.0})
    for B, u, _ in L:
        r = math.hypot(*B)
        assert u == pytest.approx((-B[0] / r, -B[1] / r))


def test_shaft_angle_horns_in_flips_in_and_out():
    out, inw = motion.legs({**G, "shaft_angle": 30.0, "horns": "out"}), motion.legs({**G, "shaft_angle": 30.0, "horns": "in"})
    for (B, u0, P), (_, u1, _) in zip(out, inw):
        assert u1 == pytest.approx((-u0[0], -u0[1]))


def test_settings_saved_before_shaft_angle_existed_default_to_90(tmp_path, monkeypatch):
    import json
    f = tmp_path / "motion.json"
    f.write_text(json.dumps({"geometry": {"base_r": 100.5, "base_half": 31.0}}))
    monkeypatch.setattr(motion, "SETTINGS", f)
    g = motion.load()["geometry"]
    assert "layout" not in g and g["shaft_angle"] == 90.0 and g["base_r"] == 100.5 and g["base_angle"] == 62.0
    f.write_text(json.dumps({"geometry": {"layout": "flat", "base_r": 62.4, "base_angle": 43.3}}))
    monkeypatch.setitem(motion._cache, "s", None)
    g2 = motion.load()["geometry"]
    assert "layout" not in g2 and g2["shaft_angle"] == 90.0


def test_old_flat_settings_keep_the_same_platform(tmp_path, monkeypatch):
    import json
    old = {"layout": "flat", "base_r": 58.0, "shaft_spacing": 46.0, "plat_r": 46.0, "plat_half": 24.0}
    f = tmp_path / "motion.json"
    f.write_text(json.dumps({"geometry": old}))
    monkeypatch.setattr(motion, "SETTINGS", f)
    g = motion.load()["geometry"]
    assert "shaft_spacing" not in g and "plat_half" not in g and g["plat_angle"] == 48.0
    (b0, _, _), (b1, _, _) = motion.legs(g)[:2]
    assert b0 == pytest.approx((58.0, -23.0), abs=0.01) and b1 == pytest.approx((58.0, 23.0), abs=0.01)


def test_svgs_carry_the_dimensions_and_coordinates():
    for shaft_angle in (0.0, 90.0, 180.0):
        g = {**G, "shaft_angle": shaft_angle}
        base, plat = motion.template_svg("base", g), motion.template_svg("platform", g)
        assert f"R{g['base_r']:g}" in base and f"{g['base_angle']:g}°" in base and "shafts " in base and "balls " in base
        assert f"horn {g['horn']:g}" in base and base.count("horn ball (") == 6
        assert f"R{g['plat_r']:g}" in plat and f"{g['plat_angle']:g}°" in plat and plat.count("ball joint (") == 6


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


@pytest.mark.parametrize("shaft_angle", [0.0, 90.0, 180.0])
def test_horns_in_flips_u_and_keeps_the_platform_working(shaft_angle):
    out = {**G, "shaft_angle": shaft_angle, "horns": "out"}
    inw = {**out, "horns": "in"}
    for (b0, u0, p0), (b1, u1, p1) in zip(motion.legs(out), motion.legs(inw)):
        assert b0 == b1 and p0 == p1 and u1 == pytest.approx((-u0[0], -u0[1]))
    assert all(abs(r["alpha"]) < 1e-9 for r in motion.ik({}, inw))
    for r in motion.ik({"roll": 6, "pitch": -5, "y": 3}, inw):
        assert math.dist(r["A"], r["P"]) == pytest.approx(G["rod"], abs=1e-6)
    rc = motion.reach(inw)
    assert rc["roll"][0] == pytest.approx(rc["roll"][1], abs=0.2) and rc["roll"][0] > 3
    assert "<svg" in motion.template_svg("base", inw)


def test_horns_in_bring_the_horn_balls_together_at_the_classic_radial_angle():
    # the intuitive "far apart" / "close together" reading of horns out/in is about the tangential
    # swing (shaft_angle 90°, the one previous layout where it was unambiguous either way)
    out, inw = {**G, "shaft_angle": 90.0, "horns": "out"}, {**G, "shaft_angle": 90.0, "horns": "in"}
    d_out, d_in = motion.design(out), motion.design(inw)
    assert 0 < d_in["ball_gap"] < d_out["ball_gap"]


def test_crossing_horns_show_no_gap():
    assert motion.design({**G, "horns": "in", "horn": SPACING / 2 + 2})["ball_gap"] == 0


def test_horns_setting_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    assert motion.save({"geometry": {"horns": "in"}})["geometry"]["horns"] == "in"
    with pytest.raises(ValueError):
        motion.save({"geometry": {"horns": "sideways"}})


def test_shaft_angle_setting_is_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    assert motion.save({"geometry": {"shaft_angle": 30.0}})["geometry"]["shaft_angle"] == 30.0
    with pytest.raises(ValueError):
        motion.save({"geometry": {"shaft_angle": 181.0}})
    with pytest.raises(ValueError):
        motion.save({"geometry": {"shaft_angle": -1.0}})


def test_upright_servos_move_the_same_but_sit_higher():
    up = {**G, "mount": "upright"}
    assert [r["alpha"] for r in motion.ik({"roll": 7, "z": 3}, up)] == [r["alpha"] for r in motion.ik({"roll": 7, "z": 3}, G)]
    d_lying, d_up = motion.design(G), motion.design(up)
    assert d_up["shaft_height"] > d_lying["shaft_height"]
    assert "upright" in motion.template_svg("base", up)


def test_home_angle_puts_every_horn_there_and_limits_the_stroke_around_it():
    g = {**G, "home_angle": 30.0, "stroke": 35.0}
    assert all(r["alpha"] == pytest.approx(30.0, abs=1e-6) for r in motion.ik({}, g))
    for r in motion.ik({"pitch": 6, "y": 4}, g):
        assert math.dist(r["A"], r["P"]) == pytest.approx(g["rod"], abs=1e-6)
    assert motion.h0(g) > motion.h0(G)                       # horns up lift the platform
    narrow = {**g, "stroke": 5.0, "transmission_min_deg": 0, "joint_max_deg": 180}
    assert "stroke" in motion.why_not(motion.ik({"z": 3}, narrow), narrow, {"z": 3})
    rc = motion.reach(g)
    assert rc["roll"][0] == pytest.approx(rc["roll"][1], abs=0.2) and rc["roll"][0] > 3
    d = motion.design(g)
    assert d["horn_range"] == [-5.0, 65.0]


def test_home_pulse_comes_from_the_calibration():
    s = {**motion.DEFAULTS, "geometry": {**G, "home_angle": 30.0, "stroke": 35.0}}
    sv = s["servos"]
    assert motion.solve({}, s)["pulse"] == [round(motion.raw_pulse(i, 30.0, sv)) for i in range(6)]
    assert motion.pulses([None] * 6, sv, 30.0) == motion.solve({}, s)["pulse"]


def test_old_servo_limit_becomes_the_stroke(tmp_path, monkeypatch):
    import json
    f = tmp_path / "motion.json"
    f.write_text(json.dumps({"geometry": {"layout": "flat", "servo_limit": 40.0}}))
    monkeypatch.setattr(motion, "SETTINGS", f)
    g = motion.load()["geometry"]
    assert g["stroke"] == 40.0 and g["home_angle"] == 0.0 and "servo_limit" not in g


@pytest.mark.parametrize("home", [0.0, 30.0])
def test_fk_finds_the_pose_back_from_the_servo_angles(home):
    g = {**G, "home_angle": home, "stroke": 35.0}
    pose = {"x": 4, "y": -3, "z": 2, "roll": 3, "pitch": -2, "yaw": 2}
    got = motion.fk([r["alpha"] for r in motion.ik(pose, g)], g)
    assert all(got[ax] == pytest.approx(pose[ax], abs=1e-3) for ax in motion.AXES)


def test_servos_driven_directly():
    s = {**motion.DEFAULTS, "geometry": {**G, "home_angle": 30.0, "stroke": 35.0}}
    r = motion.solve_servos([8] * 6, s)                   # all up together: straight up
    assert r["ok"] and r["pose"]["z"] > 1 and abs(r["pose"]["x"]) < 1e-3
    assert r["pulse"] == motion.pulses([38.0] * 6, s["servos"])
    assert r["alpha"] == [38.0] * 6
    r = motion.solve_servos([40, 0, 0, 0, 0, 0], s)
    assert not r["ok"] and "stroke" in r["why"]
    with pytest.raises(ValueError):
        motion.solve_servos([0] * 5, s)


def test_rod_ends_turn_freely_about_their_bolts():
    # a rod end is free about its bolt: the swing that a ball stud's cone refuses is fine, only the tilt counts
    stud = {**G, "transmission_min_deg": 0.0, "joint_max_deg": 20.0, "joints": "stud"}
    rod_end = {**stud, "joints": "rod_end"}
    gm = motion.geo(rod_end)
    for k, (_, u, _) in enumerate(gm["L"]):
        b = gm["bolt_plat"][k]
        assert abs(sum(b[i] * gm["home_rod"][k][i] for i in range(3))) < 1e-9   # square to the rod at home
        assert gm["bolt_horn"][k] == (u[1], -u[0], 0.0)                         # along the servo shaft
    assert motion.reach(rod_end)["z"][1] > motion.reach(stud)["z"][1]
    # too tight a tilt and the rod end limits it, with its own reason
    tight = {**rod_end, "joint_max_deg": 1.0}
    p = {"roll": 10}
    assert "off its bolt" in motion.why_not(motion.ik(p, tight), tight, p)
    assert motion.design(rod_end)["joints"] == "rod_end"
    assert "rod-end bolt" in motion.template_svg("base", rod_end) and "rod-end bolt" in motion.template_svg("platform", rod_end)


def test_joints_setting_is_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    motion._cache["s"] = None
    assert motion.save({"geometry": {"joints": "rod_end"}})["geometry"]["joints"] == "rod_end"
    import pytest
    with pytest.raises(ValueError):
        motion.save({"geometry": {"joints": "glue"}})


def test_turned_off_is_kept_and_lets_go(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "SETTINGS", tmp_path / "motion.json")
    monkeypatch.setitem(motion._cache, "s", None)
    calls = []
    monkeypatch.setattr(motion.player, "stop", lambda: calls.append("stop"))
    monkeypatch.setattr(motion.link, "arm", lambda on, home=None: calls.append(("arm", on)) or True)
    assert motion.enabled()                                     # no file: in use
    motion.set_enabled(False)
    assert calls == ["stop", ("arm", False)]                    # a course stops, the servos let go
    assert not motion.enabled()
    motion.save({"geometry": {"horn": 20}})                     # a settings save keeps it off
    assert not motion.enabled() and motion.load()["geometry"]["horn"] == 20
    motion.set_enabled(True)
    assert motion.enabled() and len(calls) == 2                 # turning on moves nothing
    assert motion.status()["enabled"] is True


def test_board_report_rates(monkeypatch):
    lk = motion.Link()
    now = [100.0]
    monkeypatch.setattr(motion.time, "monotonic", lambda: now[0])
    line = "STATE mode=pc board=limp latched=0 error=0 pca=1 demo=1 played=0 clock={c} ticks={t} loops={l} ms={m} reset=power wdt=2000"
    r = lk._report(line.format(c=5000, t=250, l=(1 << 30) - 500, m=5000))
    assert r["answered"] and r["mode"] == "pc" and r["reset"] == "power" and "tick_hz" not in r
    now[0] += 2.0
    r = lk._report(line.format(c=7000, t=350, l=1500, m=7000))      # the loop counter wrapped
    assert (r["loop_hz"], r["tick_hz"], r["clock_ok"]) == (1000, 50, True)
    now[0] += 2.0
    r = lk._report(line.format(c=7000, t=350, l=3500, m=9000))      # loop runs, timed part frozen
    assert (r["loop_hz"], r["tick_hz"], r["clock_ok"]) == (1000, 0, False)
    now[0] += 2.0
    r = lk._report(line.format(c=100, t=5, l=100, m=100))           # reset in between: no rates
    assert "tick_hz" not in r
    assert lk._report(None) == {"answered": False}


def test_write_firmware_copies_then_renames_all(monkeypatch):
    from simdash import pyboard
    log = []

    class FakeBoard:
        def __init__(self, port):
            log.append(("open", port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            log.append("restart")

        def put(self, data, dest):
            log.append(("put", dest, len(data)))

        def exec(self, code, timeout=30):
            if "statvfs" in code:
                return "['main.py', 'demo.bin']\n60000\n"
            if "os.stat(" in code:
                return str([len((motion.FIRMWARE_DIR / f).read_bytes()) for f in motion.FIRMWARE_FILES]) + "\n"
            if "os.rename" in code:
                assert "open('/flash/SKIPSD', 'w')" in code        # never boot off an SD card
            log.append("rename" if "os.rename" in code else code)
            return ""

    monkeypatch.setattr(pyboard, "Board", FakeBoard)
    monkeypatch.setattr(motion, "find_port", lambda: "/dev/ttyACM9")
    monkeypatch.setattr(motion.link, "arm", lambda on, home=None: True)
    monkeypatch.setattr(motion.link, "query", lambda: {"answered": not motion.board_busy.is_set()})
    monkeypatch.setattr(motion.time, "sleep", lambda s: None)
    r = motion.write_firmware()
    puts = [e for e in log if isinstance(e, tuple) and e[0] == "put"]
    assert [p[1] for p in puts] == ["/flash/%s.new" % f for f in motion.FIRMWARE_FILES]
    assert log.index("rename") > log.index(puts[-1]) and log[-1] == "restart"   # all copied, then renamed
    assert r["running"] and r["version"] == motion.firmware_version() and not motion.board_busy.is_set()
    assert motion.FIRMWARE_FILES[-1] == "main.py"
