"""Snapshot capture for cameras that only give a JPEG now and then (the U1, verified on one printer).

Each new JPEG is stored as its own file with its exact arrival time. No FFmpeg runs while
capturing, so it costs almost no CPU. A clip is built later from the frames with explicit
per-frame durations, so clip time equals real time (it is a slideshow, about 1 frame a second).

A frame "covers" the time from its arrival to the next frame's arrival. If the next frame is
more than max_frame_gap_s later (viewer closed, camera off), the frame only covers HOLD_S and
the rest is a real gap, which the incident manager reports as missing footage.

Read only: this only issues HTTP GET.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from guardian.camera.segment_buffer import Segment
from guardian.clock import Clock
from guardian.config import CameraConfig

log = logging.getLogger(__name__)

HOLD_S = 1.2  # how long a lone frame counts as covering time
MAX_FRAME_BYTES = 10_000_000


@dataclass(frozen=True)
class _Frame:
    index: int
    path: Path
    utc: datetime


class FrameBuffer:
    """Same interface as SegmentBuffer, so the incident manager does not care which one it has.
    Each frame is one "segment"."""

    def __init__(self, buffer_dir: Path, max_frame_gap_s: float, clock: Clock, hold_s: float = HOLD_S) -> None:
        self.buffer_dir = Path(buffer_dir)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.max_frame_gap_s = max_frame_gap_s
        self.hold_s = hold_s
        self.clock = clock
        self._frames: list[_Frame] = []
        self._high = 0
        self._first_utc: datetime | None = None

    # ---- writing ----

    def add_frame(self, data: bytes, utc: datetime) -> Path:
        self._high += 1
        path = self.buffer_dir / f"frame_{self._high:08d}.jpg"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # the file only appears whole
        self._frames.append(_Frame(self._high, path, utc))
        self._first_utc = self._first_utc or utc
        return path

    # ---- same interface as SegmentBuffer ----

    def refresh(self) -> int:
        return 0  # frames are added directly

    @property
    def first_run_start(self) -> datetime | None:
        return self._first_utc

    def all_segments(self) -> list[Segment]:
        """Frames whose coverage is final. The newest frame is not final until the next one
        arrives, or until it has been alone for max_frame_gap_s."""
        now = self.clock.now_utc()
        out: list[Segment] = []
        for i, f in enumerate(self._frames):
            nxt = self._frames[i + 1].utc if i + 1 < len(self._frames) else None
            if nxt is not None:
                end = nxt if (nxt - f.utc).total_seconds() <= self.max_frame_gap_s else f.utc + timedelta(seconds=self.hold_s)
            elif (now - f.utc).total_seconds() >= self.max_frame_gap_s:
                end = f.utc + timedelta(seconds=self.hold_s)
            else:
                continue
            base = self._first_utc or f.utc
            out.append(Segment(f.index, f.path, (f.utc - base).total_seconds(), (end - base).total_seconds(),
                               f.utc, end))
        return out

    def segments_between(self, start: datetime, end: datetime) -> list[Segment]:
        now = self.clock.now_utc()
        return [s for s in self.all_segments() if s.end_utc <= now and s.end_utc > start and s.start_utc < end]

    @staticmethod
    def pin_to(segments: list[Segment], dest_dir: Path):
        from guardian.camera.segment_buffer import SegmentBuffer
        return SegmentBuffer.pin_to(segments, dest_dir)

    def rolling_delete(self, keep_s: float) -> int:
        cutoff = self.clock.now_utc() - timedelta(seconds=keep_s)
        keep, removed = [], 0
        for f in self._frames:
            if f.utc < cutoff:
                try:
                    f.path.unlink(missing_ok=True)
                    removed += 1
                except OSError as exc:
                    log.warning("could not delete %s: %s", f.path, exc)
                    keep.append(f)
            else:
                keep.append(f)
        self._frames = keep
        return removed

    def next_index(self) -> int:
        return self._high + 1

    def clear_files(self) -> None:
        for p in [*self.buffer_dir.glob("frame_*.jpg"), *self.buffer_dir.glob("frame_*.tmp")]:
            p.unlink(missing_ok=True)
        self._frames.clear()
        self._first_utc = None


def looks_like_jpeg(data: bytes) -> bool:
    return 0 < len(data) <= MAX_FRAME_BYTES and data[:2] == b"\xff\xd8"


class SnapshotCapture:
    """Polls the snapshot URL and stores each NEW picture. Repeats of the same picture
    (nobody viewing, file not updating) are not stored, so a gap stays a gap."""

    def __init__(self, cfg: CameraConfig, buffer: FrameBuffer, clock: Clock, active=None) -> None:
        self.cfg, self.buffer, self.clock = cfg, buffer, clock
        self.active = active  # optional callable. Frames are only stored while it returns True.
        self.last_frame_utc: datetime | None = None
        self.last_frame_bytes: bytes | None = None  # newest stored picture, for the live view and AI checks
        self.last_error: str | None = None
        self.restarts = 0
        self._running = False
        self._last_md5: str | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def supervise(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set. Never raises, so a dead camera cannot take Guardian down."""
        self._running = True
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                while not stop.is_set():
                    try:
                        await self._poll_once(client)
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = f"snapshot poll failed ({type(exc).__name__})"
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.cfg.snapshot_poll_s)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self._running = False

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        r = await client.get(self.cfg.snapshot_url)
        if r.status_code != 200 or not looks_like_jpeg(r.content):
            self.last_error = f"snapshot answered {r.status_code} with no usable picture"
            return
        self.last_error = None
        md5 = hashlib.md5(r.content).hexdigest()
        if md5 == self._last_md5:
            return  # same picture again: nobody is viewing
        self._last_md5 = md5
        if self.active is not None and not self.active():
            return
        now = self.clock.now_utc()
        await asyncio.to_thread(self.buffer.add_frame, r.content, now)
        self.last_frame_utc = now
        self.last_frame_bytes = r.content
