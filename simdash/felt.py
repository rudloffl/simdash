"""What the haptics engine plays, sent back to simdash: the live car on the Live page, and the session
recording ("felt" table), show what was actually felt instead of a recomputed guess.

The engine sends one datagram per audio block (100 Hz) on a Unix socket that simdash listens on;
sending never blocks and is simply dropped when simdash is not there. simdash keeps, for each output,
the strongest level seen since it last looked, so a 10 ms knock still shows in a 20 Hz recording.

    {"sh": {"0": {effect: strength %, ...}, ...}, "pd": {"brake:abs": strength %, ...}}
"""

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger("simdash.felt")

PATH = Path.home() / ".local/state/simdash-felt.sock"
STALE = 0.3  # s without a datagram: the engine is not playing
# the "out" column of the felt table: shaker channels 0-7, then the pedals
PEDAL_OUT = {"clutch": 10, "brake": 11, "throttle": 12}
OUT_PEDAL = {v: k for k, v in PEDAL_OUT.items()}


class Sender:
    """The engine's end: send() costs a few microseconds and never waits."""

    def __init__(self, path=PATH):
        self.path = str(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def send(self, shakers, pedals, limiter=None):
        try:
            d = {"sh": shakers, "pd": pedals}
            if limiter:
                d["lim"] = limiter
            self.sock.sendto(json.dumps(d, separators=(",", ":")).encode(), self.path)
        except OSError:
            pass  # simdash not running, or its queue full: nothing to wait for


class Receiver:
    """simdash's end: a thread collecting the engine's datagrams."""

    def __init__(self, path=PATH):
        self.path = path
        self.lock = threading.Lock()
        self.at = 0.0       # monotonic time of the last datagram
        self.sh, self.pd = {}, {}   # peaks since the last take()
        self.last = ({}, {})        # the last datagram as it came
        self.lim = {}               # the limiter's reduction per channel (%), last datagram
        self.thread = None

    def start(self):
        if self.thread is not None:
            return
        try:
            self.path.unlink(missing_ok=True)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.bind(str(self.path))
            os.chmod(self.path, 0o600)
        except OSError as e:
            log.warning("no feedback from the haptics engine: %s", e)
            return
        self.thread = threading.Thread(target=self._run, args=(sock,), daemon=True, name="felt")
        self.thread.start()

    def _run(self, sock):
        while True:
            try:
                d = json.loads(sock.recv(65536))
                sh, pd = d.get("sh") or {}, d.get("pd") or {}
            except (OSError, ValueError, AttributeError):
                time.sleep(0.01)
                continue
            with self.lock:
                self.at = time.monotonic()
                self.last = (sh, pd)
                self.lim = d.get("lim") or {}
                for ch, fxs in sh.items():
                    peak = self.sh.setdefault(ch, {})
                    for fx, a in fxs.items():
                        peak[fx] = max(a, peak.get(fx, 0))
                for k, a in pd.items():
                    self.pd[k] = max(a, self.pd.get(k, 0))

    def now(self):
        """The last datagram's ({channel: {effect: strength}}, {"pedal:effect": strength}), or None."""
        with self.lock:
            return self.last if self.active() else None

    def limiter(self):
        """{channel: % the engine's limiter takes off} now, {} when it does nothing or the engine is quiet."""
        with self.lock:
            return dict(self.lim) if self.active() else {}

    def active(self):
        return time.monotonic() - self.at < STALE

    def take(self):
        """({channel: {effect: strength %}}, {"pedal:effect": strength %}): the strongest of each since the last
        take, or None when the engine has sent nothing lately."""
        with self.lock:
            sh, pd = self.sh, self.pd
            self.sh, self.pd = {}, {}
            if not self.active():
                return None
        return ({ch: {fx: a for fx, a in fxs.items() if a > 0} for ch, fxs in sh.items()},
                {k: a for k, a in pd.items() if a > 0})


def strongest(sh):
    """{channel: [strength %, effect]}: the effect that dominates each shaker (the live car's colour)."""
    out = {}
    for ch, fxs in sh.items():
        if fxs:
            fx = max(fxs, key=fxs.get)
            out[ch] = [fxs[fx], fx]
    return out


receiver = Receiver()


def rows(tick_id, sh, pd):
    """felt table rows for one tick."""
    out = [(tick_id, int(ch), fx, int(a)) for ch, fxs in sh.items() for fx, a in fxs.items()]
    for k, a in pd.items():
        pedal, _, fx = k.partition(":")
        if pedal in PEDAL_OUT:
            out.append((tick_id, PEDAL_OUT[pedal], fx, int(a)))
    return out
