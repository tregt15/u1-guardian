"""Incident Manager (spec section 6.5): trigger, dedupe, merge, finalize.

Times:
- pre_start = first trigger - pre_trigger_s
- deadline  = last trigger + post_trigger_s, but never past
              first trigger + max_incident_s
- finalize when now >= deadline + segment length (last segment must close)

Recording status is honest. `complete` only if the pinned segments cover the
whole window with no gap, the clip was built, and ffprobe agrees with the
segments. Anything else is `partial`, `missing` or `failed`, with a note.

actual_pre_s  = first trigger  - start of the clip
actual_post_s = end of the clip - last trigger
Copy mode cuts on segment edges, so these are usually a bit longer than asked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from guardian.blackbox.assembler import AssemblyError, assemble, make_playable, probe
from guardian.bus import EventBus
from guardian.camera.segment_buffer import Segment, SegmentBuffer
from guardian.clock import Clock, from_iso, to_iso
from guardian.config import Config
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

log = logging.getLogger(__name__)

TOL_S = 0.5  # gaps and edge errors smaller than this are ignored
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass(frozen=True)
class TriggerResult:
    action: str  # "created" | "merged" | "duplicate"
    uid: str
    incident_id: int


@dataclass
class _Open:
    id: int
    uid: str
    dir: Path
    pre_start: datetime
    first_trigger: datetime
    last_trigger: datetime
    deadline: datetime
    severity: str
    pinned: dict[int, tuple[Segment, Path]] = field(default_factory=dict)
    pin_failed: dict[int, Segment] = field(default_factory=dict)


@dataclass
class _Seen:
    event_id: int
    last_seen: datetime


class IncidentManager:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        buffer: SegmentBuffer,
        clock: Clock,
        ffmpeg: str,
        ffprobe: str,
        bus: EventBus | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.buffer = buffer
        self.clock = clock
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.bus = bus
        self._open: list[_Open] = []
        self._seen: dict[tuple, _Seen] = {}
        self._lock = asyncio.Lock()

    # ---- paths ----

    def incident_dir(self, incident_id: int, first_trigger: datetime) -> Path:
        return incident_dir(self.cfg.storage, incident_id, first_trigger)

    @property
    def open_count(self) -> int:
        return len(self._open)

    # ---- trigger ----

    async def report_incident(
        self,
        event_type: str,
        source: str,
        severity: str = "warning",
        dedup_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> TriggerResult:
        """The unified trigger interface (spec 6.7). `at` is for simulation; default is now.
        Any extra metadata is stored as is. Adding a trigger source needs no change here."""
        if severity not in SEVERITY_RANK:
            raise ValueError(f"severity must be one of {sorted(SEVERITY_RANK)}")
        async with self._lock:
            result = await self._report_locked(event_type, source, severity, dedup_key, metadata or {}, at)
        topic = {"created": "incident.created", "merged": "incident.merged",
                 "duplicate": "incident.duplicate"}[result.action]
        if self.bus:
            await self.bus.publish(topic, {"uid": result.uid, "event_type": event_type,
                                           "source": source, "severity": severity})
        return result

    async def _report_locked(self, event_type, source, severity, dedup_key, metadata, at) -> TriggerResult:
        bb = self.cfg.blackbox
        t = at or self.clock.now_utc()
        key = (event_type, source, dedup_key)
        event_meta = {**metadata, "dedup_key": dedup_key}

        # 1. Duplicate: same type, source and key inside the duplicate window.
        seen = self._seen.get(key)
        if seen and (t - seen.last_seen) <= timedelta(seconds=bb.duplicate_window_s):
            self.db.bump_event_repeat(seen.event_id)
            seen.last_seen = max(seen.last_seen, t)
            inc = self._find_open_for(t)
            row = self.db.incident_of_event(seen.event_id)
            return TriggerResult("duplicate", row["uid"], row["id"])

        # 2. Merge into an open incident whose deadline has not passed.
        inc = self._find_open_for(t)
        if inc is not None:
            event_id = self.db.add_event(inc.id, ts_utc=t, event_type=event_type, source=source,
                                         severity=severity, is_trigger=True, metadata=event_meta)
            cap = inc.first_trigger + timedelta(seconds=bb.max_incident_s)
            inc.deadline = min(max(inc.deadline, t + timedelta(seconds=bb.post_trigger_s)), cap)
            inc.last_trigger = max(inc.last_trigger, t)
            fields: dict[str, Any] = {"last_trigger_utc": to_iso(inc.last_trigger)}
            if SEVERITY_RANK[severity] > SEVERITY_RANK[inc.severity]:
                inc.severity = severity
                fields["severity"] = severity
            self.db.update_incident(inc.id, **fields)
            self._seen[key] = _Seen(event_id, t)
            return TriggerResult("merged", inc.uid, inc.id)

        # 3. New incident.
        uid = self.db.next_uid(t)
        pre_start = t - timedelta(seconds=bb.pre_trigger_s)
        columns = {k: metadata[k] for k in ("print_filename", "print_progress", "active_toolhead")
                   if k in metadata}
        iid = self.db.create_incident(
            uid=uid, event_type=event_type, source=source, severity=severity,
            start_time_utc=to_iso(pre_start), first_trigger_utc=to_iso(t), last_trigger_utc=to_iso(t),
            **columns,
        )
        event_id = self.db.add_event(iid, ts_utc=t, event_type=event_type, source=source,
                                     severity=severity, is_trigger=True, metadata=event_meta)
        inc = _Open(
            id=iid, uid=uid, dir=self.incident_dir(iid, t), pre_start=pre_start, first_trigger=t,
            last_trigger=t, deadline=t + timedelta(seconds=bb.post_trigger_s), severity=severity,
        )
        self._open.append(inc)
        self._seen[key] = _Seen(event_id, t)
        self._prune_seen(t)
        self.buffer.refresh()
        await self._pin_new(inc)
        return TriggerResult("created", uid, iid)

    async def add_note(self, event_type: str, source: str, metadata: dict[str, Any] | None = None,
                       at: datetime | None = None) -> str | None:
        """Add a non-trigger event to the open incident covering this moment, if there is one.
        It shows in the timeline but does not extend or create an incident. Returns the uid or None."""
        async with self._lock:
            t = at or self.clock.now_utc()
            inc = self._find_open_for(t)
            if inc is None:
                return None
            self.db.add_event(inc.id, ts_utc=t, event_type=event_type, source=source, severity="info",
                              is_trigger=False, metadata=metadata or {})
            return inc.uid

    def _find_open_for(self, t: datetime) -> _Open | None:
        candidates = [i for i in self._open if t <= i.deadline]
        return max(candidates, key=lambda i: i.first_trigger) if candidates else None

    def _prune_seen(self, now: datetime) -> None:
        if len(self._seen) > 1000:
            old = now - timedelta(seconds=max(60.0, 10 * self.cfg.blackbox.duplicate_window_s))
            self._seen = {k: v for k, v in self._seen.items() if v.last_seen >= old}

    # ---- pinning ----

    async def _pin_new(self, inc: _Open) -> None:
        """Copy any closed segments in the incident window that are not pinned yet."""
        wanted = [
            s for s in self.buffer.segments_between(inc.pre_start, inc.deadline)
            if s.index not in inc.pinned and s.index not in inc.pin_failed
        ]
        if not wanted:
            return
        copied, failed = await asyncio.to_thread(
            self.buffer.pin_to, wanted, inc.dir / "segments"
        )
        for seg, dest in copied:
            inc.pinned[seg.index] = (seg, dest)
        for seg in failed:
            inc.pin_failed[seg.index] = seg

    # ---- periodic work ----

    async def tick(self) -> list[str]:
        """Call about once a second. Pins new segments, finalizes due incidents, deletes old
        segments. Returns uids that were finalized."""
        done: list[str] = []
        async with self._lock:
            self.buffer.refresh()
            now = self.clock.now_utc()
            # Wait for the last segment to close. A snapshot frame is only final once the next
            # frame arrives, or after max_frame_gap_s alone, so wait that long too.
            seg_len = timedelta(seconds=self.cfg.camera.segment_length_s
                                + (self.cfg.camera.max_frame_gap_s if self.cfg.camera.snapshot_url else 0.0))
            for inc in list(self._open):
                await self._pin_new(inc)
                if now >= inc.deadline + seg_len:
                    try:
                        await self._finalize(inc)
                    except Exception:  # noqa: BLE001 - one bad incident must not stop the others
                        log.exception("finalize crashed for %s", inc.uid)
                        self.db.update_incident(
                            inc.id, recording_status="failed",
                            recording_notes="Guardian hit an internal error while finalizing.",
                        )
                    self._open.remove(inc)
                    done.append(inc.uid)
            keep = self.cfg.blackbox.pre_trigger_s + 2 * self.cfg.camera.segment_length_s
            await asyncio.to_thread(self.buffer.rolling_delete, keep)
        if self.bus:
            for uid in done:
                row = self.db.get_incident_by_uid(uid)
                await self.bus.publish("incident.finalized", {"uid": uid, "status": row["recording_status"]})
        return done

    # ---- finalize ----

    async def _finalize(self, inc: _Open) -> None:
        segs = [s for s, _ in sorted(inc.pinned.values(), key=lambda p: p[0].start_utc)]
        paths = [p for _, p in sorted(inc.pinned.values(), key=lambda p: p[0].start_utc)]
        notes: list[str] = []
        status = "complete"
        clip_rel: str | None = None
        fields: dict[str, Any] = {}

        if not segs:
            status = "missing"
            if self.cfg.camera.snapshot_url:
                notes.append("No video frames were available for this window. The U1 camera only gives "
                             "pictures while the Snapmaker app is showing the camera view.")
            else:
                notes.append("No video segments were available for this window (camera down or buffer empty).")
            notes += self._camera_session_notes(inc)
        else:
            notes += self._coverage_notes(inc, segs)
            if notes:
                notes += self._camera_session_notes(inc)
            if inc.pin_failed:
                notes.append(f"{len(inc.pin_failed)} segment(s) could not be copied from the buffer.")
            if notes:
                status = "partial"
            try:
                frames = paths[0].suffix.lower() in (".jpg", ".jpeg")  # snapshot camera: one JPEG per segment
                durations = [s.duration_s for s in segs] if frames else None
                clip = await asyncio.to_thread(assemble, self.ffmpeg, self.ffprobe, paths, inc.dir, durations)
                info = await asyncio.to_thread(probe, self.ffprobe, clip)
                expected = sum(s.duration_s for s in segs)
                if info.duration_s < expected - TOL_S:
                    status = "partial"
                    notes.append(
                        f"Clip is {info.duration_s:.1f}s but segments add up to {expected:.1f}s. "
                        "A segment may be damaged."
                    )
                if info.video_codec != "h264" and self.cfg.blackbox.make_playable_copy:
                    try:
                        clip = await asyncio.to_thread(make_playable, self.ffmpeg, clip)
                        playable = await asyncio.to_thread(probe, self.ffprobe, clip)
                        if abs(playable.duration_s - info.duration_s) > 1.0:
                            raise AssemblyError("playable copy has a different length")
                    except AssemblyError as exc:
                        # The footage itself is fine. Only the browser copy failed.
                        notes.append(f"Browser-playable copy failed ({exc}). The original clip is kept.")
                        clip = next(inc.dir.glob("clip_original.mkv"), None) or next(inc.dir.glob("clip.mkv"))
                clip_rel = clip.relative_to(self.cfg.storage.resolved_data_dir()).as_posix()
                shutil.rmtree(inc.dir / "segments", ignore_errors=True)  # kept only if assembly fails
            except AssemblyError as exc:
                status = "failed"
                notes.append(f"Clip assembly failed: {exc}. Raw segments kept in {inc.dir / 'segments'}.")
            clip_start, clip_end = segs[0].start_utc, segs[-1].end_utc
            fields.update(
                start_time_utc=to_iso(clip_start), end_time_utc=to_iso(clip_end),
                actual_pre_s=round(max(0.0, (inc.first_trigger - clip_start).total_seconds()), 3),
                actual_post_s=round(max(0.0, (clip_end - inc.last_trigger).total_seconds()), 3),
            )

        self.db.update_incident(
            inc.id, recording_status=status, recording_notes=" ".join(notes) or None,
            recording_path=clip_rel, **fields,
        )
        self._write_report(inc)

    def _coverage_notes(self, inc: _Open, segs: list[Segment]) -> list[str]:
        notes: list[str] = []
        # Same strict rule for every camera. In snapshot mode a frame already covers the time up to
        # the next frame, so normal 1 frame per second spacing does not show up as a gap.
        tol = TOL_S
        lead = (segs[0].start_utc - inc.pre_start).total_seconds()
        if lead > tol:
            notes.append(
                f"Pre-roll is {lead:.1f}s short. Footage starts {lead:.1f}s after the requested "
                "start (buffer not full yet, or camera was down)."
            )
        tail = (inc.deadline - segs[-1].end_utc).total_seconds()
        if tail > tol:
            notes.append(f"Post-roll is {tail:.1f}s short. Footage ends before the requested end.")
        for a, b in zip(segs, segs[1:]):
            gap = (b.start_utc - a.end_utc).total_seconds()
            if gap > tol:
                notes.append(f"Gap of {gap:.1f}s in the footage after {to_iso(a.end_utc)}.")
        return notes

    def _camera_session_notes(self, inc: _Open) -> list[str]:
        """Say why footage is missing when the printer told us it stopped the camera (verified message
        on the U1, see camera/session_watch.py). Only added when there is already a coverage problem."""
        out = []
        for e in self.db.list_events(inc.id):
            if e["event_type"] == "camera_stopped":
                out.append(f"The printer turned the camera off at {e['ts_utc']}.")
            elif e["event_type"] == "camera_started":
                out.append(f"The printer turned the camera on at {e['ts_utc']}.")
        return out

    def _write_report(self, inc: _Open) -> None:
        """Small report.json next to the clip. The full diagnostic report comes later."""
        row = self.db.get_incident(inc.id)
        events = [dict(e) for e in self.db.list_events(inc.id)]
        for e in events:
            e["metadata"] = json.loads(e["metadata"]) if e["metadata"] else None
        try:
            inc.dir.mkdir(parents=True, exist_ok=True)
            (inc.dir / "report.json").write_text(
                json.dumps({"incident": dict(row), "events": events}, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not write report.json for %s: %s", inc.uid, exc)

    # ---- restart recovery ----

    def recover_interrupted(self) -> list[str]:
        """On startup: incidents still marked `recording` were cut off by a restart.
        Mark them `partial` (or `missing` if nothing was saved). Never `complete`."""
        fixed: list[str] = []
        for row in self.db.list_incidents(status="recording"):
            seg_dir = self.incident_dir(row["id"], from_iso(row["first_trigger_utc"])) / "segments"
            saved = sorted(seg_dir.glob("seg_*")) if seg_dir.is_dir() else []
            if saved:
                status = "partial"
                note = (f"Guardian restarted while this incident was recording. {len(saved)} saved "
                        f"segment(s) are in {seg_dir}. Footage after the restart is missing.")
            else:
                status = "missing"
                note = "Guardian restarted while this incident was recording. No footage was saved."
            self.db.update_incident(row["id"], recording_status=status, recording_notes=note)
            fixed.append(row["uid"])
        return fixed
