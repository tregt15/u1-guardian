"""Pause the print when Guardian finds a problem and the printer did not pause by itself.

Owner approved (2026-11-09). Only for the incident types in `autopause.triggers`.

How it works:
1. A trigger arrives while the print is `printing`. Guardian notes "will pause in N s" on the incident.
2. It waits `grace_s`, because the printer often pauses by itself (its own AI check does).
3. If the print is still `printing`, it sends printer.print.pause once. Otherwise it notes that it
   did nothing. It never resumes or cancels.

Sending the pause to a real U1 has not been tried yet (UNVERIFIED live). The U1's PAUSE refuses
when the printer is not printing or already paused (read in its pause_resume.py).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from guardian.clock import Clock, to_iso
from guardian.config import AutopauseConfig

log = logging.getLogger(__name__)

RETRY_S = 2.0
MAX_TRIES = 5
NoteFn = Callable[[str, dict], Awaitable[Any]]


@dataclass
class _Pending:
    deadline: float
    reasons: list[str] = field(default_factory=list)
    tries: int = 0


class AutoPause:
    def __init__(self, cfg: AutopauseConfig, clock: Clock, *, print_state: Callable[[], str | None],
                 send_pause: Callable[[], Awaitable[bool]], note: NoteFn | None = None,
                 audit: Callable[[str, dict], Any] | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self._print_state = print_state
        self._send = send_pause
        self._note = note
        self._audit = audit
        self._pending: _Pending | None = None
        self.last_pause_utc: datetime | None = None
        self.last_reason: str | None = None
        self.pause_count = 0

    async def on_trigger(self, event_type: str, summary: str | None = None) -> None:
        if not self.cfg.enabled or event_type not in self.cfg.triggers:
            return
        state = self._print_state()
        if state != "printing":
            return  # not printing, or already paused: nothing to do
        reason = f"{event_type}: {summary}" if summary else event_type
        if self._pending is not None:
            self._pending.reasons.append(reason)
            return
        self._pending = _Pending(self.clock.monotonic() + self.cfg.grace_s, [reason])
        log.warning("auto pause armed (%s). Pausing in %.0f s unless the printer pauses itself.",
                    event_type, self.cfg.grace_s)
        await self._say("autopause_armed", {
            "reason": reason, "grace_s": self.cfg.grace_s,
            "summary": f"Guardian will pause the print in {self.cfg.grace_s:g} s unless the printer pauses itself.",
        })

    async def cancel(self) -> bool:
        """The person clicked "Cancel this pause" on the dashboard. True if one was pending."""
        if self._pending is None:
            return False
        reasons = "; ".join(self._pending.reasons)
        self._pending = None
        log.warning("auto pause cancelled from the dashboard (%s)", reasons)
        await self._say("autopause_cancelled", {"summary": "You cancelled this pause from the dashboard.",
                                                "reason": reasons})
        return True

    async def tick(self) -> None:
        p = self._pending
        if p is None or self.clock.monotonic() < p.deadline:
            return
        if not self.cfg.enabled:
            self._pending = None
            await self._say("autopause_cancelled", {"summary": "Auto pause was turned off. Guardian did not pause."})
            return
        state = self._print_state()
        if state != "printing":
            self._pending = None
            await self._say("autopause_not_needed", {
                "summary": f"The print is already {state or 'not running'}. Guardian did not pause it."})
            return
        try:
            ok = await self._send()
        except Exception:  # noqa: BLE001
            log.exception("could not send the pause")
            ok = False
        p.tries += 1
        if not ok:
            if p.tries >= MAX_TRIES:
                self._pending = None
                await self._say("autopause_failed", {
                    "summary": "Guardian tried to pause the print but could not reach the printer."})
                return
            p.deadline = self.clock.monotonic() + RETRY_S
            return
        self._pending = None
        self.pause_count += 1
        self.last_pause_utc = self.clock.now_utc()
        self.last_reason = "; ".join(p.reasons)
        log.warning("Guardian paused the print: %s", self.last_reason)
        await self._say("guardian_paused_print", {
            "reason": self.last_reason,
            "summary": "Guardian paused the print because the printer did not pause by itself. "
                       "Check the printer, then resume from the printer or the app.",
        })
        if self._audit:
            try:
                self._audit("paused the print (printer.print.pause)", {"reason": self.last_reason})
            except Exception:  # noqa: BLE001
                log.exception("could not write the audit log")

    async def _say(self, kind: str, meta: dict) -> None:
        if self._note:
            try:
                await self._note(kind, meta)
            except Exception:  # noqa: BLE001
                log.exception("could not add %s to the incident", kind)

    def status(self) -> dict[str, Any]:
        p = self._pending
        return {
            "enabled": self.cfg.enabled,
            "grace_s": self.cfg.grace_s,
            "triggers": list(self.cfg.triggers),
            "pending_in_s": round(max(0.0, p.deadline - self.clock.monotonic())) if p else None,
            "pending_reason": "; ".join(p.reasons) if p else None,
            "paused_count": self.pause_count,
            "last_pause_utc": to_iso(self.last_pause_utc) if self.last_pause_utc else None,
            "last_reason": self.last_reason,
        }
