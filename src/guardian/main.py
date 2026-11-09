"""Guardian app: wires the parts together and runs them.

    python -m guardian.main --config config.yaml

Read only toward the printer. It listens on the Moonraker WebSocket and (optionally)
polls the camera snapshot over HTTP GET. It never sends a command that changes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from datetime import timedelta
from pathlib import Path

import uvicorn

from guardian.api.app import ApiContext, create_app
from guardian.blackbox.incident_manager import IncidentManager
from guardian.bus import EventBus
from guardian.camera.capture import Capture, FFmpegMissing, check_ffmpeg
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.clock import Clock, SystemClock, to_iso
from guardian.config import Config, ConfigError, load_config
from guardian.printer.detectors import Detector
from guardian.printer.moonraker_client import MoonrakerClient
from guardian.printer.state_tracker import StateTracker
from guardian.storage.db import Database

log = logging.getLogger("guardian")

TICK_S = 1.0
VIEWER_ACTIVE_S = 5.0  # a snapshot newer than this means somebody is viewing the camera


class _Server(uvicorn.Server):
    """uvicorn without its own signal handlers. Guardian handles stopping itself."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # older uvicorn versions
        pass


class App:
    def __init__(self, cfg: Config, clock: Clock | None = None, *, realtime_standin: bool = True,
                 printer_port: int | None = None) -> None:
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self.ffmpeg, self.ffprobe = check_ffmpeg(cfg.camera)  # raises FFmpegMissing
        cfg.storage.resolved_data_dir().mkdir(parents=True, exist_ok=True)
        self.db = Database(cfg.storage.db_path, self.clock)
        self.bus = EventBus()
        self.buffer = SegmentBuffer(cfg.storage.resolved_buffer_dir(), cfg.camera.segment_length_s, self.clock)
        self.buffer.clear_files()  # old buffer content is not trusted after a restart
        self.manager = IncidentManager(cfg, self.db, self.buffer, self.clock, self.ffmpeg, self.ffprobe, self.bus)
        self.tracker = StateTracker(self.bus)  # must subscribe before the detector
        self.detector = Detector(self.bus, self.tracker, self._report)
        self.client = MoonrakerClient(cfg.printer.host, printer_port or cfg.printer.moonraker_port,
                                      self.bus, self.clock)
        self.capture = Capture(cfg.camera, self.buffer, self.clock, realtime=realtime_standin,
                               active=self._buffer_wanted)
        self.started_utc = self.clock.now_utc()
        self.api = create_app(ApiContext(cfg=cfg, db=self.db, bus=self.bus, status_fn=self.status,
                                         trigger_fn=self._report, started_utc=self.started_utc))
        self.bus.subscribe("trigger.manual", self._on_manual)
        self.bus.subscribe("printer.connected", self._log_event)
        self.bus.subscribe("printer.disconnected", self._log_event)
        self.bus.subscribe("incident.created", self._log_event)
        self.bus.subscribe("incident.merged", self._log_event)
        self.bus.subscribe("incident.finalized", self._log_event)

    # ---- wiring ----

    async def _report(self, event_type, source, severity, key, metadata):
        # Every trigger, from any source, carries the current printer state (file, progress, tool head).
        meta = {**self.tracker.incident_metadata(), **(metadata or {})}
        return await self.manager.report_incident(event_type, source, severity, key, meta)

    def _buffer_wanted(self) -> bool:
        """buffer_mode: `printing` buffers while a print is heating, running or paused (spec 6.1).
        Until the first printer snapshot arrives, keep buffering."""
        if self.cfg.blackbox.buffer_mode == "always":
            return True
        state = self.tracker.print_state
        return state is None or state in ("printing", "paused")

    async def _on_manual(self, topic: str, payload) -> None:
        meta = payload if isinstance(payload, dict) else {}
        await self._report("manual", "manual", "info", None, meta)

    def _log_event(self, topic: str, payload) -> None:
        log.info("%s %s", topic, payload)

    def status(self) -> dict:
        """Live state for /api/status."""
        meta = self.tracker.incident_metadata()
        cam = self.cfg.camera
        mode = "snapshot" if cam.snapshot_url else ("stream" if cam.source_url else "standin")
        last = self.capture.last_frame_utc
        if mode == "snapshot":
            viewing = bool(last and (self.clock.now_utc() - last) <= timedelta(seconds=VIEWER_ACTIVE_S))
        else:
            viewing = self.capture.running
        return {
            "printer": {
                "connected": self.client.connected,
                "print_state": self.tracker.print_state,
                "print_filename": meta.get("print_filename"),
                "print_progress": meta.get("print_progress"),
                "active_toolhead": meta.get("active_toolhead"),
                "last_message_utc": to_iso(self.client.last_message_utc) if self.client.last_message_utc else None,
            },
            "camera": {
                "mode": mode, "running": self.capture.running, "viewer_active": viewing,
                "last_frame_utc": to_iso(last) if last else None, "last_error": self.capture.last_error,
            },
            "incidents": {"open": self.manager.open_count},
        }

    async def _serve_api(self, stop: asyncio.Event) -> None:
        a = self.cfg.api
        server = _Server(uvicorn.Config(self.api, host=a.bind, port=a.port, log_level="warning"))
        task = asyncio.create_task(server.serve())
        log.info("API and dashboard on http://%s:%s/", a.bind, a.port)
        try:
            await stop.wait()
        finally:
            server.should_exit = True
            try:
                await task
            except (SystemExit, Exception):  # noqa: BLE001
                pass

    # ---- run ----

    async def run(self, stop: asyncio.Event) -> None:
        fixed = self.manager.recover_interrupted()
        if fixed:
            log.warning("recovered %d interrupted incident(s): %s", len(fixed), fixed)
        tasks = [
            asyncio.create_task(self.capture.supervise(stop), name="capture"),
            asyncio.create_task(self.client.run(stop), name="printer"),
            asyncio.create_task(self._tick_loop(stop), name="ticks"),
        ]
        if self.cfg.api.enabled:
            tasks.append(asyncio.create_task(self._serve_api(stop), name="api"))
        try:
            await stop.wait()
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Anything still open is finalized by the next start (recover_interrupted).
            self.db.close()

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.manager.tick()
            except Exception:  # noqa: BLE001 - keep ticking no matter what
                log.exception("tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="config.yaml (default: your OS config folder)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
        app = App(cfg)
    except (ConfigError, FFmpegMissing) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    async def runner() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: asyncio has no signal handlers
                pass
        log.info("Guardian started. Printer %s:%s. Data in %s", cfg.printer.host,
                 cfg.printer.moonraker_port, cfg.storage.resolved_data_dir())
        await app.run(stop)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass
    log.info("Guardian stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
