"""Bring the build plate into the camera's view for the camera setup (owner approved, 2026-11-11).

This is the only time Guardian moves the printer. Steps, each waiting until the printer says it is done:
1. Home all axes (G28). On the U1 the Z home is at the bottom (position_endstop 275), so the bed goes down.
2. Raise the bed to Z 30 and save a camera picture.
3. Raise the bed to Z 7 and save a second picture.
4. Raise the bed to Z 5 (the top used for the setup) and save a third picture.

Safety rules (a print left on the plate would hit the toolhead when the bed goes up):
- Only when no print is running or paused, and the printer is connected.
- Only after the person confirmed the build plate is empty. The question names the last print and its height.
- Stops at the first error, or if a print starts. Never goes lower than Z 5. Never sends other G-code.
UNVERIFIED on the real U1: what extra steps Snapmaker's homing runs. The first run should be watched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from guardian.clock import Clock, to_iso

log = logging.getLogger(__name__)

HOME, Z30, Z7, Z5 = "G28", "G90\nG1 Z30 F600\nM400", "G90\nG1 Z7 F600\nM400", "G90\nG1 Z5 F300\nM400"
TOP_Z = 6.0  # the bed counts as "at the top" at or below this Z (after homing)


class SetupMover:
    def __init__(self, clock: Clock, *, allowed: Callable[[], bool], print_state: Callable[[], str | None],
                 connected: Callable[[], bool], move: Callable[[str], Awaitable[None]],
                 picture: Callable[[], Awaitable[bytes | None]], camera_on: Callable[[], Awaitable[Any]],
                 bed_z: Callable[[], float | None], save: Callable[[str, bytes, float], Awaitable[Any]],
                 audit: Callable[[str, dict], Any] | None = None, settle_s: float = 2.0, picture_wait_s: float = 25.0) -> None:
        self.clock = clock
        self._allowed, self._print_state, self._connected = allowed, print_state, connected
        self._move, self._picture, self._camera_on = move, picture, camera_on
        self._bed_z, self._save, self._audit = bed_z, save, audit
        self.settle_s, self.picture_wait_s = settle_s, picture_wait_s
        self.state = "idle"
        self.step: str | None = None
        self.error: str | None = None
        self.started_utc: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def status(self) -> dict:
        return {"state": self.state, "step": self.step, "error": self.error, "started_utc": self.started_utc,
                "running": self.running}

    def refuse_reason(self, plate_empty: bool) -> str | None:
        if not self._allowed():
            return "Moving the bed for the camera setup is turned off in Settings (Printer controls)."
        if self.running:
            return "The bed is already moving."
        if not self._connected():
            return "The printer is not connected."
        st = self._print_state()
        if st in ("printing", "paused"):
            return f"A print is {st}. The bed is only moved when no print is running."
        if not plate_empty:
            return "Confirm that the build plate is empty first. A print left on it would hit the toolhead."
        return None

    def start(self, plate_empty: bool) -> None:
        """Raises ValueError with the reason if it may not start."""
        why = self.refuse_reason(plate_empty)
        if why:
            raise ValueError(why)
        self.state, self.error, self.step = "running", None, "starting"
        self.started_utc = to_iso(self.clock.now_utc())
        if self._audit:
            self._audit("camera setup: homing and raising the bed (plate confirmed empty)", {})
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            await self._do("homing the printer", HOME)
            await self._do("raising the bed to 30 mm", Z30)
            await self._shoot("setup_30")
            await self._do("raising the bed to 7 mm", Z7)
            await self._shoot("setup_07")
            await self._do("raising the bed to the top (5 mm)", Z5)
            await self._shoot("setup_05")
            self.state, self.step = "done", "done"
            log.info("camera setup: bed at the top, pictures saved")
        except Exception as exc:  # noqa: BLE001 - stop at the first problem, report it
            self.state, self.error = "error", str(exc)
            log.warning("camera setup moves stopped: %s", exc)

    async def _do(self, step: str, script: str) -> None:
        if self._print_state() in ("printing", "paused"):
            raise RuntimeError("A print started. Guardian stopped moving the bed.")
        self.step = step
        log.warning("camera setup: %s", step)
        await self._move(script)

    async def _shoot(self, name: str) -> None:
        self.step = "taking a picture"
        await asyncio.sleep(self.settle_s)
        z = self._bed_z()
        jpeg = await self._picture()
        waited = 0.0
        while jpeg is None and waited < self.picture_wait_s:
            if waited == 0.0:
                try:
                    await self._camera_on()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(1.0)
            waited += 1.0
            jpeg = await self._picture()
        if jpeg is None or z is None:
            raise RuntimeError("No camera picture came in. Turn on Auto camera in Settings and try again.")
        await self._save(name, jpeg, z)
