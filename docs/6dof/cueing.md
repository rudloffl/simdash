# Desk 6DOF: motion cueing

[← Desk 6DOF](README.md)

How telemetry becomes a pose for a 10 cm platform (`motion.Cueing`). **Built for replays** (the tab's *Play a
course* card, settings in the *Cueing* fold); live driving is still to come. Two modes, picked in the tab, to
compare on the same replay.

## Inputs and their signs

From the recording (20 Hz telemetry and per-wheel rows), converted to the car's frame by `motion.car_frame`
(pCARS local frame: X left, Y up, Z back). Each sign was checked on recorded laps, not assumed; the checks are
regression tests on a lap extracted to `tests/fixtures/brands_hatch_lap.json`:

| Input | From | Checked (Brands Hatch, MINI, 2026-11-20) |
|---|---|---|
| fwd, left, up (m/s²) | `-local_acc_z`, `local_acc_x`, `local_acc_y` | braking > 0.8 → fwd < 0 in 99.8 % of samples (median −1.12 g) |
| yaw rate (rad/s, + = turning left) | `angular_vel_y` | same sign as lateral g × speed in 99 % of corners (correlation 0.77: slip, kerbs, camber); a lap of the clockwise track adds up to −360° |
| slope (°, + = nose up) | `orientation_x` | climb rate = speed · sin(slope), correlation 0.99 |
| banking (°, + = leaning right) | `−orientation_z` | includes the body's own roll: −0.60 with lateral g |
| suspension (cm) | `suspension_travel` per wheel, + = extended | braking shortens the front (+0.74 with fwd), a left turn shortens the right side (−0.93 with left); front−rear, left−right and mean taken from the session's median (removes the static rake, 1.5 cm on the MINI) |
| slip angle (°) | `atan2(local_vel_x, −local_vel_z)` above 5 m/s | median 0.4°, 95 % under 3.7°, 75° in a spin |

`angular_vel_y` is the opposite of the change in `orientation_y` (ratio −1.00): AMS2's heading turns the other way.

## Washout mode (fools an inner ear)

| Axis | From | Setting (default) |
|---|---|---|
| Pitch | longitudinal g, low-passed: braking dips the nose | `pitch_per_g` 12°/g, `tilt_ms` 150 |
| Roll | lateral g, low-passed: leans to the outside of the corner | `roll_per_g` 10°/g |
| Surge / sway | longitudinal / lateral g, high-passed (a quick shift that drifts back) | `surge_per_g` 15 mm, `sway_per_g` 12 mm, `washout_ms` 800 |
| Heave | vertical g, high-passed | `heave_per_g` 10 mm |
| Yaw | yaw rate, high-passed (a flick on turn-in) | `yaw_per_rad` 12°/(rad/s) |

**Not built:** the tilt is low-passed but not rate-limited, and washout mode uses neither the slope nor the
banking (the body mode does).

## Body mode (a car on its springs, seen from outside)

| Axis | From | Setting (default) |
|---|---|---|
| Pitch / roll | front−rear / left−right suspension; when none was recorded (slimmed days), a spring–damper driven by the g-forces | `body_pitch_per_cm`, `body_roll_per_cm` 5°/cm; `body_hz` 1.8, `body_zeta` 0.5 (dives and rebounds with a little overshoot) with `pitch_per_g`, `roll_per_g` |
| Heave | mean suspension, plus vertical g high-passed (kerbs) | `body_heave_per_cm` 4 mm/cm, `kerb_per_g` 6 mm |
| Yaw | slip angle: the nose shows where the car points against where it goes, so drifts look like drifts | `slip_yaw` 1 (° per °) |
| Slope, banking | orientation, low-passed 0.5 s, scaled | `slope_gain` 0.4 |
| Surge / sway | none: a car body doesn't slide on its springs | |

## Both modes: shaping and safety

1. Inputs clamped to ±3 g (kerb and crash spikes reach 4 g).
2. **Soft limit per axis** before the final smoothing: each axis is squeezed (tanh) into `budget` × its
   single-axis reach (default 0.5, so linear well inside and never past it).
3. Final low-pass (`smooth_ms` 40) and `master`.
4. **Last-resort scale** (`motion.ScaleGuard`): a combined pose that still doesn't fit is shrunk as a whole;
   the scale is smoothed (50 ms to shrink, 500 ms to recover) and never above what fits. On a Brands Hatch
   race with budget 0.5: needed in 2–3 % of frames, rarely below 0.85.
5. **Fades** (0.5 s): in after load, seek, play and a skipped gap in the recording (pits, menus), where the
   filters also restart from the new inputs (no surge or yaw spike); out to home on pause.
6. The reach itself includes the joint limits (see [geometry](geometry.md#limits)).

Measured: 0.1 ms per 50 Hz frame (settings, legs and reach cached).

## Testing without driving

Replays of recorded sessions go through the same cueing; the sliders change it while it plays.
