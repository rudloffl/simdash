# Haptics notes: measurements

[← Shakers](shakers.md)

Single sessions and bench tests behind the defaults and design choices in [shakers.md](shakers.md). Dated: they
describe one rig (ICUSBAUDIO7D USB 7.1 card, BST1 seat shaker, four corner pucks) and one PC.

## Effect thresholds (laps recorded 2026-11-20)

- **Suspension** (`mSuspensionVelocity`): on track 99 % of samples under 0.11 m/s (roll, pitch); kerbs 0.2-0.4;
  landings and off-track 0.4-1. With 0.08 / 0.4 m/s it played 2-6 % of the time on track and about 30 % on
  kerbs (McLaren 720S GT3, Interlagos).
- **Impact**: driving moves the horizontal acceleration about 1 g from one sample to the next; hits gave 6 to
  20 g; a crash at 190 km/h over 50 g at 20 Hz, over 60 g at 100 Hz (so a fixed 60 g cut dropped it: hence the
  velocity check). AMS2 bogus values: 401 g for one sample at 59 km/h (MINI), 329 g for about a second at
  ~50 km/h (BMW M4 GT3), speed unchanged both times.
- **Acceleration signs**: on 35,868 cornering samples `mLocalAcceleration.x` has the sign of yaw rate x forward
  speed in all but 28 (x points left); under hard braking `z` averages +12.5 m/s² (z points backward).
- **G-force**: GT3 cars hold 1.6-1.7 g in most corners (p90), up to 2.0 (p99); road cars and the kart 1-1.5 g.
  The default (1 to 2 g, all wheels) played 25 % of a BMW M4 GT3 race at Interlagos.
- **Rev limiter**: above 97 % of max RPM for 2-5 % of the driving; above 99 % at most 1 %.
- **Traction control flag** (`mCarFlags` 0x40): set on 47-67 % of on-throttle samples, Kart Cross included.

## Clipping and the limiter (BMW M4 GT3, Interlagos, 60 s)

Before the limiter the seat (Engine + Gear + Impact...) peaked at 2.5x full scale and was clipped 1.46 % of the
samples, the corners 0.15-0.20 %. With the limiter and ducking (defaults): no clipped sample; the limiter
worked 16 % of the time on the seat (up to -54 %) and 2-6 % on the corners; 0.45 ms per 10 ms block.

## Latency (2026-11-20)

Race test, buffer (tlength) 30 / 20 / 15 ms with 10 ms requests: 49.1 / 50.6 / 49.7 ms to the card, 4 / 1 / 0
"dropouts" (writes later than the buffer) in a minute each. The buffer did not matter.

Bench test with silence (PipeWire 1.6.2, the card's quantum 256/48000 = 5.3 ms, no xruns in any run):

| Writes | Request (minreq) | Buffer | Latency (median, incl. the block) |
|---|---|---|---|
| 10 ms | 10 ms | 30 / 20 / 15 ms | 46 / 44 / 46 ms |
| 5 ms | 5 ms | 15 / 10 / 7.5 ms | 31 ms |
| 10 ms | 2.5 ms | 30 / 20 / 15 ms | 34 / 34 / 37 ms |
| 2.5 ms | 2.5 ms | 10 ms | 31 ms |
| 5 ms, quantum forced to 128 / 64 | 2.5 ms | 15 ms | 29 / 27 ms |

So pipewire-pulse sizes its queue from the request size; below ~30 ms the USB card and its driver dominate.
The engine now announces 2.5 ms with a 20 ms buffer: 28.5 ms measured in the engine, no xruns.

The ~31 ms late writes of the first races were the status file: replacing a file on ext4 can wait for a disk
flush. It is now written from a thread of its own into `/run/user/<uid>`.

## 2026-11-21: a race with silent shakers (pedals fine, replay afterwards fine)

The engine ran the whole race normally (950 s playing, 0 xruns, effects up to 100 % on every shaker in the
`felt` table), so the sound was made and written; it didn't reach the shakers. Afterwards, with AMS2 closed,
the engine path itself was fine: a test tone and a fake race came out of the card's monitor at the right level
on all eight channels. What can make a healthy stream silent: PipeWire moves a stream to another output when
its sink disappears, without the client noticing, and the 7.1 sink disappears when the card changes profile.
This card ranks its digital (S/PDIF + input) profiles far above 7.1 (5565 against 1200), and WirePlumber's
preferred default output was the card's S/PDIF output (`default.configured.audio.sink`), saved that day.

Fixes:
- `engine.CardGuard`: every 2 s (own thread, normal priority) checks with pactl that the engine's stream is on
  the sink it was opened for; if that sink is gone the card is set back to its profile, then the stream is
  moved back. Logged as `card guard: …` in `~/.local/state/simdash-haptics.log`, counted in the status
  (`card_guard`). Tested by hand: stream moved to the speakers → back in < 2 s; card switched to
  `output:iec958-stereo+input:analog-stereo` → 7.1 restored and stream moved back in < 2 s.
- WirePlumber's preferred default output set to the motherboard's analog output (backup of the old
  `default-nodes` next to it), so nothing asks for the card's S/PDIF any more.

Not proven which one happened in that race: the next one will say, in the log.

## 2026-11-21: corner shakers rebalanced (kerb, suspension)

The Output card's "Continuous" column said the corners could reach 192-250 % (wheelspin + kerb + oversteer at
their strongest together). Replaying two Brands Hatch races (2026-11-21:3 MINI, 2026-11-20:5 McLaren GT3)
through the effects showed what really happens: slip / lock-up and kerbs almost never overlap (< 0.1 % of
driving); the overload came from **Kerb itself, set to 100 % at volume 100**, above the 95 % ceiling, so the
limiter cut in on every kerb, and from **Kerb + Suspension** (a kerb is a bump), up to 220 %.

| Corners 0-3 | Before | After | Time over the ceiling (% of driving, per corner) |
|---|---|---|---|
| Kerb strength | 100 % | 70 % | before 1.1-3.1 %, after 0.0-0.3 % |
| Suspension max | 100 % | 80 % (min 30 % unchanged) | |

Kerb 65 % or Suspension 70 % gained almost nothing more. Wheelspin, lock-up, oversteer, impacts and the L/R
differences unchanged. Backups: `effects.json.bak-2026-11-21-rebalance`, `monocoque.config.bak-2026-11-21-rebalance`.
The "Continuous" column still adds wheelspin and kerb together (162 % on the fronts now): a worst case that the
recordings say doesn't happen in practice.
