"""Following the print file and drawing the plate. Small hand-written G-code, no printer."""

import struct
import zlib

import httpx

from guardian.ai.gcode_view import N, GcodeFollower, draw_top

GCODE = b"""; made up for the test
G90
M83
T1
G1 Z0.2 F600
G1 X10 Y10 F9000          ; travel, no plastic
G1 X20 Y10 E1.0           ; extrude along the front
G1 X20 Y20 E0.5
G0 X100 Y100              ; travel
G1 Z5.0
G1 X100 Y100 E0           ; no extrusion
T2
G1 X50 Y50
G1 X60 Y50 E0.4 ; tool 2 at z 5
G92 E0
M82
G1 X60 Y60 E1.0
G1 X70 Y60 E1.0           ; same absolute E: no plastic
"""


def cell(f, x, y):
    k = int(y) * N + int(x)
    return f.plate.height[k] / 10, f.plate.tool[k]


def test_parse_marks_only_extrusion_with_height_and_tool():
    f = GcodeFollower("http://p")
    f.reset("t.gcode")
    f.feed(GCODE[:120])  # split in the middle of a line on purpose
    f.feed(GCODE[120:] + b"\n")
    assert cell(f, 15, 10) == (0.2, 1)
    assert cell(f, 20, 15) == (0.2, 1)
    assert cell(f, 30, 30) == (0.0, 0)  # travel only
    assert cell(f, 55, 50) == (5.0, 2)
    assert cell(f, 60, 55) == (5.0, 2)  # absolute E after G92 grew from 0 to 1
    assert cell(f, 65, 60) == (0.0, 0)  # absolute E did not grow: no plastic
    assert f.plate.max_z == 5.0


async def test_update_reads_only_new_bytes_with_ranges():
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers["range"])
        start, end = (int(x) for x in request.headers["range"].split("=")[1].split("-"))
        return httpx.Response(206, content=GCODE[start:end + 1])

    f = GcodeFollower("http://p", transport=httpx.MockTransport(handler))
    assert await f.update("t.gcode", 200) == 200
    assert await f.update("t.gcode", len(GCODE)) == len(GCODE) - 200
    assert asked == ["bytes=0-199", f"bytes=200-{len(GCODE) - 1}"]
    assert f.plate.max_z == 5.0
    await f.update("other.gcode", 10)  # a new file starts over
    assert f.filename == "other.gcode" and f.read_upto == 10 and f.plate.max_z == 0


def test_drawing_is_a_valid_png():
    f = GcodeFollower("http://p")
    f.reset("t.gcode")
    f.feed(GCODE + b"\n")
    data = draw_top(f.plate, ["#FFFFFF", "#FF0000", "#00FF00"], size=90)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (90, 90)
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert len(zlib.decompress(idat)) == 90 * (1 + 90 * 3)
