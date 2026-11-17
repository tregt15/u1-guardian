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


MAX_RUN = 8  # cells joined into one face
SIDE_SHADE = (0.78, 0.9, 0.84, 0.7)  # the four wall directions. Flat, so a wall is one clean color (the real print is white).


def _stroke(rgb: bytearray, pts, color) -> None:
    px = bytes(color)
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        steps = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
        for s in range(steps + 1):
            x, y = int(a[0] + (b[0] - a[0]) * s / steps), int(a[1] + (b[1] - a[1]) * s / steps)
            if 0 <= x < W and 0 <= y < H:
                rgb[(y * W + x) * 3:(y * W + x) * 3 + 3] = px


def _print_items(view, grid: dict, bed_z: float, cell: int, tools: list, top: float) -> list:
    """The print as faces to draw: top faces and wall faces, with neighbours of the same height joined into one long face
    (so there are no slices and far fewer polygons). grid: (cx, cy) -> (height mm, tool, where the plastic starts mm).
    Each item is (sort key, 3D points, color). Walls start at the neighbour's top, or at the overhang floor."""
    cam = view.cam
    items: list = []

    def key(px: float, py: float) -> float:
        c = view.to_corner(px, py)
        return -math.hypot(c[0] - cam[0], c[1] - cam[1])

    rows: dict[int, list] = {}
    for (cx, cy), (h, t, _b) in grid.items():
        rows.setdefault(cy, []).append((cx, h, t))
    for cy, cells in rows.items():  # top faces: along x
        cells.sort()
        i = 0
        while i < len(cells):
            cx, h, t = cells[i]
            j = i
            while j + 1 < len(cells) and cells[j + 1][0] == cells[j][0] + 1 and cells[j + 1][1:] == (h, t) and j + 1 - i < MAX_RUN:
                j += 1
            x1 = cells[j][0] + 1
            corners = [view.to_corner(x * cell, y * cell) for x, y in ((cx, cy), (x1, cy), (x1, cy + 1), (cx, cy + 1))]
            shade = 0.9 + 0.1 * (h / top)
            items.append((key((cx + x1) / 2 * cell, (cy + 0.5) * cell) + 0.02, [(c[0], c[1], h - bed_z) for c in corners],
                          _shade(tools[t % len(tools)], shade)))
            i = j + 1
    for d, (dx, dy) in enumerate(((0, -1), (1, 0), (0, 1), (-1, 0))):  # wall faces: the four directions
        lines: dict[int, list] = {}
        for (cx, cy), (h, t, floor) in grid.items():
            nb = grid.get((cx + dx, cy + dy))
            lower = max(nb[0] if nb else 0.0, floor)  # an overhang has no wall below where it starts
            if lower < h:
                lines.setdefault(cx if dx else cy, []).append((cy if dx else cx, lower, h, t))
        for fixed, cells in lines.items():
            cells.sort()
            i = 0
            while i < len(cells):
                v, lower, h, t = cells[i]
                j = i
                while j + 1 < len(cells) and cells[j + 1][0] == cells[j][0] + 1 and cells[j + 1][1:] == (lower, h, t) and j + 1 - i < MAX_RUN:
                    j += 1
                v1 = cells[j][0] + 1
                if dx:
                    x = (fixed + (1 if dx > 0 else 0)) * cell
                    a, b = (x, v * cell), (x, v1 * cell)
                else:
                    y = (fixed + (1 if dy > 0 else 0)) * cell
                    a, b = (v * cell, y), (v1 * cell, y)
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                ca, cb = view.to_corner(*a), view.to_corner(*b)
                cm = view.to_corner(*mid)
                inside = view.to_corner(mid[0] - dx * cell / 2, mid[1] - dy * cell / 2)
                if (cm[0] - inside[0]) * (cam[0] - cm[0]) + (cm[1] - inside[1]) * (cam[1] - cm[1]) > 0:  # faces the camera
                    items.append((key(*mid) + 0.01,
                                  [(ca[0], ca[1], lower - bed_z), (cb[0], cb[1], lower - bed_z),
                                   (cb[0], cb[1], h - bed_z), (ca[0], ca[1], h - bed_z)],
                                  _shade(tools[t % len(tools)], SIDE_SHADE[d])))
                i = j + 1
    return items


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
    grid = {(b[0], b[1]): (float(b[2]), int(b[3]), float(b[4]) if len(b) > 4 else 0.0) for b in blocks}
    top = max(1.0, max_z or max((v[0] for v in grid.values()), default=1.0))
    parked = carriage_xy is not None and S.is_parked(*carriage_xy)
    items = _print_items(view, grid, bed_z, cell_mm, tools, top)  # (far to near sort key, points, color)
    if carriage_xy is not None and not parked and S.DRAW_MOVING_CARRIAGE:
        centre, faces = S.carriage_parts(view, *carriage_xy)
        key = -math.hypot(centre[0] - cam[0], centre[1] - cam[1])
        items += [(key, pts, color) for pts, color in faces]
    items.sort(key=lambda e: e[0])  # far first, so near ones cover them
    # A soft shadow on the plate around the print: the plate under every block, one cell wider, made darker once.
    if grid:
        mask = bytearray(W * H)
        for (cx, cy) in grid:
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
    for _, pts, color in items:
        o = view.outline(pts)
        if o:
            _fill(rgb, o, color)
            _stroke(rgb, o, color)  # the same color on the edges, so no thin gap shows between two faces
    S.draw_foreground(rgb, parked)
    return png(W, H, rgb)
