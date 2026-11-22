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
    assert motion.solve({"pitch": 60})["ok"] is False


def test_pulses_follow_direction_and_clamp():
    sv = dict(motion.DEFAULTS["servos"])
    p = motion.pulses([10, 10, None, 0, 500, -500], sv)
    assert p[0] == 1600 and p[1] == 1400 and p[2] == 1500 and p[3] == 1500
    assert p[4] == sv["max_us"] and p[5] == sv["max_us"]  # servo 5 reversed: -500° -> long pulse, clamped


def test_template_svg_is_true_scale():
    svg = motion.template_svg("base", G)
    assert 'mm" height="' in svg and "50 mm" in svg


def test_link_talks_the_firmware_protocol(monkeypatch):
    """A fake pyboard on a pty: answers H, records S and O lines."""
    master, slave = os.openpty()
    monkeypatch.setattr(motion, "find_port", lambda: os.ttyname(slave))
    got, stop = [], threading.Event()

    def board():
        buf = b""
        while not stop.is_set():
            import select
            if select.select([master], [], [], 0.05)[0]:
                buf += os.read(master, 1024)
                while b"\n" in buf:
                    ln, buf = buf.split(b"\n", 1)
                    got.append(ln.decode())
                    if ln == b"H":
                        os.write(master, b"6DOF 1 ch=6\n")
    th = threading.Thread(target=board, daemon=True)
    th.start()
    link = motion.Link()
    link.arm(True)
    link.send([1500, 1510, 1520, 1530, 1540, 1550])
    st = link.status()
    link.arm(False)
    stop.set()
    th.join(1)
    assert st["connected"] and st["firmware"] == "6DOF 1 ch=6", st
    assert "S 1500 1510 1520 1530 1540 1550" in got and got[-1] == "O"


def test_cueing_braking_dips_the_nose_and_washes_out():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS))
    brake = {"fwd": -1.0 * motion.G, "left": 0.0, "up": 0.0, "yaw_rate": 0.0}
    for _ in range(50):                      # 1 s of 1 g braking at 50 Hz
        p = cue.step(brake, 0.02)
    assert p["pitch"] < -0.8 * motion.CUEING_DEFAULTS["pitch_per_g"]   # tilt holds
    assert abs(p["x"]) < 0.4 * motion.CUEING_DEFAULTS["surge_per_g"]   # the shift drifted back


def test_cueing_left_turn_leans_right():
    cue = motion.Cueing(dict(motion.CUEING_DEFAULTS))
    for _ in range(50):
        p = cue.step({"fwd": 0.0, "left": 1.2 * motion.G, "up": 0.0, "yaw_rate": 0.0}, 0.02)
    assert p["roll"] > 5


def test_fit_pose_shrinks_into_reach():
    big = {"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 60, "yaw": 0}
    f, s = motion.fit_pose(big, G)
    assert 0 < s < 1 and motion.within(motion.ik(f, G), G)


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
