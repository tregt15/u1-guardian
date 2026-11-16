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
from guardian.camera.calibration import PLATE_MM, CameraSetup, distort, orient, undistort

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


def _spans(pts: list[tuple[float, float]]):
    """The rows of a polygon (scanline, even odd rule) as (y, first x, last x). Points are (x, y) in W x H pixels."""
    y0, y1 = max(0, int(math.floor(min(p[1] for p in pts)))), min(H - 1, int(math.ceil(max(p[1] for p in pts))))
    n = len(pts)
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
                yield y, a, b


def _fill(rgb: bytearray, pts: list[tuple[float, float]], color: tuple[int, int, int]) -> None:
    px = bytes(color)
    for y, a, b in _spans(pts):
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


RIM_MM = 22.0  # the bed's white side edge, seen below the plate top. Moves with the bed.
DARK = (14, 15, 18)
NODE = 8  # the picture warp is worked out every 8 pixels and filled in between
_warp_cache: dict = {}


def _hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(seq):
        h: list[tuple[float, float]] = []
        for q in seq:
            while len(h) >= 2 and (h[-1][0] - h[-2][0]) * (q[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (q[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(q)
        return h

    lo, up = half(pts), half(reversed(pts))
    return lo[:-1] + up[:-1]


def decode(ffmpeg: str, jpeg: bytes) -> bytes | None:
    """A JPEG as W x H RGB bytes (FFmpeg), or None."""
    import subprocess
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={W}:{H}",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], input=jpeg, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and len(r.stdout) == W * H * 3 else None


def _plane(p: list[list[float]], z0: float) -> list[list[float]]:
    return [[p[i][0], p[i][1], p[i][2] * z0 + p[i][3]] for i in range(3)]


def _inv3(m: list[list[float]]) -> list[list[float]] | None:
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def _bed_background(setup: CameraSetup, reference: tuple[bytes, float], bed_z: float,
                    bed_region) -> bytearray:
    """The reference photo (the empty plate, bed at ref_z) with the bed slid to where it is now (bed_z).
    The photo is worked as a flat plate: each new pixel on the plate is looked up in the photo through the camera fit.
    The old bed area is painted dark first (what is behind the bed is not known)."""
    ref, ref_z = reference
    if len(ref) != W * H * 3:
        return bytearray(ref)
    p = setup.data["P"]
    k1, k = setup.lens, W / SRC_W
    key = (id(ref), round(bed_z * 5), repr(p), k1)
    hit = _warp_cache.get(key)
    if hit is not None:
        return bytearray(hit)
    out = bytearray(ref)
    if abs(bed_z - ref_z) >= 0.2:
        old, new = bed_region(ref_z), bed_region(bed_z)
        inv = _inv3(_plane(p, -bed_z))
        a_ref = _plane(p, -ref_z)
        if old:
            _fill(out, old, DARK)
        if new and inv:
            def src(gx: float, gy: float) -> tuple[float, float] | None:
                uu, vv = undistort(gx / k, gy / k, k1)
                x, y, c = (inv[0][0] * uu + inv[0][1] * vv + inv[0][2], inv[1][0] * uu + inv[1][1] * vv + inv[1][2],
                           inv[2][0] * uu + inv[2][1] * vv + inv[2][2])
                if abs(c) < 1e-12:
                    return None
                x, y = x / c, y / c
                a, b, w = (a_ref[0][0] * x + a_ref[0][1] * y + a_ref[0][2], a_ref[1][0] * x + a_ref[1][1] * y + a_ref[1][2],
                           a_ref[2][0] * x + a_ref[2][1] * y + a_ref[2][2])
                if w <= 1e-9:
                    return None
                u2, v2 = distort(a / w, b / w, k1)
                return u2 * k, v2 * k

            cols = W // NODE + 2
            nodes = [[src(i * NODE, j * NODE) for i in range(cols)] for j in range(H // NODE + 2)]
            for y, a, b in _spans(new):
                j, t = divmod(y, NODE)
                t /= NODE
                row0, row1 = nodes[j], nodes[j + 1]
                for x in range(a, b + 1):
                    i, s = divmod(x, NODE)
                    s /= NODE
                    q = (row0[i], row0[i + 1], row1[i], row1[i + 1])
                    if None in q:
                        continue
                    sx = (q[0][0] * (1 - s) + q[1][0] * s) * (1 - t) + (q[2][0] * (1 - s) + q[3][0] * s) * t
                    sy = (q[0][1] * (1 - s) + q[1][1] * s) * (1 - t) + (q[2][1] * (1 - s) + q[3][1] * s) * t
                    ix, iy = min(W - 1, max(0, int(sx + 0.5))), min(H - 1, max(0, int(sy + 0.5)))  # past the edge: the edge pixel
                    out[(y * W + x) * 3:(y * W + x) * 3 + 3] = ref[(iy * W + ix) * 3:(iy * W + ix) * 3 + 3]
    if len(_warp_cache) > 6:
        _warp_cache.clear()
    _warp_cache[key] = bytes(out)
    return out


def render(setup: CameraSetup, blocks: list[tuple[int, int, float, int]], bed_z: float, colors: list[str],
           cell_mm: int = 3, max_z: float = 0.0, reference: tuple[bytes, float] | None = None) -> bytes | None:
    """PNG of the print as the camera should see it. reference: an earlier photo of the empty plate (W x H RGB bytes) and
    the bed height it was taken at. With it the picture looks like the real printer. Without it a plain drawing is used.
    blocks: (cx, cy, height_mm, tool) for each cell_mm block.
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

    def bed_region(z: float) -> list[tuple[float, float]] | None:
        """The bed as seen: the plate top and its white side edge (RIM_MM), as one outline."""
        pts: list[tuple[float, float]] = []
        for zz in (-z, -z - RIM_MM):
            r = ring([(*orient(x, y, rot, mirror), zz) for x, y in ((0, 0), (PLATE_MM, 0), (PLATE_MM, PLATE_MM), (0, PLATE_MM))])
            if r is None:
                return None
            pts += r
        return _hull(pts)

    if reference is not None and len(reference[0]) == W * H * 3:
        rgb = _bed_background(setup, reference, bed_z, bed_region)
    else:
        rgb = bytearray(bytes(BACKGROUND) * (W * H))
        _backdrop(rgb)
    plate = None if reference is not None else ring([(*orient(x, y, rot, mirror), -bed_z) for x, y in ((0, 0), (PLATE_MM, 0), (PLATE_MM, PLATE_MM), (0, PLATE_MM))])
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
            grain = 0.95 + 0.05 * ((cx * 7 + cy * 13) % 5) / 4  # a little variation, so a flat top does not look like paint
            _fill(rgb, q, _shade(base, (0.8 + 0.2 * (h / top)) * grain))  # type: ignore[arg-type]
    return png(W, H, rgb)
