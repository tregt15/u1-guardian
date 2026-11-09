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

import httpx
import uvicorn

from guardian.ai.diagnosis import Diagnoser
from guardian.ai.installer import OllamaInstaller
from guardian.ai.vision import AiService, VisionWatcher
from guardian.api.app import ApiContext, create_app
from guardian.blackbox.incident_manager import IncidentManager
from guardian.bus import EventBus
from guardian.camera.capture import Capture, FFmpegMissing, check_ffmpeg
from guardian.camera.keepalive import CameraKeepalive
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.camera.session_watch import CameraSessionWatch
from guardian.camera.snapshot import FrameBuffer, SnapshotCapture
from guardian.clock import Clock, SystemClock, from_iso, to_iso
from guardian.storage.paths import incident_dir
from guardian.config import Config, ConfigError, default_config_path, load_config
from guardian.env import load_dotenv
from guardian.notify.email import ResendEmailChannel
from guardian.notify.manager import NotificationManager
from guardian.printer.autopause import AutoPause
from guardian.printer.detectors import Detector
from guardian.printer.health import HealthWatch
from guardian.printer.logs import PrinterLogs
from guardian.printer.moonraker_client import MoonrakerClient
from guardian.printer.state_tracker import StateTracker
from guardian.printer.toolchanger import ToolchangeMonitor
from guardian import selftest
from guardian import settings as settings_mod
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
        snapshot_mode = bool(cfg.camera.snapshot_url)
        if snapshot_mode:  # the U1: one JPEG about every second, only while somebody views the camera
            self.buffer = FrameBuffer(cfg.storage.resolved_buffer_dir(), cfg.camera.max_frame_gap_s, self.clock)
        else:
            self.buffer = SegmentBuffer(cfg.storage.resolved_buffer_dir(), cfg.camera.segment_length_s, self.clock)
        self.buffer.clear_files()  # old buffer content is not trusted after a restart
        self.manager = IncidentManager(cfg, self.db, self.buffer, self.clock, self.ffmpeg, self.ffprobe, self.bus)
        self.tracker = StateTracker(self.bus)  # must subscribe before the detector
        self.detector = Detector(self.bus, self.tracker, self._report)
        self.toolchanger = ToolchangeMonitor(cfg.toolchanger, self.db, self.bus, self.tracker, self.clock,
                                             self._report)
        self.camera_session = CameraSessionWatch(
            self.bus, self.clock, note=lambda kind, meta: self.manager.add_note(kind, "printer_camera", meta))
        self.snapshot_mode = snapshot_mode
        self.client = MoonrakerClient(cfg.printer.host, printer_port or cfg.printer.moonraker_port,
                                      self.bus, self.clock)
        self._sync_write_permissions()
        if snapshot_mode:
            self.capture = SnapshotCapture(cfg.camera, self.buffer, self.clock, active=self._buffer_wanted)
        else:
            self.capture = Capture(cfg.camera, self.buffer, self.clock, realtime=realtime_standin)
        self.keepalive = CameraKeepalive(
            lambda: self.cfg.camera.keepalive if self.snapshot_mode else "off", self.camera_session, self.clock,
            print_state=lambda: self.tracker.print_state,
            last_frame=lambda: self.capture.last_frame_utc,
            frames_watched=lambda: snapshot_mode and self._buffer_wanted(),
            send=self.client.start_camera_monitor,
            audit=lambda msg, meta: self.db.log("camera", "info", msg, meta))
        self.autopause = AutoPause(
            cfg.autopause, self.clock, print_state=lambda: self.tracker.print_state,
            send_pause=self.client.pause_print,
            note=lambda kind, meta: self.manager.add_note(kind, "guardian", meta),
            audit=lambda msg, meta: self.db.log("autopause", "warning", msg, meta))
        self.ai = AiService(cfg.ai, self.clock)
        self.vision = VisionWatcher(self.ai, self.clock, print_state=lambda: self.tracker.print_state,
                                    get_frame=self._fresh_frame, report=self._report,
                                    save_evidence=self._save_ai_pictures,
                                    action_code=lambda: self.tracker.status.get("machine_state_manager", {}).get("action_code"))
        self.health = HealthWatch(self.clock, lambda: self.tracker.status, self._report)
        self.selftest_result: dict | None = None
        self.firmware_changed: tuple[str, str] | None = None  # (old, new)
        self.started_utc = self.clock.now_utc()
        channels = []
        if cfg.notifications.email.enabled:
            channels.append(ResendEmailChannel(cfg.notifications.email))
        self.notifier = NotificationManager(cfg.notifications, self.db, self.bus, channels, self.clock)
        self.ollama_installer = OllamaInstaller(lambda: self.ai.ollama.version())
        self.diagnoser = Diagnoser(cfg, self.db, self.ai, self.clock, self.ffmpeg,
                                   notify=self.notifier.send_diagnosis)
        self.api = create_app(ApiContext(cfg=cfg, db=self.db, bus=self.bus, status_fn=self.status,
                                         trigger_fn=self._report, started_utc=self.started_utc,
                                         notifier=self.notifier, settings_fn=self.apply_settings,
                                         extra={"ai": self.ai, "installer": self.ollama_installer,
                                                "fresh_frame": self._fresh_frame,
                                                "selftest": self.run_selftest}))
        self.bus.subscribe("trigger.manual", self._on_manual)
        self.bus.subscribe("printer.connected", self._log_event)
        self.bus.subscribe("printer.disconnected", self._log_event)
        self.bus.subscribe("incident.created", self._log_event)
        self.bus.subscribe("incident.merged", self._log_event)
        self.bus.subscribe("incident.finalized", self._log_event)
        self.printer_logs = PrinterLogs(cfg.printer.host, printer_port or cfg.printer.moonraker_port)
        self._bg: set[asyncio.Task] = set()
        self.bus.subscribe("incident.finalized", self._on_finalized)

    # ---- wiring ----

    async def _report(self, event_type, source, severity, key, metadata):
        # Every trigger, from any source, carries the current printer state (file, progress, tool head).
        meta = {**self.tracker.incident_metadata(), **(metadata or {})}
        result = await self.manager.report_incident(event_type, source, severity, key, meta)
        try:
            await self.autopause.on_trigger(event_type, meta.get("summary") or meta.get("message"))
        except Exception:  # noqa: BLE001 - never lose the incident over this
            log.exception("auto pause check failed")
        return result

    def _on_finalized(self, topic: str, payload) -> None:
        """Work that may take a while runs in the background so recording never waits on it."""
        task = asyncio.create_task(self._after_incident(payload["uid"]))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _after_incident(self, uid: str) -> None:
        try:
            if self.cfg.printer.fetch_logs:
                await self._save_printer_logs(uid)
        except Exception:  # noqa: BLE001
            log.exception("could not save printer logs for %s", uid)
        await self.diagnoser.run(uid)  # needs the logs, so it runs after them. Never raises.

    async def _fresh_frame(self) -> bytes | None:
        """The current camera picture, only if pictures are really coming in right now."""
        url = self.cfg.camera.snapshot_url
        last = self.capture.last_frame_utc
        if not url or last is None or (self.clock.now_utc() - last).total_seconds() > 3.0:
            return None
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(url)
        if r.status_code != 200 or not r.content.startswith(b"\xff\xd8"):  # JPEG start bytes
            return None
        return r.content

    def _save_ai_pictures(self, result, frames: list[bytes]) -> None:
        row = self.db.get_incident(result.incident_id)
        if row is None:
            return
        folder = incident_dir(self.cfg.storage, row["id"], from_iso(row["first_trigger_utc"]))
        folder.mkdir(parents=True, exist_ok=True)
        n = len(list(folder.glob("ai_picture_*.jpg")))
        for i, data in enumerate(frames, start=n + 1):
            (folder / f"ai_picture_{i}.jpg").write_bytes(data)

    async def _save_printer_logs(self, uid: str) -> None:
        row = self.db.get_incident_by_uid(uid)
        if row is None:
            return
        bb = self.cfg.blackbox
        first, last = from_iso(row["first_trigger_utc"]), from_iso(row["last_trigger_utc"])
        start = first - timedelta(seconds=bb.pre_trigger_s)
        end = last + timedelta(seconds=bb.post_trigger_s)
        text, problems = await self.printer_logs.collect(start, end, self.clock.now_utc())
        folder = incident_dir(self.cfg.storage, row["id"], first)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "printer_logs.txt").write_text(text, encoding="utf-8")
        if problems:
            log.warning("printer logs for %s were partly unreadable: %s", uid, problems)

    async def run_selftest(self) -> dict:
        """Read-only checks. Also notices a printer firmware change since the last run."""
        result = await selftest.run_checks(self.cfg)
        self.selftest_result = result
        old = selftest.remember_firmware(self.cfg.storage.resolved_data_dir(), result.get("firmware"))
        if old:
            self.firmware_changed = (old, result["firmware"])
            log.warning("printer firmware changed from %s to %s", old, result["firmware"])
            self.db.log("selftest", "warning", "printer firmware changed",
                        {"old": old, "new": result["firmware"]})
        if not result["ok"]:
            log.warning("self-test found problems: %s", [c for c in result["checks"] if not c["ok"]])
        return result

    def _sync_write_permissions(self) -> None:
        """The client may only send the two opt-in requests while their settings allow it."""
        self.client.allow_camera_start = self.snapshot_mode and self.cfg.camera.keepalive != "off"
        self.client.allow_pause = self.cfg.autopause.enabled

    def apply_settings(self, changes: dict) -> list[str]:
        """Check, save and apply dashboard settings. Raises settings.SettingsError."""
        changed = settings_mod.save(self.cfg, changes)
        self._sync_write_permissions()
        if any(k.startswith("notifications.email.") for k in changed):
            email = self.cfg.notifications.email
            self.notifier.channels = [ResendEmailChannel(email)] if email.enabled else []
        if changed:
            log.info("settings changed from the dashboard: %s", changed)
            self.db.log("settings", "info", "settings changed from the dashboard",
                        {k: settings_mod.current(self.cfg)[k] for k in changed})
        return changed

    def _buffer_wanted(self) -> bool:
        """buffer_mode: `printing` buffers while a print is heating, running or paused (spec 6.1).
        Until the first printer snapshot arrives, keep buffering."""
        if self.cfg.blackbox.buffer_mode == "always":
            return True
        # Auto camera "always" keeps the U1 camera on while idle, so keep the pictures too.
        # (Before this, the camera was on but Guardian did not read it, and the dashboard said no pictures came.)
        if self.snapshot_mode and self.cfg.camera.keepalive == "always":
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
                "message": self.tracker.status.get("print_stats", {}).get("message") or None,
            },
            "camera": {
                "mode": mode, "running": self.capture.running, "viewer_active": viewing,
                "last_frame_utc": to_iso(last) if last else None, "last_error": self.capture.last_error,
                **self.camera_session.status(),
                **self.keepalive.status(),
            },
            "incidents": {"open": self.manager.open_count},
            "toolchanger": self.toolchanger.status(),
            "autopause": self.autopause.status(),
            "selftest": {
                "ok": self.selftest_result["ok"] if self.selftest_result else None,
                "ran_utc": self.selftest_result["ran_utc"] if self.selftest_result else None,
                "problems": [c for c in (self.selftest_result or {}).get("checks", []) if not c["ok"]],
                "firmware": (self.selftest_result or {}).get("firmware"),
                "firmware_changed": list(self.firmware_changed) if self.firmware_changed else None,
            },
            "ai": {**self.ai.status(), "checks": self.vision.status()},
            "notifications": {"email": {
                "enabled": self.cfg.notifications.email.enabled,
                # Only whether a key exists. The key itself is never shown.
                "configured": bool(self.cfg.notifications.email.api_key()),
                "to_count": len(self.cfg.notifications.email.to_addrs),
            }},
        }

    async def _serve_api(self, stop: asyncio.Event) -> None:
        a = self.cfg.api
        server = _Server(uvicorn.Config(self.api, host=a.bind, port=a.port, log_level="warning"))

        async def serve() -> None:
            # uvicorn calls sys.exit(1) when it cannot bind (for example the port is in use). A
            # SystemExit inside a task would stop the whole program, so catch it here: recording and
            # printer monitoring must keep running without the dashboard.
            try:
                await server.serve()
            except SystemExit:
                log.error("The dashboard could not start on %s:%s (is the port already in use?). "
                          "Recording and monitoring continue without it.", a.bind, a.port)
            except Exception:  # noqa: BLE001
                log.exception("The dashboard stopped unexpectedly. Recording and monitoring continue.")

        task = asyncio.create_task(serve())
        log.info("API and dashboard on http://%s:%s/", a.bind, a.port)
        try:
            await stop.wait()
        finally:
            server.should_exit = True
            await task

    # ---- run ----

    async def run(self, stop: asyncio.Event) -> None:
        fixed = self.manager.recover_interrupted()
        if fixed:
            log.warning("recovered %d interrupted incident(s): %s", len(fixed), fixed)
        left_open = self.toolchanger.recover_interrupted()
        if left_open:
            log.warning("marked %d unfinished tool change record(s) as interrupted", left_open)
        tasks = [
            asyncio.create_task(self.capture.supervise(stop), name="capture"),
            asyncio.create_task(self.client.run(stop), name="printer"),
            asyncio.create_task(self._tick_loop(stop), name="ticks"),
            asyncio.create_task(self.notifier.run(stop), name="notify"),
        ]
        if self.cfg.api.enabled:
            tasks.append(asyncio.create_task(self._serve_api(stop), name="api"))
        tasks.append(asyncio.create_task(self._startup_selftest(), name="selftest"))
        try:
            await stop.wait()
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Anything still open is finalized by the next start (recover_interrupted).
            self.db.close()

    async def _startup_selftest(self) -> None:
        try:
            await self.run_selftest()
        except Exception:  # noqa: BLE001 - only information. Never stop the app over it.
            log.exception("self-test failed to run")

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.manager.tick()
            except Exception:  # noqa: BLE001 - keep ticking no matter what
                log.exception("tick failed")
            try:
                await self.toolchanger.tick()
            except Exception:  # noqa: BLE001
                log.exception("tool change tick failed")
            try:
                await self.keepalive.tick()
            except Exception:  # noqa: BLE001
                log.exception("camera keepalive tick failed")
            try:
                await self.autopause.tick()
            except Exception:  # noqa: BLE001
                log.exception("auto pause tick failed")
            try:
                await self.vision.tick()
                if self.cfg.ai.enabled:
                    await self.ai.refresh()  # at most once a minute, so the dashboard shows the real AI state
            except Exception:  # noqa: BLE001
                log.exception("AI check tick failed")
            try:
                await self.health.tick()
            except Exception:  # noqa: BLE001
                log.exception("health check tick failed")
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
    # httpx logs every camera poll (about 2 a second) at INFO. That fills the log, and an SD card on a Pi.
    logging.getLogger("httpx").setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    # Each user keeps their own secrets (for example their Resend key) in the environment or a .env file.
    env_files = [Path.cwd() / ".env", default_config_path().parent / ".env"]
    if args.config:
        env_files.insert(0, args.config.parent / ".env")
    load_dotenv(*env_files)
    try:
        cfg = settings_mod.load_overrides(load_config(args.config))
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
