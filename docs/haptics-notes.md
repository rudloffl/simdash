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
