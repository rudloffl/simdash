# Desk 6DOF: motion cueing

[← Desk 6DOF](README.md)

How telemetry becomes a pose for a 10 cm platform. **Built for replays** (`motion.Cueing`, the tab's
*Play a course* card, settings under "Cueing"); live driving is still to come.

## What is built (2026-11-21)

| Axis | From | Setting (default) |
|---|---|---|
| Pitch | longitudinal g, low-passed: braking dips the nose | `pitch_per_g` 12°/g, `tilt_ms` 150 |
| Roll | lateral g, low-passed: leans to the outside of the corner | `roll_per_g` 10°/g |
| Surge / sway | longitudinal / lateral g, high-passed (a quick shift that drifts back) | `surge_per_g` 15 mm, `sway_per_g` 12 mm, `washout_ms` 800 |
| Heave | vertical g, high-passed | `heave_per_g` 10 mm |
| Yaw | yaw rate, high-passed (a flick on turn-in) | `yaw_per_rad` 12°/(rad/s) |
| All | final low-pass, overall scale | `smooth_ms` 40, `master` 1 |

Inputs are clamped to ±3 g (kerb and crash spikes reach 4 g). A pose the servos can't reach is shrunk as a
whole until it fits (on a Brands Hatch race: 0.1 % of frames). Frames: pCARS local X left, Y up, −Z forward.
Not used yet: orientation (track slope, banking), suspension.

## Inputs available

From `$pcars2$`, read by the haptics engine at ~100 Hz:

| Signal | Field | Use |
|---|---|---|
| Acceleration, car frame | `mLocalAcceleration` | surge (braking / accelerating), sway (cornering), heave |
| Rotation rates | `mAngularVelocity` | yaw rate, roll and pitch rates |
| Orientation | `mOrientation` | track slope and banking (pitch, roll of the car itself) |
| Suspension travel / velocity | `mSuspensionTravel`, `mSuspensionVelocity` | bumps, kerbs, body roll and pitch |
| Speed, on track, paused | | fade in/out, park at home when paused or in the pits |

## Background: classic washout, simplified

- **Surge:** a quick forward/back shift (high-passed acceleration, so it returns to centre) plus
  **tilt coordination**: sustained braking pitches the nose down, acceleration nose up (low-passed,
  rate-limited so the tilt itself isn't noticed as a rotation).
- **Sway:** same with roll: cornering leans the car outwards.
- **Heave:** vertical acceleration and suspension, high-passed.
- **Yaw:** high-passed yaw rate (a flick on turn-in that drifts back).
- **Track:** slope and banking added directly, scaled down.
- **Body:** roll and pitch from suspension (left-right and front-rear travel differences): the most
  visible thing on a model car, and they look right without any filtering.

On a desk toy the goal is "looks right", not "feels right": exaggerate (gain per axis), keep the sum inside
the reach table, and soften with a limiter rather than hard clipping at the edge.

## Shaping and safety

- Per-axis gain, low-pass for servo jitter, rate limit, soft limiter towards the reach.
- When AMS2 pauses, leaves the track or stops sending: glide back home over ~1 s, then let go after a while.
- IK failure: hold the last good pose (already the case in the jog code).

## Testing without driving

Replay recorded sessions (20 Hz telemetry in the day databases: acceleration, orientation, suspension) through
the same cueing, like the shaker replay does. Sliders for the gains in the tab while it plays.
