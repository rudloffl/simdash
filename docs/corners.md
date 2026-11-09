# How corners are found

[← simdash](../README.md)

AMS2 gives no corner positions or names, so simdash derives the corners from a lap's speed along its lap
distance: the analysed lap in [Sessions](sessions.md), your best lap on that track and car (the reference)
in [Live](live.md) and the [Corner Genie](corner-genie.md) popup, the fastest detailed lap on the track
in Corner passes. The code is `findCorners` and `cornerStats` in `simdash/static/app.js`.

- **Corner:** a speed dip of more than 10 km/h. Going along the lap, once the speed has dropped more than
  10 km/h below its last peak and then risen more than 10 km/h above its lowest point, that lowest point is
  a corner's **apex**. Fast kinks taken without slowing are not corners. Corners are numbered C1, C2... in
  lap order (not the official turn numbers).
- **Segment:** each corner owns the stretch from the speed peak before it to the speed peak after it. The
  first starts at the lap start, the last ends at the lap end, so the segments cover the whole lap and their
  times add up to the lap time. A segment's time therefore includes the straight its exit leads onto.
- **Braking point:** the first sample after the peak before the corner where the brake is above 20 %, at
  most 600 m before the apex ("lift" if there is none). 20 % because the brake can read 6 to 10 % with a
  foot resting on the pedal. It is given in metres before the reference apex, so two laps compare only
  where they braked.
- **Full throttle:** the first sample after the slowest point with the throttle at 95 % or more, in metres
  after the reference apex.
- **Entry speed** is the speed at the braking point (at the segment start for a lift), **min speed** the
  lowest speed in the segment, **exit speed** the speed 100 m after the apex (or at the segment end if
  sooner), **gear** the most common forward gear within 30 m of the slowest point (mid-shift samples read
  neutral or a passing gear, so the lowest gear seen would be wrong).
- **Comparing laps:** the other lap (compare lap, session best, each live pass, each corner pass) is
  measured over the same segment distances. Δ is the difference in segment time; negative means faster.
- **Names:** corners you name (✎ in the Sessions corner table) are saved per track layout with their apex
  distance in `tracks/<track>.corners.json`, and a name goes to the corner whose apex is within 80 m of it.
- **Sectors:** where sector 2 and 3 start comes from the sector number AMS2 reports while you drive,
  recorded with every sample.
