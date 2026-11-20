"""Haptic presets: named sets of shaker effects + Moza pedal settings, picked before a race.

A preset holds the shaker effects (monocoque's Sound devices and simdash's own effects, as
shakers.load() gives them) and the whole pedal haptics settings (haptics.json). The live files
stay where they are; picking a preset first stores the live settings into the active preset
(so tweaks made in the Haptics tab stick to the mode they were made in), then writes the
chosen one over them. monocoque only reads its config at start, so a running one is restarted.

Built in: "performance" (the settings as they were when presets appeared) and "quiet", made
from it the first time: shakers and pedals at half strength, engine rumble mostly gone.
"""

import json
import logging
import threading

from . import haptics, shakers, store

log = logging.getLogger("simdash")
DIR = store.DATA_DIR / "haptic-presets"
STATE = DIR / "active.json"
PRESETS = {"performance": "Performance", "quiet": "Quiet"}
DEFAULT = "performance"

# "quiet" from "performance": volume factor per shaker effect, strength factor per pedal effect
QUIET_SHAKERS = {"Engine": 0.25}
QUIET_SHAKER_DEFAULT = 0.5
QUIET_PEDALS_OFF = {"engine"}
QUIET_PEDAL_FACTOR = 0.5

_lock = threading.Lock()


def _path(name):
    return DIR / f"{name}.json"


def _write(path, data):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def _live():
    return {"shakers": shakers.load()["effects"], "pedals": haptics.load_settings()}


def _quiet_from(p):
    q = json.loads(json.dumps(p))
    for e in q["shakers"]:
        f = QUIET_SHAKERS.get(e.get("effect"), QUIET_SHAKER_DEFAULT)
        e["volume"] = int(round(float(e.get("volume", 100)) * f))
    for fxs in q["pedals"]["pedals"].values():
        for name, fx in fxs.items():
            if name in QUIET_PEDALS_OFF:
                fx["on"] = False
            fx["strength"] = int(round(fx["strength"] * QUIET_PEDAL_FACTOR))
    return q


def _ensure():
    """First use: the live settings become "performance", and "quiet" is made from them."""
    if not _path("performance").exists():
        _write(_path("performance"), _live())
    if not _path("quiet").exists():
        _write(_path("quiet"), _quiet_from(json.loads(_path("performance").read_text())))
    if not STATE.exists():
        _write(STATE, {"active": DEFAULT})


def active():
    try:
        name = json.loads(STATE.read_text()).get("active")
    except (OSError, ValueError):
        name = None
    return name if name in PRESETS else DEFAULT


def info():
    with _lock:
        _ensure()
        return {"active": active(), "presets": [{"id": k, "label": v} for k, v in PRESETS.items()]}


def select(name):
    """Make `name` the live haptics. Returns True when monocoque has to be restarted."""
    if name not in PRESETS:
        raise ValueError(f"no preset {name!r}")
    with _lock:
        _ensure()
        cur = active()
        _write(_path(cur), _live())  # keep the tweaks made in the current mode
        p = json.loads(_path(name).read_text())
        devid = shakers.load()["devid"]  # the sound card is the rig's, not the preset's
        shakers.save(p["shakers"], devid)
        haptics.save_settings(p["pedals"])
        haptics.driver_thread.reload()
        _write(STATE, {"active": name})
        log.info("haptic preset: %s -> %s", cur, name)
        return cur != name
