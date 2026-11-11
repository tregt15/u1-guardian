"""Does the camera picture match the print file? A measured check, no AI. Watch only for now.

With the camera set up (camera/calibration.py), Guardian knows where printed plastic should appear in the picture
(from the plate height map built from the G-code). It then looks at the picture:

- coverage: how much of the area where parts should be really looks like plastic. A part that broke off or was
  never printed lowers it.
- extra: how much of the rest of the plate looks like plastic. Spaghetti or a part that moved raises it.

"Looks like plastic" means clearly brighter or more colorful than the bare plate, measured on the bare plate parts of
the same picture. Black filament on the black U1 plate is nearly invisible, so this check is weak for black parts.
The toolhead blocks part of the view, so the area around and above it is left out.

Pictures are turned into a small grid of pixel colors with FFmpeg (already needed by Guardian), so no image library
is needed. UNVERIFIED: how well this works on real prints. It only records scores until that is known.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from guardian.ai.gcode_view import CELL_MM, N, PlateMap
from guardian.camera.calibration import PLATE_MM, CameraSetup

W, H = 480, 270


def decode(ffmpeg: str, jpeg: bytes, src_w: int = 1920, src_h: int = 1080) -> bytes | None:
    """JPEG to W x H RGB bytes with FFmpeg, or None."""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-vf", f"scale={W}:{H}",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"], input=jpeg, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and len(r.stdout) == W * H * 3 else None


def _poly_mask(pts: list[tuple[float, float]]) -> bytearray:
    """Filled polygon (scanline) in W x H."""
    m = bytearray(W * H)
    if len(pts) < 3:
        return m
    ys = [p[1] for p in pts]
    for y in range(max(0, int(min(ys))), min(H, int(max(ys)) + 1)):
        xs = []
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            if (y0 <= y < y1) or (y1 <= y < y0):
                xs.append(x0 + (y - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        for a, b in zip(xs[0::2], xs[1::2]):
            for x in range(max(0, int(a)), min(W, int(b) + 1)):
                m[y * W + x] = 1
    return m


def _dot(m: bytearray, u: float, v: float, r: int) -> None:
    cu, cv = int(u), int(v)
    for y in range(max(0, cv - r), min(H, cv + r + 1)):
        for x in range(max(0, cu - r), min(W, cu + r + 1)):
            m[y * W + x] = 1


@dataclass
class MatchResult:
    coverage: float
    extra: float
    expected_px: int
    plate_px: int
    threshold: float

    def as_dict(self) -> dict:
        return {"coverage": round(self.coverage, 3), "extra": round(self.extra, 3), "expected_px": self.expected_px,
                "plate_px": self.plate_px, "threshold": round(self.threshold, 1)}


def score(rgb: bytes, setup: CameraSetup, plate: PlateMap, bed_z: float,
          toolhead_xy: tuple[float, float] | None = None, src_w: int = 1920) -> MatchResult | None:
    """Compare one picture (W x H RGB from decode) with the plate map. None if it cannot be judged."""
    if not setup.ready:
        return None
    k = W / src_w  # clicks are in full size picture pixels

    def img(x, y, h):
        q = setup.to_image(x, y, h, bed_z)
        return None if q is None else (q[0] * k, q[1] * k)

    corners = [img(x, y, 0) for x, y in ((0, 0), (PLATE_MM, 0), (PLATE_MM, PLATE_MM), (0, PLATE_MM))]
    if any(c is None for c in corners):
        return None
    plate_m = _poly_mask(corners)
    expected = bytearray(W * H)
    for cy in range(0, N, 2):
        for cx in range(0, N, 2):
            h = plate.height[cy * N + cx] / 10
            if not h:
                continue
            x, y = (cx + 0.5) * CELL_MM, (cy + 0.5) * CELL_MM
            for f in (0.0, 0.5, 1.0):
                q = img(x, y, h * f)
                if q:
                    _dot(expected, q[0], q[1], 1)
    block = bytearray(W * H)
    if toolhead_xy:
        q = img(toolhead_xy[0], toolhead_xy[1], bed_z + 1.0)  # nozzle tip, just above the top of the print
        if q:
            for y in range(max(0, int(q[1]) - 160), min(H, int(q[1]) + 15)):
                for x in range(max(0, int(q[0]) - 70), min(W, int(q[0]) + 70)):
                    block[y * W + x] = 1
    # grow the expected area a little, the fit is only good to a few pixels
    grown = bytearray(expected)
    for i in range(W * H):
        if expected[i]:
            y, x = divmod(i, W)
            for yy in range(max(0, y - 4), min(H, y + 5)):
                for xx in range(max(0, x - 4), min(W, x + 5)):
                    grown[yy * W + xx] = 1
    lum = [0.0] * (W * H)
    sat = [0.0] * (W * H)
    bare = []
    for i in range(W * H):
        r, g, b = rgb[3 * i], rgb[3 * i + 1], rgb[3 * i + 2]
        lum[i] = 0.299 * r + 0.587 * g + 0.114 * b
        sat[i] = max(r, g, b) - min(r, g, b)
        if plate_m[i] and not grown[i] and not block[i]:
            bare.append(lum[i])
    if len(bare) < 500:
        return None
    bare.sort()
    threshold = bare[int(len(bare) * 0.97)] + 25  # clearly brighter than nearly all bare plate

    def plastic(i):
        return lum[i] > threshold or (sat[i] > 70 and lum[i] > 60)

    exp_px = [i for i in range(W * H) if expected[i] and plate_m[i] and not block[i]]
    rest_px = [i for i in range(W * H) if plate_m[i] and not grown[i] and not block[i]]
    cov = sum(1 for i in exp_px if plastic(i)) / len(exp_px) if exp_px else 0.0
    extra = sum(1 for i in rest_px if plastic(i)) / len(rest_px) if rest_px else 0.0
    return MatchResult(cov, extra, len(exp_px), len(rest_px), threshold)
