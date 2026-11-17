"""A picture of what the printer camera should see right now, made from the print file (G-code) and the camera setup.

It is a 3D space (see scene3d.py): a model of the printer interior, the bed at the bed height the printer reports, the carriage at
the printer's X and Y, and the print so far (the plate height map read from the G-code) as 1 to 3 mm blocks, all drawn through the
camera fit from Camera setup (camera/calibration.py), so it has the same viewpoint and the same lens bend as the real camera.
A person can put it next to the live picture, and the AI can be given both and asked what is different.

It is a simulation, not a photo: plain colors and soft shading. UNVERIFIED: how well the interior model matches other pictures
and other printers. The bin contents and the cables are not live.

Pure Python (standard library only), so it also runs on a Raspberry Pi.
"""

from __future__ import annotations

import math

from guardian.ai.gcode_view import hex_rgb, png
from guardian.camera.calibration import PLATE_MM, CameraSetup  # noqa: F401

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



def render(setup: CameraSetup, blocks: list[tuple[int, int, float, int]], bed_z: float, colors: list[str],
           cell_mm: int = 3, max_z: float = 0.0, carriage_xy: tuple[float, float] | None = None,
           away: int | None = None) -> bytes | None:
    """PNG of the printer as the camera should see it. blocks: (cx, cy, height_mm, tool) for each cell_mm block.
    bed_z is where the bed is now (the camera is fixed, the bed moves). carriage_xy: printer X, Y of the carriage, or None.
    away: the toolhead (0 to 3) that is on the carriage, so its dock is drawn empty. None if the camera is not set up."""
    from guardian.camera import scene3d as S  # here, because scene3d uses the drawing helpers above

    if not setup.ready:
        return None
    view = S.View(setup)
    if view.cam is None:
        return None
    cam = view.cam
    bed_z = bed_z * S.BED_SCALE  # see scene3d.BED_SCALE
    rgb = S.background(view)
    S.draw_heads(rgb, view, away)
    S.draw_bed(rgb, view, bed_z)
    rgb = bytearray(bytes(rgb).translate(S.DARKEN))  # the printer and the room a little darker. The print itself is not.
    tools = [hex_rgb(c) for c in colors] or [(230, 230, 230)]
    # A black filament would vanish on the dark plate, so very dark colors are lifted a little. The legend shows the real colors.
    tools = [c if max(c) >= 45 else (45, 45, 48) for c in tools]
    grid = {(b[0], b[1]): b[2] for b in blocks}
    top = max(1.0, max_z or max((b[2] for b in blocks), default=1.0))
    order = []
    for b in blocks:  # (cx, cy, height, tool) or with a fifth number: where the plastic starts (above 0 is an overhang)
        cx, cy, h, t = b[:4]
        base = float(b[4]) if len(b) > 4 else 0.0
        o = view.to_corner((cx + 0.5) * cell_mm, (cy + 0.5) * cell_mm)
        order.append((-math.hypot(o[0] - cam[0], o[1] - cam[1]), "b", (cx, cy, h, t, o, base)))
    parked = carriage_xy is not None and S.is_parked(*carriage_xy)
    if carriage_xy is not None and not parked and S.DRAW_MOVING_CARRIAGE:
        centre, faces = S.carriage_parts(view, *carriage_xy)
        order.append((-math.hypot(centre[0] - cam[0], centre[1] - cam[1]), "c", faces))
    order.sort(key=lambda e: e[0])  # far first, so near ones cover them
    # A soft shadow on the plate around the print: the plate under every block, one cell wider, made darker once.
    if blocks:
        mask = bytearray(W * H)
        for _, kind, d in order:
            if kind != "b":
                continue
            cx, cy = d[0], d[1]
            x0, y0 = (cx - 1) * cell_mm, (cy - 1) * cell_mm
            q = [view.px(*view.to_corner(x0 + ex * 3 * cell_mm, y0 + ey * 3 * cell_mm), -bed_z) for ex, ey in ((0, 0), (1, 0), (1, 1), (0, 1))]
            if all(q):
                for y, a, b in _spans(q):  # type: ignore[arg-type]
                    mask[y * W + a:y * W + b + 1] = bytes([1]) * (b - a + 1)
        for i in range(len(mask)):
            if mask[i]:
                j = i * 3
                rgb[j] = rgb[j] * 3 // 5
                rgb[j + 1] = rgb[j + 1] * 3 // 5
                rgb[j + 2] = rgb[j + 2] * 3 // 5
    neighbours = ((0, -1, 0, 0, 1, 0), (1, 0, 1, 0, 1, 1), (0, 1, 0, 1, 1, 1), (-1, 0, 0, 0, 0, 1))  # dx, dy, edge corners
    for _, kind, d in order:
        if kind == "c":
            for pts, color in d:
                view.draw(rgb, pts, color)
            continue
        cx, cy, h, t, o, floor = d
        base = tools[t % len(tools)]
        x0, y0 = cx * cell_mm, cy * cell_mm
        for i, (dx, dy, ax, ay, bx, by) in enumerate(neighbours):
            lower = max(grid.get((cx + dx, cy + dy), 0.0), floor)  # an overhang has no side below where it starts
            if lower >= h:
                continue
            a = view.to_corner(x0 + ax * cell_mm, y0 + ay * cell_mm)
            b = view.to_corner(x0 + bx * cell_mm, y0 + by * cell_mm)
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            if (mx - o[0]) * (cam[0] - mx) + (my - o[1]) * (cam[1] - my) <= 0:
                continue  # this side faces away from the camera
            q = [view.px(a[0], a[1], lower - bed_z), view.px(b[0], b[1], lower - bed_z),
                 view.px(b[0], b[1], h - bed_z), view.px(a[0], a[1], h - bed_z)]
            if all(q):
                _fill(rgb, q, _shade(base, 0.66 + 0.1 * i))  # type: ignore[arg-type]
        q = [view.px(*view.to_corner(x0 + ex * cell_mm, y0 + ey * cell_mm), h - bed_z) for ex, ey in ((0, 0), (1, 0), (1, 1), (0, 1))]
        if all(q):
            grain = 0.94 + 0.06 * ((cx * 7 + cy * 13 + int(h * 5)) % 5) / 4  # a little layer to layer change
            _fill(rgb, q, _shade(base, (0.9 + 0.1 * (h / top)) * grain))  # type: ignore[arg-type]
    S.draw_foreground(rgb, parked)
    return png(W, H, rgb)
