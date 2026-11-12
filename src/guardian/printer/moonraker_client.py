"""Moonraker WebSocket client. Read only, with one opt-in exception.

It only sends the calls in ALLOWED_METHODS. These are standard Moonraker calls and
they worked on one real U1 (Moonraker 1.4.1). Nothing here can send G-code, pause,
heat or move. A test checks that.

Two exceptions, both owner approved (2026-11-09), each behind its own switch:

- `camera.start_monitor` with exactly CAMERA_START_PARAMS (allow_camera_start). It turns on
  the printer's camera pictures for LAN viewers. It does not move, heat or pause anything.
  Verified on a real U1: pictures start within about 2 s. There is no stop call, so Guardian
  can never turn off somebody else's camera view.
- `printer.print.pause` with no params (allow_pause, or allow_controls). Standard Moonraker. On the
  U1 it runs Klipper's PAUSE (read in pause_resume.py), which refuses when not printing or already
  paused. Verified on the real U1 (2026-11-10): it paused a print.
- `printer.gcode.script` with exactly one of SETUP_SCRIPTS (allow_setup_moves): home, then raise the bed to
  15 mm and 5 mm for the camera setup, only when idle and after the person confirmed the plate is empty.
- `printer.print.resume` and `printer.print.cancel` with no params (allow_controls). Only sent when a
  person clicks Resume or Stop on the dashboard and confirms (owner asked, 2026-11-10). Guardian
  itself never sends them.

Behavior from spec section 5.2:
- one persistent connection, reconnect with backoff (1 s up to 30 s)
- after every reconnect, subscribe again and fetch a full snapshot
- every message gets two receive times (UTC and monotonic) and the printer event time if there is one
- bad messages are logged and skipped, never fatal

Bus topics published:
  printer.connected, printer.disconnected, printer.snapshot, printer.message
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import websockets

from guardian.bus import EventBus
from guardian.clock import Clock, SystemClock

log = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset({"server.info", "printer.objects.list", "printer.objects.subscribe"})
CAMERA_START = "camera.start_monitor"
CAMERA_START_PARAMS = {"domain": "lan", "interval": 0}
PAUSE = "printer.print.pause"
RESUME = "printer.print.resume"
CANCEL = "printer.print.cancel"
GCODE = "printer.gcode.script"
# The only G-code Guardian may ever send (owner approved, 2026-11-11): homing and two bed heights for the camera setup.
# The U1 Z home is at the bottom (position_endstop 275), so homing lowers the bed. Z 5 is the lowest it goes.
SETUP_SCRIPTS = frozenset({"G28", "G90\nG1 Z15 F600\nM400", "G90\nG1 Z5 F300\nM400"})

_ID_INFO, _ID_LIST, _ID_SUBSCRIBE = 1, 2, 3


class MoonrakerClient:
    def __init__(
        self,
        host: str,
        port: int,
        bus: EventBus,
        clock: Clock | None = None,
        *,
        backoff_start_s: float = 1.0,
        backoff_cap_s: float = 30.0,
        allow_camera_start: bool = False,
        allow_pause: bool = False,
        allow_controls: bool = False,
        allow_setup_moves: bool = False,
    ) -> None:
        self.url = f"ws://{host}:{port}/websocket"
        self.bus = bus
        self.clock = clock or SystemClock()
        self._backoff_start = backoff_start_s
        self._backoff_cap = backoff_cap_s
        self.connected = False
        self.connect_count = 0
        self.last_message_utc: datetime | None = None
        self.sent_methods: list[str] = []  # for tests and the audit trail
        self.allow_camera_start = allow_camera_start
        self.allow_pause = allow_pause
        self.allow_controls = allow_controls
        self.allow_setup_moves = allow_setup_moves
        self._pending: dict[int, asyncio.Future] = {}
        self._ws = None
        self._next_id = 1000

    async def _send(self, ws, rpc_id: int, method: str, params: dict | None = None) -> None:
        camera_ok = self.allow_camera_start and method == CAMERA_START and params == CAMERA_START_PARAMS
        pause_ok = (self.allow_pause or self.allow_controls) and method == PAUSE and params is None
        control_ok = self.allow_controls and method in (RESUME, CANCEL) and params is None
        setup_ok = (self.allow_setup_moves and method == GCODE and isinstance(params, dict) and set(params) == {"script"}
                    and params["script"] in SETUP_SCRIPTS)
        if method not in ALLOWED_METHODS and not camera_ok and not pause_ok and not control_ok and not setup_ok:
            raise RuntimeError(f"refusing to send {method}: not on the read-only list")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
        if params is not None:
            msg["params"] = params
        self.sent_methods.append(method)
        await ws.send(json.dumps(msg))

    async def start_camera_monitor(self) -> bool:
        """Ask the printer to turn on its camera pictures (LAN). Returns False if not connected."""
        ws = self._ws
        if ws is None or not self.connected:
            return False
        self._next_id += 1
        await self._send(ws, self._next_id, CAMERA_START, dict(CAMERA_START_PARAMS))
        return True

    async def pause_print(self) -> bool:
        """Ask the printer to pause the print. Returns False if not connected."""
        return await self._simple(PAUSE)

    async def resume_print(self) -> bool:
        """Only for the dashboard Resume button (a person clicked and confirmed)."""
        return await self._simple(RESUME)

    async def cancel_print(self) -> bool:
        """Only for the dashboard Stop button (a person clicked and confirmed)."""
        return await self._simple(CANCEL)

    async def setup_move(self, script: str, timeout: float = 240.0) -> None:
        """Send one of SETUP_SCRIPTS and wait until the printer says it is done. Raises RuntimeError."""
        ws = self._ws
        if ws is None or not self.connected:
            raise RuntimeError("The printer is not connected.")
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send(ws, rid, GCODE, {"script": script})
            reply = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("The printer did not finish the move in time.") from None
        finally:
            self._pending.pop(rid, None)
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(f"The printer refused: {err.get('message') if isinstance(err, dict) else err}")

    async def _simple(self, method: str) -> bool:
        ws = self._ws
        if ws is None or not self.connected:
            return False
        self._next_id += 1
        await self._send(ws, self._next_id, method)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set. Never raises on network trouble."""
        backoff = self._backoff_start
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    self.connected = True
                    self._ws = ws
                    self.connect_count += 1
                    backoff = self._backoff_start
                    await self.bus.publish("printer.connected", {"url": self.url, "n": self.connect_count})
                    await self._send(ws, _ID_INFO, "server.info")
                    await self._send(ws, _ID_LIST, "printer.objects.list")
                    await self._read_loop(ws, stop)
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("printer connection problem: %r", exc)
            except Exception:  # noqa: BLE001 - never let the client die
                log.exception("unexpected printer client error")
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_result({"error": {"message": "lost the link to the printer"}})
            self._pending.clear()
            if self.connected:
                self.connected = False
                await self.bus.publish("printer.disconnected", {"url": self.url})
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self._backoff_cap)

    async def _read_loop(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            recv_utc, recv_mono = self.clock.now_utc(), self.clock.monotonic()
            self.last_message_utc = recv_utc
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                log.warning("skipping a message that is not JSON")
                continue
            if not isinstance(data, dict):
                continue
            try:
                await self._handle(ws, data, recv_utc, recv_mono)
            except Exception:  # noqa: BLE001
                log.exception("error handling a printer message")

    async def _handle(self, ws, data: dict, recv_utc: datetime, recv_mono: float) -> None:
        rpc_id = data.get("id")
        if rpc_id == _ID_LIST and isinstance(data.get("result"), dict):
            names = data["result"].get("objects", [])
            # Skip the many macro objects. Everything else is subscribed.
            wanted = {n: None for n in names if isinstance(n, str) and not n.startswith("gcode_macro ")}
            await self._send(ws, _ID_SUBSCRIBE, "printer.objects.subscribe", {"objects": wanted})
        elif rpc_id == _ID_SUBSCRIBE and isinstance(data.get("result"), dict):
            # Full state after every (re)connect. Do not trust anything from before the gap.
            await self.bus.publish("printer.snapshot", {
                "status": data["result"].get("status", {}),
                "printer_ts": data["result"].get("eventtime"),
                "recv_utc": recv_utc, "recv_mono": recv_mono,
            })
        elif isinstance(rpc_id, int) and rpc_id in self._pending:
            fut = self._pending.pop(rpc_id)
            if not fut.done():
                fut.set_result(data)
        elif isinstance(rpc_id, int) and rpc_id > 1000 and "error" in data:
            # A reply to one of the two opt-in requests. Log it so a refusal is visible.
            log.warning("the printer refused request %s: %s", rpc_id, data.get("error"))
        elif "method" in data:
            printer_ts = None
            params = data.get("params")
            if data["method"] == "notify_status_update" and isinstance(params, list) and len(params) > 1:
                printer_ts = params[1] if isinstance(params[1], (int, float)) else None
            await self.bus.publish("printer.message", {
                "method": data["method"], "params": params,
                "recv_utc": recv_utc, "recv_mono": recv_mono, "printer_ts": printer_ts,
            })
