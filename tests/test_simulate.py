"""The simulated camera picture (what the camera should see) made from the print file."""

import zlib

from guardian.ai import gcode_view as G
from guardian.ai.gcode_view import PlateMap
from guardian.camera import calibration, scene3d, simulate
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


DARK_PLATE = tuple(scene3d.DARKEN[c] for c in scene3d.PLATE)  # the whole picture is drawn a little darker


def pixels(data: bytes):
    """Decode our own PNG (filter 0 rows, 8 bit RGB) into rows of bytes."""
    assert data.startswith(bytes([137]) + b"PNG")
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


def test_a_shipped_default_setup_makes_a_picture_without_any_clicks(tmp_path, shipped_camera_setup):
    s = CameraSetup(tmp_path)
    assert s.ready and s.using_default
    assert simulate.render(s, [], 5.0, []).startswith(bytes([137]) + b"PNG")


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
    assert px(empty, u0 * 0.5, v0 * 0.5) == DARK_PLATE  # without the block, bare plate


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


def test_the_lens_bend_moves_the_picture_and_the_plate_is_drawn(tmp_path):
    s = setup_for(tmp_path)
    flat = simulate.render(s, [], 5.0, [])
    s.set_lens(0.4)
    bent = simulate.render(s, [], 5.0, [])
    assert flat != bent
    rows = pixels(bent)
    colors = {px(rows, x, y) for x in range(0, simulate.W, 8) for y in range(0, simulate.H, 8)}
    assert DARK_PLATE in colors


def test_the_3d_scene_draws_the_interior_under_a_lowered_bed(tmp_path, shipped_camera_setup):
    """Bug seen 2026-11-16: with the bed lowered, the old picture painted a dark patch where the bed had been. Now the wall,
    the fan and the dock are there, and a tall print still pokes up into the picture."""
    s = CameraSetup(tmp_path)  # the shipped default setup
    assert s.ready
    empty = pixels(simulate.render(s, [], 200.0, []))
    colors = {px(empty, x, y) for x in range(0, simulate.W, 6) for y in range(0, simulate.H, 6)}
    assert len(colors) > 60  # a model with many shapes, not flat patches
    dark = sum(1 for x in range(0, simulate.W, 6) for y in range(0, simulate.H, 6) if px(empty, x, y) == (14, 15, 18))
    assert dark < 50
    blocks = [(45, 45, 195.0, 1)]  # a 3 mm column in the middle of the plate, 195 mm tall, tool 1
    rows = pixels(simulate.render(s, blocks, 200.0, ["#000000", "#ff0000"]))
    view = scene3d.View(s)
    cx, cy = view.to_corner(136.5, 136.5)
    u, v = view.px(cx, cy, 195.0 - 200.0 * scene3d.BED_SCALE)  # the top of the column
    assert v < simulate.H  # it is inside the picture
    r, g, b = px(rows, u, v)
    assert r > 150 and g < 40 and b < 40


def test_the_carriage_and_its_empty_dock(tmp_path, shipped_camera_setup):
    s = CameraSetup(tmp_path)
    parked = simulate.render(s, [], 5.0, [], carriage_xy=(5.0, 5.0))
    working = simulate.render(s, [], 5.0, [], carriage_xy=(135.0, 250.0), away=1)
    docked_all = simulate.render(s, [], 5.0, [], carriage_xy=(135.0, 250.0), away=None)
    assert len({parked, working, docked_all}) == 3  # parked, at work with one head out of its dock, and at work with none out


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



def test_a_new_print_forgets_the_plate_map_of_the_last_one():
    """Bug seen 2026-11-16: printing the same file again kept the finished model, so the picture showed the whole print."""
    from types import SimpleNamespace

    from guardian.ai.gcode_view import GcodeFollower
    from guardian.main import App

    me = SimpleNamespace(tracker=SimpleNamespace(print_state="complete"), _prev_print_state=None,
                         gcode=GcodeFollower("http://x"), _gcode_task=None, _gcode_last=5.0)
    me._reset_plate_map = lambda: App._reset_plate_map(me)
    me.gcode.reset("a.gcode")
    me.gcode.plate.mark(10, 10, 20, 10, 5.0, 0)
    me.gcode.read_upto = 999
    App._new_print_check(me)                      # finished: the print is still on the plate
    assert me.gcode.plate.max_z > 0
    me.tracker.print_state = "printing"
    App._new_print_check(me)                      # a new print starts
    assert me.gcode.plate.max_z == 0 and me.gcode.filename is None and me._gcode_last == 0.0
    me.gcode.reset("a.gcode")
    me.gcode.plate.mark(10, 10, 20, 10, 5.0, 0)
    me.tracker.print_state = "paused"
    App._new_print_check(me)
    me.tracker.print_state = "printing"
    App._new_print_check(me)                      # pause then resume is the same print
    assert me.gcode.plate.max_z > 0


def test_overhangs_are_kept_and_drawn(tmp_path):
    """Plastic with nothing under it (an overhang or a bridge) has a start height above the plate, and the picture shows the
    gap under it instead of a solid column."""
    plate = PlateMap()
    for z in (0.25, 0.45, 0.65, 0.85):            # a tower at x 10 to 11, layer after layer
        plate.mark(10.1, 10.1, 10.9, 10.1, z, 0)
    for z in (0.25, 0.45):                          # a cell that is only reached high up, after a gap: an overhang
        plate.mark(20.1, 10.1, 20.9, 10.1, z, 0)
    plate.mark(20.1, 10.1, 20.9, 10.1, 4.05, 0)
    plate.mark(30.1, 10.1, 30.9, 10.1, 4.05, 0)     # plastic laid high with nothing below at all
    by_cell = {(b[0], b[1]): b for b in plate.blocks(1, with_base=True)}
    assert by_cell[(10, 10)][4] == 0.0              # the tower stands on the plate
    assert by_cell[(20, 10)][2] == 4.1 or abs(by_cell[(20, 10)][2] - 4.1) < 0.11
    assert 3.5 < by_cell[(20, 10)][4] < 4.0         # it starts well above the plate
    assert 3.5 < by_cell[(30, 10)][4] < 4.0
    assert plate.blocks(1) == [b[:4] for b in plate.blocks(1, with_base=True)]  # the plain form is unchanged
    s = setup_for(tmp_path)
    solid = [(45, 45, 10.0, 1)]
    hanging = [(45, 45, 10.0, 1, 6.0)]
    assert simulate.render(s, solid, 0.0, ["#000000", "#ff0000"]) != simulate.render(s, hanging, 0.0, ["#000000", "#ff0000"])


def test_the_moving_carriage_is_not_drawn_but_the_empty_dock_is(tmp_path, shipped_camera_setup):
    s = CameraSetup(tmp_path)
    assert not scene3d.DRAW_MOVING_CARRIAGE
    a = simulate.render(s, [], 5.0, [], carriage_xy=(135.0, 250.0), away=1)
    b = simulate.render(s, [], 5.0, [], carriage_xy=(60.0, 100.0), away=1)
    assert a == b  # where the carriage is does not change the picture


def test_clean_blocks_fill_holes_and_make_lone_overhangs_solid():
    """A one cell hole inside a wall is filled, and a lone floating cell (not part of a real overhang) stands on the plate."""
    plate = PlateMap()
    for cx in range(10, 15):
        for cy in range(10, 15):
            for z in (0.25, 0.45, 0.65):
                plate.mark(cx + 0.1, cy + 0.5, cx + 0.9, cy + 0.5, z, 0)
    k = 12 * G.N + 12
    plate.height[k] = 0                                  # a hole in the middle
    plate.base[13 * G.N + 13] = 30                       # one cell that thinks it floats
    raw = {(b[0], b[1]): b for b in plate.blocks(1, with_base=True)}
    tidy = {(b[0], b[1]): b for b in plate.blocks(1, with_base=True, clean=True)}
    assert (12, 12) not in raw and (12, 12) in tidy
    assert raw[(13, 13)][4] == 3.0 and tidy[(13, 13)][4] == 0.0
    assert len(plate.blocks(2, clean=True)) > 0
