"""Keeps the U1 camera pictures coming by asking the printer to start its LAN camera monitor.

This is the one thing Guardian sends that changes printer state (owner approved, 2026-11-09).
It only sends `camera.start_monitor` with domain "lan" (see printer/moonraker_client.py). It never
sends a stop, so it cannot cut off the Snapmaker app or Orca.

Verified on a real U1 (2026-11-09): pictures start about 2 s after the request, about 2 per second.
No cloud upload line appeared in the printer log for a LAN session (the app's WAN sessions do upload).
That no picture leaves the printer is UNVERIFIED beyond that log.

When it sends (only while `wanted`, and at most once per cooldown):
- the printer says the camera is off, or Guardian has not heard either way yet
- the printer is counting down to stop the camera
- the camera is said to be on but no new picture came for STALE_S (the camera service crashed
  once on the real U1 without any message)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from guardian.camera.session_watch import CameraSessionWatch
from guardian.clock import Clock, to_iso

log = logging.getLogger(__name__)

COOLDOWN_S = 15.0
STALE_S = 10.0
STALE_COOLDOWN_S = 30.0  # if the camera does not come back, do not hammer the printer


class CameraKeepalive:
    def __init__(self, mode: str, session: CameraSessionWatch, clock: Clock, *,
                 print_state: Callable[[], str | None],
                 last_frame: Callable[[], datetime | None],
                 frames_watched: Callable[[], bool],
                 send: Callable[[], Awaitable[bool]],
                 audit: Callable[[str, dict], Any] | None = None) -> None:
        self.mode = mode
        self.session = session
        self.clock = clock
        self._print_state = print_state
        self._last_frame = last_frame
        self._frames_watched = frames_watched
        self._send = send
        self._audit = audit
        self._last_sent_mono: float | None = None
        self._last_sent_utc: datetime | None = None
        self.last_reason: str | None = None
        self.sent_count = 0

    def wanted(self) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "printing":
            return self._print_state() in ("printing", "paused")
        return False

    def _reason(self) -> tuple[str, float] | None:
        if self.session.monitoring is None:
            return "camera state not known yet", COOLDOWN_S
        if self.session.monitoring is False:
            return "the printer camera is off", COOLDOWN_S
        if self.session.stops_at() is not None:
            return "the printer is about to stop the camera", COOLDOWN_S
        if self._frames_watched():
            last = self._last_frame()
            since_change = (self.clock.now_utc() - self.session.changed_utc).total_seconds() \
                if self.session.changed_utc else STALE_S
            if since_change >= STALE_S:
                age = (self.clock.now_utc() - last).total_seconds() if last else None
                if age is None or age >= STALE_S:
                    return (f"no new picture for {age:.0f} s" if age is not None else "no picture yet"), STALE_COOLDOWN_S
        return None

    async def tick(self) -> None:
        if not self.wanted():
            return
        found = self._reason()
        if found is None:
            return
        reason, cooldown = found
        now = self.clock.monotonic()
        if self._last_sent_mono is not None and now - self._last_sent_mono < cooldown:
            return
        try:
            ok = await self._send()
        except Exception:  # noqa: BLE001 - never stop the app over this
            log.exception("could not ask the printer to start the camera")
            return
        if not ok:
            return  # not connected. Try again next tick.
        self._last_sent_mono, self._last_sent_utc = now, self.clock.now_utc()
        self.last_reason = reason
        self.sent_count += 1
        log.info("asked the printer to start the camera (LAN): %s", reason)
        if self._audit:
            try:
                self._audit("asked the printer to start the camera (camera.start_monitor, lan)",
                            {"reason": reason, "mode": self.mode})
            except Exception:  # noqa: BLE001
                log.exception("could not write the audit log")

    def status(self) -> dict[str, Any]:
        return {
            "keepalive": self.mode,
            "keepalive_active": self.wanted(),
            "keepalive_sent": self.sent_count,
            "keepalive_last_utc": to_iso(self._last_sent_utc) if self._last_sent_utc else None,
            "keepalive_last_reason": self.last_reason,
        }
