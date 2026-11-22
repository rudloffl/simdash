"""engine.CardGuard: keeps the haptics stream on the shaker card (2026-11-21: a race with silent shakers)."""
from simdash import engine

DEV = "alsa_output.usb-0d8c_USB_Sound_Device-00.analog-surround-71"
CARD = "alsa_card.usb-0d8c_USB_Sound_Device-00"


def inputs(sink, pid=4242):
    return (f"Sink Input #100\n\tDriver: PipeWire\n\tSink: 66\n\tProperties:\n\t\tapplication.process.id = \"999\"\n"
            f"Sink Input #2879\n\tDriver: PipeWire\n\tSink: {sink}\n\tProperties:\n\t\tmedia.name = \"simdash haptics\"\n"
            f"\t\tapplication.process.id = \"{pid}\"\n")


class FakePactl:
    """A sound server whose card can lose its 7.1 output; records the commands sent."""

    def __init__(self, stream_sink="2931", has_71=True):
        self.stream_sink, self.has_71, self.sent = stream_sink, has_71, []

    def __call__(self, args):
        if args[:2] == ["list", "sink-inputs"]:
            return inputs(self.stream_sink)
        if args[:3] == ["list", "short", "sinks"]:
            rows = ["66\talsa_output.pci-0000_10_00.6.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tRUNNING"]
            rows.append(f"2931\t{DEV}\tPipeWire\ts16le 8ch 48000Hz\tRUNNING" if self.has_71
                        else "2940\talsa_output.usb-0d8c_USB_Sound_Device-00.iec958-stereo\tPipeWire\ts16le 2ch\tIDLE")
            return "\n".join(rows)
        if args[:2] == ["list", "cards"]:
            prof = "output:analog-surround-71" if self.has_71 else "output:iec958-stereo+input:analog-stereo"
            return f"Card #1\n\tName: alsa_card.pci-0000_10_00.6\n\tActive Profile: output:analog-stereo\nCard #2\n\tName: {CARD}\n\tActive Profile: {prof}\n"
        self.sent.append(args)
        if args[0] == "set-card-profile":
            self.has_71 = True
        if args[0] == "move-sink-input":
            self.stream_sink = "2931"
        return ""


def guard(fake):
    g = engine.CardGuard(run=fake)
    g.devid = DEV
    return g


def test_card_of():
    assert engine.CardGuard.card_of(DEV) == (CARD, "output:analog-surround-71")
    assert engine.CardGuard.card_of("bluez_output.x") == (None, None)


def test_all_well_does_nothing():
    fake = FakePactl()
    assert guard(fake).check(pid=4242) is None and fake.sent == []


def test_a_moved_stream_is_moved_back():
    fake = FakePactl(stream_sink="66")
    what = guard(fake).check(pid=4242)
    assert fake.sent == [["move-sink-input", "2879", DEV]] and "moved back" in what


def test_a_profile_change_is_undone_then_the_stream_moved_back(monkeypatch):
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    fake = FakePactl(stream_sink="66", has_71=False)
    g = guard(fake)
    what = g.check(pid=4242)
    assert fake.sent == [["set-card-profile", CARD, "output:analog-surround-71"], ["move-sink-input", "2879", DEV]]
    assert "iec958" in what and g.snapshot()[1] == 1


def test_idle_engine_is_left_alone():
    fake = FakePactl(stream_sink="66")
    assert guard(fake).check(pid=1) is None and fake.sent == []   # not our stream: the card isn't open
