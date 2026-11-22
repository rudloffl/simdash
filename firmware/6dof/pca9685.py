"""Minimal PCA9685 driver for servos (MicroPython)."""
import time

MODE1, MODE2, PRESCALE, LED0 = 0x00, 0x01, 0xFE, 0x06


class PCA9685:
    def __init__(self, i2c, addr=0x40, freq=50, osc=25_000_000):
        self.i2c, self.addr, self.freq = i2c, addr, freq
        self.buf = bytearray(4)
        self.all_off()
        self.i2c.writeto_mem(addr, MODE2, b"\x04")             # totem-pole outputs
        prescale = round(osc / (4096 * freq)) - 1
        self.i2c.writeto_mem(addr, MODE1, b"\x10")             # sleep to set the prescaler
        self.i2c.writeto_mem(addr, PRESCALE, bytes([prescale]))
        self.i2c.writeto_mem(addr, MODE1, b"\x20")             # wake, register auto-increment
        time.sleep_ms(1)
        self.i2c.writeto_mem(addr, MODE1, b"\xa0")             # restart PWM
        # length of one count in µs, from the frequency actually set
        self.us_per_count = 1_000_000 * (prescale + 1) / osc

    def pulse_us(self, ch, us):
        off = int(us / self.us_per_count + 0.5)
        b = self.buf
        b[0], b[1], b[2], b[3] = 0, 0, off & 0xFF, (off >> 8) & 0x0F
        self.i2c.writeto_mem(self.addr, LED0 + 4 * ch, b)

    def off(self, ch):
        self.i2c.writeto_mem(self.addr, LED0 + 4 * ch, b"\x00\x00\x00\x10")  # full off: servo goes limp

    def all_off(self):
        self.i2c.writeto_mem(self.addr, 0xFD, b"\x10")          # ALL_LED_OFF_H full-off bit (no auto-increment needed)
