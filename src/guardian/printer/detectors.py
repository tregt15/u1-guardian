"""Maps raw printer events to incident triggers.

Every rule here is based on a message seen on a real U1 (2026-11-08, Moonraker 1.4.1),
except where marked UNVERIFIED. See docs/u1-discovery-notes.md.

Snapmaker's own camera AI (dirty bed, spaghetti, residue, dirty nozzle) is read from
exception 532, found in the printer's source code. See AI_DETECTIONS.
NOT built, because no channel was found yet: bed adhesion, air printing, filament runout.
Guardian does not pretend to detect them.

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

# The printer's own camera AI (Snapmaker `defect_detection.py` on the U1, read over SSH 2026-11-09).
# On a find it raises exception id 532 with these codes and then normally pauses the print itself.
# Read from the printer's source code. Not yet seen happen on a real print, so UNVERIFIED live.
AI_EXCEPTION_ID = 532
AI_DETECTIONS = {
    1: ("bed_not_clear", "The printer's camera check found something on the build plate."),
    2: ("spaghetti_detected", "The printer's camera check saw spaghetti (noodle)."),
    3: ("residue_detected", "The printer's camera check saw residue."),
    4: ("dirty_nozzle_detected", "The printer's camera check saw a dirty nozzle."),
}
ReportFn = Callable[..., Awaitable[Any]]


class Detector:
    def __init__(self, bus: EventBus, tracker: StateTracker, report: ReportFn) -> None:
        self.tracker = tracker
        self.report = report
        self._prev_state: str | None = None
        self._feed_errors: dict[tuple[str, str], Any] = {}
        bus.subscribe("printer.snapshot", self._on_snapshot)
        bus.subscribe("printer.message", self._on_message)

    # ---- inputs ----

    async def _on_snapshot(self, topic: str, payload: dict) -> None:
        # The tracker (subscribed first) has already merged the snapshot.
        # If the state changed during a connection gap, treat it like a normal change.
        await self._check_print_state()
        await self._check_filament_feed(payload.get("status", {}), from_snapshot=True)

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
            if params and isinstance(params[0], dict):
                await self._check_filament_feed(params[0], from_snapshot=False)

    # ---- rules ----

    async def _exception(self, ex: dict) -> None:
        # Seen: {id: 531, code: 14, level: 3} and {id: 525, code: 32, level: 2}.
        # What `level` means is UNVERIFIED, so every exception is a warning here.
        # The print_state rule below raises severity when the print actually stopped.
        key = f"{ex.get('id')}:{ex.get('code')}:{ex.get('index')}"
        if ex.get("id") == AI_EXCEPTION_ID and ex.get("code") in AI_DETECTIONS:
            event_type, summary = AI_DETECTIONS[ex["code"]]
            await self._fire(event_type, "snapmaker_ai", "critical", key, {
                "exception_id": ex.get("id"), "code": ex.get("code"), "level": ex.get("level"),
                "message": ex.get("message"), "summary": summary,
            })
            return
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

    async def _check_filament_feed(self, status: dict, from_snapshot: bool) -> None:
        """Seen on a real U1 (print 1): `filament_feed right` -> `extruder2` -> `channel_error` went from
        "ok" to "wheel_speed" one second before the print paused with exception 525. Only the value
        "ok" is known to mean no error. Other error names are UNVERIFIED but reported as they are."""
        for name, feeder in status.items():
            if not (isinstance(name, str) and name.startswith("filament_feed ") and isinstance(feeder, dict)):
                continue
            for channel, info in feeder.items():
                if not (isinstance(info, dict) and "channel_error" in info):
                    continue
                err = info.get("channel_error")
                key = (name, channel)
                known = key in self._feed_errors
                prev = self._feed_errors.get(key)
                self._feed_errors[key] = err
                # A snapshot is a baseline the first time. Updates only arrive when something changed.
                if err in (None, "ok") or err == prev or (from_snapshot and not known):
                    continue
                tool = "T0" if channel == "extruder0" else ("T" + channel.removeprefix("extruder"))
                await self._fire("filament_feed_error", "moonraker", "warning", f"{name}.{channel}:{err}", {
                    "feeder": name, "channel": channel, "tool": tool, "error": err,
                    "channel_state": info.get("channel_state"),
                    "summary": f"Filament feed for {tool} reports error '{err}' (step: {info.get('channel_state')}).",
                })

    async def _fire(self, event_type: str, source: str, severity: str, key: str | None,
                    extra: dict) -> None:
        metadata = {**self.tracker.incident_metadata(), **extra}
        try:
            await self.report(event_type, source, severity, key, metadata)
        except Exception:  # noqa: BLE001 - a failed report must not stop monitoring
            log.exception("could not report incident %s", event_type)
