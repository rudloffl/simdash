# Desk 6DOF: firmware

[← Desk 6DOF](README.md)

`firmware/6dof/main.py` and `pca9685.py`, for a pyboard v1.1 running MicroPython.

## Install

`main.py` (wiring to the pyboard), `board.py` (the logic, plain Python) and `pca9685.py`, for a pyboard v1.1
running MicroPython. The pyboard shows up as a USB drive (PYBFLASH): copy the three files onto it, eject,
press RST. Or with mpremote:

```
pip install --user mpremote
mpremote connect /dev/ttyACM2 cp firmware/6dof/board.py firmware/6dof/pca9685.py firmware/6dof/main.py :
mpremote connect /dev/ttyACM2 reset
```

(Check which ttyACM it is: `ls -l /dev/serial/by-id/` lists it as `usb-MicroPython_…`. simdash finds it by
MicroPython's USB vendor id `f055`, so the Moza ports are never touched.)

LEDs: red blinking = limp, green = driving the servos, yellow = gliding to park.
`main.py` owns the USB serial port, so the REPL is busy: mpremote breaks in with Ctrl-C when it connects.

## Protocol (version 2)

Text lines over the USB serial port (no binary, so a stray byte can't look like Ctrl-C):

| Line | Meaning |
|---|---|
| `S u0 u1 u2 u3 u4 u5` | Target pulses in µs for servos 0–5, clamped to 800–2200. |
| `P u0 u1 u2 u3 u4 u5` | Park pulses: where to glide when the PC goes silent. |
| `O` | Let go: every output off, servos limp. |
| `H` | Hello: replies `6DOF 2 ch=6`. |

The board:

- **Limits the speed** of every servo to 6 µs/ms (about an MG90's top speed: a 600 µs step takes 100 ms) and
  updates the outputs every 5 ms, whatever the PC sends: no full-speed jumps on arm, seek, park or a gap.
- After a let-go it doesn't know where the horns are: the **first `S` is the starting point**. simdash always
  sends `P` (park = home pose) and `S` home first when it connects.
- **No line for 1 s** (simdash stopped, USB pulled, PC asleep): glides to the park pulses over about 1 s, holds
  0.3 s, then goes limp; without park pulses it goes limp at once. simdash re-sends the last pulses every 0.3 s
  while the servos are armed.
- Writes all six channels in **one I2C transaction** (auto-increment), so they change in the same PWM frame.

simdash refuses to arm without a `6DOF 2` (or later) hello: with the board in the REPL, a crashed `main.py`,
the old version 1 firmware or another board, the tab says why and nothing moves.

## Settings in main.py

| Name | Default | |
|---|---|---|
| `FIRST_CHANNEL` | 0 | Servos 0–5 on PCA9685 channels FIRST_CHANNEL… (consecutive, for the single I2C write). |
| `MIN_US`, `MAX_US` | 800, 2200 | Hard limits whatever the PC sends. MG90s hit their end stops near 600 / 2400 (stall at ~700 mA, stripped gears). |
| `RATE_US_PER_MS` | 6.0 | Speed limit. |
| `TIMEOUT_MS` | 1000 | Silence before gliding to park. |
| `TICK_MS` | 5 | Output update period. |
| `FREQ` | 50 | PWM frequency. Analog MG90s want ~50 Hz. |
| `OSC` | 25 MHz | The PCA9685's clock is only ±few %; if a 1500 µs pulse measures off, trim it here. |

The logic is tested on the PC (`tests/test_firmware.py`, with a fake PCA9685): the speed limit, the glide to
park before letting go, the clamp, the first `S` after a let-go.

## Ideas for later

- Report the supply voltage (a divider on an ADC pin) to warn about a weak supply.
