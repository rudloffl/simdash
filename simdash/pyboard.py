"""Talk to a MicroPython board through its raw REPL (what mpremote does): run code, copy a file, soft reset.

Used to put the 6DOF demo on the pyboard when mpremote isn't installed. The firmware is running main.py:
"O" lets go of the servos, Ctrl-C stops main.py (its finally turns the outputs off), Ctrl-A enters the raw
REPL; after the copy a soft reset (Ctrl-D) starts main.py again.
"""

import os
import select
import termios
import time


class BoardError(Exception):
    pass


def _open(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def _write(fd, data):
    end = time.monotonic() + 5
    while data:
        try:
            data = data[os.write(fd, data):]
        except BlockingIOError:
            if time.monotonic() > end:
                raise BoardError("the board is not reading")
            select.select([], [fd], [], 0.05)


def _read_until(fd, end, timeout):
    buf, t = b"", time.monotonic() + timeout
    while end not in buf:
        left = t - time.monotonic()
        if left <= 0:
            raise BoardError("no answer from the board")
        if select.select([fd], [], [], left)[0]:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                pass
    return buf


def _drain(fd):
    time.sleep(0.2)
    try:
        while os.read(fd, 65536):
            pass
    except (BlockingIOError, OSError):
        pass


# firmware v4 starts a hardware watchdog that can't be stopped; after Ctrl-C main.py leaves it at 30 s as
# `wdt` in the REPL's globals: every exec feeds it (a no-op on older firmware or in safe mode)
FEED = "try:\n    wdt.feed()\nexcept NameError:\n    pass\n"


class Board:
    def __init__(self, port):
        self.port = port
        self.fd = None

    def __enter__(self):
        self.fd = _open(self.port)
        _write(self.fd, b"O\n")            # the firmware lets go of the servos first
        time.sleep(0.1)
        _write(self.fd, b"\r\x03\x03")     # stop main.py
        _drain(self.fd)
        _write(self.fd, b"\r\x01")         # raw REPL
        _read_until(self.fd, b"raw REPL; CTRL-B to exit\r\n>", 3)
        return self

    def exec(self, code, timeout=30):
        """Run code on the board; returns what it printed. Raises BoardError on a Python error there."""
        data = (FEED + code).encode()
        for i in range(0, len(data), 256):
            _write(self.fd, data[i:i + 256])
            time.sleep(0.01)
        _write(self.fd, b"\x04")
        reply = _read_until(self.fd, b"\x04>", timeout)      # OK<output>\x04<error>\x04>
        body = reply.split(b"OK", 1)[-1][:-2]
        out, _, err = body.partition(b"\x04")
        if err.strip():
            raise BoardError(err.decode(errors="replace").strip().splitlines()[-1])
        return out.decode(errors="replace")

    def put(self, data, dest, chunk=1024):
        """Write bytes to a file on the board (through a .tmp, renamed when complete)."""
        code = "f = open(%r, 'wb')\n" % (dest + ".tmp")
        self.exec(code)
        for i in range(0, len(data), chunk):
            self.exec("f.write(%r)" % data[i:i + chunk])
        self.exec("f.close()\nimport os\ntry:\n    os.remove(%r)\nexcept OSError:\n    pass\nos.rename(%r, %r)"
                  % (dest, dest + ".tmp", dest))
        size = int(self.exec("import os\nprint(os.stat(%r)[6])" % dest).strip())
        if size != len(data):
            raise BoardError("copied %d bytes of %d" % (size, len(data)))

    def info(self):
        """{"sd": bool, "flash_free": bytes, "sd_free": bytes or None, "demo": {path: size}}"""
        out = self.exec(
            "import os\n"
            "def free(p):\n"
            "    s = os.statvfs(p)\n"
            "    return s[0] * s[3]\n"
            "sd = 'sd' in os.listdir('/')\n"
            "print(sd, free('/flash'), free('/sd') if sd else -1)\n"
            "for p in ('/flash/demo.bin', '/sd/demo.bin'):\n"
            "    try:\n"
            "        print(p, os.stat(p)[6])\n"
            "    except OSError:\n"
            "        pass\n")
        lines = out.strip().splitlines()
        sd, ffree, sfree = lines[0].split()
        return {"sd": sd == "True", "flash_free": int(ffree), "sd_free": int(sfree) if int(sfree) >= 0 else None,
                "demo": {ln.split()[0]: int(ln.split()[1]) for ln in lines[1:]}}

    def __exit__(self, *exc):
        try:
            _write(self.fd, b"\x02")         # leave the raw REPL
            time.sleep(0.1)
            _write(self.fd, b"\x04")         # soft reset: boot.py then main.py
        finally:
            os.close(self.fd)
            self.fd = None
