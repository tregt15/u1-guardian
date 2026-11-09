"""Moonraker WebSocket client. READ ONLY.

It only sends the calls in ALLOWED_METHODS. These are standard Moonraker calls and
they worked on one real U1 (Moonraker 1.4.1). Nothing here can send G-code, pause,
heat or move. A test checks that.

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

    async def _send(self, ws, rpc_id: int, method: str, params: dict | None = None) -> None:
        if method not in ALLOWED_METHODS:
            raise RuntimeError(f"refusing to send {method}: not on the read-only list")
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
        if params is not None:
            msg["params"] = params
        self.sent_methods.append(method)
        await ws.send(json.dumps(msg))

    async def run(self, stop: asyncio.Event) -> None:
        """Run until `stop` is set. Never raises on network trouble."""
        backoff = self._backoff_start
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20) as ws:
                    self.connected = True
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
        elif "method" in data:
            printer_ts = None
            params = data.get("params")
            if data["method"] == "notify_status_update" and isinstance(params, list) and len(params) > 1:
                printer_ts = params[1] if isinstance(params[1], (int, float)) else None
            await self.bus.publish("printer.message", {
                "method": data["method"], "params": params,
                "recv_utc": recv_utc, "recv_mono": recv_mono, "printer_ts": printer_ts,
            })
