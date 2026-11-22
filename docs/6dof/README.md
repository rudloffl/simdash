# Desk 6DOF

[← simdash](../../README.md)

A small Stewart platform on six hobby servos with a model car on top, moving on the desk as the car
moves in AMS2. A toy and a test bench for motion cueing: no seat, no rider.

**Status (2026-11-21):** design stage. The servos (6 × MG90) are on order, the PCA9685 and the pyboard
are on hand, parts to be designed and 3D printed. simdash has a **6DOF** tab (`/#motion`) to design the geometry, print 1:1 templates, jog the platform by hand, calibrate the servos and
**play a recorded session** on it (Play a course: track map with the car's position, and the car seen from
above with the shakers and pedal haptics that fired at that moment, as recorded by the haptics engine or
recomputed with that day's settings). The geometry is locked unless *Unlock to edit* is ticked (it locks again
when leaving the tab). Nothing drives it live from the game yet.

![The 6DOF tab: the platform in 3D with the car, pose sliders, servo angles and pulses, geometry with its reach, calibration](../img/6dof-tab.png)

| Page | What's in it |
|---|---|
| [Hardware](hardware.md) | Parts list, wiring, power, the pyboard's pins. |
| [Geometry](geometry.md) | The parameters, the inverse kinematics, how far it moves, designing the printed parts. |
| [Firmware](firmware.md) | The MicroPython program on the pyboard and the serial protocol. |
| [Motion cueing](cueing.md) | Plan: from AMS2's telemetry to a pose (not built yet). |

![Wiring: PC → pyboard v1.1 → PCA9685 → six servos, servo power from a separate supply](../../simdash/static/6dof-wiring.svg)

## How the pieces fit

```
AMS2 ──shared memory──▶ simdash (PC)                                  pyboard v1.1           PCA9685      6 × MG90
                        pose (x y z roll pitch yaw)                    MicroPython            50 Hz PWM
                        → inverse kinematics → 6 horn angles   ──USB──▶ "S 1500 … 1500\n"  ──I2C──▶ ch 0–5  ──▶ horns
                        → calibration → 6 pulse widths (µs)            clamp, write, watchdog
```

All the maths lives in simdash (`simdash/motion.py`), where it is easy to change and to test; the
pyboard only clamps the six pulses, writes them, and lets go of the servos when nothing arrives for a second.

## Plan

1. **Build and jog** (now): design and print the base, platform and rods; wire it; flash the firmware;
   calibrate each servo's centre and direction in the tab; move it with the sliders and the Demo.
2. **Replay** (built 2026-11-21): *Play a course* runs a recorded session through the cueing at 50 Hz
   (inputs interpolated from the 20 Hz telemetry), so the cueing can be tuned at the desk without driving.
3. **Live:** the haptics engine (already reading AMS2 at 100 Hz) feeds the cueing; the platform follows the race.
4. Later, maybe: smoother bus servos (Feetech STS3215), a bigger platform.

## Files

| Where | What |
|---|---|
| `simdash/motion.py` | Geometry, inverse kinematics, reach, 1:1 SVG templates, serial link to the pyboard. Settings in `~/.local/share/simdash/motion.json`. |
| `simdash/static/6dof-wiring.svg` | The wiring diagram (also shown in the tab). |
| `firmware/6dof/` | `main.py`, `board.py` (logic: speed limit, park, glide) and `pca9685.py` for the pyboard. |
| `tests/test_motion.py`, `tests/test_firmware.py` | IK and joint limits, calibration, cueing (both modes, fades, soft limits), telemetry signs on a recorded lap, the link against a fake pyboard; the firmware logic under CPython. |

API: `GET/PUT /api/motion` (settings + design numbers), `POST /api/motion/pose` (solve a pose; sent to the
pyboard when the servos are armed and the pose is reachable), `POST /api/motion/arm` `{on}`,
`GET/POST /api/motion/replay` (`{action: load|play|pause|seek|stop}`), `POST /api/motion/raw` `{servo, us}` (calibration),
`GET /api/motion/template/{base|platform}.svg`.
