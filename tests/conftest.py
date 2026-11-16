import shutil

import pytest


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def pytest_collection_modifyitems(config, items):
    if _have_ffmpeg():
        return
    skip = pytest.mark.skip(reason="ffmpeg/ffprobe not on PATH")
    for item in items:
        if "ffmpeg" in item.keywords:
            item.add_marker(skip)


from pathlib import Path  # noqa: E402

from guardian.camera import calibration  # noqa: E402

SHIPPED_DEFAULT = calibration.DEFAULT_FILE


@pytest.fixture(autouse=True)
def no_shipped_camera_setup(monkeypatch, request, tmp_path_factory):
    """Tests start with no camera setup, as a new user would before Guardian shipped one. A test that wants the shipped
    setup asks for the `shipped_camera_setup` fixture."""
    if "shipped_camera_setup" in request.fixturenames:
        return
    monkeypatch.setattr(calibration, "DEFAULT_FILE", Path(str(tmp_path_factory.getbasetemp())) / "no_default_setup.json")


@pytest.fixture
def shipped_camera_setup():
    return SHIPPED_DEFAULT
