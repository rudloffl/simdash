"""Desk 6DOF firmware for a pyboard v1.1 (MicroPython 1.9.4 or later) + PCA9685.

The logic is in board.py (pulses: clamp, slew limit, park, glide), demo.py (the standalone demo file) and
fw.py (PC control, demo mode, USR switch, LEDs); this file only wires them to the hardware.
Copy main.py, fw.py, board.py, demo.py and pca9685.py to the board.

LEDs: blue = demo playing, green = the PC driving, yellow = gliding to park, red blinking = limp
(fast: no PCA9685; 4 Hz: the demo file failed), red steady = stopped with USR (latched).
Ctrl-C still breaks into the REPL (the protocol is text only), and lets go of the servos on the way out.
"""
import os

import pyb
import utime
from machine import I2C, Pin

from board import Board
from fw import Firmware
from pca9685 import PCA9685

FIRST_CHANNEL = 0               # servos 0-5 on PCA9685 channels FIRST_CHANNEL.. FIRST_CHANNEL+5
MIN_US, MAX_US = 800, 2200      # hard limits whatever the PC or the demo says (MG90 end stops near 600 / 2400)
RATE_US_PER_MS = 6.0            # slew limit, about an MG90's top speed
TIMEOUT_MS = 1000               # PC silence before gliding to park
FREQ = 50                       # Hz; analog MG90 servos want ~50 Hz
OSC = 25000000                  # PCA9685 clock; trim if a 1500 µs pulse measures off (scope / servo tester)

oe = Pin("X8", Pin.OUT, value=1)   # high = outputs disabled until the PCA9685 is set up
i2c = I2C(1, freq=400000)          # X9 = SCL, X10 = SDA
try:
    pca = PCA9685(i2c, freq=FREQ, osc=OSC)
    oe.value(0)
except OSError:
    pca = None                     # not wired / not powered: answer the PC, never drive anything


class Out:
    def write(self, us):
        if pca:
            pca.pulses_us(FIRST_CHANNEL, us)

    def off(self):
        if pca:
            pca.all_off()


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


board = Board(Out(), min_us=MIN_US, max_us=MAX_US, rate_us_per_ms=RATE_US_PER_MS, timeout_ms=TIMEOUT_MS)
usb = pyb.USB_VCP()
sw = pyb.Switch()
fw = Firmware(board, usb.write, (pyb.LED(1), pyb.LED(2), pyb.LED(3), pyb.LED(4)), sw, open, exists,
              pca_ok=pca is not None)

board.let_go()
last = utime.ticks_ms()
try:
    while True:
        n = usb.any()
        if n:
            fw.feed(usb.read(n))
        now = utime.ticks_ms()
        # one clock: only ever adds the time measured between two looks (no ms counted twice, no wrap)
        fw.step(utime.ticks_diff(now, last))
        last = now
        pyb.delay(1)
finally:
    if pca:
        pca.all_off()              # interrupted (Ctrl-C, a copy from the PC): never leave the servos holding
