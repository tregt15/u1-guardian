"""A 3D space for the simulated camera picture.

The printer interior is a model in millimetres (interior_model.py, made by tools/build_interior.py): the room, the back wall
with rails, lead screws and fan, the dock wall with four toolheads, the purge bin and the floor. On top of that Guardian puts
the things that move: the bed (plate and its white edge) at the bed height the printer reports, the carriage with the active
toolhead at the printer's X and Y, and the print. Everything is drawn through the same camera fit as the real picture (the 3x4
matrix and the lens bend from Camera setup), far to near (painter's order), so a part that is behind another part is hidden by it.

Coordinates are the plate corner frame of the camera fit: x and y on the plate, z the height above the nozzle plane.
The bed top is at z = -bed_z. UNVERIFIED: the model was traced from one picture of one printer. The sizes of the walls and the
carriage are estimates. Pure Python (standard library only).
"""

from __future__ import annotations

import math

from guardian.camera import interior_model as M
from guardian.camera.calibration import PLATE_MM, CameraSetup, distort, orient
from guardian.camera.simulate import H, SRC_W, W, _fill, _shade, camera_centre

PLATE = (46, 49, 56)
EDGE = (120, 108, 70)
RIM_MM = 14.0          # the bed's white side edge, below the plate top. Moves with the bed.
HEAD_DEPTH = 26.0      # how far a docked toolhead reaches back from its front face (y = -12)
HEAD_FRONT_Y = -12.0
CARRIAGE = {"half_w": 40.0, "half_d": 12.0, "z0": 2.0, "z1": 135.0}  # UNVERIFIED size of the white carriage body


class View:
    """The camera fit as a function: a point in mm to a pixel in the W x H picture (None if it is behind the camera)."""

    def __init__(self, setup: CameraSetup) -> None:
        self.p = setup.data["P"]
        self.k1 = setup.lens
        self.k = W / SRC_W
        self.cam = camera_centre(self.p)
        self.rot = setup.data.get("rotation", 0)
        self.mirror = setup.data.get("mirror", False)

    def px(self, x: float, y: float, z: float) -> tuple[float, float] | None:
        p = self.p
        w = p[2][0] * x + p[2][1] * y + p[2][2] * z + p[2][3]
        if w <= 1e-9:
            return None
        u, v = distort((p[0][0] * x + p[0][1] * y + p[0][2] * z + p[0][3]) / w,
                       (p[1][0] * x + p[1][1] * y + p[1][2] * z + p[1][3]) / w, self.k1)
        return self.k * u, self.k * v

    def outline(self, pts, step: float = 26.0) -> list[tuple[float, float]] | None:
        """A closed shape in pixels. Long edges are cut in pieces so the lens bends them. None if a point is behind the camera."""
        out: list[tuple[float, float]] = []
        n = len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            qa, qb = self.px(*a), self.px(*b)
            if qa is None or qb is None:
                return None
            segs = max(1, min(32, int(math.hypot(qb[0] - qa[0], qb[1] - qa[1]) / step)))
            out.append(qa)
            for j in range(1, segs):
                f = j / segs
                q = self.px(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
                if q is None:
                    return None
                out.append(q)
        return out

    def draw(self, rgb: bytearray, pts, color) -> None:
        o = self.outline(pts)
        if o:
            _fill(rgb, o, color)

    def to_corner(self, x: float, y: float) -> tuple[float, float]:
        """Printer X, Y to the plate corner frame."""
        return orient(x, y, self.rot, self.mirror)


_base_cache: dict = {}


def _key(view: View):
    return (tuple(round(v, 6) for row in view.p for v in row), round(view.k1, 6))


def background(view: View) -> bytearray:
    """The room and the printer interior without the toolheads (drawn once and kept)."""
    key = _key(view)
    base = _base_cache.get(key)
    if base is None:
        rgb = bytearray(bytes((20, 21, 24)) * (W * H))
        for color, pts in M.STATIC:
            view.draw(rgb, pts, color)
        _base_cache.clear()
        _base_cache[key] = base = bytes(rgb)
    return bytearray(base)


def _box_faces(view: View, x0: float, x1: float, y0: float, y1: float, z0: float, z1: float, color, light=(1.0, 0.8, 0.6)):
    """The faces of a box that face the camera, as (points, color). Far to near order does not matter inside one box."""
    cam = view.cam
    faces = []
    if cam[0] > x1:
        faces.append(([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], _shade(color, light[1])))
    if cam[0] < x0:
        faces.append(([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], _shade(color, light[1])))
    if cam[1] > y1:
        faces.append(([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], _shade(color, light[0])))
    if cam[1] < y0:
        faces.append(([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], _shade(color, light[0])))
    if cam[2] > z1:
        faces.append(([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], _shade(color, light[2] + 0.3)))
    return faces


def draw_heads(rgb: bytearray, view: View, away: int | None = None) -> None:
    """The four toolheads in their docks. `away` is the one on the carriage right now (its dock is empty)."""
    for i, quad in enumerate(M.HEAD_QUADS):
        if i == away:
            continue
        tl, tr, br, bl = quad
        back = HEAD_DEPTH
        view.draw(rgb, [(tr[0], tr[1], tr[2]), (tr[0], tr[1] - back, tr[2]), (br[0], br[1] - back, br[2]), (br[0], br[1], br[2])], (10, 10, 11))  # right side
        for color, pts in M.HEADS[i]:
            view.draw(rgb, pts, color)


def draw_bed(rgb: bytearray, view: View, bed_z: float) -> None:
    """The bed: a dark carriage under the plate, the plate edge and the plate top, at the bed height. The camera never moves,
    so the bed moves in the picture. The left edge (+y side) is dark with print on it, the right edge (+x side) is white."""
    top = -bed_z
    cam = view.cam
    for pts, color in _box_faces(view, 6.0, PLATE_MM - 6.0, 6.0, PLATE_MM - 6.0, top - RIM_MM - 140.0, top - RIM_MM, (16, 16, 18),
                                 light=(1.0, 0.8, 0.6)):
        if pts[0][2] == top - RIM_MM and pts[2][2] == top - RIM_MM:
            continue
        view.draw(rgb, pts, color)
    if cam[0] > PLATE_MM:  # white edge on the right
        view.draw(rgb, [(PLATE_MM, 0.0, top - RIM_MM), (PLATE_MM, PLATE_MM, top - RIM_MM), (PLATE_MM, PLATE_MM, top), (PLATE_MM, 0.0, top)], (238, 238, 234))
    if cam[1] > PLATE_MM:  # dark edge with the print on it, on the left
        view.draw(rgb, [(0.0, PLATE_MM, top - RIM_MM), (PLATE_MM, PLATE_MM, top - RIM_MM), (PLATE_MM, PLATE_MM, top), (0.0, PLATE_MM, top)], (52, 52, 56))
        view.draw(rgb, [(8.0, PLATE_MM, top - RIM_MM * 0.78), (PLATE_MM - 8.0, PLATE_MM, top - RIM_MM * 0.78),
                        (PLATE_MM - 8.0, PLATE_MM, top - RIM_MM * 0.5), (8.0, PLATE_MM, top - RIM_MM * 0.5)], (150, 150, 152))
    plate = [(0.0, 0.0, top), (PLATE_MM, 0.0, top), (PLATE_MM, PLATE_MM, top), (0.0, PLATE_MM, top)]
    o = view.outline(plate)
    if o:
        _fill(rgb, o, PLATE)
        for i in range(len(o)):
            a, b = o[i], o[(i + 1) % len(o)]
            _edge(rgb, a, b, EDGE)


def _edge(rgb: bytearray, a, b, color) -> None:
    steps = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))))
    px = bytes(color)
    for i in range(steps + 1):
        x, y = int(a[0] + (b[0] - a[0]) * i / steps), int(a[1] + (b[1] - a[1]) * i / steps)
        if 0 <= x < W and 0 <= y < H:
            rgb[(y * W + x) * 3:(y * W + x) * 3 + 3] = px


def carriage_parts(view: View, x: float, y: float):
    """The gantry and the white carriage at printer X, Y (nozzle at z = 0): the beam with two rods, the slider on the left
    rails, the carriage body and its orange fan. Returns (sort point, [(points, color)]) in drawing order."""
    cx, cy = view.to_corner(x, y)
    c = CARRIAGE
    out: list = []
    rods = ((97.0, 7.0), (38.0, 7.0))
    for zc, r in rods:  # the beam: two rods and the belts between them, from the left rails to the right wall
        out += _box_faces(view, -12.0, 282.0, cy - 8.0, cy + 8.0, zc - r, zc + r, (14, 14, 16))
    out += _box_faces(view, -12.0, 282.0, cy - 6.0, cy + 6.0, 80.0, 88.0, (36, 36, 38))
    out += _box_faces(view, -12.0, 282.0, cy - 6.0, cy + 6.0, 49.0, 56.0, (36, 36, 38))
    out += _box_faces(view, -14.0, -2.0, cy - 18.0, cy + 18.0, 26.0, 104.0, (22, 22, 24))  # the slider on the left rails
    out += _box_faces(view, cx - c["half_w"], cx + c["half_w"], cy - c["half_d"], cy + c["half_d"], c["z0"], c["z1"], (236, 236, 232))
    if view.cam[1] > cy + c["half_d"]:  # the orange fan on the face that looks at the camera
        fy = cy + c["half_d"]
        out.append((_circle3(cx - 8.0, fy, 92.0, 17.0, "y"), (232, 160, 22)))
        out.append((_circle3(cx - 8.0, fy, 92.0, 10.0, "y"), (190, 112, 12)))
    out.append(([(cx - 3, cy, -3.0), (cx + 3, cy, -3.0), (cx + 3, cy, c["z0"] + 6), (cx - 3, cy, c["z0"] + 6)], (90, 90, 96)))  # nozzle
    return (cx, cy), out


def _circle3(a: float, b: float, c: float, r: float, plane: str, n: int = 20):
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        out.append((a + r * math.cos(t), b, c + r * math.sin(t)) if plane == "y" else (a, b + r * math.cos(t), c + r * math.sin(t)))
    return out


PARK_XY = (5.0, 5.0)  # where the carriage sits when idle (seen on a U1). UNVERIFIED for other printers.


def is_parked(x: float, y: float) -> bool:
    return math.hypot(x - PARK_XY[0], y - PARK_XY[1]) < 30.0


def draw_foreground(rgb: bytearray, parked: bool = False) -> None:
    k = W / SRC_W
    for color, pts in (M.PARKED if parked else []) + M.FOREGROUND:
        _fill(rgb, [(x * k, y * k) for x, y in pts], color)
