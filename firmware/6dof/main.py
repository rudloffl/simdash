"""Desk 6DOF firmware for a pyboard v1.1 (MicroPython) + PCA9685.

simdash does the maths and sends one text line per update over the USB serial port:
  S <us0> <us1> <us2> <us3> <us4> <us5>   pulse widths for servos 0-5 (PCA9685 channels 0-5)
  O                                       let go: all outputs off (servos limp)
  H                                       hello -> replies "6DOF <version> ch=<n>"
No line for TIMEOUT_MS -> all off (USB unplugged, simdash stopped, PC asleep).
Pulses are clamped to MIN_US..MAX_US here too, whatever the PC sends.
Ctrl-C still breaks into the REPL (text only, so no 0x03 byte appears in normal traffic).

LEDs: green = driving the servos, red blinking = off (timed out, or told to let go).
"""
import pyb
from machine import I2C, Pin

from pca9685 import PCA9685

VERSION = "1"
CHANNELS = (0, 1, 2, 3, 4, 5)   # PCA9685 channel of servo 0..5
MIN_US, MAX_US = 600, 2400
TIMEOUT_MS = 1000
FREQ = 50                       # Hz; analog MG90 servos want ~50 Hz
OSC = 25_000_000                # PCA9685 clock; trim if a 1500 µs pulse measures off (scope / servo tester)

oe = Pin("X8", Pin.OUT, value=1)   # high = outputs disabled until the PCA9685 is set up
i2c = I2C(1, freq=400_000)         # X9 = SCL, X10 = SDA
pca = PCA9685(i2c, freq=FREQ, osc=OSC)
oe.value(0)

usb = pyb.USB_VCP()
green, red = pyb.LED(2), pyb.LED(1)
last_ms = pyb.millis()
driving = False
line = b""


def let_go():
    global driving
    pca.all_off()
    driving = False
    green.off()


def handle(cmd):
    global last_ms, driving
    parts = cmd.split()
    if not parts:
        return
    op = parts[0]
    if op == b"S" and len(parts) == 1 + len(CHANNELS):
        for ch, v in zip(CHANNELS, parts[1:]):
            pca.pulse_us(ch, min(MAX_US, max(MIN_US, int(v))))
        last_ms, driving = pyb.millis(), True
        green.on(); red.off()
    elif op == b"O":
        let_go()
    elif op == b"H":
        usb.write("6DOF %s ch=%d\n" % (VERSION, len(CHANNELS)))


let_go()
while True:
    n = usb.any()
    if n:
        line += usb.read(n)
        while b"\n" in line:
            cmd, line = line.split(b"\n", 1)
            try:
                handle(cmd.strip())
            except (ValueError, OSError):
                pass
        if len(line) > 200:
            line = b""
    if driving and pyb.elapsed_millis(last_ms) > TIMEOUT_MS:
        let_go()
    if not driving:
        red.value((pyb.millis() // 500) % 2)
    pyb.delay(1)
