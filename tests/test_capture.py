from datetime import datetime, timedelta, timezone

import pytest

from guardian.camera import standin
from guardian.camera.capture import Capture, FFmpegMissing, resolve_tool
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.clock import ManualClock
from guardian.config import CameraConfig

pytestmark = pytest.mark.ffmpeg
T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def test_missing_tool_gives_clear_error():
    with pytest.raises(FFmpegMissing, match="not found"):
        resolve_tool("definitely-not-ffmpeg-xyz")


@pytest.mark.parametrize("codec", ["h264", "mjpeg"])
async def test_capture_segments_and_clock(tmp_path, codec):
    clock = ManualClock(T0)
    cfg = CameraConfig(standin_codec=codec)
    buf = SegmentBuffer(tmp_path / "buf", cfg.segment_length_s, clock)
    cap = Capture(cfg, buf, clock, realtime=False, duration_s=20)
    assert await cap.run_to_completion() == 0

    segs = buf.all_segments()
    assert len(segs) == 10
    assert segs[0].start_utc == T0
    assert segs[-1].end_utc == T0 + timedelta(seconds=20)
    assert all(abs(s.duration_s - 2.0) < 0.15 for s in segs)
    assert all(s.path.exists() for s in segs)

    # Segments are only visible once they are closed (end <= now).
    clock.set(T0 + timedelta(seconds=7))
    visible = buf.segments_between(T0, T0 + timedelta(seconds=30))
    assert [s.index for s in visible] == [1, 2, 3]

    # The burned-in clock matches the segment list.
    ffmpeg = resolve_tool("ffmpeg")
    for s in (segs[0], segs[4], segs[9]):
        assert abs(standin.read_frame_clock(ffmpeg, s.path, 0.0) - s.start_s) < 0.25


async def test_rolling_delete_and_pin(tmp_path):
    clock = ManualClock(T0)
    cfg = CameraConfig()
    buf = SegmentBuffer(tmp_path / "buf", cfg.segment_length_s, clock)
    cap = Capture(cfg, buf, clock, realtime=False, duration_s=20)
    await cap.run_to_completion()

    clock.set(T0 + timedelta(seconds=20))
    pinned, failed = buf.pin_to(buf.segments_between(T0 + timedelta(seconds=10), T0 + timedelta(seconds=14)),
                                tmp_path / "pin")
    assert len(pinned) == 2 and not failed
    removed = buf.rolling_delete(keep_s=8)  # keep roughly the last 8 s
    assert removed == 5  # segments ending before T0+12 s; one ending exactly at 12 s stays
    assert len(buf.all_segments()) == 5
    assert all(p.exists() for _, p in pinned)  # copies survive rolling deletion


async def test_next_index_does_not_go_backwards(tmp_path):
    clock = ManualClock(T0)
    cfg = CameraConfig()
    buf = SegmentBuffer(tmp_path / "buf", cfg.segment_length_s, clock)
    cap = Capture(cfg, buf, clock, realtime=False, duration_s=6)
    await cap.run_to_completion()
    clock.advance(1000)
    buf.rolling_delete(keep_s=1)
    assert buf.all_segments() == []
    assert buf.next_index() == 4
