"""Notification manager: queue, retry, cooldown, log. (spec 6.9)

Guarantees:
- It never blocks or delays recording. Bus handlers only put a job on a queue.
- A failed send is retried (3 tries with backoff) then written to notification_log.
  It never affects the incident.
- One incident sends one "created" message, even when more triggers merge into it. A later
  trigger sends a new message only if it raises the severity.
- Messages are text only. The clip is never attached. Secrets are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from guardian.bus import EventBus
from guardian.clock import Clock, SystemClock, from_iso, to_iso
from guardian.config import NotificationsConfig
from guardian.notify.email import NotifyError
from guardian.storage.db import Database

log = logging.getLogger(__name__)
RANK = {"info": 0, "warning": 1, "critical": 2}


class Channel(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def send(self, subject: str, text: str) -> None: ...


@dataclass
class _Job:
    kind: str  # incident_created | clip_ready | diagnosis
    uid: str
    escalated: bool = False
    subject: str | None = None  # prebuilt message (diagnosis)
    text: str | None = None


class NotificationManager:
    def __init__(self, cfg: NotificationsConfig, db: Database, bus: EventBus, channels: list[Channel],
                 clock: Clock | None = None, *, retry_delays_s: tuple[float, ...] = (2.0, 8.0),
                 diagnosis_coming=None) -> None:
        self.cfg, self.db, self.channels = cfg, db, channels
        self.clock = clock or SystemClock()
        self.retry_delays_s = retry_delays_s  # 3 tries in total
        # (uid) -> True when an AI diagnosis email will follow. Then the "clip ready" email is skipped, because
        # the diagnosis email also says how the footage went. Owner asked for fewer emails (2026-11-11).
        self.diagnosis_coming = diagnosis_coming
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=500)
        self._last_sent: dict[str, datetime] = {}  # event type -> time, for the cooldown
        self._sent_rank: dict[str, int] = {}  # uid -> highest severity already announced
        self._created_sent: set[str] = set()
        bus.subscribe("incident.created", self._on_created)
        bus.subscribe("incident.merged", self._on_merged)
        bus.subscribe("incident.finalized", self._on_finalized)

    # ---- bus handlers: only enqueue, never wait on the network ----

    def _enqueue(self, job: _Job) -> None:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            log.warning("notification queue full, dropping %s for %s", job.kind, job.uid)
            self._log(job.uid, "-", job.kind, "failed", 0, "notification queue was full")

    def _on_created(self, topic: str, p: dict) -> None:
        self._enqueue(_Job("incident_created", p["uid"]))

    def _on_merged(self, topic: str, p: dict) -> None:
        # A merged trigger only matters if it raises the severity (spec 6.9).
        if RANK.get(p.get("severity"), 0) > self._sent_rank.get(p["uid"], 99):
            self._enqueue(_Job("incident_created", p["uid"], escalated=True))

    def _on_finalized(self, topic: str, p: dict) -> None:
        self._enqueue(_Job("clip_ready", p["uid"]))

    async def send_diagnosis(self, uid: str, subject: str, text: str) -> None:
        """Queue an AI diagnosis message for an incident. Same rules: send_on, min_severity, log."""
        self._enqueue(_Job("diagnosis", uid, subject=subject, text=text))

    # ---- worker ----

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                job = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(job)
            except Exception:  # noqa: BLE001 - the worker must survive anything
                log.exception("notification job failed unexpectedly")

    async def drain(self) -> None:
        """Process everything queued right now (tests and shutdown)."""
        while not self._queue.empty():
            await self._process(self._queue.get_nowait())

    async def _process(self, job: _Job) -> None:
        row = self.db.get_incident_by_uid(job.uid)
        if row is None:
            return
        kind = job.kind
        if kind not in self.cfg.send_on:
            return self._skip(job, "not in send_on")
        if RANK[row["severity"]] < RANK[self.cfg.min_severity]:
            return self._skip(job, f"below min_severity ({self.cfg.min_severity})")
        if kind == "incident_created":
            now = self.clock.now_utc()
            last = self._last_sent.get(row["event_type"])
            if not job.escalated and last and (now - last) < timedelta(seconds=self.cfg.cooldown_s):
                return self._skip(job, f"cooldown ({self.cfg.cooldown_s:g}s per event type)")
            subject, text = self._created_message(row, job.escalated)
        elif kind == "diagnosis":
            subject, text = job.subject or "[Guardian] AI diagnosis", job.text or ""
        else:
            if job.uid not in self._created_sent:
                return self._skip(job, "no created message was sent for this incident")
            if self.diagnosis_coming and "diagnosis" in self.cfg.send_on:
                try:
                    coming = self.diagnosis_coming(job.uid)
                except Exception:  # noqa: BLE001
                    coming = False
                if coming:
                    return self._skip(job, "the AI diagnosis email follows and includes the footage status")
            subject, text = self._clip_message(row)
        sent_any = False
        for ch in self.channels:
            if not ch.configured:
                self._log(job.uid, ch.name, kind, "skipped", 0, "channel has no credentials configured")
                continue
            ok = await self._deliver(ch, job, subject, text)
            sent_any = sent_any or ok
        if sent_any and kind == "incident_created":
            self._created_sent.add(job.uid)
            self._last_sent[row["event_type"]] = self.clock.now_utc()
            self._sent_rank[job.uid] = max(self._sent_rank.get(job.uid, -1), RANK[row["severity"]])

    def _skip(self, job: _Job, reason: str) -> None:
        for ch in self.channels:
            self._log(job.uid, ch.name, job.kind, "skipped", 0, reason)

    async def _deliver(self, ch: Channel, job: _Job, subject: str, text: str) -> bool:
        attempts, error = 0, ""
        for i in range(len(self.retry_delays_s) + 1):
            attempts += 1
            try:
                await ch.send(subject, text)
                self._log(job.uid, ch.name, job.kind, "sent", attempts, None)
                return True
            except NotifyError as exc:
                error = str(exc)
                if not exc.retryable or i >= len(self.retry_delays_s):
                    break
            except Exception as exc:  # noqa: BLE001
                error = f"unexpected error ({type(exc).__name__})"
                if i >= len(self.retry_delays_s):
                    break
            await asyncio.sleep(self.retry_delays_s[i])
        self._log(job.uid, ch.name, job.kind, "failed", attempts, error)
        log.warning("%s notification for %s failed: %s", ch.name, job.uid, error)
        return False

    # ---- test button ----

    async def send_test(self, channel: str) -> dict:
        ch = next((c for c in self.channels if c.name == channel), None)
        if ch is None:
            return {"ok": False, "error": f"channel '{channel}' is not enabled"}
        subject = "[Guardian] Test message"
        text = "This is a test message from U1 Guardian. If you can read this, email alerts work."
        try:
            await ch.send(subject, text)
        except NotifyError as exc:
            self._log(None, ch.name, "test", "failed", 1, str(exc))
            return {"ok": False, "error": str(exc)}
        self._log(None, ch.name, "test", "sent", 1, None)
        return {"ok": True}

    # ---- messages (plain text) ----

    def _link(self, uid: str) -> str:
        return f"{self.cfg.base_url.rstrip('/')}/incident?uid={uid}"

    def _created_message(self, row, escalated: bool) -> tuple[str, str]:
        sev = row["severity"].upper()
        what = row["event_type"]
        subject = f"[Guardian] {sev}: {what}" + (f" on {row['print_filename']}" if row["print_filename"] else "")
        if escalated:
            subject = "[Guardian] RAISED to " + subject[len("[Guardian] "):]
        events = self.db.list_events(row["id"])
        summary = what
        for e in events:
            meta = json.loads(e["metadata"]) if e["metadata"] else {}
            if meta.get("message"):
                summary = f"{what}: {meta['message']}"
                break
        lines = [
            "U1 Guardian saved an incident." if not escalated else "U1 Guardian: this incident got more serious.",
            "",
            f"What:       {summary}",
            f"Severity:   {row['severity']}",
            f"Source:     {row['source']}",
            f"When:       {_when(row['first_trigger_utc'])}",
            f"Print file: {row['print_filename'] or 'unknown'}",
            f"Progress:   {_progress(row['print_progress'])}",
            f"Tool head:  {row['active_toolhead'] or 'unknown'}",
            "",
            "Footage: still recording. A second message follows when the clip is ready.",
            f"View: {self._link(row['uid'])}",
        ]
        return subject, "\n".join(lines)

    def _clip_message(self, row) -> tuple[str, str]:
        status = row["recording_status"]
        subject = f"[Guardian] Clip ready ({status.upper()}): {row['event_type']}"
        lines = [
            f"The recording for this incident is finished. Recording status: {status.upper()}.",
            "",
            f"What:           {row['event_type']} ({row['severity']})",
            f"Print file:     {row['print_filename'] or 'unknown'}",
            f"Actual pre-roll:  {_secs(row['actual_pre_s'])}",
            f"Actual post-roll: {_secs(row['actual_post_s'])}",
        ]
        if status != "complete":
            lines += ["", "THE FOOTAGE IS NOT COMPLETE.", row["recording_notes"] or "No reason was recorded."]
        lines += ["", f"View: {self._link(row['uid'])}"]
        return subject, "\n".join(lines)

    # ---- log ----

    def _log(self, uid: str | None, channel: str, kind: str, status: str, attempts: int, error: str | None) -> None:
        iid = None
        if uid:
            row = self.db.get_incident_by_uid(uid)
            iid = row["id"] if row else None
        try:
            self.db.log_notification(iid, to_iso(self.clock.now_utc()), channel, kind, status, attempts, error)
        except Exception:  # noqa: BLE001
            log.exception("could not write notification_log")


def _when(iso: str) -> str:
    return from_iso(iso).strftime("%Y-%m-%d %H:%M:%S UTC")


def _progress(v) -> str:
    return f"{v}% (by layer)" if v is not None else "unknown"


def _secs(v) -> str:
    return f"{v:.1f} s" if v is not None else "unknown"
