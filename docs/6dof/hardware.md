# Desk 6DOF: hardware

[← Desk 6DOF](README.md)

## Parts

| Part | Have | Notes |
|---|---|---|
| 6 × MG90 metal-gear micro servo | on order | ~2 kg·cm at 4.8 V, ~0.1 s/60°, ~180° travel, 3-pin plug (brown GND, red V+, orange signal). Analog: 50 Hz. Check the exact pulse range on yours (calibration in the tab). |
| PCA9685 16-channel PWM board | yes | I2C address 0x40 by default. Logic VCC 3.3 V from the pyboard, servo power on the V+ screw terminal. |
| pyboard v1.1 (MicroPython) | yes | USB serial to the PC; I2C 1 on X9/X10. |
| 5–6 V supply, ≥ 5 A | to get | A 5 V 5 A brick or a 6 V supply (MG90 is a bit stronger and faster at 6 V). |
| 1000 µF ≥ 10 V electrolytic | to get | Across V+/GND at the PCA9685: servo current comes in spikes. |
| 12 × M3 ball-joint rod ends | to get | RC car linkage ends (or ball studs + sockets). One per rod end. |
| 6 × rods | to get / print | M3 threaded rod cut to length, or printed rods with ball sockets; equal lengths matter more than the exact length. |
| Servo horns | with the servos | Single-arm horn, rod hole at the horn length (18 mm by default); drill / pick the hole that matches. |
| Base, platform, servo brackets, car mount | to print | From the 1:1 templates in the tab. |
| Dupont wires, screws M2/M3 | | |

Budget beyond what's on hand: roughly €20–30 (supply, capacitor, rod ends, rod).

## Wiring

![Wiring diagram](../../simdash/static/6dof-wiring.svg)

| From | To | Why |
|---|---|---|
| pyboard **X9** (SCL1) | PCA9685 **SCL** | I2C clock |
| pyboard **X10** (SDA1) | PCA9685 **SDA** | I2C data |
| pyboard **3V3** | PCA9685 **VCC** | PCA9685 logic only (a few mA) |
| pyboard **GND** | PCA9685 **GND** | common ground for the signals |
| pyboard **X8** | PCA9685 **OE** | optional: high = all outputs off (hardware kill). Leave it out and OE stays low (enabled) on most breakouts |
| supply **+** | PCA9685 **V+** terminal | servo power |
| supply **−** | PCA9685 **GND** terminal | servo ground (the board joins it to the logic GND) |
| PCA9685 channels **0–5** | servos **0–5** | servo k = PCA9685 channel k, numbered as in the tab |

- **Never power the servos from the pyboard or USB.** Six MG90s can pull 3–4.5 A when they stall or start
  together; USB gives 0.5 A and the pyboard's regulator far less.
- The grounds must be joined (pyboard GND ↔ PCA9685 GND ↔ supply −), or the signals have no reference.
- The pyboard's I2C 1 also has the on-board accelerometer (address 0x4C); no clash with the PCA9685 (0x40).
  The pyboard has pull-ups on X9/X10, so do most PCA9685 breakouts: fine together.
- Servo numbering follows the geometry: pairs go anticlockwise from the nose seen from above
  (0–1 under the nose, 2–3 rear left, 4–5 rear right with the defaults). See the base template.

## Power numbers

- MG90 at rest holding: ~10–150 mA each depending on load; moving: ~250–400 mA; stall ~700–800 mA.
- A small model car (100–400 g) on an ~100 mm platform is light work for six MG90s; most current goes into
  accelerating the servos themselves.
- Supply voltage: 4.8–6 V. Above 6 V most MG90s are out of spec.
