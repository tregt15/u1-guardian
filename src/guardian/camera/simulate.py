"""A picture of what the printer camera should see right now, made from the print file (G-code) and the camera setup.

The print so far (the plate height map read from the G-code up to where the printer is) is drawn as 3 mm blocks, seen
through the camera fit from Camera setup (camera/calibration.py). The picture has the same viewpoint as the real camera,
so a person can put it next to the live picture, and the AI can be given both and asked what is different.

It is a simulation, not a photo: plain colors, no shadows, no toolheads, no lens bending (the fit ignores it, so edges can
be off by some pixels, see the fit error). UNVERIFIED: how well it lines up with the real picture on the real U1.

Pure Python (standard library only), so it also runs on a Raspberry Pi. 960 x 540 draws in well under a second.
"""

from __future__ import annotations

import math

from guardian.ai.gcode_view import hex_rgb, png
from guardian.camera.calibration import PLATE_MM, CameraSetup, orient

W, H = 960, 540
SRC_W = 1920  # the clicks in the camera setup are in full size (1920 wide) picture pixels
BACKGROUND = (28, 30, 36)
PLATE = (46, 49, 56)
EDGE = (120, 108, 70)


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
    """Fill a convex polygon (scanline). Points are (x, y) in W x H pixels."""
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
        if len(xs) < 2:
            continue
        a, b = max(0, int(math.floor(min(xs) - 0.5))), min(W - 1, int(math.ceil(max(xs) - 0.5)))
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

    def to_px(ox: float, oy: float, z: float) -> tuple[float, float] | None:
        w = p[2][0] * ox + p[2][1] * oy + p[2][2] * z + p[2][3]
        if abs(w) < 1e-9:
            return None
        return (k * (p[0][0] * ox + p[0][1] * oy + p[0][2] * z + p[0][3]) / w,
                k * (p[1][0] * ox + p[1][1] * oy + p[1][2] * z + p[1][3]) / w)

    rgb = bytearray(bytes(BACKGROUND) * (W * H))
    plate = [to_px(*orient(x, y, rot, mirror), -bed_z) for x, y in ((0, 0), (PLATE_MM, 0), (PLATE_MM, PLATE_MM), (0, PLATE_MM))]
    if all(plate):
        _fill(rgb, plate, PLATE)  # type: ignore[arg-type]
        for i in range(4):
            _line(rgb, plate[i], plate[(i + 1) % 4], EDGE)  # type: ignore[arg-type]
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
