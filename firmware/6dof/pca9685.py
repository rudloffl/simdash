"""Minimal PCA9685 driver for servos (MicroPython)."""
import time

MODE1, MODE2, PRESCALE, LED0 = 0x00, 0x01, 0xFE, 0x06


class PCA9685:
    def __init__(self, i2c, addr=0x40, freq=50, osc=25_000_000):
        self.i2c, self.addr, self.freq = i2c, addr, freq
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

    def pulses_us(self, first, us):
        """Consecutive channels first, first+1, ... in ONE I2C write (auto-increment), so they all
        change in the same PWM frame."""
        b = bytearray(4 * len(us))
        for k, v in enumerate(us):
            off = int(v / self.us_per_count + 0.5)
            b[4 * k + 2], b[4 * k + 3] = off & 0xFF, (off >> 8) & 0x0F
        self.i2c.writeto_mem(self.addr, LED0 + 4 * first, b)

    def all_off(self):
        self.i2c.writeto_mem(self.addr, 0xFD, b"\x10")          # ALL_LED_OFF_H full-off bit (no auto-increment needed)
