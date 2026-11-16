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


def test_the_lens_bend_moves_the_picture_and_the_backdrop_is_drawn(tmp_path):
    s = setup_for(tmp_path)
    flat = simulate.render(s, [], 5.0, [])
    s.set_lens(0.4)
    bent = simulate.render(s, [], 5.0, [])
    assert flat != bent
    rows = pixels(bent)
    colors = {px(rows, x, y) for x in range(0, simulate.W, 8) for y in range(0, simulate.H, 8)}
    assert simulate.PLATE in colors and (56, 58, 65) in colors  # the plate, and the centre panel of the backdrop


def test_reference_photo_is_the_backdrop_and_the_bed_slides_with_its_height(tmp_path):
    s = setup_for(tmp_path)
    ref = bytes([200, 120, 40]) * (simulate.W * simulate.H)  # a flat orange "photo"
    same = simulate.render(s, [], 5.0, [], reference=(ref, 5.0))
    rows = pixels(same)
    assert px(rows, 5, 5) == (200, 120, 40)  # the photo shows through, nothing is drawn over it at the same bed height
    lower = simulate.render(s, [], 40.0, [], reference=(ref, 5.0))
    assert lower != same  # the bed moved, so the old bed area went dark and the plate is filled from the photo
    rows = pixels(lower)
    flat = {px(rows, x, y) for x in range(0, simulate.W, 16) for y in range(0, simulate.H, 16)}
    assert simulate.DARK in flat and (200, 120, 40) in flat
    assert simulate.render(s, [], 5.0, [], reference=(b"short", 5.0)) is not None  # a bad photo falls back to the drawing


def test_print_palette_lists_the_used_tools_with_color_words():
    from types import SimpleNamespace

    from guardian.main import App
    me = SimpleNamespace(tracker=SimpleNamespace(status={"print_stats": {"filename": "a.gcode"},
                                                         "print_task_config": {"extruders_used": [False, True, False, True]}}),
                         _file_meta={"filename": "a.gcode", "hex": ["#FFEBCB", "#FFFFFF", "#000000", "#FF9709"]})
    pal = App.print_palette(me)
    assert [c["tool"] for c in pal] == [1, 3] and pal[0]["hex"] == "#FFFFFF" and pal[1]["name"]
    me._file_meta = {}
    assert App.print_palette(me) == []

