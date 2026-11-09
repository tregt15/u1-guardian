"""FFmpeg capture: a managed subprocess that writes short segments.

Windows-safe by design:
- segments are named by index (seg_000001.mkv), never by time
- times come from the segment list CSV plus the wall clock at process start
- no `%s` in strftime, no shell features

The real U1 camera is UNVERIFIED. The `source_url` path below is a plain
guess at how a URL would be copied. It has never been run against a U1.
Segments are Matroska because it holds both H.264 and MJPEG.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import httpx

from guardian.camera import standin
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.clock import Clock
from guardian.config import CameraConfig

log = logging.getLogger(__name__)

SEGMENT_EXT = "mkv"
STARTUP_GRACE_S = 10.0  # extra time allowed before the first segment arrives


class FFmpegMissing(RuntimeError):
    """FFmpeg or ffprobe was not found."""


def resolve_tool(name_or_path: str) -> str:
    """Return a full path to an executable, or raise FFmpegMissing with a clear message."""
    found = shutil.which(name_or_path)
    if not found:
        raise FFmpegMissing(
            f"'{name_or_path}' not found. Install FFmpeg and make sure it is on your PATH, "
            "or set camera.ffmpeg_path / camera.ffprobe_path in the config."
        )
    return found


def check_ffmpeg(cfg: CameraConfig) -> tuple[str, str]:
    """Check both tools exist. Returns (ffmpeg, ffprobe) full paths."""
    return resolve_tool(cfg.ffmpeg_path), resolve_tool(cfg.ffprobe_path)


class Capture:
    def __init__(
        self,
        cfg: CameraConfig,
        buffer: SegmentBuffer,
        clock: Clock,
        *,
        realtime: bool = True,
        duration_s: float | None = None,
    ) -> None:
        """realtime=False runs the stand-in source as fast as FFmpeg can (for tests).
        duration_s stops the stand-in after that many seconds of video."""
        self.cfg = cfg
        self.buffer = buffer
        self.clock = clock
        self.realtime = realtime
        self.duration_s = duration_s
        self.ffmpeg = resolve_tool(cfg.ffmpeg_path)
        self._proc: subprocess.Popen | None = None
        self._run_no = 0
        self._index_high = 0
        self._started_mono = 0.0
        self.restarts = 0
        self.last_error: str | None = None
        self.last_frame_utc: datetime | None = None  # snapshot mode: last NEW frame written
        self._last_md5: str | None = None

    # ---- command ----

    def build_command(self, run_no: int, start_number: int) -> list[str]:
        seg = self.cfg.segment_length_s
        csv = self.buffer.buffer_dir / f"list_{run_no:03d}.csv"
        pattern = self.buffer.buffer_dir / f"seg_%06d.{SEGMENT_EXT}"
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning"]
        if self.cfg.snapshot_url:
            # JPEGs arrive on stdin. Wall-clock timestamps, so a gap in frames shows up as a gap
            # in time. Copy mode, no re-encode.
            cmd += ["-use_wallclock_as_timestamps", "1", "-f", "image2pipe", "-c:v", "mjpeg",
                    "-i", "-", "-c", "copy"]
        elif self.cfg.source_url:
            # UNVERIFIED: a real video stream URL on the U1 was not found. Copy mode, no re-encode.
            cmd += ["-i", self.cfg.source_url, "-c", "copy"]
        else:
            if self.realtime:
                cmd += ["-re"]
            if self.duration_s is not None:
                cmd += ["-t", f"{self.duration_s:g}"]
            cmd += ["-f", "lavfi", "-i", standin.lavfi_source(), "-vf", standin.clock_filter()]
            if self.cfg.standin_codec == "h264":
                cmd += [
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-pix_fmt", "yuv420p", "-force_key_frames", f"expr:gte(t,n_forced*{seg:g})",
                ]
            else:
                cmd += ["-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p"]
        cmd += [
            "-f", "segment", "-segment_time", f"{seg:g}", "-segment_format", "matroska",
            "-segment_start_number", str(start_number),
            "-segment_list", str(csv), "-segment_list_type", "csv",
            "-reset_timestamps", "1", str(pattern),
        ]
        return cmd

    # ---- process control ----

    def start(self) -> None:
        """Start FFmpeg. Records the wall clock for this run."""
        if self.running:
            raise RuntimeError("capture already running")
        self._run_no += 1
        self._index_high = max(self._index_high, self.buffer.next_index() - 1)
        start_number = self._index_high + 1
        cmd = self.build_command(self._run_no, start_number)
        log_path = self.buffer.buffer_dir / f"ffmpeg_{self._run_no:03d}.log"
        start_utc = self.clock.now_utc()
        self.buffer.register_run(self.buffer.buffer_dir / f"list_{self._run_no:03d}.csv", start_utc)
        self._log_fh = log_path.open("wb")
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=self._log_fh, stderr=subprocess.STDOUT
        )
        self._started_mono = self.clock.monotonic()
        log.info("capture started (run %d, pid %s)", self._run_no, self._proc.pid)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Ask FFmpeg to finish nicely (send 'q'), then kill it if it does not."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                if not self.cfg.snapshot_url:
                    proc.stdin.write(b"q")  # snapshot mode: closing stdin below ends the input
                    proc.stdin.flush()
                else:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._close_files()
        self.buffer.refresh()

    def _close_files(self) -> None:
        proc, fh = self._proc, getattr(self, "_log_fh", None)
        if proc is not None and proc.stdin:
            try:
                proc.stdin.close()
            except OSError:
                pass
        if fh:
            fh.close()

    async def run_to_completion(self) -> int:
        """Start and wait until FFmpeg exits by itself (stand-in with duration_s)."""
        self.start()
        rc = await asyncio.to_thread(self._proc.wait)
        self._close_files()
        self.buffer.refresh()
        return rc

    async def poll_snapshots(self, stop_event: asyncio.Event) -> None:
        """Snapshot mode: GET the JPEG and feed NEW frames to FFmpeg. Repeats of the same
        picture (viewer closed, file not updating) are not written, so a gap stays a gap.

        Read only: this only issues HTTP GET.
        """
        async with httpx.AsyncClient(timeout=5.0) as client:
            while not stop_event.is_set():
                try:
                    r = await client.get(self.cfg.snapshot_url)
                    if r.status_code == 200 and r.content:
                        md5 = hashlib.md5(r.content).hexdigest()
                        if md5 != self._last_md5 and self.running:
                            await asyncio.to_thread(self._write_frame, r.content)
                            self._last_md5 = md5
                            self.last_frame_utc = self.clock.now_utc()
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    self.last_error = f"snapshot poll failed: {exc!r}"
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.cfg.snapshot_poll_s)
                except asyncio.TimeoutError:
                    pass

    def _write_frame(self, data: bytes) -> None:
        stdin = self._proc.stdin if self._proc else None
        if stdin is None:
            return
        stdin.write(data)
        stdin.flush()

    async def supervise(self, stop_event: asyncio.Event, poll_s: float = 0.5) -> None:
        """Keep capture alive. Restart with backoff if it exits, kill it if it stalls.

        Never raises. A dead camera must not take Guardian down (spec section 7.3).
        """
        backoff = 1.0
        seg = self.cfg.segment_length_s
        last_count, last_progress = -1, self.clock.monotonic()
        poller = asyncio.create_task(self.poll_snapshots(stop_event)) if self.cfg.snapshot_url else None
        while not stop_event.is_set():
            try:
                if not self.running:
                    if self._proc is not None:
                        rc = self.returncode
                        self._close_files()
                        if self.duration_s is not None and rc == 0:
                            return  # stand-in finished normally
                        self.last_error = f"ffmpeg exited with code {rc}"
                        log.warning("%s, restarting in %.0fs", self.last_error, backoff)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        self.restarts += 1
                    self.start()
                    last_count, last_progress = -1, self.clock.monotonic()
                self.buffer.refresh()
                count = len(self.buffer.all_segments())
                now = self.clock.monotonic()
                if count != last_count:
                    last_count, last_progress = count, now
                    backoff = 1.0
                elif not self.cfg.snapshot_url and now - last_progress > 3 * seg + STARTUP_GRACE_S:
                    # (snapshot mode: no new frames just means nobody is viewing, not a stall)
                    self.last_error = "no new segment, capture stalled"
                    log.warning(self.last_error)
                    self.stop(timeout_s=1.0)
                    last_progress = now
            except Exception:  # noqa: BLE001
                log.exception("capture supervisor error")
                await asyncio.sleep(1.0)
            await asyncio.sleep(poll_s)
        if poller:
            await poller
        self.stop()
