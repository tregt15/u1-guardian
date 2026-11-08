"""Rolling segment index, built from FFmpeg's segment list CSV.

FFmpeg writes seg_000001.mkv, seg_000002.mkv, ... and appends one CSV line
per finished segment: `name,start_offset_s,end_offset_s`. Offsets count from
the start of that FFmpeg run. We add the wall-clock time recorded when the
run started to get UTC times (no strftime, no %s, so it works on Windows).

A segment only appears in the index after FFmpeg closed it. Segments that
end after "now" are hidden, so a simulation with a fake clock behaves like
real time.

Pinning means copying segment files out of the buffer into an incident folder,
so rolling deletion can never remove them.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from guardian.clock import Clock

log = logging.getLogger(__name__)

_INDEX_RE = re.compile(r"(\d+)\.[A-Za-z0-9]+$")


@dataclass(frozen=True)
class Segment:
    index: int
    path: Path
    start_s: float
    end_s: float
    start_utc: datetime
    end_utc: datetime

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class _Run:
    csv_path: Path
    start_utc: datetime
    offset: int = 0  # bytes of the CSV already read
    partial: bytes = b""


@dataclass
class SegmentBuffer:
    buffer_dir: Path
    segment_length_s: float
    clock: Clock
    _runs: list[_Run] = field(default_factory=list)
    _segments: dict[int, Segment] = field(default_factory=dict)
    _high: int = 0

    def __post_init__(self) -> None:
        self.buffer_dir = Path(self.buffer_dir)
        self.buffer_dir.mkdir(parents=True, exist_ok=True)

    # ---- building the index ----

    def register_run(self, csv_path: Path, start_utc: datetime) -> None:
        """Call each time FFmpeg is started. start_utc is the wall clock at that moment."""
        self._runs.append(_Run(Path(csv_path), start_utc))

    def refresh(self) -> int:
        """Read new CSV lines. Returns how many segments were added."""
        added = 0
        for run in self._runs:
            added += self._read_run(run)
        return added

    def _read_run(self, run: _Run) -> int:
        try:
            with run.csv_path.open("rb") as fh:
                fh.seek(run.offset)
                data = fh.read()
        except FileNotFoundError:
            return 0
        run.offset += len(data)
        data = run.partial + data
        *lines, run.partial = data.split(b"\n")  # last piece may be a half-written line
        added = 0
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            seg = self._parse_line(line, run)
            if seg is not None:
                self._segments[seg.index] = seg
                self._high = max(self._high, seg.index)
                added += 1
        return added

    def _parse_line(self, line: str, run: _Run) -> Segment | None:
        try:
            name, start, end = line.rsplit(",", 2)
            start_s, end_s = float(start), float(end)
            m = _INDEX_RE.search(name)
            if not m:
                raise ValueError("no index in file name")
        except ValueError:
            log.warning("bad segment list line %r", line)
            return None
        path = self.buffer_dir / Path(name).name
        return Segment(
            index=int(m.group(1)),
            path=path,
            start_s=start_s,
            end_s=end_s,
            start_utc=run.start_utc + timedelta(seconds=start_s),
            end_utc=run.start_utc + timedelta(seconds=end_s),
        )

    # ---- queries ----

    def all_segments(self) -> list[Segment]:
        return sorted(self._segments.values(), key=lambda s: s.start_utc)

    def segments_between(self, start: datetime, end: datetime) -> list[Segment]:
        """Closed segments that overlap [start, end]. Segments ending after now are hidden."""
        now = self.clock.now_utc()
        return [
            s for s in self.all_segments()
            if s.end_utc <= now and s.end_utc > start and s.start_utc < end
        ]

    def last_segment_end(self) -> datetime | None:
        segs = self.all_segments()
        return segs[-1].end_utc if segs else None

    # ---- pinning and deletion ----

    @staticmethod
    def pin_to(segments: list[Segment], dest_dir: Path) -> tuple[list[tuple[Segment, Path]], list[Segment]]:
        """Copy segments into dest_dir. Returns (copied pairs, segments that could not be copied)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        copied: list[tuple[Segment, Path]] = []
        failed: list[Segment] = []
        for seg in segments:
            dest = dest_dir / seg.path.name
            try:
                shutil.copy2(seg.path, dest)
                copied.append((seg, dest))
            except OSError as exc:
                log.warning("could not pin %s: %s", seg.path.name, exc)
                failed.append(seg)
        return copied, failed

    def rolling_delete(self, keep_s: float) -> int:
        """Delete segments that ended more than keep_s before now. Returns the count."""
        cutoff = self.clock.now_utc() - timedelta(seconds=keep_s)
        removed = 0
        for seg in self.all_segments():
            if seg.end_utc < cutoff:
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not delete %s: %s", seg.path, exc)
                    continue
                del self._segments[seg.index]
                removed += 1
        return removed

    def next_index(self) -> int:
        """Next free segment number. Never goes backwards, even after rolling deletion."""
        self._high = max(self._high, max(self._segments, default=0))
        return self._high + 1

    def clear_files(self) -> None:
        """Remove old segment and list files left from an earlier run."""
        for p in self.buffer_dir.glob("seg_*"):
            p.unlink(missing_ok=True)
        for p in self.buffer_dir.glob("list_*.csv"):
            p.unlink(missing_ok=True)
        self._segments.clear()
        self._runs.clear()
