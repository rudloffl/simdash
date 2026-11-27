"""Desk 6DOF board logic, plain Python (runs in MicroPython on the pyboard and in CPython for the tests).

Lines from the PC:
  S u0..u5   target pulses (µs) for servos 0-5
  P u0..u5   park pulses: where to glide when the PC goes silent
  O          let go now (outputs off, servos limp)
  H          hello -> "6DOF <version> ch=<n>"
  A          (fw.py) clear the stop latch set by a short press on USR
  Q          (fw.py) state report -> "STATE mode=... board=... ticks=..." (version 4)

Each servo moves towards its target by at most rate_us_per_ms (an MG90's top speed is about
6 µs/ms: a 600 µs step takes 100 ms). Outputs are updated by tick(), at a fixed period, not when a
line arrives. After a let-go the board doesn't know where the horns are: the first S is taken as the
starting point (the PC sends the home pose first). No line for timeout_ms: glide to the park pulses
over about glide_ms, hold hold_ms, then go limp (no park pulses: limp at once).
"""

VERSION = "8"   # 4: Q state report, hardware watchdog; 5: ver= in Q, LEDs red only for a problem; 6: SD card;
                # 7: L (status ring) command, leds= in the hello; 8: C (ring palette + brightness) command


class Board:
    def __init__(self, out, n=6, min_us=800, max_us=2200, rate_us_per_ms=6.0,
                 timeout_ms=1000, glide_ms=1000, hold_ms=300):
        self.out = out                  # .write([us] * n) and .off()
        self.n, self.min_us, self.max_us = n, min_us, max_us
        self.rate, self.timeout, self.glide_ms, self.hold_ms = rate_us_per_ms, timeout_ms, glide_ms, hold_ms
        self.cur = None                 # pulses on the outputs now (None: limp)
        self.target = None
        self.park = None
        self.state = "limp"             # limp | drive | glide | hold
        self.last_ms = 0
        self.glide_rate = rate_us_per_ms
        self.hold_until = 0

    def _clamp(self, parts):
        if len(parts) != self.n:
            raise ValueError("expected %d pulses" % self.n)
        return [min(self.max_us, max(self.min_us, int(v))) for v in parts]

    def line(self, text, now_ms):
        """Handle one command; returns the reply to send, or None."""
        parts = text.split()
        if not parts:
            return None
        op = parts[0]
        if op in ("S", b"S"):
            self.drive(parts[1:], now_ms)
        elif op in ("P", b"P"):
            self.park = self._clamp(parts[1:])
        elif op in ("O", b"O"):
            self.let_go()
        elif op in ("H", b"H"):
            return "6DOF %s ch=%d\n" % (VERSION, self.n)
        return None

    def drive(self, us, now_ms):
        """New target pulses (from an S line or the demo player)."""
        us = self._clamp(us)
        if self.cur is None:            # limp: the horns are wherever they are; start from here
            self.cur = list(us)
        self.target = us
        self.state = "drive"
        self.last_ms = now_ms

    def glide_to_park(self):
        """Glide to the park pulses over about glide_ms, hold, then go limp (no park: limp now)."""
        if self.park is None or self.cur is None:
            self.let_go()
            return
        self.state, self.target = "glide", list(self.park)
        dist = max(abs(p - c) for p, c in zip(self.park, self.cur))
        self.glide_rate = min(self.rate, max(dist / self.glide_ms, 0.05))

    def let_go(self):
        self.state, self.cur, self.target = "limp", None, None
        self.out.off()

    def tick(self, now_ms, dt_ms):
        """Move the outputs one step; call at a fixed period (e.g. every 5 ms)."""
        if self.state == "drive" and now_ms - self.last_ms > self.timeout:
            self.glide_to_park()
        if self.state == "limp" or self.cur is None:
            return
        rate = self.glide_rate if self.state == "glide" else self.rate
        step = rate * dt_ms
        changed = False
        for i in range(self.n):
            d = self.target[i] - self.cur[i]
            if d:
                self.cur[i] += max(-step, min(step, d))
                changed = True
        if changed:
            self.out.write([int(round(v)) for v in self.cur])
        if self.state == "glide" and not changed:
            self.state, self.hold_until = "hold", now_ms + self.hold_ms
        elif self.state == "hold" and now_ms >= self.hold_until:
            self.let_go()
