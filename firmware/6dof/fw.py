"""Desk 6DOF firmware logic: PC control, the standalone demo, the USR switch, the LEDs.

Plain Python (MicroPython 1.9.4 and CPython for the tests); main.py wires it to the hardware and calls
feed() with what arrives on USB and step() with the milliseconds since the last call.

- At boot: wait WAIT_MS for a line from the PC. None, and a demo.bin on /sd or /flash: play it (blue LED);
  not when plugged into a PC (host(): the PC drives, through simdash, or nothing moves) and not after a
  watchdog reset (autoplay False: a board that froze stays limp). A long USR press still plays.
  Any line from the PC leaves the demo (glide to park) and the PC drives (green LED).
- The demo: 25 Hz frames interpolated to 50 Hz, output every TICK_MS through the board's slew limit and
  clamp; at the end glide to park, hold HOLD_MS, play again. A read error: let go, red blinks fast.
- USR short press: stop the demo and go limp, latched (no demo, no S/P from the PC) until the next power
  cycle or an A line from the PC. USR long press (LONG_MS): play the demo again from the start.
- Q: a one-line state report (STATE mode=... board=... ticks=...), answered in any mode, changing nothing.
"""

from demo import DemoError, DemoPlayer

WAIT_MS = 3000
TICK_MS = 20          # the servos' PWM frame: outputs updated at 50 Hz
HOLD_MS = 5000
LONG_MS = 1500
DEMO_PATHS = ("/sd/demo.bin", "/flash/demo.bin")


class Firmware:
    def __init__(self, board, write, leds, switch, open_file, exists, pca_ok=True, autoplay=True, host=None):
        self.board, self.write, self.switch = board, write, switch
        self.red, self.green, self.yellow, self.blue = leds
        self.open_file, self.pca_ok = open_file, pca_ok
        self.autoplay = autoplay  # False after a watchdog reset: stay limp, no demo at power-up
        self.host = host or (lambda: False)   # True when a PC has set the board up over USB: no demo then
        self.path = None
        for p in DEMO_PATHS:
            if exists(p):
                self.path = p
                break
        self.mode = "wait"        # wait | demo | park (between two plays) | pc
        self.latched = False
        self.error = False
        self.demo = None
        self.clock = 0
        self.since = 0
        self.line = b""
        self.press = None         # clock when USR went down
        self.long_done = False
        self.hold_until = 0
        self.played = 0           # demo plays started (status, tests)
        self.ticks = 0            # timed steps run (outputs, LEDs): main.py feeds the watchdog when it moves
        self.stats = None         # main.py: a callable giving extra " key=value" fields for the Q report

    # ---- PC

    def feed(self, data):
        self.line += data
        while b"\n" in self.line:
            cmd, self.line = self.line.split(b"\n", 1)
            try:
                self.command(cmd.strip().decode())
            except (ValueError, UnicodeError):
                pass
        if len(self.line) > 200:
            self.line = b""

    def command(self, text):
        parts = text.split()
        op = parts[0] if parts else ""
        if op == "Q":             # state report: changes nothing (not even the demo)
            self.write(self.report())
            return
        if self.mode != "pc":
            self.leave_demo()
            self.mode = "pc"
        if op == "A":
            self.latched = False
            self.error = False
            return
        if self.latched and op in ("S", "P"):
            return
        reply = self.board.line(text, self.clock)
        if reply:
            tags = ("" if self.pca_ok else " pca=missing") + (" latched" if self.latched else "")
            self.write(reply.replace("\n", tags + "\n"))

    def report(self):
        """One line: STATE key=value ... (no spaces in values). clock and ticks are counters: two reports
        a second apart show whether the timed part runs (ticks +50/s) even when lines are still answered."""
        s = "STATE mode=%s board=%s latched=%d error=%d pca=%d demo=%d played=%d clock=%d ticks=%d host=%d" % (
            self.mode, self.board.state, self.latched, self.error, self.pca_ok, self.path is not None,
            self.played, self.clock, self.ticks, bool(self.host()))
        if self.stats:
            s += self.stats()
        return s + "\n"

    # ---- demo

    def start_demo(self):
        if self.demo is not None:
            self.demo.close()
        try:
            self.demo = DemoPlayer(self.open_file, self.path)
            self.demo.start()
        except (OSError, DemoError):
            self.fail()
            return
        self.board.park = list(self.demo.park)
        self.mode = "demo"
        self.played += 1

    def leave_demo(self):
        if self.demo is not None:
            self.demo.close()
            self.demo = None
            self.board.glide_to_park()

    def fail(self):
        if self.demo is not None:
            self.demo.close()
        self.demo = None
        self.board.let_go()
        self.error = True
        self.mode = "pc"

    # ---- USR switch

    def _switch(self):
        down = self.switch()
        if down:
            if self.press is None:
                self.press, self.long_done = self.clock, False
            elif not self.long_done and self.clock - self.press >= LONG_MS:
                self.long_done = True           # long press: the demo again from the start
                self.latched, self.error = False, False
                if self.path and self.pca_ok:
                    self.start_demo()
        elif self.press is not None:
            if not self.long_done:              # short press: stop, limp, latched
                if self.demo is not None:
                    self.demo.close()
                    self.demo = None
                self.board.let_go()
                self.latched = True
                self.mode = "pc"
            self.press = None

    # ---- time

    def step(self, dt):
        self.clock += dt
        self.since += dt
        self._switch()
        if self.since < TICK_MS:
            return
        t, self.since = self.since, 0
        now = self.clock
        self.ticks += 1
        if self.mode == "wait" and now >= WAIT_MS:
            if self.path and self.pca_ok and not self.latched and self.autoplay and not self.host():
                self.start_demo()
            else:
                self.mode = "pc"
        if self.mode == "demo":
            try:
                us = self.demo.next()
            except (OSError, DemoError):
                self.fail()
                us = None
            if us is not None:
                self.board.drive(us, now)
            elif self.mode == "demo":           # the end: glide to park (the clip ends there), hold, again
                self.mode, self.hold_until = "park", now + HOLD_MS
        if self.mode == "park":
            self.board.drive(self.board.park, now)
            if now >= self.hold_until:
                self.start_demo()
        self.board.tick(now, t)
        self._leds(now)

    def _leds(self, now):
        demo = self.mode in ("demo", "park")
        state = self.board.state
        _set(self.blue, demo)
        # red only for a problem; on a PC, waiting for simdash with the servos limp is a green heartbeat
        waiting = (self.mode == "pc" and state == "limp" and self.pca_ok and not self.error and not self.latched
                   and self.host())
        _set(self.green, (self.mode == "pc" and state == "drive") or (waiting and now % 2000 < 100))
        _set(self.yellow, state in ("glide", "hold"))
        if self.error:
            red = (now // 125) % 2                      # 4 Hz: the demo file failed
        elif self.latched:
            red = True                                  # stopped with USR: steady
        elif not self.pca_ok:
            red = (now // 100) % 2                      # 5 Hz: no PCA9685
        elif state == "limp" and not waiting:
            red = (now // 500) % 2                      # 1 Hz: limp, no PC
        else:
            red = False
        _set(self.red, red)


def _set(led, on):
    # LED.value() doesn't exist on MicroPython 1.9.4: on() / off() do everywhere
    if on:
        led.on()
    else:
        led.off()
