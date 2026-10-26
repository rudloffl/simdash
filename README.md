# simdash

Live dashboard and automatic session recorder for Automobilista 2, served by FastAPI.
Open http://127.0.0.1:8050 while it runs.

It is built on [simapi](https://github.com/Spacefreak18/simapi), the sim telemetry
library behind [Monocoque](https://github.com/Spacefreak18/monocoque), the Linux
sim-racing device manager (wheels, pedals, shakers, haptics...). simapi's `simd`
gets AMS2's telemetry out of Proton, and simdash records it and turns it into the
pages below.

![Session analysis: laps, lap map coloured by speed, lap traces, race replay and results](docs/session.png)
*Sessions: a recorded race at Spielberg, with lap times, the lap map, lap traces, the race replay and results.*

![Live dashboard while driving](docs/live.png)
*Live: what the dashboard shows while you are on track (here fed by the built-in fake game, `simdash.fake_ams2`).*

> **About this project:** I have not written any of this code (yet). simdash was
> built entirely through conversations with [Claude Code](https://claude.com/claude-code),
> Anthropic's coding assistant: I described what I wanted, tested it in the car,
> and reported what looked wrong.

Needs Linux, Python 3.10+, and AMS2 running under Proton with
[simd](https://github.com/Spacefreak18/simapi)'s `pcars2bridge` copying the game's
shared memory to `/dev/shm/$pcars2$` (AMS2: Options > System > Shared Memory = Project CARS 2).

## Install

```
make install        # creates .venv, installs ~/.local/bin/simdash
```

```
simdash                    # start (records while AMS2 is on track)
simdash --host 0.0.0.0     # also reachable from a phone/tablet on the LAN
```

## What it shows

- **Live**: speed, gear, RPM, position, lap times, fuel; throttle/brake/clutch/steering;
  G-force circle with a 3 s trail; per-wheel slip (spin / lock) and car slip angle;
  a rolling 30 s chart; a track map with every car; standings with approximate gaps.
- **Sessions**: every recorded session from every day, searchable by track/car and
  filterable by type and day. Per session: laps table (sectors, top speed, fuel),
  lap map coloured by speed / pedals / slip / track limits, lap traces with a second lap
  overlaid, a delta-time row and a track-limits row (left/right tyres on a kerb or off track), race replay of all cars, results, CSV export, delete.
  Links like `/#session=2026-10-25:3` open a session directly.

## Data source

It reads `/dev/shm/$pcars2$` directly (the copy pcars2bridge makes), not `SIMAPI.DAT`,
because that is where the G-forces, wheel speeds and all cars' positions are.
Offsets of every field in `pcars2_offsets.json` are generated from simapi's `pcars2data.h`
(`make offsets SIMAPI_INC=/path/to/simapi/include` regenerates them).
`simdash/static/plotly.min.js` is plotly.js (MIT), vendored so the app works offline.

- G-forces: `mLocalAcceleration`. Assumed frame: X right, −Z forward, so
  accelerating is up and right turns are right on the G circle. Check this on track.
- Wheel slip: AMS2 gives no slip ratio. simdash learns each wheel's
  speed-to-rotation ratio per car while you **coast straight above 50 km/h with no
  pedals for ~2 s** (saved in `wheels.json`), then slip = wheel surface speed vs
  car speed. It shows "learning" until then.
- Map orientation is a guess. Use **Mirror** / **Rotate** on the map once per
  setup (remembered in the browser).

## Storage

`~/.local/share/simdash/` (override with `SIMDASH_DATA`):

| Path | Contents |
|---|---|
| `days/YYYY-MM-DD.sqlite` | one database per day, every shared-memory field in its raw unit (layout below) |
| `tracks/*.json` | track outline from your best clean lap, used for the live map |
| `wheels.json` | learned wheel ratios per car |

A session belongs to the day it started on. A new session starts on a change of
track, car or session type, on a restart, or after returning to the main menu.
Sessions with under 15 s of driving and no completed lap are dropped.
Size is roughly 40 to 100 MB per hour of driving.

Tables, by how often their data changes (full column list in `simdash/store.py`):

| Table | Rows |
|---|---|
| `session` | one per session: track, car, session type, game version, summary (laps, best, result) |
| `track`, `vehicle`, `driver` | names, stored once and referenced by id |
| `participant` | each car in a session: driver, vehicle, player or not |
| `lap` | one per completed player lap: time, sectors, top speed, fuel used |
| `tick` | one per recorded instant (session, timestamp); every time-series row points to one |
| `telemetry` | player car at 20 Hz: inputs, engine, motion vectors, fluids, position |
| `wheel` | 4 rows per 20 Hz tick: suspension, temperatures, pressure, wear, damage, terrain, slip |
| `car_motion` | every car at 2 Hz: position, orientation, speed, lap, race position |
| `player_state`, `player_timing`, `event_state`, `weather`, `car_status` | change logs: a row only when a value changes (flags, damage, settings, compounds, lap and sector times, rules, weather, each car's status) |
| `enum_name` | names of the enum codes (session type, race state, terrain...) |

A change-log row is valid from its tick until the next row for the same session
(and car, for `car_status`). The views `v_sessions`, `v_samples` and `v_results`
join it all back into the shapes the web app uses. Databases from before this
layout are renamed `YYYY-MM-DD.v1.sqlite` on first start and no longer listed.

## Testing without the game

```
SIMDASH_SHM=/dev/shm/simdash-fake SIMDASH_DATA=/tmp/simdash-test .venv/bin/python -m simdash --port 8051 &
SIMDASH_SHM=/dev/shm/simdash-fake .venv/bin/python -m simdash.fake_ams2 --laps 3
```

## Record at every login (optional)

```
install -Dm644 simdash.service ~/.config/systemd/user/simdash.service
systemctl --user daemon-reload && systemctl --user enable --now simdash
```

## Credits

- [Monocoque](https://github.com/Spacefreak18/monocoque) and
  [simapi](https://github.com/Spacefreak18/simapi) by Spacefreak18: the Linux sim-racing
  stack this sits on. simapi's `simd`/`pcars2bridge` exposes AMS2's shared memory and
  its `pcars2data.h` provides the field layout simdash reads.
- [plotly.js](https://github.com/plotly/plotly.js) (MIT) for the charts and maps.
- Code written with [Claude Code](https://claude.com/claude-code).

## License

GPL-3.0, like Monocoque (simapi itself is LGPL-3.0). See [LICENSE](LICENSE).
