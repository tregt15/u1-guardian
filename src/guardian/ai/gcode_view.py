"""What the plate should look like right now, made from the print file (G-code). Read only.

Guardian reads the G-code from the printer (Moonraker serves it read only, with HTTP range requests) up to the
byte the printer has reached (virtual_sdcard.file_position, seen on the real U1) and keeps a height map of the plate:
for each 1 mm square, how tall the printed plastic is there now, and which tool printed the top of it.
From that it draws a picture ("expected view") that the AI gets next to the camera picture.

Only the standard library is used (no numpy, no image library), so it also runs on a Raspberry Pi.
UNVERIFIED: arcs (G2/G3) are drawn as straight lines (Snapmaker Orca did not use them in the files seen so far).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import struct
import zlib
from array import array
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

BED_MM = 270  # U1 build plate is 270 x 270 mm
CELL_MM = 1.0
N = int(BED_MM / CELL_MM)
CHUNK = 1024 * 1024
WORD_RE = re.compile(r"([A-Z])(-?\d*\.?\d+)")
TOOL_RE = re.compile(r"^T(\d)\b")


@dataclass
class PlateMap:
    """Height of printed plastic on the plate, in 0.1 mm, per 1 mm cell, and the tool of the top layer."""
    height: array = field(default_factory=lambda: array("H", bytes(2 * N * N)))
    tool: array = field(default_factory=lambda: array("b", bytes(N * N)))
    max_z: float = 0.0

    def mark(self, x0: float, y0: float, x1: float, y1: float, z: float, t: int) -> None:
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length / (CELL_MM / 2)))
        zz = min(65535, int(round(z * 10)))
        for i in range(steps + 1):
            f = i / steps
            cx, cy = int((x0 + (x1 - x0) * f) / CELL_MM), int((y0 + (y1 - y0) * f) / CELL_MM)
            if 0 <= cx < N and 0 <= cy < N:
                k = cy * N + cx
                if zz >= self.height[k]:
                    self.height[k] = zz
                    self.tool[k] = t
        if z > self.max_z:
            self.max_z = z

    def blocks(self, cell: int = 3) -> list[tuple[int, int, float, int]]:
        """The plate in cell x cell mm blocks: (cx, cy, height in mm, tool of the tallest cell). Empty blocks are left out."""
        best: dict[tuple[int, int], tuple[int, int]] = {}
        for cy in range(N):
            row = cy * N
            for cx in range(N):
                h = self.height[row + cx]
                if h:
                    key = (cx // cell, cy // cell)
                    if key not in best or h > best[key][0]:
                        best[key] = (h, self.tool[row + cx])
        return [(cx, cy, round(h / 10, 1), t) for (cx, cy), (h, t) in best.items()]

    def covered_cells(self) -> int:
        return sum(1 for h in self.height if h)


class GcodeFollower:
    """Follows one print file. Call update(file_position) now and then. Only new bytes are read."""

    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._transport = transport
        self.filename: str | None = None
        self.reset(None)

    def reset(self, filename: str | None) -> None:
        self.filename = filename
        self.plate = PlateMap()
        self.read_upto = 0
        self._tail = b""
        self.x = self.y = self.z = self.e = 0.0
        self.abs_xyz = True
        self.abs_e = True
        self.tool = 0
        self.lines = 0
        self.excluded = False

    async def update(self, filename: str, file_position: int, max_bytes: int = 64 * CHUNK) -> int:
        """Read the file from where we stopped to file_position. Returns bytes read. Raises httpx errors."""
        if filename != self.filename:
            self.reset(filename)
        end = min(file_position, self.read_upto + max_bytes)
        done = 0
        url = f"{self.base_url}/server/files/gcodes/{filename}"
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as c:
            while self.read_upto < end:
                stop = min(end, self.read_upto + CHUNK) - 1
                r = await c.get(url, headers={"Range": f"bytes={self.read_upto}-{stop}"})
                if r.status_code not in (200, 206):
                    raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                data = r.content if r.status_code == 206 else r.content[self.read_upto:stop + 1]
                if not data:
                    break
                await asyncio.to_thread(self.feed, data)
                self.read_upto += len(data)
                done += len(data)
        return done

    def feed(self, data: bytes) -> None:
        """Parse a piece of G-code. A line cut at the end is kept for the next piece."""
        buf = self._tail + data
        lines = buf.split(b"\n")
        self._tail = lines.pop()
        for raw in lines:
            self._line(raw.decode("ascii", errors="ignore"))

    def _line(self, line: str) -> None:
        self.lines += 1
        line = line.split(";", 1)[0].strip().upper()
        if not line:
            return
        if line.startswith("EXCLUDE_OBJECT_START"):
            return
        if line.startswith("EXCLUDE_OBJECT_END"):
            return
        head = line.split(None, 1)[0]
        if head in ("G0", "G1", "G2", "G3"):
            w = {k: float(v) for k, v in WORD_RE.findall(line[len(head):])}
            nx = (w["X"] if self.abs_xyz else self.x + w["X"]) if "X" in w else self.x
            ny = (w["Y"] if self.abs_xyz else self.y + w["Y"]) if "Y" in w else self.y
            nz = (w["Z"] if self.abs_xyz else self.z + w["Z"]) if "Z" in w else self.z
            extrude = False
            if "E" in w:
                ne = w["E"] if self.abs_e else self.e + w["E"]
                extrude = ne > self.e + 1e-5
                self.e = ne
            if extrude and (nx != self.x or ny != self.y) and nz < 400:
                self.plate.mark(self.x, self.y, nx, ny, nz, self.tool)
            self.x, self.y, self.z = nx, ny, nz
        elif head == "G90":
            self.abs_xyz = True
            self.abs_e = True
        elif head == "G91":
            self.abs_xyz = False
            self.abs_e = False
        elif head == "M82":
            self.abs_e = True
        elif head == "M83":
            self.abs_e = False
        elif head == "G92":
            w = {k: float(v) for k, v in WORD_RE.findall(line[3:])}
            if "E" in w:
                self.e = w["E"]
            self.x, self.y, self.z = w.get("X", self.x), w.get("Y", self.y), w.get("Z", self.z)
        else:
            m = TOOL_RE.match(line)
            if m:
                self.tool = int(m.group(1))


# ---- drawing (PNG, standard library only) ----

def png(width: int, height: int, rgb: bytearray) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes(rgb[y * width * 3:(y + 1) * width * 3]) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def hex_rgb(h: str) -> tuple[int, int, int]:
    try:
        h = h.strip().lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return 230, 230, 230


def draw_top(plate: PlateMap, colors: list[str], size: int = 540) -> bytes:
    """The plate seen from above: dark plate, printed plastic in its filament color, brighter where taller.
    Y grows upward like on the printer, so the front of the plate is at the bottom of the picture."""
    rgb = bytearray(size * size * 3)
    tool_rgb = [hex_rgb(c) for c in colors] or [(230, 230, 230)]
    top = max(1.0, plate.max_z * 10)
    scale = N / size
    for py in range(size):
        cy = N - 1 - int(py * scale)
        for px in range(size):
            cx = int(px * scale)
            k = cy * N + cx
            o = (py * size + px) * 3
            h = plate.height[k]
            if h:
                r, g, b = tool_rgb[plate.tool[k] % len(tool_rgb)]
                f = 0.45 + 0.55 * (h / top)
                rgb[o], rgb[o + 1], rgb[o + 2] = int(r * f), int(g * f), int(b * f)
            else:
                grid = (cx % 30 == 0) or (cy % 30 == 0)
                rgb[o], rgb[o + 1], rgb[o + 2] = (52, 52, 58) if grid else (34, 34, 38)
    return png(size, size, rgb)


# Tested 2026-11-11 with qwen2.5vl:7b: given this top down drawing next to a photo that showed only 1 of 8 parts, the model
# still said "fine". So this drawing is NOT given to the AI. The AI compare check uses camera/simulate.py instead (watch only). It is shown to people, and kept for a measured comparison
# once the camera is set up (4 plate corners).
EXPECTED_HINT = ("The SECOND picture is not a photo. It is a drawing Guardian made from the print file. It shows the build "
                 "plate seen from straight above, with the printed plastic where it should be right now (brighter means "
                 "taller). The front of the plate is at the bottom of the drawing. Use it to check the photo: each part in "
                 "the drawing should be standing in the photo, and there should be no large amount of plastic in the photo "
                 "where the drawing shows nothing. The photo is taken from the front left, so the parts look tilted.\n\n")
