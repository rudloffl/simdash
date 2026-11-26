"""OLED race-screen mockups for the 2.42" SSD1309 (128x64, 1 bit) on the desk 6DOF's pyboard.

Each screen is drawn at the display's real resolution, 1 bit per pixel, no anti-aliasing (what the
pyboard can send), then shown three ways: 128x64 as is, x5 with an OLED-like pixel grid, and all ten on
one review sheet. The values are made up but plausible (MINI Cooper JCW at Brands Hatch GP).

    python3 make_mockups.py            # writes the PNGs next to this file
"""
import json
import math
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 128, 64
OUT = os.path.dirname(os.path.abspath(__file__))
FONTS = "/usr/share/fonts/truetype"
ON = (225, 240, 255)        # white OLED, slightly cold
OFF = (10, 12, 16)
SCALE = 5


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


SMALL = font("dejavu/DejaVuSansMono-Bold.ttf", 9)       # ~ a 6x9 bitmap font on the board
TINY = font("dejavu/DejaVuSansMono.ttf", 8)
MID = font("dejavu/DejaVuSans-Bold.ttf", 14)
BIG = font("dejavu/DejaVuSans-Bold.ttf", 30)
HUGE = font("dejavu/DejaVuSans-Bold.ttf", 52)
NARROW = font("liberation/LiberationSansNarrow-Bold.ttf", 34)
ALERT = font("liberation/LiberationSansNarrow-Bold.ttf", 31)


def screen():
    im = Image.new("1", (W, H), 0)
    d = ImageDraw.Draw(im)
    d.fontmode = "1"                                   # no anti-aliasing: pixels are on or off
    return im, d


def text(d, xy, s, f, anchor="la", fill=1):
    d.text(xy, s, font=f, fill=fill, anchor=anchor)


def bar(d, x, y, w, h, frac, segments=0):
    d.rectangle([x, y, x + w - 1, y + h - 1], outline=1)
    n = int(round((w - 4) * max(0.0, min(1.0, frac))))
    if segments:
        seg = (w - 4) / segments
        for i in range(segments):
            if (i + 1) * seg <= n + 0.5:
                d.rectangle([x + 2 + int(i * seg) + 1, y + 2, x + 2 + int((i + 1) * seg) - 1, y + h - 3], fill=1)
    elif n:
        d.rectangle([x + 2, y + 2, x + 1 + n, y + h - 3], fill=1)


# ---------- the ten screens ----------

def s01_gear_speed_rpm():
    """Gear, speed and a shift-light RPM bar."""
    im, d = screen()
    bar(d, 0, 0, 128, 9, 0.82, segments=16)            # RPM, segmented like shift lights
    text(d, (2, 12), "5", HUGE)
    text(d, (126, 13), "142", BIG, "ra")
    text(d, (126, 44), "km/h", SMALL, "ra")
    text(d, (60, 56), "6850 rpm", TINY, "la")
    return im


def s02_delta():
    """Live delta to your best lap, with a centred bar."""
    im, d = screen()
    text(d, (2, 0), "DELTA vs BEST", SMALL)
    text(d, (64, 11), "-0.27", BIG, "ma")
    d.rectangle([4, 44, 123, 52], outline=1)
    d.line([64, 41, 64, 55], fill=1)
    d.rectangle([64 - 22, 46, 63, 50], fill=1)          # left of centre = faster
    text(d, (4, 56), "1:12.48", TINY)
    text(d, (124, 56), "best 1:48.91", TINY, "ra")
    return im


def s03_lap_times():
    """The running lap time big, last and best below."""
    im, d = screen()
    text(d, (2, 0), "LAP 7/12", SMALL)
    text(d, (126, 0), "P4", SMALL, "ra")
    text(d, (64, 12), "1:12.4", BIG, "ma")
    d.line([0, 43, 127, 43], fill=1)
    text(d, (2, 45), "LAST", TINY)
    text(d, (2, 53), "1:49.31", SMALL)
    text(d, (126, 45), "BEST", TINY, "ra")
    text(d, (126, 53), "1:48.91", SMALL, "ra")
    return im


def s04_position_gaps():
    """Race position and the gaps to the cars around you."""
    im, d = screen()
    text(d, (0, 2), "P4", NARROW)
    text(d, (44, 2), "/12", MID)
    text(d, (44, 20), "lap 7/12", TINY)
    text(d, (126, 2), "AHEAD", TINY, "ra")
    text(d, (126, 12), "+1.2", MID, "ra")
    d.line([0, 40, 127, 40], fill=1)
    text(d, (2, 44), "▲ Rossi      +1.2", SMALL)
    text(d, (2, 54), "▼ Novak      -0.8", SMALL)
    return im


def s05_fuel():
    """Fuel: what is left, how many laps it lasts, what each lap uses."""
    im, d = screen()
    text(d, (2, 0), "FUEL", SMALL)
    text(d, (126, 0), "2.94 L/lap", SMALL, "ra")
    text(d, (2, 12), "18.4", BIG)
    text(d, (76, 30), "L", MID)
    text(d, (126, 12), "6.2", MID, "ra")
    text(d, (126, 28), "laps", TINY, "ra")
    bar(d, 0, 43, 128, 8, 18.4 / 40)
    text(d, (64, 53), "enough: +1.2 laps", TINY, "ma")
    return im


def s06_tyres():
    """Four tyres: temperature, and wear as a bar in each."""
    im, d = screen()
    temps = (("FL", 82, 0.91), ("FR", 88, 0.87), ("RL", 76, 0.94), ("RR", 79, 0.93))
    for i, (name, t, wear) in enumerate(temps):
        x = 4 + (i % 2) * 76
        y = 2 + (i // 2) * 32
        hot = t >= 85
        d.rounded_rectangle([x, y, x + 47, y + 27], radius=4, outline=1, fill=1 if hot else 0)
        text(d, (x + 4, y + 5), name, TINY, fill=0 if hot else 1)
        text(d, (x + 44, y + 2), "%d°" % t, MID, "ra", fill=0 if hot else 1)
        bar_y = y + 19
        d.rectangle([x + 4, bar_y, x + 43, bar_y + 4], outline=0 if hot else 1)
        d.rectangle([x + 5, bar_y + 1, x + 5 + int(37 * wear), bar_y + 3], fill=0 if hot else 1)
    text(d, (64, 12), "TYRE", TINY, "ma")
    text(d, (64, 20), "°C", TINY, "ma")
    text(d, (64, 36), "bar =", TINY, "ma")
    text(d, (64, 44), "left", TINY, "ma")
    return im


def s07_pedals_g():
    """Throttle and brake bars, and the g-force as a dot in a circle."""
    im, d = screen()
    text(d, (2, 0), "THR", TINY)
    text(d, (2, 32), "BRK", TINY)
    d.rectangle([22, 0, 75, 13], outline=1)
    d.rectangle([24, 2, 24 + int(49 * 0.12), 11], fill=1)
    d.rectangle([22, 32, 75, 45], outline=1)
    d.rectangle([24, 34, 24 + int(49 * 0.86), 43], fill=1)
    text(d, (2, 54), "ABS", TINY)
    d.rectangle([22, 54, 30, 62], fill=1)
    text(d, (34, 54), "lock FL", TINY)
    cx, cy, r = 104, 31, 22
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=1)
    d.ellipse([cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2], outline=1)
    d.line([cx - r, cy, cx + r, cy], fill=1)
    d.line([cx, cy - r, cx, cy + r], fill=1)
    gx, gy = -0.35, -1.05                               # braking into a left-hander
    px, py = cx + gx * r / 1.5, cy - gy * r / 1.5
    d.ellipse([px - 3, py - 3, px + 3, py + 3], fill=1)
    text(d, (cx, 56), "1.1 g", TINY, "ma")
    return im


def s08_essentials_grid():
    """Four corners: gear, position, lap, delta, split by lines."""
    im, d = screen()
    d.line([64, 0, 64, 63], fill=1)
    d.line([0, 32, 127, 32], fill=1)
    text(d, (32, 1), "4", BIG, "ma")
    text(d, (96, 3), "P4", MID, "ma")
    text(d, (96, 19), "of 12", TINY, "ma")
    text(d, (32, 36), "7/12", MID, "ma")
    text(d, (32, 53), "lap", TINY, "ma")
    text(d, (96, 36), "-0.27", MID, "ma")
    text(d, (96, 53), "delta", TINY, "ma")
    return im


def s09_sectors():
    """Sector times of this lap against your best, marked faster or slower."""
    im, d = screen()
    text(d, (2, 0), "SECTORS  lap 7", SMALL)
    rows = (("S1", "32.84", -0.21, True), ("S2", "41.07", +0.12, True), ("S3", "--.--", None, False))
    for i, (name, t, dlt, done) in enumerate(rows):
        y = 11 + i * 14
        text(d, (2, y), name, MID)
        text(d, (30, y), t, MID)
        if dlt is None:
            text(d, (126, y + 2), "now", SMALL, "ra")
            continue
        faster = dlt < 0
        box = [86, y, 127, y + 12]
        d.rectangle(box, fill=1 if faster else 0, outline=1)
        text(d, (106, y + 1), "%+.2f" % dlt, SMALL, "ma", fill=0 if faster else 1)
    text(d, (64, 55), "filled = faster than best", TINY, "ma")
    return im


def s10_alert():
    """A full-screen alert that takes over when it matters (here a yellow flag), inverted to stand out."""
    im, d = screen()
    d.rectangle([0, 0, 127, 63], fill=1)
    d.rectangle([3, 3, 124, 60], outline=0)
    text(d, (64, 6), "YELLOW", ALERT, "ma", fill=0)
    text(d, (64, 38), "S2 · no overtaking", SMALL, "ma", fill=0)
    text(d, (64, 49), "car stopped at Paddock", TINY, "ma", fill=0)
    return im


def track(box):
    """The recorded Brands Hatch lap scaled into box (x0, y0, x1, y1): screen points, and the scale."""
    pts = json.load(open(os.path.join(OUT, "track-brands-hatch.json")))["points"]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, y0, x1, y1 = box
    k = min((x1 - x0) / (max(xs) - min(xs)), (y1 - y0) / (max(ys) - min(ys)))
    ox = x0 + ((x1 - x0) - k * (max(xs) - min(xs))) / 2
    oy = y0 + ((y1 - y0) - k * (max(ys) - min(ys))) / 2
    return [(round(ox + (x - min(xs)) * k), round(oy + (y - min(ys)) * k)) for x, y in pts], k


def draw_track(d, scr, me, rivals=(), sf=True):
    d.line(scr + scr[:1], fill=1)
    if sf:                                             # start/finish: a short tick across the line
        (ax, ay), (bx, by) = scr[0], scr[2]
        nx, ny = -(by - ay), bx - ax
        n = math.hypot(nx, ny) or 1
        d.line([ax - 3 * nx / n, ay - 3 * ny / n, ax + 3 * nx / n, ay + 3 * ny / n], fill=1)
    for i in rivals:                                   # other cars: a 3x3 blob, thicker than the line
        x, y = scr[i % len(scr)]
        d.rectangle([x - 1, y - 1, x + 1, y + 1], fill=1)
    x, y = scr[me % len(scr)]                          # you: a ring with a dark gap, can't be missed
    d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=0, outline=1)
    d.rectangle([x - 1, y - 1, x + 1, y + 1], fill=1)


def s11_track_side():
    """Track map on the left (your car as a ring, rivals as dots), the race on the right."""
    im, d = screen()
    scr, k = track((1, 1, 62, 62))
    n = len(scr)
    draw_track(d, scr, me=int(n * 0.62), rivals=(int(n * 0.66), int(n * 0.58), int(n * 0.15), int(n * 0.9)))
    d.line([65, 0, 65, 63], fill=1)
    text(d, (127, 0), "P4", BIG, "ra")
    text(d, (70, 33), "lap 7/12", SMALL)
    text(d, (70, 43), "-0.27 s", SMALL)
    text(d, (70, 53), "+1.2 ahead", TINY)
    return im


def s12_track_full():
    """The track as big as it fits, with the lap time and position in the corners."""
    im, d = screen()
    scr, k = track((30, 0, 127, 63))
    n = len(scr)
    draw_track(d, scr, me=int(n * 0.62), rivals=(int(n * 0.66), int(n * 0.58), int(n * 0.15), int(n * 0.9)))
    text(d, (0, 0), "P4", MID)
    text(d, (0, 17), "7/12", SMALL)
    text(d, (0, 45), "1:12.4", TINY)
    text(d, (0, 54), "-0.27", SMALL)
    return im


SCREENS = [
    ("01-gear-speed-rpm", "Gear, speed, shift-light RPM bar", s01_gear_speed_rpm),
    ("02-delta", "Live delta to your best lap", s02_delta),
    ("03-lap-times", "Running lap time, last and best", s03_lap_times),
    ("04-position-gaps", "Position, gaps ahead and behind", s04_position_gaps),
    ("05-fuel", "Fuel left, laps it lasts, use per lap", s05_fuel),
    ("06-tyres", "Tyre temperatures and wear (hot = inverted)", s06_tyres),
    ("07-pedals-g", "Throttle, brake, ABS and the g-force ball", s07_pedals_g),
    ("08-essentials-grid", "Four at once: gear, position, lap, delta", s08_essentials_grid),
    ("09-sectors", "Sector times vs your best", s09_sectors),
    ("10-alert", "Full-screen alert (flags, pit window, fuel)", s10_alert),
    ("11-track-side", "Track map (you = ring, dots = rivals) + race", s11_track_side),
    ("12-track-full", "Track as big as it fits, race in the corner", s12_track_full),
]


def oled_look(im, scale=SCALE):
    """x scale, each pixel a lit square with a thin dark gap, on a black glass border."""
    pad = 6 * scale
    out = Image.new("RGB", (W * scale + 2 * pad, H * scale + 2 * pad), (0, 0, 0))
    d = ImageDraw.Draw(out)
    px = im.load()
    for y in range(H):
        for x in range(W):
            x0, y0 = pad + x * scale, pad + y * scale
            d.rectangle([x0, y0, x0 + scale - 2, y0 + scale - 2], fill=ON if px[x, y] else OFF)
    return out


def sheet(images, cols=2):
    label = ImageFont.truetype(os.path.join(FONTS, "dejavu/DejaVuSans.ttf"), 22)
    head = ImageFont.truetype(os.path.join(FONTS, "dejavu/DejaVuSans-Bold.ttf"), 22)
    tw, th = images[0][2].size
    cell_w, cell_h = tw + 40, th + 70
    rows = math.ceil(len(images) / cols)
    out = Image.new("RGB", (cols * cell_w + 40, rows * cell_h + 90), (245, 244, 240))
    d = ImageDraw.Draw(out)
    d.text((40, 28), "Desk 6DOF · 2.42″ OLED (128×64) · race-screen ideas", font=head, fill=(11, 11, 11))
    for i, (name, caption, big) in enumerate(images):
        x = 40 + (i % cols) * cell_w
        y = 80 + (i // cols) * cell_h
        out.paste(big, (x, y))
        d.text((x, y + th + 10), "%s  %s" % (name[:2], caption), font=label, fill=(60, 60, 56))
    return out


def main():
    made = []
    for name, caption, fn in SCREENS:
        im = fn()
        im.save(os.path.join(OUT, name + "-128x64.png"))           # the real thing, 1 bit
        big = oled_look(im)
        big.save(os.path.join(OUT, name + ".png"))
        made.append((name, caption, big))
    sheet(made).save(os.path.join(OUT, "all-screens.png"))
    print("wrote %d screens + all-screens.png to %s" % (len(made), OUT))


if __name__ == "__main__":
    main()
