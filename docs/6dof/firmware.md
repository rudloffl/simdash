# Desk 6DOF: firmware

[← Desk 6DOF](README.md)

`firmware/6dof/main.py` and `pca9685.py`, for a pyboard v1.1 running MicroPython.

## Install

The pyboard shows up as a USB drive (PYBFLASH): copy `main.py` and `pca9685.py` onto it, eject, press RST.
Or with mpremote:

```
pip install --user mpremote
mpremote connect /dev/ttyACM2 cp firmware/6dof/pca9685.py firmware/6dof/main.py :
mpremote connect /dev/ttyACM2 reset
```

(Check which ttyACM it is: `ls -l /dev/serial/by-id/` lists it as `usb-MicroPython_…`. simdash finds it by
MicroPython's USB vendor id `f055`, so the Moza ports are never touched.)

On start the red LED blinks: outputs are off, servos limp. Green = driving the servos.
`main.py` owns the USB serial port, so the REPL is busy: mpremote breaks in with Ctrl-C when it connects.

## Protocol

Text lines over the USB serial port (no binary, so a stray byte can't look like Ctrl-C):

| Line | Meaning |
|---|---|
| `S u0 u1 u2 u3 u4 u5` | Pulse widths in µs for servos 0–5 (PCA9685 channels 0–5), clamped to 600–2400. |
| `O` | Let go: every output off, servos limp. |
| `H` | Hello: replies `6DOF <version> ch=6`. simdash shows the reply in the tab. |

No line for 1 s → all off. simdash re-sends the last pulses every 0.3 s while the servos are armed, so a
platform held still stays powered, and stopping simdash or pulling the USB lets go within a second.

## Settings in main.py

| Name | Default | |
|---|---|---|
| `CHANNELS` | 0–5 | PCA9685 channel of servo 0..5, if the wiring differs. |
| `MIN_US`, `MAX_US` | 600, 2400 | Hard limits whatever the PC sends. Narrow them once the horns' safe range is known. |
| `TIMEOUT_MS` | 1000 | Silence before letting go. |
| `FREQ` | 50 | PWM frequency. Analog MG90s want ~50 Hz. |
| `OSC` | 25 MHz | The PCA9685's clock is only ±few %; if a 1500 µs pulse measures off, trim it here. |

## Ideas for later

- Smooth on the board: limit the change per 20 ms frame (the PC sends at up to ~30–100 Hz, the servos see 50 Hz).
- Report the supply voltage (a divider on an ADC pin) to warn about a weak supply.
