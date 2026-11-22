"""Desk 6DOF demo player: a recorded race baked into demo.bin by simdash, played with no PC.

demo.bin, little-endian (37-byte header, then the frames):
  magic b"6DOFDEMO", u8 version (1), u8 rate_hz (25), u16 frames, 6 x u16 center_us, 6 x u16 park_us, u8 step_us
  then frames x 6 int8: pulse = center_us + step_us x value
Frames are streamed from the file through one preallocated buffer (the file is never loaded whole) and
interpolated up to the output rate (25 -> 50 Hz: every other output is the midpoint of two frames).
Plain Python: runs in MicroPython 1.9.4 and in CPython (tests).
"""

MAGIC = b"6DOFDEMO"
HEADER = 37


def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


class DemoError(Exception):
    pass


class DemoPlayer:
    def __init__(self, open_file, path, out_rate=50, chunk=50):
        self.open_file, self.path, self.out_rate = open_file, path, out_rate
        self.buf = bytearray(6 * chunk)      # the only buffer the frames go through
        self.f = None
        f = self._open()                     # check the header now: a bad file fails here, not mid-demo
        f.close()
        self.f = None

    def _open(self):
        f = self.open_file(self.path, "rb")
        h = f.read(HEADER)
        if h is None or len(h) < HEADER or bytes(h[:8]) != MAGIC:
            f.close()
            raise DemoError("not a demo file")
        if h[8] != 1:
            f.close()
            raise DemoError("demo version %d" % h[8])
        self.rate, self.frames, self.step = h[9], _u16(h, 10), h[36]
        if self.rate <= 0 or self.out_rate % self.rate:
            f.close()
            raise DemoError("rate %d" % self.rate)
        self.center = [_u16(h, 12 + 2 * i) for i in range(6)]
        self.park = [_u16(h, 24 + 2 * i) for i in range(6)]
        self.sub_n = self.out_rate // self.rate
        return f

    def start(self):
        """From the first frame."""
        self.close()
        self.f = self._open()
        self.read = 0                        # frames read from the file so far
        self.pos = self.have = 0             # position in / frames in the buffer
        self.cur, self.nxt = [0] * 6, [0] * 6
        self.out = [0] * 6
        self.sub = 0
        self.has_cur = self._frame(self.cur)
        self.has_nxt = self.has_cur and self._frame(self.nxt)

    def _frame(self, into):
        """The next frame's pulses into a list; False at the end."""
        if self.read >= self.frames:
            return False
        if self.pos >= self.have:
            n = self.f.readinto(self.buf)
            if not n:
                raise DemoError("demo file shorter than its header says")
            self.have, self.pos = n // 6, 0
            if self.have == 0:
                raise DemoError("truncated frame")
        b, o, c, s = self.buf, 6 * self.pos, self.center, self.step
        for i in range(6):
            v = b[o + i]
            into[i] = c[i] + s * (v - 256 if v > 127 else v)
        self.pos += 1
        self.read += 1
        return True

    def next(self):
        """The pulses for the next output tick, or None when the clip has ended."""
        if not self.has_cur:
            return None
        out, cur = self.out, self.cur
        if self.sub == 0 or not self.has_nxt:
            for i in range(6):
                out[i] = cur[i]
        else:
            k, nxt = self.sub / self.sub_n, self.nxt
            for i in range(6):
                out[i] = int(cur[i] + (nxt[i] - cur[i]) * k + 0.5)
        self.sub += 1
        if self.sub >= self.sub_n or not self.has_nxt:
            self.sub = 0
            self.cur, self.nxt = self.nxt, self.cur
            self.has_cur = self.has_nxt
            self.has_nxt = self.has_cur and self._frame(self.nxt)
        return out

    def close(self):
        if self.f is not None:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None
