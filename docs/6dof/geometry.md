# Desk 6DOF: geometry

[← Desk 6DOF](README.md)

## Frame and parameters

x forward (the car's nose), y left, z up. Origin: centre of the base, at the height of the servo shafts.
Angles in the tab: roll + leans right, pitch + nose up, yaw + nose left.

| Parameter | Default | Meaning |
|---|---|---|
| `base_r` | 62.4 mm | Base centre → each servo shaft (where it crosses the horns' plane), as in the CAD sketch. |
| `base_angle` | 43.3° | Angle, seen from the centre, between the two shafts of a pair. The shafts are 2·`base_r`·sin(`base_angle`/2) apart, and the line through a pair's two shafts is `base_r`·cos(`base_angle`/2) from the centre. |
| `shaft_angle` | 90° | Each shaft's own rotation axis, from **perpendicular** to the base centre → shaft line (0°) turning towards that line (90°, **parallel**): at 0° the horn points straight at (or away from) the centre, its ball at home landing inside the base circle; at 90° the shaft is radial and the horn tangent to the base circle (the only layout this platform had before 2026-11-26); past 90°, up to 180°, it leans the other way, out past the base edge. Both shafts of a pair lean the same way (in or out) together — it doesn't depend on which of the pair. Pushing it towards 0° or 180° trades rod lean and the horn–rod angle (both improve) against the two servo bodies of a pair sitting closer together, or even colliding: check the 1:1 base drawing before printing anything. |
| `plat_r` | 46 mm | Platform centre → rod ball joint. |
| `plat_angle` | 48° | Angle between the two joints of a platform pair. Platform pairs sit 60° from base pairs. |
| `rod` | 90 mm | Ball centre to ball centre. |
| `horn` | 18 mm | Shaft → rod hole on the horn. Longer = faster, bigger moves, less resolution and torque. |
| `horns` | out | **out**: the horns of a pair point away from each other (at `shaft_angle` 90°, the horn-end ball joints sit far apart: 2·`base_r`·sin(`base_angle`/2) − 2·`horn` closer with **in**, where they point at each other instead). Away from 90° this still flips the horn's direction, but "far apart" / "close together" no longer describes it as simply. |
| `mount` | lying | The servo bodies; the shafts are horizontal either way, so the motion is identical. **lying**: length along the horn's swing (shaft axis ≈ 7 mm above the plate, or more if the horn swung down needs it). **upright**: standing on end, shaft near the top: only the 12.2 mm body width along the swing, tabs pointing up and down, shaft axis ≈ 22.8 mm above the plate. The design panel and the base SVG give the shaft height. |
| `first_pair` | 0° | Where servos 0–1 sit (0 = under the nose, so pitch and roll are symmetric left/right). |
| `home_angle` | 0° | Horn angle at home, from horizontal (+ = horn tip up). Every horn sits there when the platform is at home; the home height and the ball studs' directions follow. |
| `stroke` | 60° | How far each servo may turn each way from `home_angle` (e.g. 30 ± 35 = −5° … +65°; horn and rod must not hit the body or the base). |
| `transmission_min_deg` | 35° | Horn–rod angle kept within 35–145°. Near 0 or 180 the leg toggles: the horn turns, the platform barely moves, loads spike. 99° at home with the defaults (90 is ideal). |
| `joint_max_deg` | 30° | Each ball joint's swivel from its home direction, at the horn end and at the platform end (typical RC rod ends). |

The three servo pairs are 120° apart; the horns of a pair point away from each other or, with `horns` = in, at
each other. Settings saved before `shaft_angle` existed (2026-11-26, when the discrete `flat` / `radial` /
`aimed` `layout` choice was dropped in its favour) read as `shaft_angle` 90° — each shaft's own radial direction,
the same as the old `radial` (and the closest single value to the old `flat`). Settings saved before 2026-11-22
(`base_r` to the pair's face + `shaft_spacing`, half-angles `base_half` / `plat_half`, `servo_limit` = travel
from horizontal) are read as the same platform in today's terms.

The 1:1 SVGs (Base / Platform buttons) carry the same dimensions in green (radius, angle within a pair, 120°/60° between pairs, distance between the shafts and between the horn balls, horn length) and list every point's x, y, z under the drawing, ready to type into the CAD sketch.

With the defaults (`shaft_angle` 90°): home height 86.4 mm (shafts to platform joints), rods lean 16° from
vertical, and each axis alone reaches about ±23 mm surge/sway, ±9 mm heave, ±11° roll, ±12.5° pitch, ±30° yaw
with the joint limits (±35 mm, ±15 mm, ±19.5°, ±22°, ±49° without them). The tab recomputes all of this as the
numbers change.

Pushing `shaft_angle` towards 0° (everything else at its default) lowers the rod lean and brings the horn–rod
angle closer to the 90° ideal: at 0° home height is 89.9 mm, rods lean only 2° from vertical (horn–rod angle
88° against 99° at 90°), and the reach per axis is about the same. The trade-off: the two horn balls of a pair
end up closer together (≈ 53 mm at home, ≈ 50 mm over the stroke, against ≈ 82 / 64 mm at 90°) — still clear of
each other with these dimensions, but with less margin, and the two servo bodies themselves start to crowd each
other (see the 1:1 base drawing).

## Limits

A pose is allowed (`motion.why_not` / `within`, used by the reach table, the cueing and the tab) when every
servo reaches it within `home_angle` ± `stroke`, every horn–rod angle stays within `transmission_min_deg`…180−that, and
every ball joint swivels less than `joint_max_deg` from its home direction:

- at the platform end the stud turns with the platform: the rod is seen in the platform's frame;
- at the horn end the stud turns with the horn (about the shaft, by the horn angle): the rod is seen in the
  horn's frame.

That assumes the **studs are printed along the home rod direction**: the 1:1 drawings show it in blue
(16° from vertical with the defaults). The tab says which limit stops a pose ("held: joint 3 on the horn
swivels 34°").

## Inverse kinematics (rotary servos)

For each leg k: shaft **B**, horn direction **u** (unit, horizontal), horn length a, rod length s, and the platform
joint **P** = **T** + R·**p** in the world (p its position on the platform, R the rotation, **T** = (x, y, h₀ + z)).
With **l** = **P** − **B**:

```
L = |l|² + a² − s²
M = 2a · l_z
N = 2a · (l · u)
alpha = asin( L / √(M² + N²) ) − atan2(N, M)
```

The horn tip is **A** = **B** + a(cos α · **u** + sin α · **ẑ**); |**P** − **A**| = s is exactly the rod constraint
(`tests/test_motion.py` checks it). When |L| > √(M² + N²) the leg can't reach: the pose is refused and the
servos hold the last good one. Home height h₀ is where all α = 0.

Rotation: R = Rz(yaw) · Ry(−pitch) · Rx(roll), about the platform centre at the joints' height.

## Designing the parts

1. Measure your MG90s (body length, shaft offset, tab height, horn hole distances) and the rod ends.
2. Set the numbers in the tab; watch the reach table and the warnings (rod lean, shaft spacing).
3. **Download the 1:1 SVGs** (base and platform), import them as sketches (Fusion: Insert → SVG; FreeCAD:
   File → Import), or print at 100 % and check the 50 mm bar with a ruler.
4. Things to design: base plate with six servo brackets (walls the tab screws go into, shaft at `base_r`),
   platform with six ball studs (M3 at `plat_r`) and a car mount on top, rods if printed, maybe a cable
   channel and a mount for the PCA9685 and the pyboard under the base.
5. Centre of rotation is the platform's joint plane: a car mounted high moves its body more for the same angle.
   A low mount looks calmer, a high one more dramatic.

Things that matter more than they look: all six rods exactly the same length; horns fitted as close to
horizontal as the splines allow (the rest is trimmed in calibration); no slop in the ball joints.

## Servo angles and pulses

Angles are the horn's, from horizontal, + = tip up. At home every horn sits at `home_angle`; a pose is refused
when a servo would leave `home_angle` ± `stroke`. Calibration stays three points per servo measured from
horizontal (level, +45°, −45°): the home pulse and all others are interpolated from them (beyond ±45° the
nearest slope carries on). While calibrating, the servos not under test hold their home pulse. The standalone
demo file stores its steps from the home pulses, so ±127 steps of 4 µs cover the stroke wherever home is.

## Driving the servos directly

"Move it" has two modes. **Platform pose** sets x, y, z, roll, pitch, yaw and each servo follows (inverse
kinematics). **Servos directly** sets each horn's angle from home within the stroke and the platform follows:
`motion.fk` finds the pose by Newton steps on the inverse kinematics, starting from the last pose (a big jump
is walked in small steps), and `motion.solve_servos` applies the same limits as a pose before anything is sent
(`POST /api/motion/servos`). Moving one servo alone shows how stiff each axis is: on a geometry whose servos
barely change with yaw, one servo turning a degree swings the platform round by tens of degrees and soon
leaves its reach. Switching mode carries the platform over (the pose's servo angles, or the servos' pose).
