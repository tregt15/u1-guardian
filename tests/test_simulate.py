"""The simulated camera picture (what the camera should see) made from the print file."""

import zlib

from guardian.ai.gcode_view import PlateMap
from guardian.camera import simulate
from guardian.camera.calibration import CORNERS, CameraSetup, project

# The same made-up camera as test_calibration, in full size (1920 wide) picture pixels.
P_TRUE = [[4.2, -1.1, 0.3, 300.0], [0.6, 1.4, -4.0, 600.0], [0.0007, 0.0022, 0.0009, 1.0]]


def setup_for(tmp_path):
    s = CameraSetup(tmp_path)
    clicks = []
    for bed_z, frame in ((0.2, "a"), (30.0, "b")):
        for i, (x, y) in enumerate(CORNERS):
            u, v = project(P_TRUE, x, y, -bed_z)
            clicks.append({"frame": frame, "bed_z": bed_z, "corner": i, "u": u, "v": v})
    s.set_clicks(clicks, lens=0)  # no lens bend, so project() below is exact
    return s


def pixels(data: bytes):
    """Decode our own PNG (filter 0 rows, 8 bit RGB) into rows of bytes."""
    assert data.startswith(b"\x89PNG")
    i, idat = 8, b""
    while i < len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        if kind == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    stride = simulate.W * 3 + 1
    return [raw[y * stride + 1:(y + 1) * stride] for y in range(simulate.H)]


def px(rows, x, y):
    return tuple(rows[int(y)][int(x) * 3:int(x) * 3 + 3])


def test_not_set_up_gives_nothing(tmp_path):
    assert simulate.render(CameraSetup(tmp_path), [], 5.0, []) is None


def test_camera_centre_is_where_the_matrix_sees_nothing():
    c = simulate.camera_centre(P_TRUE)
    assert all(abs(sum(r[i] * c[i] for i in range(3)) + r[3]) < 1e-6 for r in P_TRUE)


def test_a_printed_block_is_drawn_in_its_color_where_the_camera_would_see_it(tmp_path):
    s = setup_for(tmp_path)
    blocks = [(45, 45, 10.0, 1)]  # a 3 mm block at x, y 135 to 138, 10 mm tall, tool 1
    rows = pixels(simulate.render(s, blocks, 0.0, ["#000000", "#ff0000"]))
    u, v = project(s.data["P"], 136.5, 136.5, 10.0)  # the middle of its top face, in full size pixels
    r, g, b = px(rows, u * simulate.W / simulate.SRC_W, v * simulate.W / simulate.SRC_W)
    assert r > 150 and g < 30 and b < 30  # filament red, top face
    empty = pixels(simulate.render(s, [], 0.0, []))
    u0, v0 = project(s.data["P"], 136.5, 136.5, 0.0)  # the plate under the block
    assert px(empty, u0 * 0.5, v0 * 0.5) == simulate.PLATE  # without the block, bare plate


def test_the_plate_moves_with_the_bed(tmp_path):
    s = setup_for(tmp_path)
    a = simulate.render(s, [], 0.0, [])
    b = simulate.render(s, [], 30.0, [])
    assert a != b  # the camera is fixed and the bed is lower, so the plate is somewhere else


def test_plate_map_blocks_keep_the_tallest_cell_and_its_tool():
    plate = PlateMap()
    plate.mark(10, 10, 12, 10, 4.2, 1)
    plate.mark(10, 10, 10, 10, 8.0, 0)
    assert sorted(plate.blocks()) == [(3, 3, 8.0, 0), (4, 3, 4.2, 1)]


def test_the_lens_bend_moves_the_picture_and_walls_are_drawn(tmp_path):
    s = setup_for(tmp_path)
    flat = simulate.render(s, [], 5.0, [])
    s.set_lens(0.4)
    bent = simulate.render(s, [], 5.0, [])
    assert flat != bent
    rows = pixels(bent)
    colors = {px(rows, x, y) for x in range(0, simulate.W, 8) for y in range(0, simulate.H, 8)}
    assert simulate.WALL in colors and simulate.PLATE in colors  # a wall panel behind the plate
