"""Keeps the latest printer state from snapshots and status updates.

Only reads what the bus delivers. The object names below were seen on one real U1
(print_stats, toolhead, machine_state_manager). Anything else is UNVERIFIED.
"""

from __future__ import annotations

from typing import Any

from guardian.bus import EventBus


class StateTracker:
    def __init__(self, bus: EventBus) -> None:
        self.status: dict[str, dict[str, Any]] = {}
        bus.subscribe("printer.snapshot", self._on_snapshot)
        bus.subscribe("printer.message", self._on_message)

    def _merge(self, update: dict) -> None:
        for name, fields in update.items():
            if isinstance(fields, dict):
                self.status.setdefault(name, {}).update(fields)

    def _on_snapshot(self, topic: str, payload: dict) -> None:
        self.status = {}  # do not trust state from before a gap
        self._merge(payload.get("status", {}))

    def _on_message(self, topic: str, payload: dict) -> None:
        if payload.get("method") == "notify_status_update":
            params = payload.get("params")
            if isinstance(params, list) and params and isinstance(params[0], dict):
                self._merge(params[0])

    # ---- convenience ----

    @property
    def print_state(self) -> str | None:
        return self.status.get("print_stats", {}).get("state")

    def incident_metadata(self) -> dict[str, Any]:
        """Fields stored on an incident (spec 6.7: filename, progress, toolhead)."""
        ps = self.status.get("print_stats", {})
        meta: dict[str, Any] = {}
        if ps.get("state") not in ("printing", "paused"):
            # Between prints print_stats still holds the last print's file and progress. Seen 2026-11-10: an email
            # about a refused start showed the old file at 100%. So only say the tool head then.
            if self.status.get("toolhead", {}).get("extruder"):
                meta["active_toolhead"] = self.status["toolhead"]["extruder"]
            return meta
        if ps.get("filename"):
            meta["print_filename"] = ps["filename"]
        info = ps.get("info") or {}
        total, cur = info.get("total_layer"), info.get("current_layer")
        if total and cur is not None:
            meta["print_progress"] = round(100.0 * cur / total, 1)  # layer based, not time based
        if self.status.get("toolhead", {}).get("extruder"):
            meta["active_toolhead"] = self.status["toolhead"]["extruder"]
        return meta
