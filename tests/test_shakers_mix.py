"""Test all: every effect's test summed into one 8-channel buffer."""
from simdash import shakers


def eff(effect, pan, volume=100, **kw):
    return {"device": "Sound", "effect": effect, "pan": pan, "volume": volume, "frequency": 40, "amplitude": 60, **kw}


def test_effects_on_different_channels_keep_their_own_signal():
    a, b = eff("Gear", 0, duration=0.1), eff("Gear", 3, volume=50, duration=0.1)
    pcm, reduced = shakers.mix([a, b])
    alone = shakers.render(a)
    assert reduced == {}
    assert pcm[0::8][:len(alone) // 8] == alone[0::8]                        # channel 0 untouched
    peak3 = max(abs(v) for v in pcm[3::8])
    assert abs(peak3 - max(abs(v) for v in alone[0::8]) * 0.5) <= 2          # channel 3 at its own volume (50 %)


def test_a_channel_that_would_clip_is_limited_not_clipped():
    loud = [eff("Gear", 7, amplitude=100, duration=0.1), eff("Gear", 7, amplitude=100, duration=0.1)]
    pcm, reduced = shakers.mix(loud)
    assert 45 <= reduced[7] <= 60                                             # two full-scale tones: about halved
    ceil = 32767 * shakers.load_shaping()["ceiling"] / 100
    assert max(abs(v) for v in pcm) <= ceil * 1.02                            # kept under the ceiling
    # only while too loud: a quiet effect alone elsewhere in time is untouched
    quiet = [eff("Gear", 7, amplitude=30, duration=0.1)]
    pcm_q, red_q = shakers.mix(quiet)
    assert red_q == {} and pcm_q == shakers.render(quiet[0])


def test_the_mix_lasts_as_long_as_the_longest_effect():
    short, long_ = eff("Gear", 0, duration=0.1), eff("Engine", 1)
    pcm, _ = shakers.mix([short, long_])
    assert len(pcm) == max(len(shakers.render(short)), len(shakers.render(long_)))


def test_test_all_refuses_a_missing_card(monkeypatch):
    import pytest
    monkeypatch.setattr(shakers, "sinks", lambda: [])
    with pytest.raises(LookupError):
        shakers.play_all([eff("Gear", 0)], "alsa_output.nothing")
