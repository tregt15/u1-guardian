"""Guardian app: wires the parts together and runs them.

    python -m guardian.main --config config.yaml

Read only toward the printer. It listens on the Moonraker WebSocket and (optionally)
polls the camera snapshot over HTTP GET. It never sends a command that changes anything.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn

from guardian.ai.diagnosis import Diagnoser
from guardian.ai.gcode_view import GcodeFollower, draw_top
from guardian.camera.calibration import CameraSetup
from guardian.camera.setup_moves import HOME, PARK, TOP_Z, Z5, SetupMover
from guardian.camera.match import decode as decode_jpeg
from guardian.camera.simulate import render as render_simulation
from guardian.camera.match import score as match_score
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
from guardian import secrets_store, selftest
from guardian import settings as settings_mod
from guardian.storage.db import Database
from guardian.storage.retention import purge_old

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


class PlateCheckFailed(ValueError):
    """The plate check did not say clear. `check` is the result (state, message). Nothing was sent to the printer."""

    def __init__(self, check: dict) -> None:
        super().__init__(check.get("message", "The plate check did not pass."))
        self.check = check


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
        self._file_meta: dict = {}
        self._file_meta_tried: str | None = None
        self._printer_port = printer_port or cfg.printer.moonraker_port
        self.gcode = GcodeFollower(f"http://{cfg.printer.host}:{self._printer_port}")
        self._gcode_task: asyncio.Task | None = None
        self._gcode_last = 0.0
        self._prev_print_state: str | None = None
        self.gcode_error: str | None = None
        self.camera_setup = CameraSetup(cfg.storage.resolved_data_dir())
        self.frames_dir = cfg.storage.resolved_data_dir() / "calibration" / "frames"
        self._frames_this_print: set[str] = set()
        self._frames_file: str | None = None
        self._match_task: asyncio.Task | None = None
        self._match_last = 0.0
        self.match_history: list[dict] = []
        self.setup_mover = SetupMover(
            self.clock, allowed=lambda: self.cfg.controls.setup_moves, print_state=lambda: self.tracker.print_state,
            connected=lambda: self.client.connected, move=self.client.setup_move, picture=self._fresh_frame,
            camera_on=self.client.start_camera_monitor, bed_z=self._bed_z, save=self.save_frame,
            audit=lambda msg, meta: self.db.log("controls", "warning", msg, meta))
        self.ai = AiService(cfg.ai, self.clock, key_fn=lambda: secrets_store.ai_key(self.cfg))
        self.vision = VisionWatcher(self.ai, self.clock, print_state=lambda: self.tracker.print_state,
                                    get_frame=self._fresh_frame, report=self._report,
                                    save_evidence=self._save_ai_pictures,
                                    action_code=lambda: self.tracker.status.get("machine_state_manager", {}).get("action_code"),
                                    context=self.print_context,
                                    failure_severity=lambda: "critical" if "ai_print_failure" in self.cfg.autopause.triggers else "warning",
                                    get_simulated=self._simulated_async)
        self.health = HealthWatch(self.clock, lambda: self.tracker.status, self._report)
        self.selftest_result: dict | None = None
        self.firmware_changed: tuple[str, str] | None = None  # (old, new)
        self._purge_at: float | None = None
        self.started_utc = self.clock.now_utc()
        channels = []
        if cfg.notifications.email.enabled:
            channels.append(self._email_channel())
        self.notifier = NotificationManager(cfg.notifications, self.db, self.bus, channels, self.clock,
                                            diagnosis_coming=self._diagnosis_coming)
        self.ollama_installer = OllamaInstaller(lambda: self.ai.ollama.version())
        self.diagnoser = Diagnoser(cfg, self.db, self.ai, self.clock, self.ffmpeg,
                                   notify=self.notifier.send_diagnosis)
        self.api = create_app(ApiContext(cfg=cfg, db=self.db, bus=self.bus, status_fn=self.status,
                                         trigger_fn=self._report, started_utc=self.started_utc,
                                         notifier=self.notifier, settings_fn=self.apply_settings,
                                         extra={"ai": self.ai, "installer": self.ollama_installer,
                                                "fresh_frame": self._fresh_frame,
                                                "selftest": self.run_selftest,
                                                "control": self.printer_control,
                                                "printer_files": self.printer_files,
                                                "start_file": self.start_file,
                                                "view_bed": self.view_bed,
                                                "check_plate": self.check_plate,
                                                "upload_file": self.upload_file,
                                                "latest_frame": self._latest_frame,
                                                "set_ai_key": self.set_ai_key,
                                                "cancel_autopause": self.autopause.cancel,
                                                "expected_picture": self.expected_picture,
                                                "simulated_picture": self.simulated_picture,
                                                "app": self}))
        self.bus.subscribe("trigger.manual", self._on_manual)
        self.bus.subscribe("printer.connected", self._log_event)
        self.bus.subscribe("printer.connected", self._on_reconnect)
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

    def _latest_frame(self) -> tuple | None:
        """(time, JPEG bytes) of the newest stored picture, or None. No extra request to the printer."""
        data = getattr(self.capture, "last_frame_bytes", None)
        last = self.capture.last_frame_utc
        return (last, data) if data and last else None

    async def _fresh_frame(self) -> bytes | None:
        """The current camera picture, only if pictures are really coming in right now."""
        latest = self._latest_frame()
        if latest is None or (self.clock.now_utc() - latest[0]).total_seconds() > 3.0:
            return None
        return latest[1]

    async def set_ai_key(self, key: str | None) -> dict:
        """Save or remove the AI key typed on the Settings page. Raises ValueError. The key is never logged."""
        secrets_store.put(self.cfg, secrets_store.AI_KEY, key)
        self.db.log("settings", "info", "AI API key " + ("saved" if key else "removed") + " from the dashboard")
        await self.ai.refresh(force=True)
        return {"set": bool(key), "status": self.ai.status()}

    def _diagnosis_coming(self, uid: str) -> bool:
        row = self.db.get_incident_by_uid(uid)
        if row is None or not self.cfg.ai.enabled or not self.ai.usable:
            return False
        from guardian.ai.diagnosis import wanted
        types = {e["event_type"] for e in self.db.list_events(row["id"])}
        return wanted(self.cfg.ai.diagnose, row, types)

    def print_context(self) -> dict:
        """What is being printed, for the AI checks (from the printer's live state and the file's info)."""
        st = self.tracker.status
        ps = st.get("print_stats", {})
        info = ps.get("info") or {}
        meta = self._file_meta if self._file_meta.get("filename") == ps.get("filename") else {}
        objects = [o.get("name") for o in (st.get("exclude_object", {}).get("objects") or []) if isinstance(o, dict)]
        pos = st.get("toolhead", {}).get("position") or []
        used = st.get("print_task_config", {}).get("extruders_used") or []
        colors = [c for i, c in enumerate(meta.get("colors") or []) if i < len(used) and used[i]] or meta.get("colors") or []
        return {"file": ps.get("filename"), "objects": objects, "layer": info.get("current_layer"),
                "total_layers": info.get("total_layer") or meta.get("layer_count"),
                "height_now": pos[2] if len(pos) > 2 and isinstance(pos[2], (int, float)) else None,
                "height_total": meta.get("object_height"), "colors": sorted(set(colors))}

    def print_palette(self) -> list[dict]:
        """The file's filament colors for the tools this print uses: [{tool, hex, name}]. For the legend under the simulated picture."""
        st = self.tracker.status
        name = st.get("print_stats", {}).get("filename")
        if not name or self._file_meta.get("filename") != name:
            return []
        used = st.get("print_task_config", {}).get("extruders_used") or []
        hexes = self._file_meta.get("hex") or []
        return [{"tool": i, "hex": h, "name": color_name(h)} for i, h in enumerate(hexes) if not used or (i < len(used) and used[i])]

    def _new_print_check(self) -> None:
        """When a print starts, forget the plate map of the last one. Bug seen 2026-11-16: the same file printed again kept
        the finished model, so the picture showed the whole print from the first layer."""
        state = self.tracker.print_state
        if state == "printing" and self._prev_print_state not in ("printing", "paused"):
            self._reset_plate_map()
        self._prev_print_state = state

    def _reset_plate_map(self) -> None:
        if self._gcode_task and not self._gcode_task.done():
            self._gcode_task.cancel()
        self.gcode.reset(None)
        self._gcode_last = 0.0  # read again right away

    def _follow_gcode(self) -> None:
        """Keep the plate height map up to date, every 30 s, in the background."""
        if self._gcode_task and not self._gcode_task.done():
            return
        now = self.clock.monotonic()
        if now - self._gcode_last < 30:
            return
        self._gcode_last = now
        st = self.tracker.status
        name = st.get("print_stats", {}).get("filename")
        pos = st.get("virtual_sdcard", {}).get("file_position")
        if self.tracker.print_state == "complete" and self._file_meta.get("filename") == name:
            pos = self._file_meta.get("size")  # a finished print is still on the plate: read the file to the end
        if not name or not isinstance(pos, int):
            return
        if self.gcode.filename == name and pos < self.gcode.read_upto and self.tracker.print_state in ("printing", "paused"):
            self._reset_plate_map()  # the printer is earlier in the file than we have read: it is a new print
            return

        async def run() -> None:
            try:
                await self.gcode.update(name, pos)
                self.gcode_error = None
            except Exception as exc:  # noqa: BLE001 - only extra detail
                self.gcode_error = f"{type(exc).__name__}: {exc}"
                log.info("could not read the print file: %s", self.gcode_error)

        self._gcode_task = asyncio.create_task(run())

    # ---- camera setup pictures and the measured match ----

    SETUP_HEIGHTS = (("z01", 0.0, 1.5), ("z10", 8.0, 12.0), ("z30", 27.0, 33.0), ("z50", 45.0, 55.0))

    def _bed_z(self) -> float | None:
        pos = self.tracker.status.get("toolhead", {}).get("position") or []
        return float(pos[2]) if len(pos) > 2 and isinstance(pos[2], (int, float)) else None

    def _objects(self) -> list[dict]:
        out = []
        for o in self.tracker.status.get("exclude_object", {}).get("objects") or []:
            if isinstance(o, dict) and isinstance(o.get("polygon"), list):
                out.append({"name": o.get("name"), "polygon": o["polygon"]})
        return out

    async def save_frame(self, name: str, jpeg: bytes, bed_z: float) -> dict:
        """Keep one camera picture for the setup page, with the bed height and the part outlines."""
        import json as _json
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        meta = {"name": name, "bed_z": round(bed_z, 2), "utc": to_iso(self.clock.now_utc()),
                "file": self.tracker.status.get("print_stats", {}).get("filename"),
                "height_now": round(self.gcode.plate.max_z, 2) if self.gcode.filename else 0.0, "objects": self._objects()}
        (self.frames_dir / f"{name}.jpg").write_bytes(jpeg)
        (self.frames_dir / f"{name}.json").write_text(_json.dumps(meta), encoding="utf-8")
        live = sorted(self.frames_dir.glob("live_*.json"))
        for old in live[:-4]:  # keep the last 4 hand-saved pictures
            old.unlink(missing_ok=True)
            old.with_suffix(".jpg").unlink(missing_ok=True)
        return meta

    async def _save_setup_frame(self) -> None:
        name = self.tracker.status.get("print_stats", {}).get("filename")
        if name != self._frames_file:
            self._frames_file, self._frames_this_print = name, set()
        z = self._bed_z()
        if z is None:
            return
        for tag, lo, hi in self.SETUP_HEIGHTS:
            if lo <= z <= hi and tag not in self._frames_this_print:
                jpeg = await self._fresh_frame()
                if jpeg:
                    self._frames_this_print.add(tag)
                    await self.save_frame(tag, jpeg, z)
                    log.info("saved a camera setup picture at bed height %.1f mm", z)

    def _run_match(self) -> None:
        if not self.camera_setup.ready or (self._match_task and not self._match_task.done()):
            return
        now = self.clock.monotonic()
        if now - self._match_last < 60 or not self.gcode.plate.max_z:
            return
        self._match_last = now
        z = self._bed_z()
        pos = self.tracker.status.get("toolhead", {}).get("position") or []

        async def run() -> None:
            jpeg = await self._fresh_frame()
            if not jpeg or z is None:
                return
            rgb = await asyncio.to_thread(decode_jpeg, self.ffmpeg, jpeg)
            if not rgb:
                return
            res = await asyncio.to_thread(match_score, rgb, self.camera_setup, self.gcode.plate, z,
                                          (pos[0], pos[1]) if len(pos) > 1 else None)
            if res:
                self.match_history.append({"utc": to_iso(self.clock.now_utc()), "bed_z": round(z, 2), **res.as_dict()})
                del self.match_history[:-240]

        self._match_task = asyncio.create_task(run())

    def bed_at_top(self) -> bool:
        z = self._bed_z()
        homed = "z" in str(self.tracker.status.get("toolhead", {}).get("homed_axes", ""))
        return bool(homed and z is not None and z <= TOP_Z)

    def last_print(self) -> dict:
        ps = self.tracker.status.get("print_stats", {})
        name = ps.get("filename")
        height = self.gcode.plate.max_z if self.gcode.filename == name and self.gcode.plate.max_z else \
            (self._file_meta.get("object_height") if self._file_meta.get("filename") == name else None)
        return {"file": name or None, "state": ps.get("state"), "height_mm": round(height, 1) if height else None}

    def match_status(self) -> dict:
        h = self.match_history
        last = h[-1] if h else None
        return {"setup_ready": self.camera_setup.ready, "rms_px": self.camera_setup.data.get("rms_px"),
                "last": last, "count": len(h),
                "note": "Watch only. Guardian records these scores to learn what is normal before it may act on them."}

    def expected_picture(self) -> bytes | None:
        """The plate as the print file says it should be now (PNG), or None."""
        name = self.tracker.status.get("print_stats", {}).get("filename")
        if not name or self.gcode.filename != name or not self.gcode.plate.max_z:
            return None
        colors = self._file_meta.get("hex") or []
        return draw_top(self.gcode.plate, colors)

    async def _simulated_async(self) -> bytes | None:
        return await asyncio.to_thread(self.simulated_picture)

    def _tool_on_carriage(self) -> int | None:
        """The toolhead number (0 to 3) that is on the carriage, so its dock is drawn empty. Only during a print: when the
        printer is idle every toolhead is in its dock. UNVERIFIED: that dock number equals the extruder number."""
        if self.tracker.print_state not in ("printing", "paused"):
            return None
        name = str(self.tracker.status.get("toolhead", {}).get("extruder") or "")
        digits = name[len("extruder"):] if name.startswith("extruder") else ""
        if name == "extruder":
            return 0
        return int(digits) if digits.isdigit() and int(digits) < 4 else None

    def simulated_picture(self) -> bytes | None:
        """What the camera should see now (PNG), drawn from the print file through the camera setup. None if not set up."""
        name = self.tracker.status.get("print_stats", {}).get("filename")
        z = self._bed_z()
        if not self.camera_setup.ready or z is None:
            return None
        plate = self.gcode.plate
        have = bool(name and self.gcode.filename == name and plate.max_z)
        colors = self._file_meta.get("hex") or [] if self._file_meta.get("filename") == name else []
        cell, blocks = 1, []
        if have:
            for cell in (1, 2, 3):  # the finest blocks that stay quick to draw
                blocks = plate.blocks(cell, with_base=True)
                if len(blocks) <= 30000:
                    break
        pos = self.tracker.status.get("toolhead", {}).get("position") or []
        xy = (float(pos[0]), float(pos[1])) if len(pos) > 1 and all(isinstance(v, (int, float)) for v in pos[:2]) else None
        return render_simulation(self.camera_setup, blocks, z, colors, cell_mm=cell, max_z=plate.max_z if have else 0.0,
                                 carriage_xy=xy, away=self._tool_on_carriage())

    async def _refresh_file_meta(self) -> None:
        """Read the print file's info (height, layers, colors) once per print. Read only."""
        name = self.tracker.status.get("print_stats", {}).get("filename")
        if not name or self._file_meta.get("filename") == name or self._file_meta_tried == name:
            return
        self._file_meta_tried = name
        url = f"http://{self.cfg.printer.host}:{self._printer_port}/server/files/metadata"
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(url, params={"filename": name})
            m = r.json().get("result", {})
        except Exception:  # noqa: BLE001 - only extra detail for the AI
            log.info("could not read the print file's info", exc_info=True)
            return
        hexes = [h.strip() for h in str(m.get("filament_colour") or "").split(";") if h.strip()]
        self._file_meta = {"filename": name, "object_height": m.get("object_height"), "layer_count": m.get("layer_count"),
                           "size": m.get("size"),
                           "colors": [color_name(h) for h in hexes], "hex": hexes}

    async def printer_control(self, action: str) -> dict:
        """Pause, Resume or Stop from the dashboard. A person clicked and confirmed. Raises ValueError."""
        if not self.cfg.controls.enabled:
            raise ValueError("Printer controls are turned off in Settings.")
        state = self.tracker.print_state
        allowed = {"pause": ("printing",), "resume": ("paused",), "cancel": ("printing", "paused")}
        if action not in allowed:
            raise ValueError("unknown action")
        if state not in allowed[action]:
            raise ValueError(f"The printer is {state or 'not connected'}, so {action} does not apply.")
        send = {"pause": self.client.pause_print, "resume": self.client.resume_print,
                "cancel": self.client.cancel_print}[action]
        if not await send():
            raise ValueError("The printer is not connected.")
        log.warning("dashboard user asked the printer to %s the print", action)
        self.db.log("controls", "warning", f"user asked the printer to {action} the print", {"state_before": state})
        return {"sent": action, "state_before": state}

    # ---- reprint, print a file, upload a file (owner asked, 2026-11-15) ----
    _bed_view_busy = False
    IDLE_STATES = ("standby", "complete", "cancelled")

    @staticmethod
    def clean_gcode_name(name: str) -> str:
        """A G-code path on the printer (folders allowed, no '..', no drive or leading slash). Raises ValueError."""
        n = (name or "").replace("\\", "/").strip()
        parts = n.split("/")
        if (not n or n.startswith("/") or ":" in n or any(p in ("", ".", "..") for p in parts)
                or not n.lower().endswith((".gcode", ".gco", ".g"))):
            raise ValueError("That is not a G-code file name.")
        return n

    def _check_can_start(self) -> None:
        if not self.cfg.controls.print_files:
            raise ValueError("Print files are turned off in Settings.")
        state = self.tracker.print_state
        if state not in self.IDLE_STATES:
            raise ValueError(f"The printer is {state or 'not connected'}. It must be idle to start a print or send a file.")

    async def view_bed(self) -> dict:
        """Before a new print: home, park the toolhead at the back and raise the bed to Z 5, so the camera sees the whole
        plate. The same three approved scripts as the camera setup. Saves no pictures. Only when idle. Raises ValueError."""
        self._check_can_start()
        if not self.cfg.controls.setup_moves:
            raise ValueError("Bed moves are turned off in Settings (Let the camera setup move the bed).")
        if self._bed_view_busy:
            raise ValueError("The bed is already moving.")
        self._bed_view_busy = True
        try:
            for script in (HOME, PARK, Z5):
                try:
                    await self.client.setup_move(script)
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
                if self.tracker.print_state not in self.IDLE_STATES:
                    raise ValueError("A print started, so Guardian stopped moving the bed.")
        finally:
            self._bed_view_busy = False
        self.db.log("controls", "warning", "user asked to raise the bed to see the plate before a print", {})
        return {"moved": True}

    def _files_url(self, tail: str) -> str:
        return f"http://{self.cfg.printer.host}:{self._printer_port}/server/files/{tail}"

    async def printer_files(self) -> list[dict]:
        """G-code files on the printer, newest first. Read only. Raises ValueError."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(self._files_url("list"), params={"root": "gcodes"})
            r.raise_for_status()
            rows = r.json().get("result", [])
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Could not read the printer's file list: {exc}") from exc
        out = [{"path": f["path"], "size": f.get("size"), "modified": f.get("modified")}
               for f in rows if isinstance(f, dict) and isinstance(f.get("path"), str)]
        out.sort(key=lambda f: f["modified"] or 0, reverse=True)
        return out[:200]

    PLATE_VISIBLE_Z = 60.0  # the camera sees the plate when the bed is raised to about this Z or less (setup pictures go to 50)

    async def check_plate(self) -> dict:
        """Look at the build plate with the AI before a print starts. Read only: it uses the camera picture and the AI.
        Returns {"state": "clear" | "not_clear" | "cannot_check", "message", ...}. It only says clear when two looks, a
        couple of seconds apart, both say the plate is clear with enough confidence. UNVERIFIED on the real U1."""
        from guardian.ai.vision import _bool, _conf

        def out(state: str, message: str, **extra) -> dict:
            return {"state": state, "message": message, **extra}

        z = self._bed_z()
        if z is None or z > self.PLATE_VISIBLE_Z:
            return out("cannot_check", "The bed is lowered, so the camera cannot see the plate. Use \"Raise the bed so I can see "
                       "the plate\" first, then check again.")
        if not self.ai.usable:
            return out("cannot_check", "The plate check needs the AI. Turn it on in Settings (AI), then try again.")
        looks = []
        for n in range(2):
            frame = await self._fresh_frame()
            if frame is None:
                return out("cannot_check", "There is no fresh camera picture right now.")
            try:
                a = await self.vision.look_at_plate(frame)
            except Exception as exc:  # noqa: BLE001 - any AI failure means no verdict
                log.info("plate check failed: %s", exc)
                a = None
            if a is None:
                return out("cannot_check", "The AI did not give an answer.")
            looks.append(a)
            if n == 0:
                await asyncio.sleep(2.0)
        minc = self.cfg.ai.min_confidence
        seen = [a for a in looks if _bool(a.get("plate_visible")) is not False]
        if len(seen) < len(looks):
            return out("cannot_check", "The AI could not see the build plate in the picture.", description=looks[-1].get("description"))
        for a in looks:
            if _bool(a.get("clear")) is False and _conf(a.get("confidence")) >= minc:
                objs = a.get("objects") if isinstance(a.get("objects"), list) else []
                what = ", ".join(str(o) for o in objs[:5])
                return out("not_clear", "The AI sees something on the plate" + (f" ({what})" if what else "") + ": "
                           + str(a.get("description") or ""), description=a.get("description"), objects=objs)
        if all(_bool(a.get("clear")) is True and _conf(a.get("confidence")) >= minc for a in looks):
            return out("clear", "The AI looked twice and the plate looks clear.", description=looks[-1].get("description"))
        return out("cannot_check", "The AI was not sure about the plate.", description=looks[-1].get("description"))

    async def start_file(self, filename: str | None, plate_clear: bool, override: bool = False) -> dict:
        """Start a print. filename None means reprint the last file. A person clicked, confirmed and said the plate is empty."""
        self._check_can_start()
        if not plate_clear:
            raise ValueError("Confirm that the plate is empty first.")
        name = filename or self.last_print()["file"]
        if not name:
            raise ValueError("There is no last file to reprint.")
        name = self.clean_gcode_name(name)
        if self.cfg.controls.plate_check:
            check = await self.check_plate()
            if check["state"] == "not_clear" and override:
                self.db.log("controls", "warning", "user started a print although the plate check saw something", check)
            elif check["state"] != "clear":
                raise PlateCheckFailed(check)
        try:
            await self.client.start_print(name)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        log.warning("dashboard user asked the printer to start %s", name)
        self.db.log("controls", "warning", f"user asked the printer to start {name}",
                    {"reprint": filename is None, "state_before": self.tracker.print_state})
        return {"started": name}

    async def upload_file(self, name: str, path: Path) -> dict:
        """Send a G-code file to the printer's gcodes folder. It does not start a print. Raises ValueError."""
        self._check_can_start()
        name = self.clean_gcode_name(name)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)) as c:
                with path.open("rb") as fh:
                    r = await c.post(self._files_url("upload"), data={"root": "gcodes", "print": "false"},
                                     files={"file": (name, fh, "application/octet-stream")})
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"The printer did not take the file: {exc}") from exc
        self.db.log("controls", "warning", f"user sent {name} to the printer", {"bytes": path.stat().st_size})
        return {"uploaded": name}

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

    def _email_channel(self) -> ResendEmailChannel:
        return ResendEmailChannel(self.cfg.notifications.email, key_fn=lambda: secrets_store.email_key(self.cfg)[0])

    async def set_email_key(self, key: str | None) -> dict:
        """Save or remove the Resend key typed on the Settings page. Raises ValueError. The key is never logged."""
        secrets_store.put(self.cfg, secrets_store.EMAIL_KEY, key)
        self.db.log("settings", "info", "Resend API key " + ("saved" if key else "removed") + " from the dashboard")
        k, src = secrets_store.email_key(self.cfg)
        return {"set": bool(k), "source": src, "hint": secrets_store.hint(k)}

    def _sync_write_permissions(self) -> None:
        """The client may only send the two opt-in requests while their settings allow it."""
        self.client.allow_camera_start = self.snapshot_mode and self.cfg.camera.keepalive != "off"
        self.client.allow_pause = self.cfg.autopause.enabled
        self.client.allow_controls = self.cfg.controls.enabled
        self.client.allow_setup_moves = self.cfg.controls.setup_moves
        self.client.allow_start_print = self.cfg.controls.print_files

    def apply_settings(self, changes: dict) -> list[str]:
        """Check, save and apply dashboard settings. Raises settings.SettingsError."""
        changed = settings_mod.save(self.cfg, changes)
        self._sync_write_permissions()
        if any(k.startswith("notifications.email.") for k in changed):
            email = self.cfg.notifications.email
            self.notifier.channels = [self._email_channel()] if email.enabled else []
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
        if getattr(self, "setup_mover", None) is not None and self.setup_mover.running:
            return True  # the camera setup needs fresh pictures while the bed moves
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
            "controls": {"enabled": self.cfg.controls.enabled, "print_files": self.cfg.controls.print_files},
            "last_file": self.last_print()["file"],
            "blackbox": {"pre_s": self.cfg.blackbox.pre_trigger_s, "post_s": self.cfg.blackbox.post_trigger_s},
            "match": self.match_status(),
            "print_file": {"read_mb": round(self.gcode.read_upto / 1e6, 1) if self.gcode.filename else 0,
                           "max_z_mm": round(self.gcode.plate.max_z, 2), "error": self.gcode_error,
                           "has_picture": bool(self.gcode.filename and self.gcode.plate.max_z), "palette": self.print_palette()},
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
                "configured": bool(secrets_store.email_key(self.cfg)[0]),
                "key_source": secrets_store.email_key(self.cfg)[1],
                "key_hint": secrets_store.hint(secrets_store.email_key(self.cfg)[0]),
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

    def _on_reconnect(self, topic: str, payload) -> None:
        """A reconnect often means the printer restarted, maybe after a firmware update. Check again."""
        if isinstance(payload, dict) and payload.get("n", 1) > 1:
            task = asyncio.create_task(self._startup_selftest())
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

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
                self._new_print_check()
                if self.tracker.print_state == "complete":
                    await self._refresh_file_meta()
                    self._follow_gcode()
                if self.tracker.print_state in ("printing", "paused"):
                    await self._refresh_file_meta()
                    self._follow_gcode()
                    await self._save_setup_frame()
                    self._run_match()
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
                now = self.clock.monotonic()
                if self._purge_at is None or now >= self._purge_at:  # at start, then once an hour
                    self._purge_at = now + 3600.0
                    await asyncio.to_thread(purge_old, self.cfg.storage, self.db, self.clock)
            except Exception:  # noqa: BLE001
                log.exception("old incident cleanup failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)
            except asyncio.TimeoutError:
                pass


def color_name(hexcode: str) -> str:
    """A plain color word for a filament hex code like #FFEBCB (the AI understands words better than hex)."""
    names = {"white": (255, 255, 255), "cream": (255, 235, 200), "black": (20, 20, 20), "grey": (128, 128, 128),
             "red": (220, 30, 30), "orange": (255, 150, 10), "yellow": (250, 220, 30), "green": (40, 170, 60),
             "blue": (30, 80, 220), "purple": (130, 50, 180), "pink": (255, 130, 180), "brown": (120, 70, 30)}
    try:
        h = hexcode.lstrip("#")
        rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return hexcode
    return min(names, key=lambda n: sum((a - b) ** 2 for a, b in zip(names[n], rgb)))


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
