"""Build the 3D model of the printer interior (src/guardian/camera/interior_model.py).

The shapes below were traced by eye on ONE real camera picture (1920 x 1080, bed lowered so the plate is out of view, all
toolheads in their docks). Each outline is put on a plane in the printer (a wall, the dock wall, the floor) by sending a
ray through the camera fit (default_calibration.json) and finding where it meets that plane. The result is a model in
millimetres, in the same plate corner frame the camera fit uses. Run this only when you want to redo the model:

    python tools/build_interior.py path/to/photo.jpg

Needs Pillow and numpy (this tool only, not Guardian). Colors for the big planes are sampled from the photo (median of
each cell), the small parts have fixed colors. UNVERIFIED: the plane depths (walls 12 mm behind the plate edge, dock wall
12 mm, floor 330 mm below the nozzle plane, room wall 400 mm behind). They only change the size of things in mm, not how
they look from the camera.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from guardian.camera.calibration import undistort  # noqa: E402
from guardian.camera.simulate import camera_centre  # noqa: E402

CAL = json.loads((ROOT / "src/guardian/camera/default_calibration.json").read_text(encoding="utf-8"))
P, K1 = CAL["P"], CAL["k1"]
C = camera_centre(P)


def _inv3(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


MI = _inv3([r[:3] for r in P])
AXIS = {"x": 0, "y": 1, "z": 2}


def hit(u, v, plane):
    """Picture pixel (1920 x 1080) to the point on the plane ('x', -12) where its ray lands."""
    uu, vv = undistort(u, v, K1)
    d = [sum(MI[i][j] * q for j, q in enumerate((uu, vv, 1.0))) for i in range(3)]
    ax = AXIS[plane[0]]
    t = (plane[1] - C[ax]) / d[ax]
    if t <= 0:
        raise ValueError(f"pixel {u},{v} does not meet plane {plane}")
    return tuple(round(C[i] + t * d[i], 1) for i in range(3))


WALL_A = ("x", -12.0)     # back left wall (rails, fan, lead screws)
DOCK = ("y", -12.0)       # dock wall: toolhead fronts
BIN = ("y", 10.0)         # front of the purge bin
ROOM = ("y", -400.0)      # the room beyond the printer (right half of the picture)
ROOM_L = ("x", -400.0)    # and beyond the left wall (left half of the picture)
FLOOR = ("z", -330.0)

PHOTO = None
PHOTO2 = None   # a picture with the carriage away from the left end (setup_05), used for the left end of the rails
STATIC: list = []        # (rgb, [(x, y, z)]) in drawing order
HEADS: list[list] = [[], [], [], []]
FOREGROUND: list = []    # (rgb, [(u, v)]) screen space, 1920 x 1080
PARKED: list = []        # the carriage seen parked at X 5, Y 5 (screen space)
HEAD_QUADS: list = []


def median_color(poly):
    m = Image.new("L", PHOTO.size, 0)
    ImageDraw.Draw(m).polygon([tuple(p) for p in poly], fill=255)
    a = np.asarray(PHOTO)[np.asarray(m) > 0]
    if len(a) == 0:
        return (40, 40, 44)
    return tuple(int(x) for x in np.median(a, axis=0))


def clip_rect(poly, x0, y0, x1, y1):
    """Sutherland-Hodgman: the part of a polygon inside a rectangle."""
    def clip(pts, inside, cut):
        out = []
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            ia, ib = inside(a), inside(b)
            if ia:
                out.append(a)
            if ia != ib:
                out.append(cut(a, b))
        return out
    pts = list(poly)
    for inside, cut in (
        (lambda p: p[0] >= x0, lambda a, b: (x0, a[1] + (b[1] - a[1]) * (x0 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[0] <= x1, lambda a, b: (x1, a[1] + (b[1] - a[1]) * (x1 - a[0]) / (b[0] - a[0]))),
        (lambda p: p[1] >= y0, lambda a, b: (a[0] + (b[0] - a[0]) * (y0 - a[1]) / (b[1] - a[1]), y0)),
        (lambda p: p[1] <= y1, lambda a, b: (a[0] + (b[0] - a[0]) * (y1 - a[1]) / (b[1] - a[1]), y1)),
    ):
        if not pts:
            return []
        pts = clip(pts, inside, cut)
    return pts


def add(plane, poly, rgb=None, into=None):
    """A flat shape. rgb None: color sampled from the photo. A plane can be a list: the first one every corner meets."""
    pts = None
    for pl in (plane if isinstance(plane, list) else [plane]):
        try:
            pts = [hit(u, v, pl) for u, v in poly]
            break
        except ValueError:
            continue
    if pts is None:
        return
    (STATIC if into is None else into).append((rgb or median_color(poly), pts))


def window_median(cx, cy, half):
    a = np.asarray(PHOTO2 if (PHOTO2 is not None and cx < 300 and cy < 430) else PHOTO)
    cx, cy = min(max(cx, 0), a.shape[1] - 1), min(max(cy, 0), a.shape[0] - 1)  # grid points off the picture use the edge
    x0, x1 = max(0, int(cx - half)), min(a.shape[1], int(cx + half) + 1)
    y0, y1 = max(0, int(cy - half)), min(a.shape[0], int(cy + half) + 1)
    return np.median(a[y0:y1, x0:x1].reshape(-1, 3), axis=0)


def mosaic(plane, poly, cell=40, ctrl=200):
    """A big plane as many small cells. The color of each is smooth: it is blended between the median colors of the photo
    on a coarse grid (every `ctrl` pixels), so light and shade show but no photo detail is copied."""
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    gx0, gy0 = int(min(xs)) // ctrl * ctrl, int(min(ys)) // ctrl * ctrl
    nx, ny = int(max(xs) - gx0) // ctrl + 2, int(max(ys) - gy0) // ctrl + 2
    grid = {(i, j): window_median(gx0 + i * ctrl, gy0 + j * ctrl, ctrl * 0.6) for i in range(nx + 1) for j in range(ny + 1)}

    def color_at(x, y):
        fx, fy = (x - gx0) / ctrl, (y - gy0) / ctrl
        i, j = int(fx), int(fy)
        tx, ty = fx - i, fy - j
        c = (grid[(i, j)] * (1 - tx) * (1 - ty) + grid[(i + 1, j)] * tx * (1 - ty)
             + grid[(i, j + 1)] * (1 - tx) * ty + grid[(i + 1, j + 1)] * tx * ty)
        return tuple(int(v) for v in c)

    for gy in range(int(min(ys)) // cell * cell, int(max(ys)) + cell, cell):
        for gx in range(int(min(xs)) // cell * cell, int(max(xs)) + cell, cell):
            part = clip_rect(poly, gx, gy, gx + cell, gy + cell)
            if len(part) >= 3:
                add(plane, part, color_at(gx + cell / 2, gy + cell / 2))


def ellipse(cx, cy, rx, ry, n=28):
    return [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]


def stroke(p0, p1, w):
    """A thick line as a quad (w in pixels)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    n = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / n * w / 2, dx / n * w / 2
    return [(p0[0] + nx, p0[1] + ny), (p1[0] + nx, p1[1] + ny), (p1[0] - nx, p1[1] - ny), (p0[0] - nx, p0[1] - ny)]


def build():
    # ---- the room beyond and the floor ----
    add([ROOM, ROOM_L], [(0, 0), (1920, 0), (1920, 1080), (0, 1080)], (236, 236, 232))
    mosaic(FLOOR, [(1150, 760), (1920, 800), (1920, 1080), (1150, 1080)], 40, 520)
    # ---- back left wall: rail housing and the panel below it ----
    mosaic(WALL_A, [(30, 80), (250, 66), (700, 88), (1000, 132), (1150, 172), (1160, 440), (1000, 455), (600, 462),
                    (300, 466), (190, 440), (30, 440)], 24, 200)
    mosaic(WALL_A, [(0, 440), (190, 440), (300, 466), (1010, 455), (1215, 425), (1210, 1080), (0, 1080)], 24, 200)
    # fan panel (a raised plate below the lead screws)
    add(WALL_A, [(432, 560), (975, 540), (990, 1080), (452, 1080)], (30, 31, 35))
    add(WALL_A, [(432, 548), (975, 528), (975, 542), (432, 562)], (52, 53, 58))  # its top lip
    # slot
    add(WALL_A, [(472, 462), (480, 445), (840, 446), (850, 462), (840, 478), (480, 479)], (8, 8, 10))
    add(WALL_A, [(480, 452), (836, 453), (840, 462), (836, 472), (482, 473), (478, 462)], (18, 18, 20))
    # rails: black channels, silver rod, belts, white heads of screws, a QR sticker
    for a, b, w, col in (((60, 142), (1150, 215), 18, (10, 10, 12)), ((60, 198), (1150, 262), 22, (14, 14, 16)),
                         ((60, 256), (1145, 316), 6, (205, 206, 210)), ((60, 305), (1145, 340), 16, (30, 30, 33)),
                         ((60, 345), (1100, 388), 22, (12, 12, 14)), ((60, 380), (1100, 408), 8, (60, 60, 64)),
                         ((60, 124), (1150, 198), 4, (150, 150, 155))):
        add(WALL_A, stroke(a, b, w), col)
    for cx, cy, r in ((487, 195, 14), (578, 201, 14), (300, 150, 9), (388, 152, 9), (913, 203, 8), (975, 212, 7),
                      (480, 350, 14), (570, 350, 14), (1120, 238, 7), (895, 390, 5), (965, 397, 5)):
        add(WALL_A, ellipse(cx, cy, r, r * 0.85, 12), (225, 226, 222))
    add(WALL_A, [(304, 222), (368, 226), (368, 245), (304, 242)], (222, 222, 220))
    # lead screw (left) with thread, smooth rod (right)
    add(WALL_A, [(305, 462), (425, 458), (432, 520), (305, 525)], (12, 12, 14))
    add(WALL_A, [(345, 470), (385, 468), (430, 1080), (392, 1080)], (118, 118, 122))
    for k in range(0, 56):
        t0, t1 = k / 56.0, (k + 0.5) / 56.0
        def at(t, side):
            v = 468 + t * 612
            return (365 + t * 45 + side * (20 - 2 * t), v)
        add(WALL_A, [at(t0, -1), at(t1, -1), at(t1, 1), at(t0, 1)], (60, 60, 64))
    add(WALL_A, [(945, 462), (985, 460), (990, 1040), (962, 1040)], (88, 88, 92))
    add(WALL_A, [(966, 470), (976, 470), (980, 1030), (970, 1030)], (210, 210, 214))
    # fan
    add(WALL_A, ellipse(646, 897, 126, 132, 40), (12, 12, 14))
    add(WALL_A, ellipse(646, 897, 112, 118, 40), (46, 47, 52))
    for k in range(18):  # blades
        a0 = 2 * math.pi * k / 18
        r0, r1 = 62, 112
        pts = [(646 + r0 * math.cos(a0), 897 + r0 * 1.04 * math.sin(a0)), (646 + r1 * math.cos(a0 + 0.28), 897 + r1 * 1.05 * math.sin(a0 + 0.28)),
               (646 + r1 * math.cos(a0 + 0.40), 897 + r1 * 1.05 * math.sin(a0 + 0.40)), (646 + r0 * math.cos(a0 + 0.2), 897 + r0 * 1.04 * math.sin(a0 + 0.2))]
        add(WALL_A, pts, (26, 27, 30))
    add(WALL_A, ellipse(640, 878, 82, 84, 36), (20, 20, 23))
    add(WALL_A, ellipse(636, 872, 70, 72, 36), (30, 30, 34))
    add(WALL_A, ellipse(528, 640, 18, 19, 14), (4, 4, 5))
    # ---- right: post, dock plate, bin, heads ----
    add(DOCK, [(1010, 456), (1180, 440), (1222, 520), (1215, 1080), (1010, 1080)], (16, 16, 18))
    add(DOCK, [(1150, 175), (1235, 172), (1230, 420), (1150, 412)], (14, 14, 16))
    # purge bin: back wall, floor of it, then the see through front
    add(BIN, [(1245, 528), (1920, 590), (1920, 700), (1250, 640)], (205, 208, 212))
    add(BIN, [(1148, 560), (1240, 552), (1920, 640), (1920, 880), (1262, 762), (1205, 742), (1152, 600)], (196, 200, 204))
    add(BIN, [(1148, 558), (1240, 550), (1920, 636), (1920, 650), (1240, 566), (1150, 574)], (232, 234, 236))  # rim
    add(BIN, [(1262, 762), (1920, 880), (1920, 892), (1262, 772)], (225, 228, 230))
    # (the bin is drawn empty: the plastic bits in it in the photo are not part of the model)
    # cables above the heads
    for a, b, w, col in (((1305, 0), (1330, 135), 12, (10, 10, 12)), ((1420, 0), (1425, 130), 10, (20, 20, 22)),
                         ((1470, 0), (1485, 120), 10, (150, 152, 150)), ((1560, 0), (1565, 118), 10, (14, 14, 16)),
                         ((1590, 0), (1600, 110), 10, (18, 18, 20)), ((1650, 0), (1655, 112), 12, (150, 152, 150)),
                         ((1700, 0), (1715, 110), 12, (10, 10, 12)), ((1760, 0), (1775, 105), 10, (12, 12, 14)),
                         ((1800, 0), (1790, 100), 10, (14, 14, 16)), ((1860, 0), (1860, 105), 10, (12, 12, 12))):
        add(DOCK, stroke(a, b, w), col)
    # chair and clutter above the printer (far away)
    add([ROOM, ROOM_L], [(1020, 150), (1090, 120), (1180, 95), (1260, 58), (1272, 100), (1230, 150)], (156, 104, 70))
    add([ROOM, ROOM_L], [(870, 120), (950, 100), (1010, 110), (1020, 150), (900, 140)], (24, 24, 26))
    # ---- toolheads: box from the traced front face, then decals on the face ----
    faces = [((1226, 152), (1347, 140), (1350, 438), (1226, 441)),
             ((1386, 138), (1565, 126), (1566, 440), (1386, 442)),
             ((1570, 112), (1768, 118), (1768, 452), (1570, 447)),
             ((1775, 108), (1990, 100), (1990, 468), (1775, 462))]
    for f in faces:  # the traced corners (top left, top right, bottom right, bottom left) on the dock plane, as they are
        HEAD_QUADS.append([hit(u, v, DOCK) for u, v in f])

    def face_pt(i, fx, fy):  # fractions of the front face (0..1) to a 3D point (bilinear between the 4 corners)
        tl, tr, br, bl = HEAD_QUADS[i]
        top = [tl[k] + (tr[k] - tl[k]) * fx for k in range(3)]
        bot = [bl[k] + (br[k] - bl[k]) * fx for k in range(3)]
        return tuple(round(top[k] + (bot[k] - top[k]) * fy, 1) for k in range(3))

    # The toolhead, measured on a zoomed picture. Positions are fractions of the front face: fx 0 to 1 left to right,
    # fy 0 to 1 top to bottom. Below fy 1 is the nozzle block and the dock cup.
    def box(i, x0, y0, x1, y1, rgb, into=None):
        (HEADS[i] if into is None else into).append((rgb, [face_pt(i, x0, y0), face_pt(i, x1, y0), face_pt(i, x1, y1), face_pt(i, x0, y1)]))

    def blob(i, cx, cy, rx, ry, rgb, into=None, n=14):
        pts = [face_pt(i, cx + rx * math.cos(2 * math.pi * k / n), cy + ry * math.sin(2 * math.pi * k / n)) for k in range(n)]
        (HEADS[i] if into is None else into).append((rgb, pts))

    def shape(i, pts, rgb, into=None):
        (HEADS[i] if into is None else into).append((rgb, [face_pt(i, x, y) for x, y in pts]))

    for i in range(4):
        shape(i, [(0, 0.05), (0.05, 0), (0.95, 0), (1, 0.05), (1, 1), (0, 1)], (21, 22, 24))                # body, rounded top corners
        shape(i, [(0, 0.05), (0.05, 0), (0.95, 0), (1, 0.05), (1, 0.10), (0, 0.10)], (31, 32, 35))          # lighter top
        box(i, 0, 0.90, 1, 1, (14, 15, 16))                                                                 # darker bottom
        box(i, 0.0, 0.10, 0.02, 0.97, (112, 124, 50))                                                       # yellow edge lines
        box(i, 0.96, 0.10, 0.98, 0.97, (112, 124, 50))
        box(i, 0.26, 0.05, 0.48, 0.15, (74, 74, 80))                                                        # top plug
        box(i, 0.29, 0.07, 0.45, 0.13, (20, 20, 22))
        box(i, 0.27, 0.17, 0.45, 0.27, (198, 200, 204))                                                     # top silver screw plate
        blob(i, 0.36, 0.22, 0.04, 0.03, (120, 120, 126))
        blob(i, 0.36, 0.22, 0.02, 0.015, (60, 60, 64))
        for fy in (0.41, 0.59, 0.84):                                                                       # round holes
            blob(i, 0.07, fy, 0.025, 0.016, (6, 6, 7), n=10)
        box(i, 0.03, 0.485, 0.22, 0.545, (10, 10, 11))                                                      # latch slot with pin
        blob(i, 0.11, 0.515, 0.035, 0.025, (196, 196, 200))
        box(i, 0.285, 0.47, 0.48, 0.63, (66, 66, 72))                                                       # latch socket
        box(i, 0.305, 0.49, 0.46, 0.61, (5, 5, 6))
        box(i, 0.345, 0.55, 0.42, 0.60, (190, 118, 40))
        for fx0 in (0.01, 0.63):                                                                            # lower silver screw plates
            box(i, fx0, 0.67, fx0 + 0.14, 0.79, (186, 188, 193))
            blob(i, fx0 + 0.07, 0.73, 0.035, 0.025, (70, 70, 76))
        shape(i, [(0.25, 0.79), (0.385, 0.64), (0.52, 0.79)], (214, 198, 108))                              # hazard sign
        shape(i, [(0.285, 0.775), (0.385, 0.665), (0.485, 0.775)], (226, 212, 124))
        blob(i, 0.385, 0.735, 0.035, 0.024, (62, 56, 30))
        box(i, 0.30, 1.0, 0.68, 1.10, (88, 89, 94))                                                         # nozzle block with fins
        for fy in (1.025, 1.055, 1.085):
            box(i, 0.30, fy, 0.68, fy + 0.012, (52, 52, 57))
        shape(i, [(0.40, 1.10), (0.60, 1.10), (0.55, 1.18), (0.45, 1.18)], (150, 150, 156))                 # nozzle tip
        # the dock cup stays when the head is away (so it goes in the static shapes)
        box(i, -0.06, 1.14, 0.60, 1.42, (13, 13, 14), STATIC)
        box(i, -0.06, 1.08, 0.22, 1.14, (22, 22, 24), STATIC)
        box(i, -0.06, 1.14, 0.60, 1.165, (52, 52, 57), STATIC)
        box(i, 0.22, 1.21, 0.50, 1.35, (4, 4, 5), STATIC)
        blob(i, 0.08, 1.23, 0.035, 0.03, (172, 172, 178), STATIC)
    # ---- near the camera, fixed to the picture (no 3D): the dark curve at the bottom right ----
    PARKED.append(((20, 20, 22), [(190, 160), (250, 155), (250, 405), (195, 408)]))  # the beam's slider
    PARKED.append(((205, 205, 206), [(28, 40), (100, 60), (100, 400), (190, 400), (190, 438), (322, 440), (322, 487), (100, 487), (40, 470), (25, 400)]))
    PARKED.append(((120, 120, 124), [(55, 0), (250, 0), (238, 140), (172, 300), (158, 365), (108, 372), (98, 330), (72, 150)]))
    PARKED.append(((165, 165, 168), [(55, 0), (110, 0), (100, 150), (108, 330), (98, 330), (72, 150)]))
    PARKED.append(((196, 196, 200), ellipse(130, 362, 30, 14, 16)))
    PARKED.append(((40, 40, 44), ellipse(130, 362, 14, 7, 14)))
    FOREGROUND.append(((26, 26, 28), [(1738, 1080), (1795, 990), (1860, 925), (1920, 868), (1920, 1080)]))
    FOREGROUND.append(((52, 50, 52), [(1700, 1080), (1760, 1000), (1850, 915), (1920, 845), (1920, 868), (1860, 925), (1795, 990), (1738, 1080)]))


def fmt(v):
    return "(" + ", ".join(f"{x:g}" for x in v) + ")"


def main(argv):
    global PHOTO
    path = Path(argv[1]) if len(argv) > 1 else ROOT / "tools" / "interior_photo.jpg"
    PHOTO = Image.open(path).convert("RGB")
    global PHOTO2
    if len(argv) > 2:
        PHOTO2 = Image.open(argv[2]).convert("RGB")
    if PHOTO.size != (1920, 1080):
        raise SystemExit("the photo must be 1920 x 1080")
    build()
    lines = ['"""The 3D model of the printer interior, in mm, in the plate corner frame of the camera fit.',
             "",
             "GENERATED by tools/build_interior.py from a real camera picture. Do not edit by hand, run the tool instead.",
             "Coordinates are (x, y, z): x and y on the plate corner frame, z the height above the nozzle plane.",
             "STATIC: (color, points) drawn first to last. HEADS: one list per toolhead dock, drawn on top of STATIC.",
             "PARKED: the carriage when it is parked at X 5, Y 5, in picture pixels, drawn instead of the 3D carriage.",
             "HEAD_QUADS: the 4 corners (top left, top right, bottom right, bottom left) of each toolhead's front face, on y = -12.",
             "FOREGROUND: shapes in picture pixels (1920 x 1080), parts close to the camera that do not move.",
             'UNVERIFIED: how well it matches other pictures and other U1s."""', "",
             "STATIC = ["]
    for rgb, pts in STATIC:
        lines.append(f"    ({fmt(rgb)}, ({', '.join(fmt(p) for p in pts)},)),")
    lines += ["]", "", "HEADS = ["]
    for h in HEADS:
        lines.append("    [")
        for rgb, pts in h:
            lines.append(f"        ({fmt(rgb)}, ({', '.join(fmt(p) for p in pts)},)),")
        lines.append("    ],")
    lines += ["]", "", f"HEAD_QUADS = {HEAD_QUADS!r}", "", "FOREGROUND = ["]
    for rgb, pts in FOREGROUND:
        lines.append(f"    ({fmt(rgb)}, ({', '.join(fmt(p) for p in pts)},)),")
    lines += ["]", "", "PARKED = ["]
    for rgb, pts in PARKED:
        lines.append(f"    ({fmt(rgb)}, ({', '.join(fmt(p) for p in pts)},)),")
    lines.append("]")
    out = ROOT / "src" / "guardian" / "camera" / "interior_model.py"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(STATIC)} static shapes, {sum(len(h) for h in HEADS)} toolhead shapes, head quads {len(HEAD_QUADS)}")


if __name__ == "__main__":
    main(sys.argv)
