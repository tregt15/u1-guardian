"""Camera setup math with a made-up camera."""

import math
import random

import pytest

from guardian.camera.calibration import CORNERS, CalibrationError, CameraSetup, orient, project, solve_camera

# A made-up camera looking at the plate from the front left and above.
P_TRUE = [[4.2, -1.1, 0.3, 300.0], [0.6, 1.4, -4.0, 600.0], [0.0007, 0.0022, 0.0009, 1.0]]


def clicks_at(bed_z, frame, noise=0.0, rnd=None):
    out = []
    for i, (x, y) in enumerate(CORNERS):
        u, v = project(P_TRUE, x, y, -bed_z)
        if noise:
            u, v = u + rnd.gauss(0, noise), v + rnd.gauss(0, noise)
        out.append({"frame": frame, "bed_z": bed_z, "corner": i, "u": u, "v": v})
    return out


def test_solves_the_camera_from_two_bed_heights(tmp_path):
    setup = CameraSetup(tmp_path)
    d = setup.set_clicks(clicks_at(0.2, "a") + clicks_at(30.0, "b"))
    assert d["rms_px"] < 0.5
    for x, y, h, z in [(135, 135, 10, 10), (50, 200, 0, 45), (260, 20, 40, 41)]:
        want = project(P_TRUE, x, y, h - z)
        got = setup.to_image(x, y, h, z)
        assert math.hypot(want[0] - got[0], want[1] - got[1]) < 1.0
    again = CameraSetup(tmp_path)  # stored and read back
    assert again.ready and again.data["rms_px"] == d["rms_px"]


def test_noisy_clicks_still_fit_and_report_the_error(tmp_path):
    rnd = random.Random(3)
    d = CameraSetup(tmp_path).set_clicks(clicks_at(0.5, "a", 3, rnd) + clicks_at(20, "b", 3, rnd) + clicks_at(45, "c", 3, rnd))
    assert 0.5 < d["rms_px"] < 8


def test_one_height_or_too_few_points_is_refused():
    with pytest.raises(CalibrationError, match="different bed height"):
        solve_camera([(c["u"], c["v"], *CORNERS[c["corner"]], 0.0) for c in clicks_at(0, "a") + clicks_at(0, "b")])
    with pytest.raises(CalibrationError, match="at least 6"):
        solve_camera([(1, 2, 0, 0, 0)] * 5)


def test_orientation_turns_and_mirrors():
    assert orient(0, 0, 0, False) == (0, 0)
    assert orient(0, 0, 1, False) == (0, 270)
    assert orient(10, 20, 0, True) == (260, 20)
    assert orient(*orient(30, 40, 1, False), 3, False) == (30, 40)
