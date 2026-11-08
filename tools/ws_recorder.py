"""Record Moonraker WebSocket traffic to a JSONL file. Read only.

    python tools/ws_recorder.py --host 192.168.0.66 --out recordings/idle.jsonl
    python tools/ws_recorder.py --host 192.168.0.66 --minutes 90 --camera-watch

What it sends (nothing that changes the printer):
  - printer.objects.list     (list object names)
  - printer.objects.subscribe (ask for status updates, standard Moonraker read call)
It never sends G-code, never sends pause, heat or move requests.
UNVERIFIED on the U1: that these standard calls behave as in stock Moonraker.
The first run on the U1 idle printer worked (see docs/u1-discovery-notes.md).

--camera-watch also does an HTTP GET of /server/files/camera/monitor.jpg every few
seconds and logs its size and hash, to see when the stock camera snapshot updates.

Every line in the output is JSON: {"t_utc", "t_mono", "dir", "data"}.
dir is "rx" (received), "tx" (sent), "info" (recorder notes) or "camera".
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

# Calls this tool is allowed to send. Anything else is refused in code.
ALLOWED_METHODS = {"printer.objects.list", "printer.objects.subscribe", "server.info"}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Recorder:
    def __init__(self, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        self._fh = out.open("a", encoding="utf-8")
        self.count = 0

    def write(self, direction: str, data) -> None:
        rec = {"t_utc": now_iso(), "t_mono": round(time.monotonic(), 3), "dir": direction, "data": data}
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.count += 1

    def close(self) -> None:
        self._fh.close()


async def send(ws, rec: Recorder, rpc_id: int, method: str, params: dict | None = None) -> None:
    if method not in ALLOWED_METHODS:
        raise RuntimeError(f"refusing to send {method}: not on the read-only list")
    msg = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params is not None:
        msg["params"] = params
    rec.write("tx", msg)
    await ws.send(json.dumps(msg))


async def watch_connection(host: str, port: int, rec: Recorder, stop: asyncio.Event) -> None:
    url = f"ws://{host}:{port}/websocket"
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                rec.write("info", {"event": "connected", "url": url})
                backoff = 1.0
                await send(ws, rec, 1, "server.info")
                await send(ws, rec, 2, "printer.objects.list")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"raw": raw if isinstance(raw, str) else repr(raw)}
                    rec.write("rx", data)
                    if isinstance(data, dict) and data.get("id") == 2 and "result" in data:
                        names = data["result"].get("objects", [])
                        # Skip the many gcode_macro objects. Everything else is subscribed.
                        wanted = {n: None for n in names if not n.startswith("gcode_macro ")}
                        await send(ws, rec, 3, "printer.objects.subscribe", {"objects": wanted})
        except (OSError, websockets.WebSocketException) as exc:
            rec.write("info", {"event": "disconnected", "error": repr(exc), "retry_s": backoff})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def watch_camera(host: str, rec: Recorder, stop: asyncio.Event, every_s: float) -> None:
    url = f"http://{host}/server/files/camera/monitor.jpg"
    last = None
    async with httpx.AsyncClient(timeout=5.0) as client:
        while not stop.is_set():
            try:
                r = await client.get(url)
                digest = hashlib.md5(r.content).hexdigest() if r.status_code == 200 else None
                rec.write("camera", {"status": r.status_code, "bytes": len(r.content),
                                     "md5": digest, "changed": digest != last})
                last = digest
            except httpx.HTTPError as exc:
                rec.write("camera", {"error": repr(exc)})
            try:
                await asyncio.wait_for(stop.wait(), timeout=every_s)
            except asyncio.TimeoutError:
                pass


async def main_async(args) -> None:
    rec = Recorder(args.out)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(watch_connection(args.host, args.port, rec, stop))]
    if args.camera_watch:
        tasks.append(asyncio.create_task(watch_camera(args.host, rec, stop, args.camera_every)))
    try:
        if args.minutes:
            await asyncio.sleep(args.minutes * 60)
        else:
            await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        rec.close()
        print(f"Wrote {rec.count} lines to {args.out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="192.168.0.66")
    ap.add_argument("--port", type=int, default=80, help="80 (nginx) or 7125 (Moonraker). Both answered on the U1.")
    ap.add_argument("--out", type=Path,
                    default=Path("recordings") / f"ws_{datetime.now():%Y%m%d_%H%M%S}.jsonl")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (0 = until Ctrl+C)")
    ap.add_argument("--camera-watch", action="store_true")
    ap.add_argument("--camera-every", type=float, default=3.0)
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
