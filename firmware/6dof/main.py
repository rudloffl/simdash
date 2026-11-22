"""Desk 6DOF firmware for a pyboard v1.1 (MicroPython) + PCA9685. The logic is in board.py.

simdash does the maths and sends text lines over the USB serial port (protocol in board.py).
Ctrl-C still breaks into the REPL (text only, so no 0x03 byte appears in normal traffic).
LEDs: green = driving the servos, yellow = gliding to park, red blinking = limp.
Copy main.py, board.py and pca9685.py to the board.
"""
import pyb
from machine import I2C, Pin

from board import Board
from pca9685 import PCA9685

FIRST_CHANNEL = 0               # servos 0-5 on PCA9685 channels FIRST_CHANNEL.. FIRST_CHANNEL+5
MIN_US, MAX_US = 800, 2200      # hard limits whatever the PC sends (MG90 end stops are near 600 / 2400)
RATE_US_PER_MS = 6.0            # slew limit, about an MG90's top speed
TIMEOUT_MS = 1000               # silence before gliding to park
TICK_MS = 5                     # output update period
FREQ = 50                       # Hz; analog MG90 servos want ~50 Hz
OSC = 25_000_000                # PCA9685 clock; trim if a 1500 µs pulse measures off (scope / servo tester)

oe = Pin("X8", Pin.OUT, value=1)   # high = outputs disabled until the PCA9685 is set up
i2c = I2C(1, freq=400_000)         # X9 = SCL, X10 = SDA
pca = PCA9685(i2c, freq=FREQ, osc=OSC)
oe.value(0)


class Out:
    def write(self, us):
        pca.pulses_us(FIRST_CHANNEL, us)

    def off(self):
        pca.all_off()


board = Board(Out(), min_us=MIN_US, max_us=MAX_US, rate_us_per_ms=RATE_US_PER_MS, timeout_ms=TIMEOUT_MS)
usb = pyb.USB_VCP()
red, green, yellow = pyb.LED(1), pyb.LED(2), pyb.LED(3)
line = b""
last_tick = pyb.millis()

board.let_go()
while True:
    n = usb.any()
    if n:
        line += usb.read(n)
        while b"\n" in line:
            cmd, line = line.split(b"\n", 1)
            try:
                reply = board.line(cmd.strip().decode(), pyb.millis())
                if reply:
                    usb.write(reply)
            except (ValueError, OSError):
                pass
        if len(line) > 200:
            line = b""
    now = pyb.millis()
    dt = pyb.elapsed_millis(last_tick)
    if dt >= TICK_MS:
        last_tick = now
        board.tick(now, dt)
        green.value(board.state == "drive")
        yellow.value(board.state in ("glide", "hold"))
        red.value(board.state == "limp" and (now // 500) % 2)
    pyb.delay(1)
