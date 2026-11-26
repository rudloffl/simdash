"""firmware/6dof/board.py and ring.py under CPython, with a fake PCA9685/SPI: slew limit, glide to park,
then limp; the status ring's WS2812 buffer and its standalone rainbow."""
import importlib.util
import os

import pytest


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name[:-3] + "6dof", os.path.join(os.path.dirname(__file__), "..", "firmware", "6dof", name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


board6dof = _load("board.py")
ring6dof = _load("ring.py")


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
    for name in ("main.py", "board.py", "pca9685.py", "fw.py", "demo.py", "ring.py"):
        src = open(os.path.join(folder, name)).read()
        code = "\n".join(ln.split("#")[0] for ln in src.splitlines())
        assert not re.search(r"\d_\d", code), name
        assert not re.search(r"\bf[\"']", code), name
        assert ".value(" not in code.replace("oe.value(", ""), name   # pyb.LED has no value() on 1.9.4


def _decode_pixel(buf, i):
    """The 9 SPI bytes for pixel i, decoded back to (r, g, b) -- the reverse of ring.py's _LUT."""
    o = i * 9
    out = []
    for k in range(3):
        bits = (buf[o + 3 * k] << 16) | (buf[o + 3 * k + 1] << 8) | buf[o + 3 * k + 2]
        v = 0
        for b in range(8):
            v = (v << 1) | (1 if ((bits >> (21 - 3 * b)) & 0b111) == 0b110 else 0)
        out.append(v)
    g, r, b = out                      # WS2812B order: green, red, blue
    return r, g, b


def test_ring_show_encodes_grb_and_pads_a_reset_gap():
    writes = []
    r = ring6dof.Ring(lambda buf: writes.append(bytes(buf)), n=3)
    r.show([10, 20, 30, 255, 0, 128, 0, 0, 0])
    buf = writes[-1]
    assert len(buf) == 3 * 9 + ring6dof.RESET_BYTES
    assert _decode_pixel(buf, 0) == (10, 20, 30)
    assert _decode_pixel(buf, 1) == (255, 0, 128)
    assert _decode_pixel(buf, 2) == (0, 0, 0)
    assert buf[-ring6dof.RESET_BYTES:] == b"\x00" * ring6dof.RESET_BYTES   # the latch gap, held low


def test_ring_off_is_all_black():
    writes = []
    r = ring6dof.Ring(lambda buf: writes.append(bytes(buf)), n=2)
    r.off()
    for i in range(2):
        assert _decode_pixel(writes[-1], i) == (0, 0, 0)


def test_rainbow_turns_full_circle_and_stays_in_range():
    a, b = ring6dof.rainbow(0, n=6), ring6dof.rainbow(4000, n=6)   # one full turn later (ROT_DEG_PER_MS)
    assert a == b
    for v in ring6dof.rainbow(1234, n=6):
        assert 0 <= v <= 255
    # a quarter turn moves the pattern around the ring, not just dims it
    assert ring6dof.rainbow(0, n=4) != ring6dof.rainbow(1000, n=4)

