"""Camera setup: where the U1 camera looks, so Guardian can tell where printed plastic should appear in a picture.

The user clicks the 4 corners of the build plate on pictures taken at 2 or more bed heights. On the U1 the bed moves
down as the print grows (seen: after a print the printer reports the bed at Z = 200 and the plate is out of view), and
the camera is fixed to the frame. So a point on the plate at printer coordinates (x, y), at height h above the plate,
is at "frame height" h - z_bed. With clicks at two bed heights the points are not all in one plane, and a full
3x4 camera matrix can be solved (DLT, least squares). Lens bending (the U1 camera is wide angle) is ignored, so the
fit is only good to some pixels. The fit error is shown so the user can judge.

A square plate looks the same turned 4 ways, so the corners alone cannot tell which corner is X=0, Y=0. The user turns
the drawing (rotation, mirror) until the printed parts line up. UNVERIFIED: that the plate edges are exactly the printer's
0 to 270 mm area.

Pure Python, no numpy.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

PLATE_MM = 270.0
# The 4 corners in the order the user is asked to click them (going around the plate), before rotation/mirror.
CORNERS = [(0.0, 0.0), (PLATE_MM, 0.0), (PLATE_MM, PLATE_MM), (0.0, PLATE_MM)]


class CalibrationError(ValueError):
    pass


# ---- small linear algebra ----

def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Solve a x = b (square) with Gaussian elimination and partial pivoting."""
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            raise CalibrationError("the clicked points do not pin down the camera (try two different bed heights)")
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                if f:
                    for k in range(c, n + 1):
                        m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _least_squares(rows: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rows[0])
    ata = [[sum(r[i] * r[j] for r in rows) for j in range(n)] for i in range(n)]
    atb = [sum(r[i] * y for r, y in zip(rows, rhs)) for i in range(n)]
    return _solve(ata, atb)


def solve_camera(points: list[tuple[float, float, float, float, float]]) -> list[list[float]]:
    """points: (u, v, X, Y, Z) with image pixels u, v and world X, Y, Z (Z = height above plate minus bed height).
    Returns the 3x4 matrix P with P[2][3] = 1. Needs 6 or more points that are not all in one plane."""
    if len(points) < 6:
        raise CalibrationError("click at least 6 points (4 corners on one picture and more on a second picture)")
    zs = {round(p[4], 1) for p in points}
    if len(zs) < 2:
        raise CalibrationError("all points are at one bed height. Add clicks on a picture at a different bed height")
    # normalise for numerical stability
    us, vs = [p[0] for p in points], [p[1] for p in points]
    mu, mv, su = sum(us) / len(us), sum(vs) / len(vs), max(1.0, max(us) - min(us))
    xs, ys, zz = [p[2] for p in points], [p[3] for p in points], [p[4] for p in points]
    mx, my, mz = sum(xs) / len(xs), sum(ys) / len(ys), sum(zz) / len(zz)
    sw = max(1.0, max(max(xs) - min(xs), max(ys) - min(ys), max(zz) - min(zz)))
    rows, rhs = [], []
    for u, v, x, y, z in points:
        u2, v2 = (u - mu) / su, (v - mv) / su
        x2, y2, z2 = (x - mx) / sw, (y - my) / sw, (z - mz) / sw
        rows.append([x2, y2, z2, 1, 0, 0, 0, 0, -u2 * x2, -u2 * y2, -u2 * z2]); rhs.append(u2)
        rows.append([0, 0, 0, 0, x2, y2, z2, 1, -v2 * x2, -v2 * y2, -v2 * z2]); rhs.append(v2)
    p = _least_squares(rows, rhs) + [1.0]
    pn = [p[0:4], p[4:8], p[8:12]]
    # undo the normalisation: P = T_img^-1 * Pn * T_world
    tw = [[1 / sw, 0, 0, -mx / sw], [0, 1 / sw, 0, -my / sw], [0, 0, 1 / sw, -mz / sw], [0, 0, 0, 1]]
    ti = [[su, 0, mu], [0, su, mv], [0, 0, 1]]
    a = [[sum(pn[i][k] * tw[k][j] for k in range(4)) for j in range(4)] for i in range(3)]
    out = [[sum(ti[i][k] * a[k][j] for k in range(3)) for j in range(4)] for i in range(3)]
    s = out[2][3] or 1.0
    return [[x / s for x in row] for row in out]


def project(p: list[list[float]], x: float, y: float, z: float) -> tuple[float, float] | None:
    w = p[2][0] * x + p[2][1] * y + p[2][2] * z + p[2][3]
    if abs(w) < 1e-9:
        return None
    return ((p[0][0] * x + p[0][1] * y + p[0][2] * z + p[0][3]) / w,
            (p[1][0] * x + p[1][1] * y + p[1][2] * z + p[1][3]) / w)


def orient(x: float, y: float, rotation: int, mirror: bool) -> tuple[float, float]:
    """Printer (x, y) to the plate corner frame used for the clicks: mirror, then turn by 90 degree steps."""
    if mirror:
        x = PLATE_MM - x
    for _ in range(rotation % 4):
        x, y = y, PLATE_MM - x
    return x, y


# ---- stored setup ----

DEFAULT_FILE = Path(__file__).with_name("default_calibration.json")


class CameraSetup:
    """Clicks and the solved camera, stored as calibration.json in the data folder.

    Every U1 has the same camera mount, so a default setup ships with Guardian (default_calibration.json). It is used until
    the person saves their own, and comes back when they press Start over. UNVERIFIED: that every unit matches it."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.path = folder / "calibration.json"
        self.data: dict[str, Any] = {}
        self.using_default = False
        for src, is_default in ((self.path, False), (DEFAULT_FILE, True)):
            if src.is_file():
                try:
                    data = json.loads(src.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("P"):
                    self.data, self.using_default = data, is_default
                    break

    @property
    def ready(self) -> bool:
        return bool(self.data.get("P"))

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def set_clicks(self, clicks: list[dict], rotation: int | None = None, mirror: bool | None = None) -> dict:
        """clicks: [{frame, bed_z, corner (0 to 3), u, v}]. Solves the camera. Raises CalibrationError."""
        pts = []
        for c in clicks:
            cx, cy = CORNERS[int(c["corner"]) % 4]
            pts.append((float(c["u"]), float(c["v"]), cx, cy, -float(c["bed_z"])))
        p = solve_camera(pts)
        errs = []
        for u, v, x, y, z in pts:
            q = project(p, x, y, z)
            errs.append(math.hypot(q[0] - u, q[1] - v) if q else 1e9)
        rms = math.sqrt(sum(e * e for e in errs) / len(errs))
        # Per click error, so the page can mark the click that fits worst (often a guessed or off-screen corner).
        clicks = [dict(c, err_px=round(e, 1)) for c, e in zip(clicks, errs)]
        self.data.update({"clicks": clicks, "P": p, "rms_px": round(rms, 1),
                          "rotation": self.data.get("rotation", 0) if rotation is None else int(rotation) % 4,
                          "mirror": self.data.get("mirror", False) if mirror is None else bool(mirror)})
        self.using_default = False
        self.save()
        return self.data

    def set_orientation(self, rotation: int, mirror: bool) -> None:
        self.data["rotation"], self.data["mirror"] = int(rotation) % 4, bool(mirror)
        self.using_default = False
        self.save()

    def clear(self) -> None:
        """Drop the person's own setup. The shipped default (if any) takes over again."""
        if self.path.exists():
            self.path.unlink()
        self.__init__(self.folder)

    def to_image(self, x: float, y: float, height: float, bed_z: float) -> tuple[float, float] | None:
        """Printer point (x, y) at `height` mm above the plate, with the bed at bed_z, to picture pixels."""
        if not self.ready:
            return None
        cx, cy = orient(x, y, self.data.get("rotation", 0), self.data.get("mirror", False))
        return project(self.data["P"], cx, cy, height - bed_z)
