"""Bass shaker setup: read/write monocoque's Sound effects and play test vibrations.

monocoque reads ~/.config/monocoque/monocoque.config (libconfig) once at start, so saved
changes apply to the next session. Each Sound effect is one PulseAudio stream on one
channel (`pan`) of the sink `devid`; the tests below synthesise the same signal as
monocoque's usb_generic_shaker_pulse.c (sine, amplitude/100, stream volume volume/100).
"""

import json
import os
import math
import re
import shutil
import subprocess
import threading
import time
from array import array
from pathlib import Path

from . import store

CONFIG = Path.home() / ".config/monocoque/monocoque.config"
LAYOUT = store.DATA_DIR / "shakers.json"  # where each channel's shaker sits on the car (simdash only)

RATE = 44100
CHANNEL_MAP = ["front-left", "front-right", "front-center", "lfe", "rear-left", "rear-right", "side-left", "side-right"]
CHANNEL_SHORT = ["FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR"]
EFFECTS = ["Engine", "Gear", "TyreSlip", "TyreLock", "ABS", "Suspension"]
WHEEL_EFFECTS = {"TyreSlip", "TyreLock", "ABS", "Suspension"}
TYRES = ["All", "Fronts", "Rears", "FrontLeft", "FrontRight", "RearLeft", "RearRight"]
MODULATIONS = ["none", "frequency", "amplify"]
SPOTS = ["none", "seat", "seat-back", "pedals", "front-left", "front-right", "rear-left", "rear-right"]
FLOAT_KEYS = {"threshold", "duration", "ampfactor", "fanpower"}
# key order in the written file, the rest follow
KEY_ORDER = ["device", "effect", "tyre", "devid", "channels", "pan", "fps", "threshold", "volume", "modulation",
             "frequency", "frequencyMax", "amplitude", "amplitudeMax", "duration", "noise"]


# ---------- minimal libconfig reader / writer ----------

_TOKEN = re.compile(r"""
    (?P<ws>\s+|//[^\n]*|\#[^\n]*|/\*.*?\*/)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<num>[-+]?(?:0[xX][0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)L{0,2})
  | (?P<name>[A-Za-z*][-A-Za-z0-9_*]*)
  | (?P<punct>[=:;,{}()\[\]])
""", re.S | re.X)


def _tokens(text):
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"monocoque.config: unexpected {text[pos:pos + 20]!r}")
        pos = m.end()
        if m.lastgroup != "ws":
            yield m.lastgroup, m.group()


class _Parser:
    def __init__(self, text):
        self.toks = list(_tokens(text))
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise ValueError(f"monocoque.config: expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def settings(self, end):
        out = {}
        while self.peek()[1] != end:
            kind, name = self.take()
            if kind != "name":
                raise ValueError(f"monocoque.config: expected a setting name, got {name!r}")
            if self.peek()[1] in ("=", ":"):
                self.take()
            out[name] = self.value()
            if self.peek()[1] in (";", ","):
                self.take()
        return out

    def value(self):
        kind, tok = self.take()
        if tok == "{":
            v = self.settings("}")
            self.take("}")
            return v
        if tok in ("(", "["):
            close = ")" if tok == "(" else "]"
            items = []
            while self.peek()[1] != close:
                items.append(self.value())
                if self.peek()[1] == ",":
                    self.take()
            self.take(close)
            return items
        if kind == "str":
            s = json.loads(tok)
            while self.peek()[0] == "str":  # adjacent strings concatenate
                s += json.loads(self.take()[1])
            return s
        if kind == "num":
            t = tok.rstrip("L")
            if t.lower().lstrip("+-").startswith("0x"):
                return int(t, 16)
            return float(t) if re.search(r"[.eE]", t) else int(t)
        if kind == "name" and tok.lower() in ("true", "false"):
            return tok.lower() == "true"
        raise ValueError(f"monocoque.config: bad value {tok!r}")


def parse(text):
    p = _Parser(text)
    out = p.settings(None)
    return out


def _fmt(v, ind):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        return s if re.search(r"[.eE]", s) else s + ".0"
    if isinstance(v, str):
        return json.dumps(v)
    pad = "    " * (ind + 1)
    if isinstance(v, dict):
        lines = [f"{pad}{k:<12} = {_fmt(x, ind + 1)};" for k, x in v.items() if not k.startswith("//")]
        return "{\n" + "\n".join(lines) + "\n" + "    " * ind + "}"
    parts = []
    for x in v:
        note = x.get("//") if isinstance(x, dict) else None
        parts.append((f"{pad}// {note}\n" if note else "") + pad + _fmt(x, ind + 1))
    return "(\n" + ",\n".join(parts) + "\n" + "    " * ind + ")"


def dump(cfg):
    head = ("// Written by simdash (Shakers tab). Effects: one PulseAudio stream each, on channel `pan` of\n"
            "// sink `devid`. 8-channel order: 0 FL, 1 FR, 2 FC, 3 LFE, 4 RL, 5 RR, 6 SL, 7 SR.\n")
    return head + "\n".join(f"{k} = {_fmt(v, 0)};" for k, v in cfg.items()) + "\n"


# ---------- config <-> effects ----------

def _read_cfg():
    return parse(CONFIG.read_text()) if CONFIG.exists() else {"configs": [{"sim": "default", "car": "default", "devices": []}]}


def _default_entry(cfg):
    for c in cfg.setdefault("configs", []):
        if str(c.get("sim", "")).lower() == "default" and str(c.get("car", "")).lower() == "default":
            return c
    c = {"sim": "default", "car": "default", "devices": []}
    cfg["configs"].insert(0, c)
    return c


def _norm_effect(name):
    n = str(name).lower()
    for e in EFFECTS:
        if n == e.lower():
            return e
    return {"slip": "TyreSlip", "tireslip": "TyreSlip", "lock": "TyreLock", "tirelock": "TyreLock"}.get(n, name)


def load():
    cfg = _read_cfg()
    devices = _default_entry(cfg).get("devices", [])
    effects = []
    for d in devices:
        if str(d.get("device", "")).lower() != "sound":
            continue
        e = {k: d[k] for k in d}
        e["effect"] = _norm_effect(d.get("effect", "Engine"))
        e.setdefault("modulation", "none")
        e["modulation"] = str(e["modulation"]).lower()
        e.setdefault("amplitude", 50)
        e.setdefault("volume", 100)
        e.setdefault("pan", 0)
        e.setdefault("frequency", 0)
        effects.append(e)
    devid = next((e.get("devid") for e in effects if e.get("devid")), "")
    return {"effects": effects, "devid": devid, "path": str(CONFIG), "mtime": CONFIG.stat().st_mtime if CONFIG.exists() else 0}


def _clean(e, devid, layout):
    out = {"device": "Sound", "effect": _norm_effect(e.get("effect", "Engine"))}
    if out["effect"] not in EFFECTS:
        raise ValueError(f"unknown effect {e.get('effect')!r}")
    if out["effect"] in WHEEL_EFFECTS:
        out["tyre"] = e.get("tyre") if e.get("tyre") in TYRES else "All"
    out["devid"] = devid
    out["channels"] = 8
    out["pan"] = max(0, min(7, int(e.get("pan", 0))))
    for k, lo, hi in (("fps", 1, 240), ("volume", 0, 100), ("frequency", 0, 200), ("frequencyMax", 0, 200),
                      ("amplitude", 0, 100), ("amplitudeMax", 0, 100), ("noise", 0, 50)):
        if e.get(k) not in (None, ""):
            out[k] = max(lo, min(hi, int(round(float(e[k])))))
    out.setdefault("fps", 60)
    for k, lo, hi in (("threshold", 0.0, 1.0), ("duration", 0.01, 2.0)):
        if e.get(k) not in (None, ""):
            out[k] = max(lo, min(hi, float(e[k])))
    mod = str(e.get("modulation", "none")).lower()
    if mod in ("frequency", "amplify"):
        out["modulation"] = mod
    if out["effect"] == "Gear":
        out.pop("modulation", None)
    spot = layout.get(str(out["pan"]), {})
    where = spot.get("label") or spot.get("spot") or ""
    tyre = f" {out['tyre']}" if "tyre" in out else ""
    out["//"] = f"{out['effect']}{tyre} -> channel {out['pan']} ({CHANNEL_SHORT[out['pan']]}){' ' + where if where else ''}"
    return {k: out[k] for k in sorted(out, key=lambda k: (KEY_ORDER + [k]).index(k) if k != "//" else -1)}


def save(effects, devid):
    if not devid:
        raise ValueError("choose the shaker sound card first")
    layout = load_layout()
    cleaned = [_clean(e, devid, layout) for e in effects]
    cfg = _read_cfg()
    entry = _default_entry(cfg)
    others = [d for d in entry.get("devices", []) if str(d.get("device", "")).lower() != "sound"]
    entry["devices"] = others + cleaned
    text = dump(cfg)
    parse(text)  # never write something we cannot read back
    if CONFIG.exists():
        shutil.copy2(CONFIG, CONFIG.with_name(f"monocoque.config.bak-{time.strftime('%Y-%m-%d-%H%M%S')}"))
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(CONFIG)
    return load()


# ---------- channel layout on the car (simdash's own file) ----------

def load_layout():
    try:
        return json.loads(LAYOUT.read_text())
    except (OSError, ValueError):
        # 2026-10-24 setup: one BST1 under the seat on channel 0
        return {"0": {"spot": "seat", "label": "BST1"}}


def save_layout(layout):
    clean = {}
    for ch, v in layout.items():
        if str(ch) in {str(i) for i in range(8)} and v.get("spot") in SPOTS and v["spot"] != "none":
            clean[str(ch)] = {"spot": v["spot"], "label": str(v.get("label", ""))[:24]}
    LAYOUT.parent.mkdir(parents=True, exist_ok=True)
    LAYOUT.write_text(json.dumps(clean, indent=1))
    return clean


# ---------- sound cards ----------

def sinks():
    # plain text: pactl's JSON output drops descriptions with accents ("Stéréo") as (null)
    try:
        out = subprocess.run(["pactl", "list", "sinks"], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C.UTF-8"}).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        key, _, val = line.strip().partition(": ")
        if key == "Name":
            rows.append({"name": val, "description": val, "channels": 2})
        elif rows and key == "Description":
            rows[-1]["description"] = val
        elif rows and key == "Channel Map":
            rows[-1]["channels"] = len(val.split(","))
    return rows


def monocoque_running():
    return subprocess.run(["pgrep", "-x", "monocoque"], capture_output=True).returncode == 0


# ---------- test vibrations ----------

class _Synth:
    """Phase-continuous sine like monocoque's stream callbacks, written to one channel of 8."""

    def __init__(self, pan):
        self.pan, self.phase, self.buf = pan, 0.0, array("h")

    def play(self, seconds, freq, amp, noise=0, fade=0.005):
        n = int(seconds * RATE)
        frame = [0] * 8
        for i in range(n):
            a = amp / 100
            left = (n - i) / RATE
            if left < fade:
                a *= left / fade
            frame[self.pan] = int(a * 32767 * math.sin(2 * math.pi * self.phase))
            self.buf.extend(frame)
            f = freq + (noise * (2 * ((i * 7919) % 1000) / 1000 - 1) if noise else 0)
            self.phase = (self.phase + f / RATE) % 1.0

    def rest(self, seconds):
        self.buf.extend([0] * (8 * int(seconds * RATE)))
        self.phase = 0.0


def _modulated(e, x):
    """monocoque's modulate(): frequency/amplitude for a raw effect strength x in 0..1."""
    f, a = e.get("frequency", 0), e.get("amplitude", 50)
    mod = e.get("modulation", "none")
    if mod == "frequency" and e.get("frequencyMax", 0) >= f:
        f = f + (e["frequencyMax"] - f) * x
    elif mod == "amplify":
        a = a + (e.get("amplitudeMax", 50) - a) * x
    return f, a


def render(e, mode="effect"):
    """Raw 8-channel s16le for a test: 'identify' = a plain tone, 'effect' = the effect as in the car."""
    s = _Synth(int(e.get("pan", 0)))
    if mode == "identify":
        for _ in range(3):
            s.play(0.5, 40, 80)
            s.rest(0.25)
        return s.buf
    eff, noise = e.get("effect"), e.get("noise", 0) or 0
    step = 0.02
    if eff == "Engine":
        # idle to redline in 2.5 s, then held
        for k in range(150):
            s.play(step, *_modulated(e, min(1.0, k * step / 2.5)), noise=noise, fade=0)
        s.play(0.02, *_modulated(e, 1.0))
    elif eff == "Gear":
        dur = e.get("duration", 0.125)
        for _ in range(3):
            s.play(dur, e.get("frequency", 0), e.get("amplitude", 50), noise=noise)
            s.rest(0.7)
    else:
        # three slides: strength rises from the threshold to full
        th = e.get("threshold", 0) or 0
        for _ in range(3):
            for k in range(35):
                x = th + (1 - th) * k / 34
                s.play(step, *_modulated(e, x), noise=noise, fade=0)
            s.play(0.01, *_modulated(e, 1.0))
            s.rest(0.35)
    return s.buf


_play_lock = threading.Lock()
_player = None


def stop():
    global _player
    with _play_lock:
        if _player and _player.poll() is None:
            _player.kill()
        _player = None


def play(e, devid, mode="effect"):
    """Start a test on sink devid in the background; replaces a test still playing."""
    global _player
    if not devid or devid not in {s["name"] for s in sinks()}:
        # never let PulseAudio fall back to the speakers
        raise LookupError("the shaker sound card is not connected")
    pcm = render(e, mode).tobytes()
    vol = int(max(0, min(100, int(e.get("volume", 100)))) / 100 * 65536) if mode == "effect" else 65536
    stop()
    p = subprocess.Popen(["pacat", "--playback", f"--device={devid}", "--format=s16le", f"--rate={RATE}",
                          "--channels=8", "--channel-map=" + ",".join(CHANNEL_MAP), f"--volume={vol}",
                          "--client-name=simdash", "--stream-name=shaker test", "--latency-msec=60"],
                         stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with _play_lock:
        _player = p

    def feed():
        try:
            p.stdin.write(pcm)
            p.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass
    threading.Thread(target=feed, daemon=True).start()
    return len(pcm) / 16 / RATE
