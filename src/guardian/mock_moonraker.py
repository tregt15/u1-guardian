"""A fake Moonraker for tests. It speaks just enough JSON-RPC over WebSocket.

It answers the three read-only calls Guardian sends (server.info, printer.objects.list,
printer.objects.subscribe) and then replays scripted messages, like a real printer would after
a subscription. It also accepts `camera.start_monitor` (the one opt-in write, kept in
`camera_starts`) and answers it like the U1 does, with notify_camera_status_change.
Any other method is refused and written to `refused`, so a test can prove Guardian never
sends anything else.

Run on its own to replay a JSONL file (one JSON message per line):
    python -m guardian.mock_moonraker tests/fixtures/u1_real_events.jsonl --port 7126
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import websockets

OBJECTS = ["webhooks", "print_stats", "machine_state_manager", "toolhead", "extruder",
           "gcode_macro T4", "filament_motion_sensor e0_filament"]


class MockMoonraker:
    def __init__(
        self,
        scripts: list[list[Any]],
        snapshots: list[dict] | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        delay_s: float = 0.01,
        drop_after_script: bool = False,
    ) -> None:
        """scripts[n] is what connection n replays after subscribing. A script item is a dict
        (sent as JSON) or a str (sent as is, for bad-message tests). snapshots[n] is the initial
        status for connection n. drop_after_script closes the connection when a script ends,
        except for the last connection."""
        self.scripts = scripts
        self.snapshots = snapshots or []
        self.host, self.port = host, port
        self.delay_s = delay_s
        self.drop_after_script = drop_after_script
        self.connections = 0
        self.received_methods: list[str] = []
        self.refused: list[str] = []
        self.camera_starts: list[Any] = []
        self.pauses: list[Any] = []
        self._server = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws) -> None:
        n = self.connections
        self.connections += 1
        replay: asyncio.Task | None = None
        try:
            async for raw in ws:
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                method, rpc_id = req.get("method"), req.get("id")
                self.received_methods.append(str(method))
                if method == "server.info":
                    result: Any = {"klippy_state": "ready", "moonraker_version": "mock"}
                elif method == "printer.objects.list":
                    result = {"objects": OBJECTS}
                elif method == "printer.objects.subscribe":
                    status = self.snapshots[n] if n < len(self.snapshots) else {
                        "print_stats": {"state": "standby", "filename": "", "exception": {}}}
                    result = {"eventtime": 1000.0, "status": status}
                elif method == "printer.print.pause":
                    self.pauses.append(req.get("params"))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": "ok"}))
                    continue
                elif method == "camera.start_monitor":
                    self.camera_starts.append(req.get("params"))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notify_camera_status_change",
                                              "params": [{"monitor_domain": "lan", "monitoring": True}]}))
                    continue
                else:
                    self.refused.append(str(method))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id,
                                              "error": {"code": -32601, "message": "refused by mock"}}))
                    continue
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": result}))
                if method == "printer.objects.subscribe" and n < len(self.scripts):
                    replay = asyncio.create_task(self._replay(ws, n))
        except websockets.ConnectionClosed:
            pass
        finally:
            if replay:
                replay.cancel()

    async def _replay(self, ws, n: int) -> None:
        try:
            for item in self.scripts[n]:
                await asyncio.sleep(self.delay_s)
                await ws.send(item if isinstance(item, str) else json.dumps(item))
            if self.drop_after_script and n < len(self.scripts) - 1:
                await ws.close()
        except websockets.ConnectionClosed:
            pass


async def _main(path: Path, port: int, delay: float) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    messages = [json.loads(line) for line in lines if line.strip()]
    mock = MockMoonraker([messages], port=port, delay_s=delay)
    await mock.start()
    print(f"Mock Moonraker on ws://127.0.0.1:{mock.port}/websocket, replaying {len(messages)} messages")
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--port", type=int, default=7126)
    ap.add_argument("--delay", type=float, default=0.2)
    args = ap.parse_args()
    try:
        asyncio.run(_main(args.file, args.port, args.delay))
    except KeyboardInterrupt:
        pass
