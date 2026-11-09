"""Print health checks from printer state: stalls and heater problems. Read only.

Objects used (seen on a real U1): print_stats.state, virtual_sdcard.file_position,
machine_state_manager.action_code, extruder..extruder3 and heater_bed (temperature, target).
The thresholds are first guesses, checked against two real prints for false alarms (none).
They are UNVERIFIED against real failures. Klipper's own heater safety still does the hard stop.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from guardian.clock import Clock

log = logging.getLogger(__name__)

HEATER_RE = re.compile(r"^(extruder\d*|heater_bed)$")
ReportFn = Callable[..., Awaitable[Any]]

STALL_S = 600.0
HEATING_MARGIN_C = 3.0
REACH_WINDOW_S = {"heater_bed": 600.0, "extruder": 300.0}
REACH_BELOW_C = 10.0
REACH_GAIN_WINDOW_S = 120.0
REACH_MIN_GAIN_C = 2.0
AT_TARGET_C = 3.0
DROP_BELOW_C = 15.0
DROP_FOR_S = 30.0


def tool_name(heater: str) -> str:
    if heater == "heater_bed":
        return "bed"
    return "T0" if heater == "extruder" else "T" + heater[len("extruder"):]


@dataclass
class _Heater:
    target: float = 0.0
    target_since: float = 0.0
    reached: bool = False
    below_since: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=400))
    fired: set = field(default_factory=set)


class HealthWatch:
    def __init__(self, clock: Clock, status: Callable[[], dict[str, dict]], report: ReportFn) -> None:
        self.clock = clock
        self._status = status
        self._report = report
        self._heaters: dict[str, _Heater] = {}
        self._last_pos: Any = None
        self._last_progress: float | None = None
        self._stall_fired = False
        self._was_printing = False

    async def tick(self) -> None:
        st = self._status()
        state = st.get("print_stats", {}).get("state")
        printing = state in ("printing", "paused")
        now = self.clock.monotonic()
        if printing and not self._was_printing:
            self._stall_fired, self._last_pos, self._last_progress = False, None, now
        self._was_printing = printing
        heating = await self._heaters_tick(st, now, printing)
        if state == "printing":
            await self._stall_tick(st, now, heating)
        else:
            self._last_progress = now if printing else None

    # ---- stall ----

    async def _stall_tick(self, st: dict, now: float, heating: bool) -> None:
        pos = st.get("virtual_sdcard", {}).get("file_position")
        busy = heating or (st.get("machine_state_manager", {}).get("action_code") not in (None, 0))
        if pos != self._last_pos or busy or self._last_progress is None:
            self._last_pos, self._last_progress = pos, now
            return
        idle = now - self._last_progress
        if idle >= STALL_S and not self._stall_fired:
            self._stall_fired = True
            await self._fire("print_stalled", "warning", "stall", {
                "summary": f"The print says it is printing, but it has not moved on in the file for {idle / 60:.0f} minutes.",
                "file_position": pos, "minutes": round(idle / 60, 1)})

    # ---- heaters ----

    async def _heaters_tick(self, st: dict, now: float, printing: bool) -> bool:
        # Only the bed or the active tool heating holds up a print. The U1 preheats the next tool while
        # the current one prints (seen in print 2), so other tools heating says nothing.
        active = st.get("toolhead", {}).get("extruder")
        heating = False
        for name, obj in st.items():
            if not HEATER_RE.match(name) or not isinstance(obj, dict):
                continue
            temp, target = obj.get("temperature"), obj.get("target")
            if not isinstance(temp, (int, float)) or not isinstance(target, (int, float)):
                continue
            h = self._heaters.setdefault(name, _Heater(target_since=now))
            if target != h.target:
                h.target, h.target_since, h.reached, h.below_since = target, now, False, None
                h.fired = {f for f in h.fired if not f.endswith(f":{target}")}
            h.history.append((now, temp))
            if target <= 0:
                continue
            if temp < target - HEATING_MARGIN_C and not h.reached and name in ("heater_bed", active):
                heating = True
            if abs(temp - target) <= AT_TARGET_C:
                h.reached = True
            if printing:
                await self._check_reach(name, h, now, temp)
                await self._check_drop(name, h, now, temp)
        return heating

    async def _check_reach(self, name: str, h: _Heater, now: float, temp: float) -> None:
        window = REACH_WINDOW_S["heater_bed" if name == "heater_bed" else "extruder"]
        if h.reached or now - h.target_since < window or temp >= h.target - REACH_BELOW_C:
            return
        old = [t for (ts, t) in h.history if now - ts >= REACH_GAIN_WINDOW_S]
        if not old:
            return
        gain = temp - old[-1]
        key = f"reach:{h.target}"
        if gain < REACH_MIN_GAIN_C and key not in h.fired:
            h.fired.add(key)
            await self._fire("heater_not_reaching", "warning", f"{name}:{key}", {
                "summary": f"The {tool_name(name)} heater is at {temp:.0f} °C but should be {h.target:.0f} °C, "
                           f"and it is not getting warmer ({gain:+.1f} °C in 2 minutes).",
                "heater": name, "temperature": temp, "target": h.target})

    async def _check_drop(self, name: str, h: _Heater, now: float, temp: float) -> None:
        if not h.reached:
            return
        if temp >= h.target - DROP_BELOW_C:
            h.below_since = None
            return
        h.below_since = h.below_since or now
        key = f"drop:{h.target}"
        if now - h.below_since >= DROP_FOR_S and key not in h.fired:
            h.fired.add(key)
            await self._fire("heater_temperature_drop", "warning", f"{name}:{key}", {
                "summary": f"The {tool_name(name)} heater dropped to {temp:.0f} °C after reaching {h.target:.0f} °C.",
                "heater": name, "temperature": temp, "target": h.target})

    async def _fire(self, event_type: str, severity: str, key: str, meta: dict) -> None:
        try:
            await self._report(event_type, "guardian", severity, key, meta)
        except Exception:  # noqa: BLE001
            log.exception("could not report %s", event_type)
