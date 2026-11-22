"""firmware/6dof/board.py under CPython, with a fake PCA9685: slew limit, glide to park, then limp."""
import importlib.util
import os

import pytest

_spec = importlib.util.spec_from_file_location(
    "board6dof", os.path.join(os.path.dirname(__file__), "..", "firmware", "6dof", "board.py"))
board6dof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board6dof)


class FakeOut:
    def __init__(self):
        self.writes, self.offs = [], 0

    def write(self, us):
        self.writes.append(list(us))

    def off(self):
        self.offs += 1


def run(b, start, until, tick=5):
    """Tick from start to until (ms); returns the time of each output write."""
    t, times = start, []
    while t < until:
        t += tick
        n = len(b.out.writes)
        b.tick(t, tick)
        if len(b.out.writes) > n:
            times.append(t)
    return times


def make():
    out = FakeOut()
    return board6dof.Board(out), out


def test_hello_reports_the_version():
    b, _ = make()
    assert b.line("H", 0) == "6DOF %s ch=6\n" % board6dof.VERSION
    assert int(board6dof.VERSION) >= 2


def test_a_step_is_slew_limited():
    b, out = make()
    b.line("S 1500 1500 1500 1500 1500 1500", 0)   # first S after limp: the starting point, no move
    run(b, 0, 50)
    assert out.writes == []
    b.line("S 2100 1500 1500 1500 1500 1500", 50)  # a 600 µs step
    t = 50
    while out.writes == [] or out.writes[-1][0] < 2100:
        t += 5
        b.tick(t, 5)
        b.line("S 2100 1500 1500 1500 1500 1500", t)   # the PC keeps sending
        assert t < 1000
    assert t - 50 >= 100                             # ≥ 100 ms at 6 µs/ms
    steps = [b2[0] - b1[0] for b1, b2 in zip(out.writes, out.writes[1:])]
    assert max(steps) <= 6 * 5 + 1e-9                # never more than rate × tick between updates


def test_pulses_are_clamped():
    b, out = make()
    b.line("S 100 3000 1500 1500 1500 1500", 0)
    assert b.cur[:2] == [800, 2200]


def test_timeout_glides_to_park_then_lets_go():
    b, out = make()
    b.line("P 1500 1500 1500 1500 1500 1500", 0)
    b.line("S 1900 1100 1500 1500 1500 1500", 0)
    run(b, 0, 990)                                   # still driving: no timeout yet
    assert b.state == "drive" and out.offs == 0
    times = run(b, 990, 4000)
    assert out.offs == 1 and b.state == "limp"
    assert out.writes[-1][:2] == [1500, 1500]        # reached park before letting go
    glide = [t for t in times if t > 1000]
    assert glide[-1] - glide[0] >= 800               # ~1 s glide, not a jump
    steps = [abs(w2[0] - w1[0]) for w1, w2 in zip(out.writes, out.writes[1:])]
    assert max(steps) <= 6 * 5


def test_without_park_the_timeout_lets_go_at_once():
    b, out = make()
    b.line("S 1500 1500 1500 1500 1500 1500", 0)
    run(b, 0, 1100)
    assert b.state == "limp" and out.offs == 1


def test_O_lets_go_and_the_next_S_is_the_new_start():
    b, out = make()
    b.line("S 1500 1500 1500 1500 1500 1500", 0)
    b.line("O", 10)
    assert b.state == "limp" and out.offs == 1
    b.line("S 1800 1800 1800 1800 1800 1800", 20)
    assert b.cur == [1800] * 6                        # unknown horn positions: start from the first S


def test_bad_lines_are_ignored():
    b, _ = make()
    with pytest.raises(ValueError):
        b.line("S 1500 1500", 0)
    assert b.line("", 0) is None and b.state == "limp"


def test_firmware_runs_on_old_micropython():
    """The pyboard here runs MicroPython 1.9.4 (2018): no numeric underscores (400_000), no f-strings."""
    import re
    folder = os.path.join(os.path.dirname(__file__), "..", "firmware", "6dof")
    for name in ("main.py", "board.py", "pca9685.py", "fw.py", "demo.py"):
        src = open(os.path.join(folder, name)).read()
        code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
        assert not re.search(r"\d_\d", code), name
        assert not re.search(r"\bf[\"']", code), name
        assert ".value(" not in code.replace("oe.value(", ""), name   # pyb.LED has no value() on 1.9.4
