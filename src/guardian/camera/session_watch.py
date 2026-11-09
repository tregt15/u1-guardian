"""Watches the printer's camera session messages. Read only: Guardian cannot start or keep the camera on.

Seen on a real U1 (two prints, see docs/u1-discovery-notes.md):

- `notify_camera_monitoring_to_stop` `[{"remain": N}]` about once a second, N counting down from about 50 to 0.
- then `notify_camera_status_change` `[{"monitor_domain": "lan", "monitoring": false, ...}]`.
- `notify_camera_status_change` with `monitoring: true` when a viewer turns the camera on.

UNVERIFIED: what happens to the countdown when a viewer renews the session in time. Guardian assumes the countdown
is over when the messages stop coming. Also, a short lapse seen in the printer's own log (2026-11-09 06:18) sent no
message at all, so these messages do not report every gap. Frame times stay the honest record of footage.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from guardian.bus import EventBus
from guardian.clock import Clock, to_iso

log = logging.getLogger(__name__)

COUNTDOWN_STALE_S = 5.0  # countdown messages came every 1 to 3 s on the real U1

# async (event_type, metadata) -> anything. Adds a non-trigger event to an open incident, if any.
NoteFn = Callable[[str, dict], Awaitable[Any]]


class CameraSessionWatch:
    def __init__(self, bus: EventBus, clock: Clock, note: NoteFn | None = None) -> None:
        self.clock = clock
        self.note = note
        self.monitoring: bool | None = None  # None: no message seen since Guardian started
        self.domain: str | None = None
        self.changed_utc: datetime | None = None
        self._remain: float | None = None
        self._remain_at: datetime | None = None
        self._warned = False
        bus.subscribe("printer.message", self._on_message)

    # ---- state ----

    def stops_at(self) -> datetime | None:
        """When the printer said the camera will stop. None if no countdown is running."""
        if self._remain is None or self._remain_at is None:
            return None
        if (self.clock.now_utc() - self._remain_at).total_seconds() > COUNTDOWN_STALE_S:
            return None  # messages stopped without a stop: renewed, or lost. Not known which.
        return self._remain_at + timedelta(seconds=self._remain)

    def status(self) -> dict[str, Any]:
        stop = self.stops_at()
        left = max(0.0, (stop - self.clock.now_utc()).total_seconds()) if stop else None
        return {
            "session": None if self.monitoring is None else ("on" if self.monitoring else "off"),
            "session_domain": self.domain,
            "session_changed_utc": to_iso(self.changed_utc) if self.changed_utc else None,
            "stops_in_s": round(left) if left is not None else None,
            "stops_at_utc": to_iso(stop) if stop else None,
        }

    # ---- input ----

    async def _on_message(self, topic: str, payload: dict) -> None:
        method, params = payload.get("method"), payload.get("params")
        if not (isinstance(params, list) and params and isinstance(params[0], dict)):
            return
        item = params[0]
        now = payload.get("recv_utc") or self.clock.now_utc()
        if method == "notify_camera_monitoring_to_stop":
            remain = item.get("remain")
            if not isinstance(remain, (int, float)):
                return
            fresh = self.stops_at() is None
            self._remain, self._remain_at = float(remain), now
            if fresh or not self._warned:
                self._warned = True
                log.warning("the printer says the camera stops in %s s", remain)
                await self._note("camera_stop_warning", {
                    "remain_s": remain,
                    "summary": f"The printer said the camera will stop in {remain} s unless a viewer renews it.",
                })
        elif method == "notify_camera_status_change":
            on = item.get("monitoring")
            if not isinstance(on, bool):
                return
            changed = on != self.monitoring
            self.monitoring, self.domain, self.changed_utc = on, item.get("monitor_domain"), now
            self._remain = self._remain_at = None
            self._warned = False
            if changed:
                log.info("printer camera session %s", "on" if on else "off")
                await self._note("camera_started" if on else "camera_stopped", {
                    "domain": item.get("monitor_domain"),
                    "summary": "The printer turned the camera on." if on else
                               "The printer turned the camera off. No footage until a viewer turns it on again.",
                })

    async def _note(self, event_type: str, meta: dict) -> None:
        if self.note is None:
            return
        try:
            await self.note(event_type, meta)
        except Exception:  # noqa: BLE001 - a note must never stop monitoring
            log.exception("could not add %s to the incident", event_type)
