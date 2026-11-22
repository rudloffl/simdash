# Desk 6DOF: geometry

[← Desk 6DOF](README.md)

## Frame and parameters

x forward (the car's nose), y left, z up. Origin: centre of the base, at the height of the servo shafts.
Angles in the tab: roll + leans right, pitch + nose up, yaw + nose left.

| Parameter | Default | Meaning |
|---|---|---|
| `layout` | flat | **flat**: the two servos of a pair back to back on one flat face, shafts parallel, horns swinging outwards (like most kits: one flat bracket per pair). **radial**: each shaft points away from the centre (pairs splay apart; bodies end up facing the next pair). |
| `base_r` | 58 mm | Base centre → the horns' plane (flat: the pair's face; radial: each shaft). |
| `shaft_spacing` | 46 mm | Flat: between the two shafts of a pair. MG90 bodies plus their screw tabs back to back need ≈ 44.5 mm. |
| `base_half` | 20° | Radial: half the angle between the two shafts of a pair. |
| `plat_r` | 46 mm | Platform centre → rod ball joint. |
| `plat_half` | 24° | Half the angle between the two joints of a platform pair. Platform pairs sit 60° from base pairs. |
| `horn` | 18 mm | Shaft → rod hole on the horn. Longer = faster, bigger moves, less resolution and torque. |
| `rod` | 90 mm | Ball centre to ball centre. |
| `first_pair` | 0° | Where servos 0–1 sit (0 = under the nose, so pitch and roll are symmetric left/right). |
| `servo_limit` | 60° | Horn travel allowed each way from horizontal (horn and rod must not hit the body or the base). |
| `transmission_min_deg` | 35° | Horn–rod angle kept within 35–145°. Near 0 or 180 the leg toggles: the horn turns, the platform barely moves, loads spike. 99° at home with the defaults (90 is ideal). |
| `joint_max_deg` | 30° | Each ball joint's swivel from its home direction, at the horn end and at the platform end (typical RC rod ends). |

The three servo pairs are 120° apart and the horns of a pair point away from each other. In the flat layout
both horns swing along the pair's face; in the radial one each swings in a plane tangent to the base circle and
each body runs from its shaft towards the next pair. Settings saved before the flat layout existed (2026-11-21)
stay radial.

With the defaults (flat): home height 86.4 mm (shafts to platform joints), rods lean 16° from vertical, and each
axis alone reaches about ±23 mm surge/sway, ±9 mm heave, ±11° roll, ±12.5° pitch, ±30° yaw with the joint
limits (±35 mm, ±15 mm, ±19.5°, ±22°, ±49° without them). The tab recomputes all of this as the numbers change.

## Limits

A pose is allowed (`motion.why_not` / `within`, used by the reach table, the cueing and the tab) when every
servo reaches it within `servo_limit`, every horn–rod angle stays within `transmission_min_deg`…180−that, and
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
