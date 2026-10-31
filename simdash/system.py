"""Status of the telemetry chain (AMS2 -> bridge -> simd -> monocoque -> shakers) and restart actions.

The launchers live in ~/.local/bin: start-race runs a whole session (fresh simd, CrewChief,
AMS2 through Steam, monocoque) and cleans up when AMS2 exits, so restarting AMS2 goes
through it rather than through Steam directly.
"""

import fcntl
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import shakers

log = logging.getLogger("simdash")
BIN = Path.home() / ".local/bin"
STATE = Path.home() / ".local/state"
LOCK = STATE / "start-race.lock"
LOGS = {"monocoque": STATE / "monocoque-play.log", "start-race": STATE / "start-race.log",
        "crewchief": STATE / "crewchief.log", "simdash": STATE / "simdash.log"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")

_actions = deque(maxlen=20)  # recent actions shown in the page
_busy = {}                    # component -> what it is doing (background restarts)
_lock = threading.Lock()


def _run(*cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))


def _pids(pattern, full=False):
    """pgrep: an exact process name, a case-insensitive regex if it starts with ^, or the full command line."""
    args = ["-f", pattern] if full else ["-i", pattern] if pattern.startswith("^") else ["-x", pattern]
    return [int(p) for p in _run("pgrep", *args).stdout.split()]


def _started(pid):
    """Process start time (epoch) from /proc."""
    try:
        ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(float(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines() if l.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _note(what):
    log.info("system: %s", what)
    with _lock:
        _actions.appendleft({"t": time.time(), "text": what})


def _tail(path, n=12):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 6000))
            lines = f.read().decode(errors="replace").splitlines()[-n:]
        return [ANSI.sub("", l) for l in lines]
    except OSError:
        return []


def _session_running():
    """True while a start-race session holds its lock."""
    try:
        with open(LOCK, "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _bridge_pids():
    # Wine names the process after the exe (comm truncated to 15 chars); `pgrep -f pcars2bridge`
    # would also match the Steam launch command that names it in SIMD_BRIDGE_EXE
    r = _run("ps", "-eo", "pid=,comm=")
    return [int(l.split()[0]) for l in r.stdout.splitlines() if l.split()[1:2] and l.split()[1].lower().startswith("pcars2bridge")]


def _simapi():
    r = _run(str(BIN / "simtelemetry"), "--check", timeout=4)
    info = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    return r.returncode, info


def status(rec_live=None):
    now = time.time()
    rec_live = rec_live or {}
    out = {"now": now, "busy": dict(_busy), "session": _session_running()}

    ams2 = _pids("^AMS2AVX")
    out["ams2"] = {"running": bool(ams2), "pid": ams2[0] if ams2 else None, "since": _started(ams2[0]) if ams2 else None}

    bridge = _bridge_pids()
    out["bridge"] = {"running": bool(bridge), "pid": bridge[0] if bridge else None}

    st = rec_live.get("status", "starting")
    s = rec_live.get("session") or {}
    out["shm"] = {"exists": Path("/dev/shm/$pcars2$").exists(), "status": st, "detail": rec_live.get("detail", ""),
                  "track": s.get("track"), "car": s.get("car")}

    act = _run("systemctl", "--user", "show", "simd", "-p", "ActiveState", "-p", "MainPID", "-p", "ActiveEnterTimestampMonotonic")
    props = dict(l.split("=", 1) for l in act.stdout.splitlines() if "=" in l)
    pid = int(props.get("MainPID") or 0)
    rc, info = _simapi()
    out["simd"] = {"state": props.get("ActiveState", "unknown"), "pid": pid or None, "since": _started(pid) if pid else None,
                   "check": {0: "live", 2: "no sim", 3: "stale"}.get(rc, "error"), "simapi": info}

    mono = _pids("monocoque")
    out["monocoque"] = {"running": bool(mono), "pid": mono[0] if mono else None, "since": _started(mono[0]) if mono else None,
                        "log": _tail(LOGS["monocoque"], 8)}

    cc = _pids(r"CrewChiefV4\.exe", full=True)
    udp = _run("ss", "-uln").stdout
    out["crewchief"] = {"running": bool(cc), "listening": ":5606 " in udp}

    try:
        devid = shakers.load()["devid"]
    except (OSError, ValueError):
        devid = ""
    sinks = {x["name"]: x["description"] for x in shakers.sinks()}
    out["card"] = {"devid": devid, "connected": devid in sinks, "description": sinks.get(devid, "")}

    with _lock:
        out["actions"] = list(_actions)
    return out


# ---------- actions ----------

def _kill_monocoque():
    """Stop monocoque and the `sleep infinity` feeding its stdin (its own session, see start-race)."""
    me = os.getsid(0)
    n = 0
    for pid in _pids("monocoque"):
        try:
            sid = os.getsid(pid)
            if sid != me:
                _run("pkill", "-s", str(sid))
            os.kill(pid, signal.SIGTERM)
            n += 1
        except ProcessLookupError:
            pass
    for _ in range(30):
        if not _pids("monocoque"):
            break
        time.sleep(0.1)
    return n


def _start_monocoque():
    with open(LOGS["monocoque"], "w") as logf:
        # monocoque exits at once when stdin is /dev/null, so feed it an idle pipe (as start-race does)
        subprocess.Popen(["bash", "-c", f'sleep infinity | exec "{BIN / "start-monocoque"}"'],
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)


def restart_monocoque():
    n = _kill_monocoque()
    _start_monocoque()
    _note("monocoque restarted" if n else "monocoque started")


def stop_monocoque():
    n = _kill_monocoque()
    _note("monocoque stopped" if n else "monocoque was not running")


def restart_simd():
    r = _run("systemctl", "--user", "restart", "simd", timeout=20)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "systemctl failed")
    _note("simd restarted")


def _in_background(name, what, fn):
    if _busy.get(name):
        raise RuntimeError(f"{name} is already busy: {_busy[name]}")
    _busy[name] = what

    def run():
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - shown in the page
            _note(f"{what} failed: {e}")
            log.exception("system action failed")
        finally:
            _busy.pop(name, None)
    threading.Thread(target=run, daemon=True).start()


def _quit_ams2(timeout=40):
    pids = _pids("^AMS2AVX")
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    end = time.time() + timeout
    while _pids("^AMS2AVX") and time.time() < end:
        time.sleep(0.5)
    if _pids("^AMS2AVX"):
        for pid in _pids("^AMS2AVX"):
            os.kill(pid, signal.SIGKILL)
        time.sleep(1)
    # start-race notices the exit within 5 s, stops CrewChief and monocoque and frees its lock
    end = time.time() + 30
    while _session_running() and time.time() < end:
        time.sleep(0.5)
    return bool(pids)


def _start_session():
    if _session_running():
        raise RuntimeError("a start-race session is still running")
    subprocess.Popen([str(BIN / "start-race")], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def start_ams2():
    if _pids("^AMS2AVX"):
        raise RuntimeError("AMS2 is already running")
    _start_session()
    _note("session started (start-race: simd, CrewChief, AMS2, monocoque)")


def quit_ams2():
    def run():
        _note("AMS2 closed" if _quit_ams2() else "AMS2 was not running")
    _in_background("ams2", "closing AMS2", run)


def restart_ams2():
    def run():
        _quit_ams2()
        _start_session()
        _note("AMS2 restarted through start-race (fresh simd, CrewChief, monocoque)")
    _in_background("ams2", "restarting AMS2", run)


ACTIONS = {
    "simd/restart": restart_simd,
    "monocoque/restart": restart_monocoque,
    "monocoque/stop": stop_monocoque,
    "ams2/start": start_ams2,
    "ams2/quit": quit_ams2,
    "ams2/restart": restart_ams2,
}
