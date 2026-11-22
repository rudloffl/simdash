"""The standalone board demo: simdash's export, parsed by the board's own demo.py, and fw.py under CPython."""
import importlib.util
import io
import math
import os
import sys

import pytest

from simdash import motion

FW = os.path.join(os.path.dirname(__file__), "..", "firmware", "6dof")
sys.path.insert(0, FW)          # fw.py imports demo.py by name, as on the board
board6 = importlib.import_module("board")
demo6 = importlib.import_module("demo")
fw6 = importlib.import_module("fw")


class FakeCourse:
    """A 10 s course: braking then a left corner, 20 Hz like a recording."""
    t0, t1 = 100.0, 110.0
    d = {"laps": [{"lap": 1, "ts": 100.0}, {"lap": 2, "ts": 105.0}]}

    def sample(self, t):
        u = t - self.t0
        return {"fwd": -9.0 if u < 4 else 0.0, "left": 11.0 if 4 <= u < 8 else 0.0, "up": 0.0, "yaw_rate": 0.0,
                "slip": 0.0, "slope": 0.0, "bank": 0.0, "susp": None}

    def skip_gap(self, t):
        return t


S = motion.DEFAULTS


def _export(course=None, t0=100.0, t1=110.0):
    frames, park = motion.demo_frames(course or FakeCourse(), t0, t1, S)
    data, clipped = motion.demo_encode(frames, S["servos"]["center_us"], park)
    return frames, park, data, clipped


def _player(data, out_rate=50):
    return demo6.DemoPlayer(lambda path, mode: io.BytesIO(data), "/flash/demo.bin", out_rate=out_rate, chunk=7)


def test_export_parse_round_trip():
    frames, park, data, clipped = _export()
    glide = int(motion.DEMO_GLIDE_S * motion.DEMO_RATE)
    assert len(frames) == 1 + glide + (10 * motion.DEMO_RATE + 1) + glide   # park, glide in, 0-10 s both ends, glide out
    assert frames[0] == park and frames[-1] == park                  # starts and ends at park
    assert len(data) == demo6.HEADER + 6 * len(frames) and clipped == 0
    p = _player(data, out_rate=25)                                    # no interpolation: frame by frame
    assert p.frames == len(frames) and p.center == S["servos"]["center_us"] and p.park == park and p.step == 4
    p.start()
    got = []
    while True:
        us = p.next()
        if us is None:
            break
        got.append(list(us))
    assert len(got) == len(frames)
    assert all(abs(a - b) <= 2 for f, g in zip(frames, got) for a, b in zip(f, g))   # within half a 4 µs step
    # the motion is in there: braking moved the servos
    assert max(abs(a - c) for f in got for a, c in zip(f, S["servos"]["center_us"])) > 50


def test_export_counts_clipped_frames():
    center = [1500] * 6
    frames = [[1500] * 6, [1500 + 600] + [1500] * 5, [1500] * 6]      # 600 µs > 127 x 4
    data, clipped = motion.demo_encode(frames, center, [1500] * 6)
    assert clipped == 1
    p = _player(data, out_rate=25)
    p.start()
    p.next()
    assert p.next()[0] == 1500 + 127 * 4


def test_demo_range_lap_and_time():
    c = FakeCourse()
    assert motion.demo_range(c, "lap", lap=1) == (100.0, 105.0)
    assert motion.demo_range(c, "lap", lap=2) == (105.0, 110.0)
    assert motion.demo_range(c, "time", from_s=2, to_s=4) == (102.0, 104.0)
    with pytest.raises(ValueError):
        motion.demo_range(c, "time", from_s=5, to_s=5)


def test_bad_files_are_refused():
    with pytest.raises(demo6.DemoError):
        _player(b"NOTADEMO" + bytes(40))
    _, _, data, _ = _export()
    with pytest.raises(demo6.DemoError):          # truncated: fails while playing, not silently
        p = _player(data[:demo6.HEADER + 6 * 3])
        p.start()
        for _ in range(20):
            p.next()


# ---- fw.py under CPython

class Out:
    def __init__(self):
        self.writes, self.offs = [], 0

    def write(self, us):
        self.writes.append(list(us))

    def off(self):
        self.offs += 1


class Led:
    def __init__(self):
        self.lit = False

    def on(self):
        self.lit = True

    def off(self):
        self.lit = False


class Switch:
    def __init__(self):
        self.down = False

    def __call__(self):
        return self.down


def make_fw(data, pca_ok=True):
    out, sw, said = Out(), Switch(), []
    board = board6.Board(out)
    leds = (Led(), Led(), Led(), Led())
    files = {"/flash/demo.bin": data} if data is not None else {}
    fw = fw6.Firmware(board, said.append, leds, sw, lambda p, m: io.BytesIO(files[p]), lambda p: p in files, pca_ok)
    drives = []
    real = board.drive
    board.drive = lambda us, now: (drives.append((now, list(us))), real(us, now))[1]
    return fw, board, out, sw, said, leds, drives


def run(fw, ms, step=1):
    for _ in range(int(ms / step)):
        fw.step(step)


def test_waits_for_the_pc_then_plays_every_frame_at_50_hz():
    frames, park, data, _ = _export()
    fw, board, out, sw, said, leds, drives = make_fw(data)
    run(fw, 2990)
    assert fw.mode == "wait" and drives == []
    run(fw, 20)
    assert fw.mode == "demo" and leds[3].lit                           # blue
    run(fw, len(frames) * 40 + 100)
    last = 3000 + 20 * 2 * (len(frames) - 1)                          # the tick of the last frame
    clip = [d for d in drives if d[0] <= last]
    assert len(clip) == 2 * (len(frames) - 1) + 1                      # 25 Hz frames -> 50 Hz outputs
    assert clip[-1][1] == park and fw.mode == "park"                   # ends at park, then holds there
    gaps = {b[0] - a[0] for a, b in zip(clip, clip[1:])}
    assert gaps == {20}                                                # a fixed 20 ms tick
    assert all(800 <= us <= 2200 for _, f in drives for us in f)


def test_loops_through_park():
    frames, park, data, _ = _export(t1=102.0)
    fw, board, out, sw, said, leds, drives = make_fw(data)
    run(fw, 3000 + len(frames) * 40 + 200)
    assert fw.mode == "park" and fw.played == 1 and board.target == park
    run(fw, fw6.HOLD_MS + 100)
    assert fw.mode == "demo" and fw.played == 2                       # held 5 s at park, then again


def test_a_line_from_the_pc_takes_over():
    _, park, data, _ = _export()
    fw, board, out, sw, said, leds, drives = make_fw(data)
    run(fw, 4000)
    assert fw.mode == "demo"
    fw.feed(b"H\n")
    assert fw.mode == "pc" and said == ["6DOF %s ch=6\n" % board6.VERSION]
    assert board.state == "glide" and board.target == park            # glides to park
    n = len(drives)
    run(fw, 500)
    assert len(drives) == n                                            # the demo stopped
    fw.feed(b"S 1600 1600 1600 1600 1600 1600\n")
    assert board.state == "drive" and board.target == [1600] * 6      # then the PC drives


def test_the_pc_before_3_s_means_no_demo():
    _, _, data, _ = _export()
    fw, board, out, sw, said, leds, drives = make_fw(data)
    run(fw, 1000)
    fw.feed(b"H\n")
    run(fw, 5000)
    assert fw.mode == "pc" and fw.played == 0


def test_usr_short_press_latches_long_press_restarts():
    _, _, data, _ = _export()
    fw, board, out, sw, said, leds, drives = make_fw(data)
    run(fw, 4000)
    sw.down = True
    run(fw, 200)
    sw.down = False
    run(fw, 20)
    assert fw.latched and board.state == "limp" and fw.mode == "pc" and leds[0].lit
    fw.feed(b"S 1600 1600 1600 1600 1600 1600\n")                    # latched: the PC can't move it ...
    assert board.state == "limp"
    fw.feed(b"H\n")
    assert said[-1].endswith(" latched\n")
    fw.feed(b"A\n")                                                    # ... until it says A
    fw.feed(b"S 1600 1600 1600 1600 1600 1600\n")
    assert board.state == "drive"
    sw.down = True                                                     # long press: the demo from the start
    run(fw, fw6.LONG_MS + 40)
    assert fw.mode == "demo" and fw.played == 2 and not fw.latched
    sw.down = False
    run(fw, 40)
    assert fw.mode == "demo"                                           # releasing a long press doesn't stop it


def test_a_read_error_lets_go_and_blinks_red():
    _, _, data, _ = _export()
    fw, board, out, sw, said, leds, drives = make_fw(data[:demo6.HEADER + 6 * 20])   # cut short
    run(fw, 4000)
    assert fw.error and board.state == "limp" and fw.mode == "pc"
    reds = set()
    for _ in range(20):
        run(fw, 20)
        reds.add(leds[0].lit)
    assert reds == {True, False}                                       # blinking


def test_no_file_or_no_pca_means_no_demo():
    _, _, data, _ = _export()
    for fw, *_ in (make_fw(None), make_fw(data, pca_ok=False)):
        run(fw, 4000)
        assert fw.mode == "pc" and fw.played == 0
