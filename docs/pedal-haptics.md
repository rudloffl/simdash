# Pedal haptics

[← simdash](../README.md)

Drives the Moza **Pedal Haptic Feedback module** (Pit House's "Vibration module", USB `346e:002f`) from AMS2's
telemetry, like Pit House does on Windows. The module stores nothing: simdash streams every effect while you drive,
on the **Pedal haptics** tab (`/#haptics`).

- Plug the module into its **own USB port** (a serial port `ttyACM*`, opened by boxflat's udev rule
  `99-boxflat.rules`), or **behind the wheelbase**. The USB port wins when both are there. Behind the base, simdash
  sends a query through the base's port and uses the extended address once the module answers (asked again every
  10 s while it does not). That form was captured behind an R16/R21 base by AZOM; other bases are untested.
- Close Pit House and boxflat while driving: only one program may write to the module.
- The defaults are Pit House's "PHF-Official" preset: engine on all pedals (100 Hz, 10 %), TC and gear shift on the
  throttle, ABS and gear shift on the brake, bite point on the clutch. Wheel spin, lockup, brake threshold and road
  texture are there but off.
- Each effect has a **Test** button (3 s with its current settings, even when switched off).

| Effect | Plays when | From AMS2 |
|---|---|---|
| Engine | RPM above the start point; 10 Hz rising to the chosen frequency at the end point | `mRpm`, `mMaxRPM` |
| Gear shift | 200 ms on every gear change (not into neutral) | `mGear` |
| Traction control | TC cuts in, with over 6 % throttle | `mCarFlags` bit `0x40` |
| ABS | ABS works, with over 6 % brake | `mAntiLockActive`, `mCarFlags` bit `0x10` |
| Wheel spin | a wheel slips over 15 % on throttle | wheel speeds and the tyre size learned while coasting |
| Wheel lockup | a wheel slips under −20 % under braking, above 2 m/s | same |
| Brake threshold | brake travel past the trigger | `mBrake` |
| Clutch bite point | clutch travel within 5 % of the trigger | `mClutch` |
| Road texture | the suspension moves (bumps, kerbs), above 2 m/s | `mSuspensionTravel` |

Everything stops 0.3 s after the telemetry does (pause, menus, AMS2 closed); a fresh connection first silences
every slot.

## Protocol

Moza serial framing on the module's port, 115200 baud, usual checksum (`0x0d + sum of the bytes`, modulo 256):

```
7e 0b 4d 12  01 SUB PEDAL SLOT EN  DUR(2) FREQ(2) STRENGTH(2)  checksum
```

`SUB` 01 = set; `PEDAL` 01 throttle, 02 brake, 03 clutch; `SLOT` 00 TC, 01 ABS, 02 lockup, 03 brake threshold,
04 engine, 05 bite point, 06 gear shift, 07 wheel spin, 08 road texture; `EN` 01 on, 00 off (with zero values: stop);
`DUR` ms the effect plays after this frame (so it is resent to hold it); `FREQ` Hz 10–100 (road texture: a suspension
position 0–100, lower = bigger bump); `STRENGTH` fraction of 65535. All big-endian. Timings copy Pit House's (ABS
66 ms every 45 ms, TC 200 ms every ~105 ms, engine one period every 33 ms).

Behind a wheelbase the same payload goes to the base's port with the extended address, one byte longer:
`7e 0c 4d 1f 1e 01 SUB PEDAL …` (replies `cd f1`). Example from AZOM's capture: `7e 0c 4d 1f 1e 01 01 02 00 01 01 f4 00 32
80 00 cd`; the same frame on USB ends in `a1`.

The protocol was described by [AZOM](https://github.com/giantorth/AZOM) and checked against Pit House by
[omarchy-moza](https://github.com/crmne/omarchy-moza/blob/main/docs/Pedal-haptics.md). Code: `simdash/haptics.py`.
