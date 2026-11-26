"""Desk 6DOF status ring: a WS2812B ring (12 pixels by default) on SPI2 MOSI (Y8).

No precision bit-banging: each WS2812 bit becomes 3 SPI bits (0 = 0b100, 1 = 0b110) at a baudrate chosen
so one SPI bit is about a third of a WS2812 bit cell (3.2 MHz: 312.5 ns/bit, close enough to the 0.4/0.8 µs
high times the datasheet wants); the SPI peripheral's own clock does the timing, not the interpreter. Each
colour byte (sent MSB first, GRB order: WS2812B wants green first) becomes 3 SPI bytes, so one pixel is 9
bytes; a run of zero bytes at the end holds the line low long enough to latch.

Plain buffer logic, testable in CPython; main.py supplies the hardware spi.write() and SPI2 shares Y8 with
the planned OLED (not wired yet): give the OLED's SDA a different pin when that's built.
"""

N = 12               # pixels on the ring
RESET_BYTES = 12      # ~30 µs low at 3.2 MHz: comfortably over WS2812's reset gap (>= 50 µs is the old spec;
                      # some newer parts want 280 µs -- lengthen this if a real ring needs it

# byte -> 3 SPI bytes (24 bits: one 0b100/0b110 triple per input bit, MSB first)
_LUT = bytearray(256 * 3)
for _v in range(256):
    _bits = 0
    for _i in range(8):
        _bits = (_bits << 3) | (0b110 if _v & (0x80 >> _i) else 0b100)
    _LUT[_v * 3] = (_bits >> 16) & 0xFF
    _LUT[_v * 3 + 1] = (_bits >> 8) & 0xFF
    _LUT[_v * 3 + 2] = _bits & 0xFF


class Ring:
    """n pixels; show() takes a flat [r, g, b, r, g, b, ...] list (0-255 each, n * 3 long)."""

    def __init__(self, spi_write, n=N):
        self.spi_write = spi_write
        self.n = n
        self.buf = bytearray(n * 9 + RESET_BYTES)   # the tail is already zero: bytearray() starts zeroed

    def show(self, rgb):
        buf = self.buf
        for i in range(self.n):
            r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
            o = i * 9
            buf[o:o + 3] = _LUT[g * 3:g * 3 + 3]       # WS2812B order: green, red, blue
            buf[o + 3:o + 6] = _LUT[r * 3:r * 3 + 3]
            buf[o + 6:o + 9] = _LUT[b * 3:b * 3 + 3]
        self.spi_write(buf)

    def off(self):
        self.show([0] * (3 * self.n))


ROT_DEG_PER_MS = 360 / 4000   # one full turn every 4 s


def rainbow(t_ms, n=N, v=160):
    """A rotating rainbow for when nothing is driving the ring (no PC, or simdash hasn't sent one lately):
    needs nothing but the clock, so it runs standalone like the servo demo does."""
    out = []
    for i in range(n):
        out.extend(_hsv((t_ms * ROT_DEG_PER_MS + i * 360 / n) % 360, v))
    return out


def _hsv(hue, v):
    """Hue 0-360, full saturation, value 0-255 (int) -> (r, g, b)."""
    h = hue / 60.0
    i = int(h) % 6
    f = h - int(h)
    q, t = round(v * (1 - f)), round(v * f)
    if i == 0:
        return (v, t, 0)
    if i == 1:
        return (q, v, 0)
    if i == 2:
        return (0, v, t)
    if i == 3:
        return (0, q, v)
    if i == 4:
        return (t, 0, v)
    return (v, 0, q)
