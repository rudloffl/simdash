"""Desk 6DOF firmware for a pyboard v1.1 (MicroPython 1.9.4 or later) + PCA9685.

The logic is in board.py (pulses: clamp, slew limit, park, glide), demo.py (the standalone demo file) and
fw.py (PC control, demo mode, USR switch, LEDs); this file only wires them to the hardware.
Copy main.py, fw.py, board.py, demo.py, ring.py and pca9685.py to the board.

LEDs: blue = demo playing, green = the PC driving, green flash every 2 s = on a PC, waiting for simdash
(servos limp), yellow = gliding to park. Red only for a problem: blinking 1 Hz = limp with no PC, 5 Hz = no
PCA9685, 4 Hz = the demo file failed; steady = stopped with USR (latched).
Ctrl-C still breaks into the REPL (the protocol is text only), and lets go of the servos on the way out.

SD card: /flash/SKIPSD (written by simdash's Install firmware) stops the pyboard from booting off a card,
so this firmware on /flash always starts; main.py then mounts a card itself at /sd (a demo there wins over
the one on /flash). A card that can't be mounted is ignored.

Watchdog: the board resets itself (servos limp, as with RST) when the timed part of the loop (outputs,
speed limit, PC timeout, LEDs) hasn't run for WDT_MS. It can't be stopped once started: after Ctrl-C it is
set to WDT_REPL_MS and `wdt` stays in the REPL's globals (simdash's copy tool feeds it); an idle REPL
resets after that long. For a REPL with no watchdog, start in safe mode (hold USR while pressing RST,
release at the orange LED): main.py doesn't run.
"""
import os

import pyb
import machine
import utime
from machine import I2C, Pin

from board import Board
from fw import Firmware
from pca9685 import PCA9685
from ring import Ring

FIRST_CHANNEL = 0               # servos 0-5 on PCA9685 channels FIRST_CHANNEL.. FIRST_CHANNEL+5
MIN_US, MAX_US = 800, 2200      # hard limits whatever the PC or the demo says (MG90 end stops near 600 / 2400)
RATE_US_PER_MS = 6.0            # slew limit, about an MG90's top speed
TIMEOUT_MS = 1000               # PC silence before gliding to park
FREQ = 50                       # Hz; analog MG90 servos want ~50 Hz
OSC = 25000000                  # PCA9685 clock; trim if a 1500 µs pulse measures off (scope / servo tester)
WDT_MS = 2000                   # the timed loop stuck this long: reset (servos limp)
WDT_REPL_MS = 30000             # after Ctrl-C (REPL, a copy from the PC)

USB_DCFG = 0x50000800             # STM32F405 USB OTG FS device config: bits 4-10 = the address the host gave


def on_a_pc():
    """A PC has enumerated the board (a USB charger never gives it an address): simdash decides then."""
    return (machine.mem32[USB_DCFG] >> 4) & 0x7F != 0


RESET = {machine.PWRON_RESET: "power", machine.HARD_RESET: "button", machine.WDT_RESET: "watchdog",
         machine.DEEPSLEEP_RESET: "sleep", machine.SOFT_RESET: "soft"}.get(machine.reset_cause(), "other")

def mount_sd():
    """/sd from the card when one is in (SKIPSD keeps the boot from doing it). True when mounted."""
    try:
        if "sd" in os.listdir("/"):
            return True
        sd = pyb.SDCard()
        if not sd.present():
            return False
        os.mount(sd, "/sd")
        return True
    except OSError:
        return False


SD = mount_sd()

oe = Pin("X8", Pin.OUT, value=1)   # high = outputs disabled until the PCA9685 is set up
i2c = I2C(1, freq=400000)          # X9 = SCL, X10 = SDA
try:
    pca = PCA9685(i2c, freq=FREQ, osc=OSC)
    oe.value(0)
except OSError:
    pca = None                     # not wired / not powered: answer the PC, never drive anything

# status ring (optional): SPI2 on Y8 (MOSI2) only, bit-banged through the SPI hardware (ring.py); the OLED
# is only planned, not wired, so this is free for now. Nothing to detect: writing to an unplugged ring is
# harmless, so it's always set up.
spi2 = machine.SPI(2, baudrate=3200000, polarity=0, phase=0, firstbit=machine.SPI.MSB)
ring = Ring(spi2.write)


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
              pca_ok=pca is not None, autoplay=RESET != "watchdog",   # it froze: stay limp, no demo
              host=on_a_pc, ring=ring)                                # on a PC: only what simdash sends

loops = 0
fw.stats = lambda: " loops=%d ms=%d reset=%s wdt=%d sd=%d" % (loops, utime.ticks_ms(), RESET, WDT_MS, SD)

board.let_go()
last = utime.ticks_ms()
wdt = machine.WDT(timeout=WDT_MS)
fed = fw.ticks
try:
    while True:
        loops = (loops + 1) & 0x3FFFFFFF   # a small int forever (no heap use); the PC takes differences
        # any() is True/False on MicroPython 1.9.4, not a count: read(any()) took ONE byte a loop
        # (~1 kB/s, about 30 S lines/s: simdash's 50 Hz stream fell behind and the board went limp)
        if usb.any():
            fw.feed(usb.read(256))
        now = utime.ticks_ms()
        # one clock: only ever adds the time measured between two looks (no ms counted twice, no wrap)
        fw.step(utime.ticks_diff(now, last))
        last = now
        if fw.ticks != fed:        # fed only while the timed part runs, not merely the loop
            fed = fw.ticks
            wdt.feed()
        pyb.delay(1)
finally:
    wdt = machine.WDT(timeout=WDT_REPL_MS)   # can't be stopped: the REPL gets longer
    if pca:
        pca.all_off()              # interrupted (Ctrl-C, a copy from the PC): never leave the servos holding
