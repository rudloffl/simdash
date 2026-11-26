# Desk 6DOF: firmware

[← Desk 6DOF](README.md)

`firmware/6dof/main.py` and `pca9685.py`, for a pyboard v1.1 running MicroPython.

## Install

From simdash: **System → Desk 6DOF → Install firmware on the board** copies every file below to the
pyboard's `/flash` (each as `<name>.new`, checked, then all renamed at once, so a failed copy leaves the old
firmware whole), writes an empty `/flash/SKIPSD` and restarts it; `demo.bin` is kept. The card says whether the board runs the same version
as simdash's copy. By hand:


`main.py` (wiring to the pyboard), `board.py` (pulses: clamp, slew, park, glide), `fw.py` (PC control,
demo mode, USR switch, LEDs), `demo.py` (the demo file player) and `pca9685.py`, for a pyboard v1.1 running
MicroPython 1.9.4 or later. All but `main.py` and `pca9685.py` are plain Python, tested on the PC. The pyboard shows up as a USB drive (PYBFLASH): copy the three files onto it, eject,
press RST. Or with mpremote:

```
pip install --user mpremote
mpremote connect /dev/ttyACM2 cp firmware/6dof/board.py firmware/6dof/demo.py firmware/6dof/fw.py firmware/6dof/pca9685.py firmware/6dof/main.py :
mpremote connect /dev/ttyACM2 reset
```

(Check which ttyACM it is: `ls -l /dev/serial/by-id/` lists it as `usb-MicroPython_…`. simdash finds it by
MicroPython's USB vendor id `f055`, so the Moza ports are never touched.)

LEDs: blue = demo playing, green = the PC driving, a short green flash every 2 s = on a PC, waiting for
simdash (servos limp: the normal resting state), yellow = gliding to park. **Red only for a problem**:
blinking 1 Hz = limp with no PC, 5 Hz = no PCA9685, 4 Hz = the demo file failed; steady = stopped with USR
(latched; Arm in simdash clears it).

**Access to the port.** The pyboard's serial port belongs to the `dialout` group; a udev rule gives the
logged-in user access instead (rules that add `uaccess` must sort before `73-seat-late.rules`):

```
echo 'SUBSYSTEM=="tty", KERNEL=="ttyACM*", ATTRS{idVendor}=="f055", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/70-micropython.rules
sudo udevadm control --reload   # then unplug and replug the board
```

**MicroPython version.** The board here came with MicroPython 1.9.4 (2018). The firmware is kept compatible
with it (no `400_000` numbers, no f-strings, `LED.on()/off()` rather than `LED.value()`): a test checks the
sources. Updating MicroPython (DFU, BOOT0 jumper) isn't needed.

**Without the PCA9685** (not wired or not powered) the board still starts: the hello says
`6DOF 2 ch=6 pca=missing`, nothing is driven, and simdash refuses to arm with "the pyboard answers but its
PCA9685 doesn't".

Tested on the real board (2026-11-21): LEDs, I2C (the on-board accelerometer answers at 0x4C), all three
files compile on 1.9.4, `board.py` on the board: hello, clamp, slew exactly 6.00 µs/ms by the board's clock,
glide to park after 1000 ms of silence over 1000 ms then limp; a tick takes under 0.5 ms of its 5 ms; boot
into `main.py` and the handshake from simdash.
`main.py` owns the USB serial port, so the REPL is busy: mpremote breaks in with Ctrl-C when it connects.

## Protocol (version 5)

Text lines over the USB serial port (no binary, so a stray byte can't look like Ctrl-C):

| Line | Meaning |
|---|---|
| `S u0 u1 u2 u3 u4 u5` | Target pulses in µs for servos 0–5, clamped to 800–2200. |
| `P u0 u1 u2 u3 u4 u5` | Park pulses: where to glide when the PC goes silent. |
| `O` | Let go: every output off, servos limp. |
| `H` | Hello: replies `6DOF 5 ch=6`, plus ` pca=missing` without a PCA9685 and ` latched` after a USR stop. |
| `A` | Clear the USR stop latch (simdash sends it when you arm the servos in the tab). |
| `Q` | State report, answered in any mode and changing nothing (not even a playing demo): `STATE ver=5 mode=pc board=limp latched=0 error=0 pca=1 demo=1 played=0 clock=… ticks=… loops=… ms=… reset=power wdt=2000`. simdash's System tab shows it (Desk 6DOF card) with the loop and output rates from two reports. |

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

## SD card (version 6)

The pyboard v1.1 has a micro-SD slot. Out of the box it **boots from a card** when one is in (`/sd/main.py`)
instead of `/flash`, where this firmware lives. `/flash/SKIPSD` (written by *Install firmware*) stops that;
`main.py` then mounts the card itself at `/sd`, and `/sd/demo.bin` wins over `/flash/demo.bin` (room for a
whole race, and later the track map for the display). `Q` says `sd=1` (System tab: *SD card*). A board that
booted off a card before this still takes *Install firmware* (the copy uses absolute `/flash` paths).

## Watchdog (version 4)

The board resets itself when the **timed part** of its loop (outputs, speed limit, PC timeout, LEDs) hasn't
run for 2 s: the hardware watchdog is fed only when that part runs, not merely when the loop turns. This
covers the freeze seen on 2026-11-25 (the board still answered `H`, but its LED stood still on red until RST).
After a watchdog reset the board stays **limp and doesn't play its demo** (a long USR press still does), and
`Q` says `reset=watchdog` (System tab: *Last reset: watchdog*).

The watchdog can't be stopped once started. After Ctrl-C (the REPL, or simdash copying files) `main.py`
sets it to 30 s and leaves it as `wdt` in the REPL's globals; simdash's raw-REPL copier feeds it before every
command, so copies are fine, but an idle REPL resets after 30 s (mpremote and Thonny don't feed it). For a
REPL with no watchdog, start in safe mode: hold USR, press RST, release USR when the orange LED is lit;
`main.py` doesn't run.

## Standalone demo (no PC)

simdash's 6DOF tab bakes a recorded session, a lap or a time range into `demo.bin` (*Play a course* →
*Save as board demo*): the same pipeline as the replay (cueing, whole-pose guard, IK, the current calibration)
at 25 Hz in sim time, starting and ending at park with a 1 s glide. **It bakes in the calibration: build and
write it again after recalibrating.**

`demo.bin`, little-endian: `6DOFDEMO`, u8 version 1, u8 rate 25, u16 frames, 6 × u16 level pulses,
6 × u16 park pulses, u8 step 4 µs; then 6 × int8 per frame = (pulse − level) / 4, clamped to ±127 (±508 µs;
the tab says how many frames were clipped). 150 bytes a second: about 8 minutes fit in the board's flash
(73 kB free with the firmware), a whole race needs an SD card (`/sd/demo.bin` is preferred when present).

*Write to board* sends `O`, copies the file (mpremote when installed, else simdash's own raw-REPL copy,
`simdash/pyboard.py`, checked byte for byte: 16 kB in 7 s) and restarts the board. Or download `demo.bin`
and copy it onto the PYBFLASH drive.

On the board: **only without a PC** (powered from a charger: no PC has given it a USB address, read from the
STM32's USB register; `host=0` in `Q`), with a `demo.bin` present and a PCA9685, 3 s after power-up it plays
(blue LED): frames streamed from the file through one small buffer, interpolated to 50 Hz, a 20 ms output
tick, through the same clamp and speed limit as PC control. At the end it holds park 5 s and plays again. Any
line from a PC leaves the demo (glide to park) and the PC drives. **USR short press**: stop and go limp,
latched (red steady: no demo, and the PC can't move it until it sends `A`) until the next power-up; **long
press** (1.5 s): play again from the start. A file that can't be read: let go, red blinks at 4 Hz.

Measured on the board (MicroPython 1.9.4, a 109 s lap of Brands Hatch): 50.0 Hz ticks, frames read at 25 Hz,
1.1 kB of RAM kept, the slowest step 3.6 ms of its 20.

## Settings in main.py

| Name | Default | |
|---|---|---|
| `FIRST_CHANNEL` | 0 | Servos 0–5 on PCA9685 channels FIRST_CHANNEL… (consecutive, for the single I2C write). |
| `MIN_US`, `MAX_US` | 800, 2200 | Hard limits whatever the PC sends. MG90s hit their end stops near 600 / 2400 (stall at ~700 mA, stripped gears). |
| `RATE_US_PER_MS` | 6.0 | Speed limit. |
| `TIMEOUT_MS` | 1000 | Silence before gliding to park. |
| `FREQ` | 50 | PWM frequency. Analog MG90s want ~50 Hz. |
| `OSC` | 25 MHz | The PCA9685's clock is only ±few %; if a 1500 µs pulse measures off, trim it here. |

The logic is tested on the PC (`tests/test_firmware.py`, `tests/test_demo.py`, with fakes for the PCA9685,
the switch, the LEDs, the files and USB): the speed limit, the glide to park before letting go, the clamp, the
first `S` after a let-go; the demo file round trip, playing every frame at 50 Hz, looping through park,
leaving the demo when a PC talks, the USR presses, a broken file. Outputs update every 20 ms (`fw.TICK_MS`,
the servos' PWM frame).

## Ideas for later

- Report the supply voltage (a divider on an ADC pin) to warn about a weak supply.
