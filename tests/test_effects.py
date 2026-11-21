"""The effect formulas (simdash/effects.py): what each effect plays for a given piece of telemetry."""

import pytest

from simdash import effects, shakers

G = effects.G


def dev(effect, wheels=("FL", "FR", "RL", "RR"), **k):
    return shakers._clean_own({"effect": effect, "wheels": list(wheels), "pan": 0, **k})


def tick(acc=(0.0, 0.0), base=(0.0, 0.0), glitch=False, speed=0.0, rpm=0.0, maxrpm=8000.0):
    """One tick of a recording, as effects read it (acc/base: local x, z in m/s²; x left, z backward)."""
    return {"acc": [acc], "acc_base": [base], "acc_glitch": [glitch], "speed": [speed], "rpm": [rpm], "maxrpm": [maxrpm]}


# ---------- Impact ----------

def test_impact_below_threshold_is_silent():
    assert effects.impact_play(dev("Impact"), tick(acc=(2.5 * G, 0.0)), 0) == 0.0


def test_impact_full_strength_at_full():
    assert effects.impact_play(dev("Impact"), tick(acc=(-12 * G, 0.0)), 0) == pytest.approx(1.0)


def test_impact_wheels_pick_the_side_of_the_obstacle():
    # pushed to the right (x points left): the wall is on the left
    d = tick(acc=(-12 * G, 0.0))
    assert effects.impact_play(dev("Impact", ["FL"]), d, 0) == pytest.approx(1.0)
    assert effects.impact_play(dev("Impact", ["RL"]), d, 0) == pytest.approx(1.0)
    assert effects.impact_play(dev("Impact", ["FR"]), d, 0) == 0.0
    assert effects.impact_play(dev("Impact", ["RR"]), d, 0) == 0.0


def test_impact_frontal_hit_plays_on_both_fronts():
    d = tick(acc=(0.0, 6 * G))  # pushed backward: the obstacle is in front
    fl, fr = (effects.impact_play(dev("Impact", [w]), d, 0) for w in ("FL", "FR"))
    assert fl == pytest.approx(fr) and fl > 0
    assert effects.impact_play(dev("Impact", ["RL"]), d, 0) == 0.0


def test_impact_compares_with_the_recent_level():
    # 5 g of steady cornering is no hit; the same 5 g arriving at once is
    assert effects.impact_play(dev("Impact"), tick(acc=(5 * G, 0.0), base=(5 * G, 0.0)), 0) == 0.0
    assert effects.impact_play(dev("Impact"), tick(acc=(5 * G, 0.0), base=(0.0, 0.0)), 0) > 0


def test_impact_ignores_a_bogus_huge_reading():
    assert effects.impact_play(dev("Impact"), tick(acc=(400 * G, 0.0), glitch=True), 0) == 0.0


def test_impact_keeps_a_huge_reading_that_comes_with_a_crash():
    assert effects.impact_play(dev("Impact"), tick(acc=(-70 * G, 0.0), glitch=False), 0) == pytest.approx(1.0)


# ---------- AccBase: the recent level, and telling glitches from crashes ----------

def test_accbase_flags_a_huge_reading_with_the_speed_unchanged():
    b = effects.AccBase()
    b.update((0.0, 0.0), 0.00, (0.0, -16.0), key=1)
    b.update((0.0, 400 * G), 0.01, (0.0, -16.0), key=2)
    assert b.glitch


def test_accbase_keeps_a_huge_reading_when_the_velocity_changes_with_it():
    b = effects.AccBase()
    b.update((0.0, 0.0), 0.00, (0.0, -53.0), key=1)
    b.update((0.0, 70 * G), 0.01, (0.0, -46.0), key=2)   # 7 m/s lost in 10 ms: 71 g, real
    assert not b.glitch


def test_accbase_a_glitch_never_enters_the_level():
    b = effects.AccBase()
    b.update((0.0, 0.0), 0.00, (0.0, -16.0), key=1)
    b.update((0.0, 400 * G), 0.01, (0.0, -16.0), key=2)
    assert b.update((0.0, 0.0), 0.02, (0.0, -16.0), key=3) == (0.0, 0.0)


def test_accbase_same_sample_read_twice_counts_once():
    b = effects.AccBase()
    b.update((0.0, 0.0), 0.00, (0.0, -16.0), key=1)
    first = b.update((G, 0.0), 0.01, (0.0, -16.0), key=2)
    assert b.update((G, 0.0), 0.02, (0.0, -16.0), key=2) == first


# ---------- GForce ----------

def test_gforce_braking_loads_the_fronts():
    d = tick(base=(0.0, 1.5 * G))   # decelerating: the acceleration points backward
    assert effects.gforce_play(dev("GForce", ["FL"]), d, 0) == pytest.approx(0.5)
    assert effects.gforce_play(dev("GForce", ["FR"]), d, 0) == pytest.approx(0.5)
    assert effects.gforce_play(dev("GForce", ["RL", "RR"]), d, 0) == 0.0


def test_gforce_left_corner_loads_the_right_side():
    d = tick(base=(1.5 * G, 0.0))   # turning left: the acceleration points left
    assert effects.gforce_play(dev("GForce", ["FR"]), d, 0) > 0
    assert effects.gforce_play(dev("GForce", ["RR"]), d, 0) > 0
    assert effects.gforce_play(dev("GForce", ["FL"]), d, 0) == 0.0


def test_gforce_threshold_full_and_crash_limit():
    assert effects.gforce_play(dev("GForce"), tick(base=(0.9 * G, 0.0)), 0) == 0.0
    assert effects.gforce_play(dev("GForce"), tick(base=(2.5 * G, 0.0)), 0) == pytest.approx(1.0)
    assert effects.gforce_play(dev("GForce"), tick(base=(7 * G, 0.0)), 0) == 0.0   # a crash: Impact's


# ---------- RevLimiter, Speed ----------

def test_rev_limiter():
    d = dev("RevLimiter", [], threshold=0.97)
    assert effects.rev_play(d, tick(rpm=0.96 * 8000), 0) == 0.0
    assert effects.rev_play(d, tick(rpm=0.985 * 8000), 0) == pytest.approx(0.5)
    assert effects.rev_play(d, tick(rpm=8200), 0) == pytest.approx(1.0)
    assert effects.rev_play(d, tick(rpm=5000, maxrpm=0), 0) == 0.0   # max not known


def test_speed():
    d = dev("Speed", [], threshold=100, full=250)
    assert effects.speed_play(d, tick(speed=90 / 3.6), 0) == 0.0
    assert effects.speed_play(d, tick(speed=175 / 3.6), 0) == pytest.approx(0.5)
    assert effects.speed_play(d, tick(speed=300 / 3.6), 0) == pytest.approx(1.0)


# ---------- Knock: how a hit is held and fades ----------

def test_knock_envelope_from_the_duration():
    assert effects.knock_envelope(dev("Impact", duration=0.4)) == pytest.approx((0.12, 0.28))
    assert effects.knock_envelope(dev("Suspension", ["FL"])) == (0.0, 0.06)
    assert effects.knock_envelope(dev("Kerb", ["FL"])) is None


def test_knock_holds_then_fades():
    k = effects.Knock(0.1, 0.2)
    assert k.update(30, 80, 0.0) == (30, 80)
    assert k.update(30, 0, 0.05) == (30, 80)                        # held
    assert k.update(30, 0, 0.2)[1] == pytest.approx(40)              # halfway down the fade
    assert k.update(30, 0, 0.31) == (30, 0.0)                        # over


def test_knock_a_stronger_hit_restarts_it_a_weaker_one_does_not():
    k = effects.Knock(0.1, 0.2)
    k.update(30, 50, 0.0)
    assert k.update(30, 20, 0.05) == (30, 50)
    assert k.update(30, 90, 0.06) == (30, 90)
    assert k.update(30, 0, 0.15) == (30, 90)                         # held from the new hit
