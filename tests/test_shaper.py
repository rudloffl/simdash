"""The engine's output shaping (engine.Shaper) and ducking (Engine._ducking)."""

import math

import pytest

from simdash import engine, replay, shakers

N = int(engine.BLOCK * engine.RATE)


@pytest.fixture(autouse=True)
def no_user_files(tmp_path, monkeypatch):
    """Default shaping, never the user's shaping.json."""
    monkeypatch.setattr(shakers, "SHAPING", tmp_path / "shaping.json")


def shaper(**s):
    sh = engine.Shaper()
    sh.set(shakers._clean_shaping(s))
    return sh


def block(amp, ch=0, f=40.0, start=0):
    mix = [0.0] * (8 * N)
    for j in range(N):
        mix[8 * j + ch] = amp * math.sin(2 * math.pi * f * (start + j) / engine.RATE)
    return mix


def peak(mix, ch=0):
    return max(abs(mix[8 * j + ch]) for j in range(N))


def test_quiet_sound_passes_unchanged():
    sh = shaper()
    mix = block(0.5)
    before = list(mix)
    sh.process(mix, N)
    assert mix == pytest.approx(before)
    assert sh.reduction == {}


def test_limiter_keeps_a_loud_sum_under_the_ceiling():
    sh = shaper(ceiling=95)
    sh.process(block(2.0), N)                  # the gain comes down over the first 1.5 ms
    mix = block(2.0, start=N)
    sh.process(mix, N)
    assert peak(mix) <= 0.95 + 1e-9
    assert sh.reduction["0"] == pytest.approx(52, abs=1)


def test_limiter_recovers_over_the_release():
    sh = shaper(release_ms=150)
    sh.process(block(2.0), N)
    low = sh.g[0]
    sh.process(block(0.1), N)
    assert low < sh.g[0] < 1.0                 # on the way back, not at once
    for _ in range(100):                       # a second later
        sh.process(block(0.1), N)
    assert sh.g[0] == pytest.approx(1.0, abs=0.01)


def test_limiter_off_leaves_the_sum_alone():
    sh = shaper(limiter=False)
    mix = block(2.0)
    sh.process(mix, N)
    assert peak(mix) == pytest.approx(2.0, rel=1e-3)


def test_master_and_shaker_volumes():
    sh = shaper(master=50, gains={"0": 50})
    mix = block(0.8)
    sh.process(mix, N)
    assert peak(mix) == pytest.approx(0.2, rel=1e-3)


def test_the_limiter_works_on_each_shaker_alone():
    sh = shaper()
    mix = [a + b for a, b in zip(block(2.0, ch=0), block(0.5, ch=7))]
    sh.process(mix, N)
    assert "0" in sh.reduction and "7" not in sh.reduction
    assert peak(mix, 7) == pytest.approx(0.5, rel=1e-3)


# ---------- ducking ----------

class _NoGame:
    def sample(self):
        return None


def duck_engine(duck=50):
    eng = engine.Engine(reader=_NoGame())
    eng.shaper.set(shakers._clean_shaping({"duck": duck}))
    devs = [{"effect": "Engine", "pan": 7, "volume": 100, "frequency": 25, "amplitude": 80},
            {"effect": "Impact", "pan": 7, "volume": 100, "frequency": 28, "amplitude": 100}]
    eng.devices, eng.streams = devs, [replay._Stream(d) for d in devs]
    return eng


def test_a_knock_ducks_the_continuous_effects_on_its_shaker():
    eng = duck_engine(50)
    _, now = eng._ducking({0: (25, 80), 1: (28, 100)}, False)
    assert now[7] == pytest.approx(0.5)
    assert now[0] == 0.0


def test_ducking_comes_back_after_the_knock():
    eng = duck_engine(50)
    eng._ducking({0: (25, 80), 1: (28, 100)}, False)
    _, now = eng._ducking({0: (25, 80), 1: (28, 0)}, False)
    assert 0 < now[7] < 0.5
    for _ in range(50):                        # half a second later
        _, now = eng._ducking({0: (25, 80), 1: (28, 0)}, False)
    assert now[7] < 0.01

