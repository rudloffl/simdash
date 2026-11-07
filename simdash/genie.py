"""Corner Genie: settings of the corner recap popup shown while driving.

The popup itself is computed in the browser from the live trace and the reference lap; only
the settings live here, so every screen showing simdash uses the same ones.
"""

import json

from . import store

SETTINGS = store.DATA_DIR / "genie.json"
OLD_SETTINGS = store.DATA_DIR / "assistant.json"  # name used before "Corner Genie"
FIELDS = ("time", "brake", "throttle", "entry", "min", "exit", "gear")
DEFAULTS = {
    "enabled": True,
    "duration": 4.0,          # seconds on screen
    "position": "top",        # top | middle | bottom of the screen
    "size": "screen",         # normal | large | huge | screen (about 75 % of the screen)
    "only": "all",            # all | lost | gained
    "min_delta": 0.05,        # s: with "lost"/"gained", smaller differences are not shown
    "fields": {f: True for f in FIELDS},
}


def load():
    s = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads((SETTINGS if SETTINGS.exists() else OLD_SETTINGS).read_text())
        s.update({k: v for k, v in saved.items() if k in DEFAULTS and k != "fields"})
        s["fields"].update({k: bool(v) for k, v in (saved.get("fields") or {}).items() if k in FIELDS})
    except (OSError, ValueError):
        pass
    return s


def save(patch):
    s = load()
    for k, v in patch.items():
        if k == "fields":
            s["fields"].update({f: bool(x) for f, x in v.items() if f in FIELDS})
        elif k in DEFAULTS:
            s[k] = v
    s["enabled"] = bool(s["enabled"])
    s["duration"] = min(15.0, max(1.0, float(s["duration"])))
    s["min_delta"] = min(2.0, max(0.0, float(s["min_delta"])))
    if s["position"] not in ("top", "middle", "bottom"):
        raise ValueError("position is top, middle or bottom")
    if s["size"] not in ("normal", "large", "huge", "screen"):
        raise ValueError("size is normal, large, huge or screen")
    if s["only"] not in ("all", "lost", "gained"):
        raise ValueError("only is all, lost or gained")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(SETTINGS)
    return s
