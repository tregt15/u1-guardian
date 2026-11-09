"""Snapshot (JPEG polling) capture: frame buffer rules, and end to end with a fake camera."""

import asyncio
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from guardian.blackbox.incident_manager import IncidentManager
from guardian.bus import EventBus
from guardian.camera import standin
from guardian.camera.capture import check_ffmpeg, resolve_tool
from guardian.camera.snapshot import HOLD_S, FrameBuffer, SnapshotCapture
from guardian.clock import ManualClock, SystemClock, from_iso
from guardian.config import BlackboxConfig, CameraConfig, Config, PrinterConfig, StorageConfig
from guardian.storage.db import Database

T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8fakejpegbytes\xff\xd9"


def at(s):
    return T0 + timedelta(seconds=s)


# ---- FrameBuffer rules (no FFmpeg needed) ----

def test_frame_coverage_runs_until_next_frame(tmp_path):
    clock = ManualClock(T0)
    fb = FrameBuffer(tmp_path, 3.0, clock)
    for t in (0, 1.1, 2.3):
        fb.add_frame(JPEG, at(t))
    clock.set(at(2.4))
    segs = fb.all_segments()
    # The newest frame is not final yet: the camera may still send another one.
    assert [(s.start_utc, s.end_utc) for s in segs] == [(at(0), at(1.1)), (at(1.1), at(2.3))]
    clock.set(at(2.3 + 3.0))
    last = fb.all_segments()[-1]
    assert last.start_utc == at(2.3) and last.end_utc == at(2.3 + HOLD_S)  # alone too long: holds briefly only


def test_a_long_pause_is_a_visible_gap_not_covered_footage(tmp_path):
    clock = ManualClock(T0)
    fb = FrameBuffer(tmp_path, 3.0, clock)
    for t in (0, 1, 2, 12, 13):
        fb.add_frame(JPEG, at(t))
    clock.set(at(20))
    segs = fb.all_segments()
    gaps = [(b.start_utc - a.end_utc).total_seconds() for a, b in zip(segs, segs[1:])]
    assert gaps[0] == 0 and gaps[1] == 0           # normal spacing: continuous
    assert gaps[2] == pytest.approx(10 - HOLD_S)   # the 10 s pause: only HOLD_S of it is "covered"
    assert max(gaps) > 3.0


def test_segments_between_pin_and_rolling_delete(tmp_path):
    clock = ManualClock(T0)
    fb = FrameBuffer(tmp_path / "buf", 3.0, clock)
    for t in range(0, 20):
        fb.add_frame(JPEG, at(t))
    clock.set(at(25))
    window = fb.segments_between(at(5.5), at(8.5))
    assert [s.start_utc for s in window] == [at(5), at(6), at(7), at(8)]  # frame 5 still covers 5.5
    copied, failed = fb.pin_to(window, tmp_path / "pin")
    assert len(copied) == 4 and not failed
    removed = fb.rolling_delete(keep_s=10)  # keep what arrived after t = 15
    assert removed == 15
    assert all(p.exists() for _, p in copied)           # pinned copies survive
    assert len(list((tmp_path / "buf").glob("frame_*.jpg"))) == 5
    fb.clear_files()
    assert fb.all_segments() == [] and not list((tmp_path / "buf").glob("frame_*"))


async def test_capture_stores_only_new_pictures_and_respects_gating(tmp_path):
    cam = FakeCamera(tmp_path, n_frames=6)
    try:
        clock = SystemClock()
        cfg = CameraConfig(snapshot_url=cam.url, snapshot_poll_s=0.1)
        fb = FrameBuffer(tmp_path / "buf", 3.0, clock)
        gate = {"on": False}
        cap = SnapshotCapture(cfg, fb, clock, active=lambda: gate["on"])
        stop = asyncio.Event()
        task = asyncio.create_task(cap.supervise(stop))
        await asyncio.sleep(1.5)
        assert cap.running and fb._frames == []          # gated off: nothing stored
        gate["on"] = True
        await asyncio.sleep(3.5)                          # about 3 new pictures, polled many times
        n_on = len(fb._frames)
        cam.paused = True
        await asyncio.sleep(1.5)                          # same picture again: not stored
        n_paused = len(fb._frames)
        stop.set()
        await task
        assert 2 <= n_on <= 5, n_on
        assert n_paused <= n_on + 1
        assert not cap.running and cap.last_frame_utc is not None
    finally:
        cam.close()


async def test_capture_survives_a_dead_camera(tmp_path):
    cfg = CameraConfig(snapshot_url="http://127.0.0.1:9/monitor.jpg", snapshot_poll_s=0.1)  # nothing listens
    fb = FrameBuffer(tmp_path / "buf", 3.0, SystemClock())
    cap = SnapshotCapture(cfg, fb, SystemClock())
    stop = asyncio.Event()
    task = asyncio.create_task(cap.supervise(stop))
    await asyncio.sleep(0.6)
    stop.set()
    await task
    assert cap.last_error and "snapshot poll failed" in cap.last_error and fb._frames == []


# ---- fake camera ----

class FakeCamera:
    """Serves frame n (burned-in number) and moves to the next one each second, unless paused
    (then the same picture, like a closed viewer). Logs when the picture changes."""

    def __init__(self, tmp_path, n_frames=60):
        frames_dir = tmp_path / "jpgs"
        frames_dir.mkdir()
        try:
            subprocess.run(
                [resolve_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", str(n_frames),
                 "-i", "testsrc2=size=320x240:rate=1", "-vf", standin.clock_filter(), "-q:v", "5",
                 str(frames_dir / "f_%03d.jpg")], check=True)
            self.frames = [p.read_bytes() for p in sorted(frames_dir.glob("f_*.jpg"))]
        except Exception:  # no FFmpeg: tiny fake pictures are enough for the non-FFmpeg tests
            self.frames = [JPEG + bytes([i]) for i in range(n_frames)]
        self.paused = False
        self.changes: list[tuple[datetime, int]] = []  # (utc, frame number) each time the picture changed
        self._i, self._last = 0, time.monotonic()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                now = time.monotonic()
                if not outer.paused and now - outer._last >= 1.0:
                    outer._i = (outer._i + 1) % len(outer.frames)
                    outer._last = now
                    outer.changes.append((datetime.now(timezone.utc), outer._i))
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

    def frame_number_at(self, utc):
        last = 0
        for t, n in self.changes:
            if t <= utc:
                last = n
        return last

    def close(self):
        self.server.shutdown()


# ---- end to end: fake camera -> frames -> incident -> clip (real time, about 25 s each) ----

class Rig:
    def __init__(self, tmp_path):
        self.cam = FakeCamera(tmp_path)
        self.cfg = Config(
            printer=PrinterConfig(host="127.0.0.1"),
            camera=CameraConfig(snapshot_url=self.cam.url, snapshot_poll_s=0.2),
            blackbox=BlackboxConfig(pre_trigger_s=6, post_trigger_s=4),
            storage=StorageConfig(data_dir=tmp_path / "data", buffer_dir=tmp_path / "buf"))
        self.clock = SystemClock()
        self.ffmpeg, self.ffprobe = check_ffmpeg(self.cfg.camera)
        self.db = Database(self.cfg.storage.db_path)
        self.buf = FrameBuffer(self.cfg.storage.resolved_buffer_dir(), self.cfg.camera.max_frame_gap_s, self.clock)
        self.mgr = IncidentManager(self.cfg, self.db, self.buf, self.clock, self.ffmpeg, self.ffprobe, EventBus())
        self.cap = SnapshotCapture(self.cfg.camera, self.buf, self.clock)
        self.stop = asyncio.Event()

    async def run(self, seconds_before_trigger, during=None):
        capture = asyncio.create_task(self.cap.supervise(self.stop))

        async def ticks():
            while not self.stop.is_set():
                await self.mgr.tick()
                await asyncio.sleep(0.3)

        ticker = asyncio.create_task(ticks())
        try:
            await asyncio.sleep(seconds_before_trigger)
            if during:
                await during()
            result = await self.mgr.report_incident("test", "manual", "info")
            async with asyncio.timeout(30):
                while self.mgr.open_count:
                    await asyncio.sleep(0.3)
            return result
        finally:
            self.stop.set()
            await asyncio.gather(capture, ticker)
            self.cam.close()


@pytest.mark.ffmpeg
@pytest.mark.slow
async def test_clip_time_matches_real_time(tmp_path):
    """The bug this guards against: a clip whose timeline is shorter than real time, which
    would put the trigger marker in the wrong place and make 'complete' meaningless."""
    r = Rig(tmp_path)
    result = await r.run(11)
    row = r.db.get_incident_by_uid(result.uid)
    assert row["recording_status"] == "complete", row["recording_notes"]
    start, end = from_iso(row["start_time_utc"]), from_iso(row["end_time_utc"])
    trigger = from_iso(row["first_trigger_utc"])

    from guardian.blackbox.assembler import probe
    clip = r.cfg.storage.resolved_data_dir() / row["recording_path"]
    info = probe(r.ffprobe, clip)
    assert info.duration_s == pytest.approx((end - start).total_seconds(), abs=0.5)  # clip time = real time
    assert row["actual_pre_s"] >= 6 and info.duration_s >= 10

    # The frame shown at the trigger position is the one the camera was showing at the trigger.
    shown = round(standin.read_frame_clock(r.ffmpeg, clip, (trigger - start).total_seconds()) * standin.FPS)
    expected = r.cam.frame_number_at(trigger)
    assert abs(shown - expected) <= 1, (shown, expected)


@pytest.mark.ffmpeg
@pytest.mark.slow
async def test_camera_pause_in_the_window_is_reported_as_a_gap(tmp_path):
    r = Rig(tmp_path)

    async def viewer_closes_then_returns():
        # Runs after 8 s of normal capture. The picture freezes for 4 s, then moves again for 2 s.
        r.cam.paused = True
        await asyncio.sleep(4)
        r.cam.paused = False
        await asyncio.sleep(2)

    result = await r.run(8, during=viewer_closes_then_returns)
    row = r.db.get_incident_by_uid(result.uid)
    # Trigger at about 14 s, so the 6 s pre-roll window (8 s to 14 s) contains the freeze.
    assert row["recording_status"] == "partial"
    assert "Gap" in row["recording_notes"]
    assert row["recording_path"]  # what exists is still kept
