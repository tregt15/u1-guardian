"""A picture of what the printer camera should see right now, made from the print file (G-code) and the camera setup.

The print so far (the plate height map read from the G-code up to where the printer is) is drawn as 3 mm blocks, seen
through the camera fit from Camera setup (camera/calibration.py). The picture has the same viewpoint as the real camera,
so a person can put it next to the live picture, and the AI can be given both and asked what is different.

It is a simulation, not a photo: plain colors, no shadows. It bends straight lines like the wide angle lens
does (one number, "Lens bend" on the Camera setup page). Behind the plate it draws a flat backdrop of the printer interior
(rear wall, fan, gantry rails, toolhead docks, purge bin). The backdrop was traced by eye from one real camera picture, so
it already has the lens bend in it. UNVERIFIED: how well it matches other pictures. Toolheads away from their docks and
the bin contents are not drawn.

Pure Python (standard library only), so it also runs on a Raspberry Pi. 960 x 540 draws in well under a second.
"""

from __future__ import annotations

import math

from guardian.ai.gcode_view import hex_rgb, png
from guardian.camera.calibration import PLATE_MM, CameraSetup, distort, orient

W, H = 960, 540
SRC_W = 1920  # the clicks in the camera setup are in full size (1920 wide) picture pixels
BACKGROUND = (28, 30, 36)
PLATE = (46, 49, 56)
EDGE = (120, 108, 70)
SEGMENTS = 24  # long edges are cut in pieces so they can bend with the lens


def camera_centre(p: list[list[float]]) -> tuple[float, float, float] | None:
    """Where the camera is in the plate frame: C = -M^-1 p4 (Cramer's rule on the left 3x3 part of P)."""
    m = [r[:3] for r in p]
    b = [-r[3] for r in p]

    def det(a: list[list[float]]) -> float:
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))

    d = det(m)
    if not math.isfinite(d) or abs(d) < 1e-12:
        return None
    out = []
    for c in range(3):
        mc = [[b[i] if j == c else m[i][j] for j in range(3)] for i in range(3)]
        out.append(det(mc) / d)
    return out[0], out[1], out[2]


def _fill(rgb: bytearray, pts: list[tuple[float, float]], color: tuple[int, int, int]) -> None:
    """Fill a polygon (scanline, even odd rule). Points are (x, y) in W x H pixels."""
    y0, y1 = max(0, int(math.floor(min(p[1] for p in pts)))), min(H - 1, int(math.ceil(max(p[1] for p in pts))))
    n = len(pts)
    px = bytes(color)
    for y in range(y0, y1 + 1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            if (ay <= yc < by) or (by <= yc < ay):
                xs.append(ax + (yc - ay) / (by - ay) * (bx - ax))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            a, b = max(0, int(math.floor(xs[i] - 0.5))), min(W - 1, int(math.ceil(xs[i + 1] - 0.5)))
            if b >= a:
                rgb[(y * W + a) * 3:(y * W + b + 1) * 3] = px * (b - a + 1)


def _line(rgb: bytearray, a: tuple[float, float], b: tuple[float, float], color: tuple[int, int, int]) -> None:
    steps = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
    for i in range(steps + 1):
        x, y = int(a[0] + (b[0] - a[0]) * i / steps), int(a[1] + (b[1] - a[1]) * i / steps)
        if 0 <= x < W and 0 <= y < H:
            rgb[(y * W + x) * 3:(y * W + x) * 3 + 3] = bytes(color)


def _shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v * f))) for v in c)  # type: ignore[return-value]


# Backdrop shapes in full size (1920 x 1080) camera picture pixels, traced from a real picture with the bed at the bottom.
# (color, points). Drawn first, in this order.
def _circle(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [(cx + r * math.cos(i * math.pi / 20), cy + r * math.sin(i * math.pi / 20)) for i in range(40)]


BACKDROP: list[tuple[tuple[int, int, int], list[tuple[float, float]]]] = [
    ((20, 21, 24), [(0, 0), (1920, 0), (1920, 1080), (0, 1080)]),
    ((34, 36, 41), [(230, 70), (1140, 170), (1150, 420), (200, 440)]),  # gantry plate
    ((62, 64, 70), [(240, 125), (1090, 205), (1090, 225), (240, 150)]),  # top rail
    ((120, 122, 128), [(260, 262), (1130, 318), (1130, 328), (260, 272)]),  # bright rail
    ((70, 72, 78), [(235, 345), (1120, 375), (1120, 388), (235, 360)]),  # lower rail
    ((44, 46, 52), [(0, 600), (310, 555), (1225, 525), (1245, 1080), (0, 1080)]),  # rear wall
    ((56, 58, 65), [(435, 548), (970, 545), (1000, 1080), (440, 1080)]),  # centre panel
    ((24, 25, 28), _circle(645, 890, 125)),  # fan
    ((52, 54, 60), _circle(645, 890, 80)),
    ((150, 152, 158), [(320, 470), (395, 465), (430, 1080), (335, 1080)]),  # left lead screw
    ((165, 167, 172), [(960, 462), (990, 462), (995, 1000), (965, 1000)]),  # right rod
    ((205, 205, 210), [(70, 0), (260, 40), (215, 120), (105, 210), (100, 380), (200, 450), (130, 485), (55, 470), (20, 390), (25, 150)]),
    ((16, 17, 19), [(1225, 130), (1360, 130), (1350, 470), (1225, 470)]),  # toolheads
    ((16, 17, 19), [(1385, 125), (1545, 125), (1535, 470), (1385, 470)]),
    ((16, 17, 19), [(1570, 110), (1730, 110), (1735, 475), (1570, 475)]),
    ((16, 17, 19), [(1775, 100), (1920, 100), (1920, 490), (1775, 490)]),
    ((190, 172, 50), [(1268, 340), (1300, 340), (1284, 375)]),  # warning marks
    ((190, 172, 50), [(1432, 340), (1470, 340), (1451, 380)]),
    ((190, 172, 50), [(1626, 345), (1665, 345), (1645, 388)]),
    ((190, 172, 50), [(1848, 350), (1888, 350), (1868, 392)]),
    ((172, 175, 180), [(1145, 560), (1920, 640), (1920, 890), (1670, 880), (1225, 740), (1160, 600)]),  # purge bin
    ((24, 24, 27), [(1230, 880), (1850, 930), (1700, 1080), (1190, 1080)]),  # floor under the bin
]


def _backdrop(rgb: bytearray) -> None:
    k = W / SRC_W
    for color, pts in BACKDROP:
        _fill(rgb, [(x * k, y * k) for x, y in pts], color)


def render(setup: CameraSetup, blocks: list[tuple[int, int, float, int]], bed_z: float, colors: list[str],
           cell_mm: int = 3, max_z: float = 0.0) -> bytes | None:
    """PNG of the print as the camera should see it. blocks: (cx, cy, height_mm, tool) for each cell_mm block.
    bed_z is where the bed is now (the camera is fixed, the bed moves). None if the camera is not set up."""
    if not setup.ready:
        return None
    p = setup.data["P"]
    rot, mirror = setup.data.get("rotation", 0), setup.data.get("mirror", False)
    cam = camera_centre(p)
    if cam is None:
        return None
    k = W / SRC_W
    k1 = setup.lens

    def to_px(ox: float, oy: float, z: float) -> tuple[float, float] | None:
        w = p[2][0] * ox + p[2][1] * oy + p[2][2] * z + p[2][3]
        if w <= 1e-9:  # at or behind the camera
            return None
        u, v = distort((p[0][0] * ox + p[0][1] * oy + p[0][2] * z + p[0][3]) / w,
                       (p[1][0] * ox + p[1][1] * oy + p[1][2] * z + p[1][3]) / w, k1)
        return k * u, k * v

    def ring(corners: list[tuple[float, float, float]]) -> list[tuple[float, float]] | None:
        """A closed outline with every edge cut in pieces, so the lens bends it. Points are plate frame (x, y, z)."""
        out = []
        for i, a in enumerate(corners):
            b = corners[(i + 1) % len(corners)]
            for j in range(SEGMENTS):
                f = j / SEGMENTS
                q = to_px(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
                if q is None:
                    return None
                out.append(q)
        return out

    rgb = bytearray(bytes(BACKGROUND) * (W * H))
    _backdrop(rgb)
    plate = ring([(*orient(x, y, rot, mirror), -bed_z) for x, y in ((0, 0), (PLATE_MM, 0), (PLATE_MM, PLATE_MM), (0, PLATE_MM))])
    if plate:
        _fill(rgb, plate, PLATE)
        for i in range(len(plate)):
            _line(rgb, plate[i], plate[(i + 1) % len(plate)], EDGE)
    tools = [hex_rgb(c) for c in colors] or [(230, 230, 230)]
    grid = {(cx, cy): h for cx, cy, h, _ in blocks}
    top = max(1.0, max_z or max((b[2] for b in blocks), default=1.0))
    order = []
    for cx, cy, h, t in blocks:
        o = orient((cx + 0.5) * cell_mm, (cy + 0.5) * cell_mm, rot, mirror)
        order.append((-math.hypot(o[0] - cam[0], o[1] - cam[1]), cx, cy, h, t, o))
    order.sort()  # far blocks first, so near ones cover them
    neighbours = ((0, -1, 0, 0, 1, 0), (1, 0, 1, 0, 1, 1), (0, 1, 0, 1, 1, 1), (-1, 0, 0, 0, 0, 1))  # dx, dy, edge corners
    for _, cx, cy, h, t, o in order:
        base = tools[t % len(tools)]
        x0, y0 = cx * cell_mm, cy * cell_mm
        for i, (dx, dy, ax, ay, bx, by) in enumerate(neighbours):
            lower = grid.get((cx + dx, cy + dy), 0.0)
            if lower >= h:
                continue
            a = orient(x0 + ax * cell_mm, y0 + ay * cell_mm, rot, mirror)
            b = orient(x0 + bx * cell_mm, y0 + by * cell_mm, rot, mirror)
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if (mx - o[0]) * (cam[0] - mx) + (my - o[1]) * (cam[1] - my) <= 0:
                continue  # this side faces away from the camera
            q = [to_px(a[0], a[1], lower - bed_z), to_px(b[0], b[1], lower - bed_z),
                 to_px(b[0], b[1], h - bed_z), to_px(a[0], a[1], h - bed_z)]
            if all(q):
                _fill(rgb, q, _shade(base, 0.5 + 0.1 * i))  # type: ignore[arg-type]
        q = [to_px(*orient(x0 + ex * cell_mm, y0 + ey * cell_mm, rot, mirror), h - bed_z) for ex, ey in ((0, 0), (1, 0), (1, 1), (0, 1))]
        if all(q):
            _fill(rgb, q, _shade(base, 0.8 + 0.2 * (h / top)))  # type: ignore[arg-type]
    return png(W, H, rgb)
