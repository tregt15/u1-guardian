"""Tool change monitor (spec 5.3). Read only: it only watches messages.

What a tool change looks like on a real U1 (2 prints, 399+ changes, see docs/u1-discovery-notes.md):

1. gcode response `// A -> B`, then `// park A !!!`
2. A goes `state=PARKED`, `park_pin=true`, `active_pin=false`, `grab_valid_pin=false`
3. gcode response `// pick B !!!`
4. B goes `active_pin=true`, `state=ACTIVATE`, `park_pin=false`, `grab_valid_pin=true`
5. gcode response `// Activating extruder B`, then `toolhead.extruder = "B"`

Tool 0 is the object `extruder`, tools 1 to 3 are `extruder1` to `extruder3`.
The order inside steps 2 and 4 changes from change to change, so this module only checks that each
expected value was seen at some point, never the order. What the pins mean physically is UNVERIFIED.
Only the names and values above were seen. Nothing here was seen failing on a real U1 yet, so the
failure results (timeout, wrong_tool, bad_state, error) are built from the spec and tested with
made-up messages only.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from guardian.bus import EventBus
from guardian.clock import Clock, to_iso
from guardian.config import ToolchangerConfig
from guardian.printer.state_tracker import StateTracker
from guardian.storage.db import Database

log = logging.getLogger(__name__)

ReportFn = Callable[..., Awaitable[Any]]

TOOL_RE = re.compile(r"^extruder\d*$")
REQUEST_RE = re.compile(r"^// (extruder\d*) -> (extruder\d*)\s*$")
ACTIVATED_RE = re.compile(r"^// Activating extruder (extruder\d*)\s*$")
STEP_RE = re.compile(r"^// (park|pick) (extruder\d*) !!!")

TOOL_FIELDS = ("state", "park_pin", "active_pin", "grab_valid_pin", "activating_move")
MAX_TRANSITIONS = 60
LATE_WATCH_S = 600.0  # keep watching a timed-out change this long in case it finishes late
LEARN_KEEP = 200  # recent good changes used to learn the timeout
# Slowing down: the median of the last TREND_RECENT good changes of one tool pair against the median of
# the ones before. Real U1 changes were 3.5 to 4.4 s (406 changes), so 40% and 1 s more is well outside that.
TREND_RECENT = 10
TREND_MIN = 30
TREND_RATIO = 1.4
TREND_MIN_EXTRA_MS = 1000
TREND_QUIET_S = 6 * 3600  # warn about one pair at most every 6 hours


def tool_label(name: str | None) -> str:
    """`extruder` -> T0, `extruder2` -> T2. Anything else is shown as is."""
    if not name:
        return "unknown"
    if name == "extruder":
        return "T0"
    if TOOL_RE.match(name):
        return "T" + name[len("extruder"):]
    return name


@dataclass
class _Change:
    record_id: int
    prev: str | None
    new: str
    start_utc: datetime
    start_mono: float
    status: str = "running"  # running -> settling -> done, or running -> late (timed out)
    end_utc: datetime | None = None
    end_mono: float | None = None
    settle_until: float | None = None
    seen: set[str] = field(default_factory=set)
    transitions: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{tool_label(self.prev)} -> {tool_label(self.new)}"


class ToolchangeMonitor:
    def __init__(self, cfg: ToolchangerConfig, db: Database, bus: EventBus, tracker: StateTracker,
                 clock: Clock, report: ReportFn) -> None:
        self.cfg = cfg
        self.db = db
        self.tracker = tracker
        self.clock = clock
        self.report = report
        self._open: list[_Change] = []
        self._trend_warned: dict[tuple, float] = {}
        self._pending_trend: list[_Change] = []
        self._ok_ms: deque[int] = deque(reversed(db.recent_ok_toolchange_ms(LEARN_KEEP)), maxlen=LEARN_KEEP)
        bus.subscribe("printer.snapshot", self._on_snapshot)
        bus.subscribe("printer.message", self._on_message)

    # ---- public ----

    def recover_interrupted(self) -> int:
        """Records left open when Guardian stopped. Their end is not known, so say so."""
        return self.db.close_open_toolchanges("interrupted", "Guardian stopped during this tool change.")

    def timeout_s(self) -> float:
        if self.cfg.timeout_s:
            return self.cfg.timeout_s
        if len(self._ok_ms) < self.cfg.learn_count:
            return self.cfg.timeout_floor_s
        ordered = sorted(self._ok_ms)
        p99 = ordered[max(0, math.ceil(0.99 * len(ordered)) - 1)] / 1000.0
        return max(self.cfg.timeout_floor_s, p99 * self.cfg.timeout_margin)

    def status(self) -> dict[str, Any]:
        learned = not self.cfg.timeout_s and len(self._ok_ms) >= self.cfg.learn_count
        return {
            "enabled": self.cfg.enabled,
            "in_progress": [c.label for c in self._open if c.status in ("running", "settling")],
            "timeout_s": round(self.timeout_s(), 1),
            "timeout_source": "fixed" if self.cfg.timeout_s else ("learned" if learned else "default"),
            "good_changes_measured": len(self._ok_ms),
            "learn_count": self.cfg.learn_count,
            "median_ms": int(sorted(self._ok_ms)[len(self._ok_ms) // 2]) if self._ok_ms else None,
        }

    async def tick(self) -> None:
        if not self.cfg.enabled:
            return
        while self._pending_trend:
            await self._check_trend(self._pending_trend.pop(0))
        now = self.clock.monotonic()
        for ch in list(self._open):
            if ch.status == "running" and now - ch.start_mono > self.timeout_s():
                await self._timeout(ch, now)
            elif ch.status == "settling" and ch.settle_until is not None and now >= ch.settle_until:
                await self._settle_failed(ch)
            elif ch.status == "late" and now - ch.start_mono > LATE_WATCH_S:
                self._open.remove(ch)

    # ---- inputs ----

    async def _on_snapshot(self, topic: str, payload: dict) -> None:
        # A new snapshot means the link dropped. Messages were lost, so an open change cannot be judged.
        for ch in list(self._open):
            if ch.status in ("running", "settling"):
                self._note(ch, "link", "lost the printer link during the change")
                self._finish(ch, "interrupted", error="Lost the link to the printer during this tool change.")
            else:
                self._open.remove(ch)

    async def _on_message(self, topic: str, payload: dict) -> None:
        if not self.cfg.enabled:  # read every time: the setting can change while running
            if self._open:
                for ch in list(self._open):
                    if ch.status in ("running", "settling"):
                        self._finish(ch, "interrupted", error="Tool change watching was turned off.")
                self._open.clear()
            return
        method, params = payload.get("method"), payload.get("params")
        if not isinstance(params, list):
            return
        if method == "notify_gcode_response":
            for line in params:
                if isinstance(line, str):
                    await self._gcode(line.strip())
        elif method == "notify_exception_notification":
            for item in params:
                if isinstance(item, dict):
                    await self._error(str(item.get("message") or item), item)
        elif method == "notify_status_update" and params and isinstance(params[0], dict):
            await self._status(params[0])

    # ---- message handling ----

    async def _gcode(self, line: str) -> None:
        if m := REQUEST_RE.match(line):
            self._start(m.group(1), m.group(2), how=line)
            return
        if m := ACTIVATED_RE.match(line):
            for ch in self._open:
                if ch.new == m.group(1) and ch.status in ("running", "late"):
                    self._note(ch, "gcode", line)
                    await self._ended(ch)
            return
        if m := STEP_RE.match(line):
            for ch in self._running():
                if m.group(2) in (ch.prev, ch.new):
                    self._note(ch, "gcode", line)
                    if m.group(1) == "pick" and m.group(2) == ch.new:
                        ch.seen.add("pick")
            return
        if line.startswith("!!"):
            await self._error(line, None)

    async def _status(self, update: dict) -> None:
        # Fallback start: a tool starts moving in but no `A -> B` line was seen.
        for name, fields in update.items():
            if (TOOL_RE.match(name) and isinstance(fields, dict) and fields.get("activating_move") is True
                    and not any(c.new == name and c.status == "running" for c in self._open)):
                prev = self.tracker.status.get("toolhead", {}).get("extruder")
                self._start(prev if prev != name else None, name, how=f"{name}.activating_move = true")

        for ch in list(self._open):
            if ch.status not in ("running", "settling", "late"):
                continue
            for obj in (ch.prev, ch.new):
                fields = update.get(obj) if obj else None
                if isinstance(fields, dict):
                    for key in TOOL_FIELDS:
                        if key in fields:
                            self._note(ch, f"{obj}.{key}", fields[key])
                            self._latch(ch, obj, key, fields[key])
            head = update.get("toolhead")
            new_fields = update.get(ch.new)
            if (ch.status == "running" and isinstance(new_fields, dict) and new_fields.get("activating_move") is False
                    and not ({"pick", "new_activate"} & ch.seen)
                    and not (isinstance(head, dict) and head.get("extruder") == ch.new)):
                # Seen once on a real U1, at the end of a print: `// B -> A`, B parks, then
                # A.activating_move goes back to false with no pick step. Only a park, not a stuck change.
                ch.end_utc, ch.end_mono = self.clock.now_utc(), self.clock.monotonic()
                self._finish(ch, "park_only", error=f"{tool_label(ch.prev)} was parked and no tool was picked up.")
                continue
            if isinstance(head, dict) and "extruder" in head:
                actual = head["extruder"]
                self._note(ch, "toolhead.extruder", actual)
                if actual == ch.new:
                    ch.seen.add("toolhead_new")
                    if ch.status in ("running", "late"):
                        await self._ended(ch)
                elif ch.status == "running" and actual not in (ch.prev, None):
                    await self._wrong_tool(ch, actual)
                    continue
            ps = update.get("print_stats")
            if (isinstance(ps, dict) and ps.get("state") in ("cancelled", "error")
                    and ch.status in ("running", "settling")):
                self._note(ch, "print_stats.state", ps["state"])
                self._finish(ch, "interrupted", error=f"The print went to '{ps['state']}' during this tool change.")
                continue
            if ch.status == "settling":
                self._try_pass(ch)

    def _latch(self, ch: _Change, obj: str, key: str, value: Any) -> None:
        good = {
            (ch.prev, "state"): ("old_parked", value == "PARKED"),
            (ch.prev, "park_pin"): ("old_park_pin", value is True),
            (ch.new, "state"): ("new_activate", value == "ACTIVATE"),
            (ch.new, "grab_valid_pin"): ("new_grab", value is True),
            (ch.new, "active_pin"): ("new_active_pin", value is True),
            (ch.new, "park_pin"): ("new_unparked", value is False),
        }.get((obj, key))
        if good and good[1]:
            ch.seen.add(good[0])

    # ---- checks ----

    def _missing(self, ch: _Change) -> list[str]:
        """Expected end values not seen yet. The current state counts too, in case a value was
        already right before the change began."""
        st = self.tracker.status
        old, new = st.get(ch.prev or "", {}), st.get(ch.new, {})
        checks = [
            ("new_grab", new.get("grab_valid_pin") is True, f"{tool_label(ch.new)} never reported a good grab (grab_valid_pin)"),
            ("new_active_pin", new.get("active_pin") is True, f"{tool_label(ch.new)} never reported active_pin"),
            ("new_unparked", new.get("park_pin") is False, f"{tool_label(ch.new)} still reports park_pin"),
            ("new_activate", new.get("state") == "ACTIVATE", f"{tool_label(ch.new)} never reached state ACTIVATE"),
            ("toolhead_new", st.get("toolhead", {}).get("extruder") == ch.new,
             f"the printer never reported {tool_label(ch.new)} as the active tool"),
        ]
        if ch.prev:
            checks[:0] = [
                ("old_parked", old.get("state") == "PARKED", f"{tool_label(ch.prev)} never reached state PARKED"),
                ("old_park_pin", old.get("park_pin") is True, f"{tool_label(ch.prev)} never reported park_pin"),
            ]
        return [text for key, now_ok, text in checks if key not in ch.seen and not now_ok]

    def _stuck_at(self, ch: _Change) -> str:
        missing = " ".join(self._missing(ch))
        if ch.prev and tool_label(ch.prev) + " never" in missing:
            return f"parking {tool_label(ch.prev)}"
        if "grab" in missing or "active_pin" in missing or "park_pin" in missing:
            return f"picking up {tool_label(ch.new)}"
        if "ACTIVATE" in missing:
            return f"activating {tool_label(ch.new)}"
        return f"waiting for the printer to report {tool_label(ch.new)} as the active tool"

    # ---- state changes ----

    def _running(self) -> list[_Change]:
        return [c for c in self._open if c.status == "running"]

    def _start(self, prev: str | None, new: str, how: str) -> None:
        now_utc, now_mono = self.clock.now_utc(), self.clock.monotonic()
        for ch in self._running():
            if ch.new == new:  # asked again before the first one ended
                self._note(ch, "gcode", how)
                return
        ps = self.tracker.status.get("print_stats", {})
        filename = ps.get("filename") if ps.get("state") in ("printing", "paused") else None
        rid = self.db.create_toolchange(print_filename=filename or None, requested_tool=new,
                                        previous_tool=prev, started_utc=to_iso(now_utc))
        ch = _Change(rid, prev, new, now_utc, now_mono)
        self._note(ch, "start", how)
        self._open.append(ch)

    async def _ended(self, ch: _Change) -> None:
        if ch.status == "late":
            ch.end_utc, ch.end_mono = self.clock.now_utc(), self.clock.monotonic()
            self._note(ch, "finished_late", f"finished after {self._dur_ms(ch) / 1000:.1f} s")
            self.db.update_toolchange(ch.record_id, ended_utc=to_iso(ch.end_utc), duration_ms=self._dur_ms(ch),
                                      transitions=json.dumps(ch.transitions))
            self._open.remove(ch)
            return
        ch.end_utc, ch.end_mono = self.clock.now_utc(), self.clock.monotonic()
        ch.status = "settling"
        ch.settle_until = ch.end_mono + self.cfg.settle_s
        self._try_pass(ch)

    def _try_pass(self, ch: _Change) -> None:
        if not self._missing(ch):
            self._ok_ms.append(self._dur_ms(ch))
            self._finish(ch, "ok")
            self._pending_trend.append(ch)  # checked on the next tick (needs an await)

    async def _settle_failed(self, ch: _Change) -> None:
        actual = self.tracker.status.get("toolhead", {}).get("extruder")
        if actual != ch.new and "toolhead_new" not in ch.seen:
            await self._wrong_tool(ch, actual)
            return
        problems = self._missing(ch)
        self._finish(ch, "bad_state", error="; ".join(problems))
        await self._fire(ch, "toolchange_bad_state", "warning",
                         f"Tool change {ch.label} ended, but the tool states look wrong: {'; '.join(problems)}.",
                         {"problems": problems})

    async def _timeout(self, ch: _Change, now: float) -> None:
        limit = self.timeout_s()
        stuck = self._stuck_at(ch)
        self._note(ch, "timeout", f"no finish after {limit:.1f} s, stuck {stuck}")
        self._finish(ch, "timeout", error=f"Did not finish in {limit:.1f} s. Last step: {stuck}.", keep_watching=True)
        await self._fire(ch, "toolchange_timeout", "warning",
                         f"Tool change {ch.label} did not finish in {limit:.1f} s. It looks stuck {stuck}.",
                         {"timeout_s": round(limit, 1), "stuck_at": stuck, "missing": self._missing(ch)})

    async def _wrong_tool(self, ch: _Change, actual: str | None) -> None:
        self._finish(ch, "wrong_tool", actual=actual,
                     error=f"Asked for {tool_label(ch.new)}, the printer reports {tool_label(actual)}.")
        await self._fire(ch, "toolchange_wrong_tool", "critical",
                         f"Tool change {ch.label}: the printer reports {tool_label(actual)} as the active tool.",
                         {"actual_tool": tool_label(actual)})

    async def _error(self, text: str, exception: dict | None) -> None:
        for ch in self._running():
            self._note(ch, "error", text)
            self._finish(ch, "error", error=text)
            extra = {"printer_message": text}
            if exception:
                extra["exception"] = exception
            await self._fire(ch, "toolchange_error", "critical",
                             f"The printer reported an error during tool change {ch.label}: {text}", extra)

    def _finish(self, ch: _Change, result: str, *, error: str | None = None, actual: str | None = None,
                keep_watching: bool = False) -> None:
        fields: dict[str, Any] = {"result": result, "transitions": json.dumps(ch.transitions)}
        if ch.end_utc:
            fields["ended_utc"] = to_iso(ch.end_utc)
            fields["duration_ms"] = self._dur_ms(ch)
        fields["actual_tool"] = actual if actual is not None else (ch.new if result == "ok" else
                                                                   self.tracker.status.get("toolhead", {}).get("extruder"))
        if error:
            fields["error"] = error
        self.db.update_toolchange(ch.record_id, **fields)
        if keep_watching:
            ch.status = "late"
        else:
            ch.status = "done"
            if ch in self._open:
                self._open.remove(ch)
        if result not in ("ok", "park_only"):
            log.warning("tool change %s: %s %s", ch.label, result, error or "")

    async def _fire(self, ch: _Change, event_type: str, severity: str, summary: str, extra: dict) -> None:
        elapsed = (ch.end_mono or self.clock.monotonic()) - ch.start_mono
        meta = {"summary": summary, "from_tool": tool_label(ch.prev), "to_tool": tool_label(ch.new),
                "elapsed_s": round(elapsed, 1), "toolchange_id": ch.record_id,
                "steps": ch.transitions[-20:], **extra}
        try:
            result = await self.report(event_type, "toolchanger", severity, f"{event_type}:{ch.prev}->{ch.new}", meta)
        except Exception:  # noqa: BLE001 - a failed report must not stop monitoring
            log.exception("could not report %s", event_type)
            return
        incident_id = getattr(result, "incident_id", None)
        if incident_id:
            self.db.update_toolchange(ch.record_id, incident_id=incident_id)

    async def _check_trend(self, ch: _Change) -> None:
        pair = (ch.prev, ch.new)
        times = self.db.recent_ok_pair_ms(ch.prev, ch.new, limit=60)
        if len(times) < TREND_MIN:
            return
        recent, older = sorted(times[:TREND_RECENT]), sorted(times[TREND_RECENT:])
        r_med, o_med = recent[len(recent) // 2], older[len(older) // 2]
        if r_med < o_med * TREND_RATIO or r_med - o_med < TREND_MIN_EXTRA_MS:
            return
        now = self.clock.monotonic()
        if now - self._trend_warned.get(pair, -1e18) < TREND_QUIET_S:
            return
        self._trend_warned[pair] = now
        await self._fire(ch, "toolchange_slowing", "warning",
                         f"Tool changes {ch.label} are getting slower: lately {r_med / 1000:.1f} s, "
                         f"before that {o_med / 1000:.1f} s. Check that tool's dock and pickup.",
                         {"recent_median_ms": r_med, "earlier_median_ms": o_med})

    # ---- helpers ----

    def _dur_ms(self, ch: _Change) -> int:
        end = ch.end_mono if ch.end_mono is not None else self.clock.monotonic()
        return int(round((end - ch.start_mono) * 1000))

    def _note(self, ch: _Change, what: str, value: Any) -> None:
        if len(ch.transitions) >= MAX_TRANSITIONS:
            return
        ch.transitions.append({"t_ms": int(round((self.clock.monotonic() - ch.start_mono) * 1000)),
                               "ts": to_iso(self.clock.now_utc()), "what": what, "value": value})
