"""simdash -> haptics engine: replay and Test buttons played through the engine instead of their own streams.

While the engine runs (on the card the page plays to), simdash sends it, on a Unix datagram socket:

    b"P" + s16le 8-channel PCM at replay.RATE   sound to mix into the engine's output, in order
    b"F"                                         drop what is still queued (seek, pause, stop)
    b"J" + JSON                                  {"pedals": values, "source": "replay"}: a replay sample for the pedals
                                                 {"pedal_test": [pedal, effect]}: a pedal Test button

The engine mixes the sound with the live effects, so there is one stream on the card and one connection to
the pedals. Without the engine, callers fall back to pacat and simdash's own pedal driver.
"""

import json
import socket
import time
from array import array
from pathlib import Path

PATH = Path.home() / ".local/state/simdash-haptics.sock"
DGRAM = 6400        # bytes of PCM per datagram: 50 ms of 8 channels at 8 kHz
FRAME = 16          # bytes per 8-channel s16le frame


def engine_devid():
    """The card the running engine plays to, or None without the engine."""
    from . import engine
    st = engine.read_status()
    return st.get("devid") if st else None


def usable(devid):
    d = engine_devid()
    return bool(d) and (not devid or devid == d)


class EngineOut:
    """The replay's or a test's sound, sent to the engine. write() raises OSError when the engine is gone."""

    def __init__(self, path=None, timeout=1.0):
        self.path = str(path or PATH)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)  # the engine drains every 10 ms: a full queue clears at once

    def _send(self, data):
        self.sock.sendto(data, self.path)

    def write(self, pcm):
        for i in range(0, len(pcm), DGRAM):
            self._send(b"P" + pcm[i:i + DGRAM])

    def flush(self):
        try:
            self._send(b"F")
        except OSError:
            pass

    def message(self, d):
        self._send(b"J" + json.dumps(d, separators=(",", ":")).encode())

    def close(self):
        self.sock.close()


def send(d):
    """One command to the engine; False if it is not there."""
    out = EngineOut(timeout=0.2)
    try:
        out.message(d)
        return True
    except OSError:
        return False
    finally:
        out.close()


def flush():
    """Have the engine drop the sound it still has queued."""
    out = EngineOut(timeout=0.2)
    try:
        out.flush()
    finally:
        out.close()


def resample(pcm44, rate_in, rate_out):
    """8-channel s16le from rate_in to rate_out by nearest sample (the shakers feel nothing above ~200 Hz)."""
    src = array("h", pcm44)
    n_in = len(src) // 8
    n_out = int(n_in * rate_out / rate_in)
    out = array("h", bytes(n_out * FRAME))
    step = rate_in / rate_out
    for j in range(n_out):
        i = int(j * step) * 8
        out[j * 8:j * 8 + 8] = src[i:i + 8]
    return out.tobytes()


class Inbox:
    """The engine's end: queued sound, and commands handed to the engine."""

    MAX = 10.0  # s of sound kept queued at most (a test is a few seconds; the replay sends ~0.1 s ahead)

    def __init__(self, rate, path=None):
        self.rate = rate
        self.path = path or PATH
        self.buf = bytearray()
        self.sock = None
        self.got = 0.0   # monotonic time sound last arrived

    def open(self):
        try:
            self.path.unlink(missing_ok=True)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            s.bind(str(self.path))
            s.setblocking(False)
            self.path.chmod(0o600)
            self.sock = s
        except OSError:
            self.sock = None
        return self.sock is not None

    def drain(self, on_message):
        """Read everything waiting; JSON commands go to on_message(dict)."""
        if self.sock is None:
            return
        while True:
            try:
                d = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            if not d:
                continue
            kind, body = d[:1], d[1:]
            if kind == b"P":
                self.buf += body
                self.got = time.monotonic()
                over = len(self.buf) - int(self.MAX * self.rate) * FRAME
                if over > 0:
                    del self.buf[:over - over % FRAME]
            elif kind == b"F":
                self.buf.clear()
            elif kind == b"J":
                try:
                    on_message(json.loads(body))
                except (ValueError, TypeError):
                    pass

    def pending(self):
        return len(self.buf) >= FRAME

    def mix_into(self, mix, n):
        """Add up to n frames of queued sound to mix (floats, 8 per frame)."""
        k = min(n * FRAME, len(self.buf) - len(self.buf) % FRAME)
        if not k:
            return
        a = array("h", bytes(self.buf[:k]))
        del self.buf[:k]
        for i, v in enumerate(a):
            mix[i] += v / 32767

    def close(self):
        if self.sock is not None:
            self.sock.close()
            try:
                self.path.unlink()
            except OSError:
                pass
