"""Maps raw printer events to incident triggers.

Every rule here is based on a message seen on a real U1 (2026-11-08, Moonraker 1.4.1),
except where marked UNVERIFIED. See docs/u1-discovery-notes.md.

NOT built, because no channel was found yet (detection catalog stays Unverified):
spaghetti, bed adhesion, air printing, bed obstruction, filament runout. Guardian does
not pretend to detect them.

Read only. This only turns events into report_incident calls.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from guardian.bus import EventBus
from guardian.printer.state_tracker import StateTracker

log = logging.getLogger(__name__)

# async (event_type, source, severity, dedup_key, metadata) -> anything
ReportFn = Callable[..., Awaitable[Any]]


class Detector:
    def __init__(self, bus: EventBus, tracker: StateTracker, report: ReportFn) -> None:
        self.tracker = tracker
        self.report = report
        self._prev_state: str | None = None
        bus.subscribe("printer.snapshot", self._on_snapshot)
        bus.subscribe("printer.message", self._on_message)

    # ---- inputs ----

    async def _on_snapshot(self, topic: str, payload: dict) -> None:
        # The tracker (subscribed first) has already merged the snapshot.
        # If the state changed during a connection gap, treat it like a normal change.
        await self._check_print_state()

    async def _on_message(self, topic: str, payload: dict) -> None:
        method, params = payload.get("method"), payload.get("params")
        if not isinstance(params, list):
            return
        if method == "notify_exception_notification":
            for item in params:
                if isinstance(item, dict):
                    await self._exception(item)
        elif method == "notify_gcode_response":
            for line in params:
                if isinstance(line, str) and line.startswith("!!"):
                    await self._fire("klipper_error_response", "klipper", "warning", line,
                                     {"message": line})
        elif method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
            # Standard Moonraker notifications. UNVERIFIED on the U1 (never seen yet).
            await self._fire(method.removeprefix("notify_"), "klipper", "critical", None, {})
        elif method == "notify_status_update":
            await self._check_print_state()

    # ---- rules ----

    async def _exception(self, ex: dict) -> None:
        # Seen: {id: 531, code: 14, level: 3} and {id: 525, code: 32, level: 2}.
        # What `level` means is UNVERIFIED, so every exception is a warning here.
        # The print_state rule below raises severity when the print actually stopped.
        key = f"{ex.get('id')}:{ex.get('code')}:{ex.get('index')}"
        await self._fire("printer_exception", "moonraker", "warning", key, {
            "exception_id": ex.get("id"), "code": ex.get("code"), "level": ex.get("level"),
            "index": ex.get("index"), "message": ex.get("message"),
        })

    async def _check_print_state(self) -> None:
        state = self.tracker.print_state
        prev, self._prev_state = self._prev_state, state
        if state is None or state == prev or prev is None:
            return  # first sighting is not a change
        ps = self.tracker.status.get("print_stats", {})
        exception = ps.get("exception") or {}
        if state == "paused":
            if exception:
                await self._fire("print_paused_with_exception", "moonraker", "critical",
                                 f"paused:{exception.get('id')}:{exception.get('code')}",
                                 {"exception": exception, "message": ps.get("message")})
            elif prev == "printing":
                # A person may have paused it. Keep it, but it is not a failure.
                await self._fire("print_paused", "moonraker", "info", "paused", {})
        elif state == "error":
            await self._fire("print_error", "moonraker", "critical", "error",
                             {"exception": exception, "message": ps.get("message")})
        elif state == "cancelled" and prev in ("printing", "paused"):
            # UNVERIFIED on the U1: the exact state names. Standard Moonraker names used.
            await self._fire("print_cancelled", "moonraker", "warning", "cancelled", {})

    async def _fire(self, event_type: str, source: str, severity: str, key: str | None,
                    extra: dict) -> None:
        metadata = {**self.tracker.incident_metadata(), **extra}
        try:
            await self.report(event_type, source, severity, key, metadata)
        except Exception:  # noqa: BLE001 - a failed report must not stop monitoring
            log.exception("could not report incident %s", event_type)
