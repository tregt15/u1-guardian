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
