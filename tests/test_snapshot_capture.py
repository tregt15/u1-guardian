"""Snapshot capture against a fake camera server (no printer involved)."""

import asyncio
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from guardian.camera import standin
from guardian.camera.capture import Capture, resolve_tool
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.clock import SystemClock
from guardian.config import CameraConfig

pytestmark = [pytest.mark.ffmpeg, pytest.mark.slow]


def _make_jpegs(tmp_path, n=40):
    out = tmp_path / "frames"
    out.mkdir()
    subprocess.run(
        [resolve_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", str(n),
         "-i", "testsrc2=size=320x240:rate=1", "-vf", standin.clock_filter(), "-q:v", "5",
         str(out / "f_%03d.jpg")],
        check=True,
    )
    return [p.read_bytes() for p in sorted(out.glob("f_*.jpg"))]


class _Camera:
    """Serves a new picture each second, unless paused (then the same one, like a closed viewer)."""

    def __init__(self, frames):
        self.frames = frames
        self.paused = False
        self._i = 0
        self._last = time.monotonic()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                now = time.monotonic()
                if not outer.paused and now - outer._last >= 1.0:
                    outer._i = (outer._i + 1) % len(outer.frames)
                    outer._last = now
                body = outer.frames[outer._i]
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/monitor.jpg"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


async def test_snapshot_mode_makes_segments_and_shows_gaps(tmp_path):
    cam = _Camera(_make_jpegs(tmp_path))
    try:
        clock = SystemClock()
        cfg = CameraConfig(snapshot_url=cam.url, snapshot_poll_s=0.3)
        buf = SegmentBuffer(tmp_path / "buf", cfg.segment_length_s, clock)
        cap = Capture(cfg, buf, clock, realtime=True)
        stop = asyncio.Event()
        task = asyncio.create_task(cap.supervise(stop))

        await asyncio.sleep(9)       # viewer on: about 1 new frame per second
        cam.paused = True            # viewer closes: same picture from now on
        await asyncio.sleep(9)
        cam.paused = False           # viewer comes back
        await asyncio.sleep(7)
        stop.set()
        await task
        buf.refresh()

        segs = buf.all_segments()
        assert len(segs) >= 4, [(round(x.start_s, 1), round(x.end_s, 1)) for x in segs]
        # In copy mode a segment ends at its last frame, so a gap shows up BETWEEN segments.
        gaps = [(b.start_s - a.end_s) for a, b in zip(segs, segs[1:])]
        tol = cfg.max_frame_gap_s
        assert any(g > tol for g in gaps), f"the 9 s pause should be a visible gap: {[round(g, 1) for g in gaps]}"
        assert any(g <= tol for g in gaps), "normal spacing while the viewer was on should not look like a gap"
        assert cap.last_frame_utc is not None
    finally:
        cam.close()
