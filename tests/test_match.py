"""Measured match between the camera and the print file, with made-up pictures (no FFmpeg)."""

from guardian.ai.gcode_view import PlateMap
from guardian.camera.calibration import CORNERS, CameraSetup, project
from guardian.camera.match import H, W, score

# A made-up camera, in full size picture pixels (1920 x 1080), looking down at the plate.
P_TRUE = [[5.0, 1.0, 0.0, 300.0], [0.4, -3.0, -2.0, 950.0], [0.0, 0.0006, 0.0, 1.0]]
BED_Z = 10.0


def setup(tmp_path):
    clicks = []
    for z, f in ((0.5, "z01"), (30.0, "z30")):
        for i, (x, y) in enumerate(CORNERS):
            u, v = project(P_TRUE, x, y, -z)
            clicks.append({"frame": f, "bed_z": z, "corner": i, "u": u, "v": v})
    s = CameraSetup(tmp_path)
    s.set_clicks(clicks)
    return s


def plate_with_part():
    p = PlateMap()
    for x in range(100, 130):
        for y in range(100, 130):
            p.mark(x, y, x + 0.5, y, 8.0, 0)
    return p


def picture(s, plate, draw_part=True, blob=None):
    """Dark plate (with some texture), white plastic where the part is."""
    rgb = bytearray(W * H * 3)
    for i in range(W * H):
        v = 40 + (i * 7919) % 23
        rgb[3 * i:3 * i + 3] = bytes((v, v, v + 4))
    k = W / 1920

    def paint(x, y, h):
        q = s.to_image(x, y, h, BED_Z)
        if q:
            u, v = int(q[0] * k), int(q[1] * k)
            for yy in range(v - 2, v + 3):
                for xx in range(u - 2, u + 3):
                    if 0 <= xx < W and 0 <= yy < H:
                        rgb[3 * (yy * W + xx):3 * (yy * W + xx) + 3] = b"\xf0\xf0\xf0"
    if draw_part:
        for x in range(100, 131, 2):
            for y in range(100, 131, 2):
                for h in (0, 2, 4, 6, 8):
                    paint(x, y, h)
    if blob:
        for x in range(blob[0], blob[0] + 30, 2):
            for y in range(blob[1], blob[1] + 30, 2):
                paint(x, y, 0)
    return bytes(rgb)


def test_matching_picture_scores_high_and_clean(tmp_path):
    s = setup(tmp_path)
    plate = plate_with_part()
    r = score(picture(s, plate), s, plate, BED_Z)
    assert r is not None and r.coverage > 0.8 and r.extra < 0.01


def test_missing_part_lowers_coverage(tmp_path):
    s = setup(tmp_path)
    plate = plate_with_part()
    r = score(picture(s, plate, draw_part=False), s, plate, BED_Z)
    assert r.coverage < 0.1


def test_plastic_where_nothing_should_be_raises_extra(tmp_path):
    s = setup(tmp_path)
    plate = plate_with_part()
    clean = score(picture(s, plate), s, plate, BED_Z)
    messy = score(picture(s, plate, blob=(200, 40)), s, plate, BED_Z)
    assert messy.extra > clean.extra + 0.005


def test_no_setup_means_no_score(tmp_path):
    assert score(bytes(W * H * 3), CameraSetup(tmp_path), plate_with_part(), BED_Z) is None
