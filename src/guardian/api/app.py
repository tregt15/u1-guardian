"""Guardian HTTP API (spec section 11). FastAPI.

Rules followed here:
- Read only toward the printer. No endpoint sends a printer command.
- Video and file paths come from the database, never from the request.
- On a non-local bind a token is required for everything except /api/status and /api/health.
  (If a token is set, it is required on a local bind too.)
- Not built yet: notification test endpoints (email is not built) and diagnostic findings.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from guardian import __version__, hostinfo
from guardian import settings as settings_mod
from guardian.bus import EventBus
from guardian.clock import from_iso, to_iso
from guardian.config import AI_MODELS, Config
from guardian.camera.calibration import CalibrationError as CalibrationErrorApi
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z_[0-9]{4,}$")
SSE_TOPICS = ("incident.", "printer.connected", "printer.disconnected")


@dataclass
class ApiContext:
    cfg: Config
    db: Database
    bus: EventBus
    status_fn: Callable[[], dict]  # live printer/camera info from the running app
    trigger_fn: Callable[..., Awaitable[Any]]  # (event_type, source, severity, key, metadata)
    started_utc: datetime
    notifier: Any = None  # NotificationManager or None
    settings_fn: Callable[[dict], list[str]] | None = None  # check, save and apply settings
    extra: dict = field(default_factory=dict)


class PatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protected: bool | None = None
    user_note: str | None = Field(default=None, max_length=2000)


class TestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email"] = "email"


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict[str, Any]


class AiModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None


AI_PICTURE_RE = re.compile(r"^ai_picture_[0-9]{1,3}\.jpg$")


class AiKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str | None = Field(default=None, max_length=500)


class SetupBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clicks: list[dict] = Field(default_factory=list, max_length=64)
    rotation: int | None = Field(default=None, ge=0, le=3)
    mirror: bool | None = None
    lens: float | None = Field(default=None, ge=0.0, le=1.0)


SETUP_SHOWN = ("setup_30", "setup_07", "setup_05")  # the only pictures the setup page lists
SETUP_FRAME_RE = re.compile(r"^(z01|z10|z30|z50|setup_30|setup_15|setup_07|setup_05|live_[0-9]{8,14})$")


class MoveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plate_empty: bool = False


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    plate_clear: bool = False
    filename: str | None = Field(default=None, max_length=300)  # None means reprint the last file
    override: bool = False  # start although the plate check saw something (only if the person looked and says it is fine)


MAX_UPLOAD = 300 * 1024 * 1024


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class ManualBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=500)
    severity: Literal["info", "warning", "critical"] = "info"


def create_app(ctx: ApiContext) -> FastAPI:
    cfg, db = ctx.cfg, ctx.db
    app = FastAPI(title="U1 Guardian", version=__version__, docs_url=None, redoc_url=None)
    token = cfg.api.effective_token

    # ---- helpers ----

    def auth(request: Request) -> None:
        if not token:
            return
        supplied = request.headers.get("x-guardian-token", "")
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            supplied = supplied or bearer[7:].strip()
        supplied = supplied or request.query_params.get("token", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "token required")

    def get_row(uid: str):
        if not UID_RE.match(uid):
            raise HTTPException(404, "no such incident")
        row = db.get_incident_by_uid(uid)
        if row is None:
            raise HTTPException(404, "no such incident")
        return row

    def clip_path(row) -> Path | None:
        """Resolve the clip from the database path. It must stay inside the data folder."""
        rel = row["recording_path"]
        if not rel:
            return None
        base = cfg.storage.resolved_data_dir().resolve()
        path = (base / rel).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            return None
        return path

    def summary(row) -> dict:
        d = dict(row)
        d["protected"] = bool(d["protected"])
        d["clip_available"] = clip_path(row) is not None
        return d

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "no-cache")  # re-check each time, so an update shows at once
        return resp

    # ---- open endpoints ----

    @app.get("/api/status")
    async def status() -> dict:
        live = ctx.status_fn()
        data_dir = cfg.storage.resolved_data_dir()
        free = hostinfo.disk_free_gb(data_dir)
        used = hostinfo.folder_size_gb(cfg.storage.incidents_dir)
        warnings = []
        if free is not None and free < cfg.storage.warn_free_gb:
            warnings.append(f"Low disk space: {free} GB free (warning below {cfg.storage.warn_free_gb} GB).")
        if used > cfg.storage.max_total_gb:
            warnings.append(f"Saved incidents use {used} GB, above the {cfg.storage.max_total_gb} GB limit.")
        return {
            **live,
            "guardian": {"version": __version__, "started_utc": to_iso(ctx.started_utc),
                         "auth_required": bool(token)},
            "storage": {"free_gb": free, "incidents_gb": used, "max_total_gb": cfg.storage.max_total_gb,
                        "warnings": warnings},
        }

    @app.get("/api/health")
    async def health() -> dict:
        return {"host": hostinfo.describe(), "cpu_temp_c": hostinfo.cpu_temp_c(),
                "mem_available_mb": hostinfo.mem_available_mb(), "load_avg": hostinfo.load_avg(),
                "disk_free_gb": hostinfo.disk_free_gb(cfg.storage.resolved_data_dir())}

    # ---- incidents (token protected on non-local binds) ----

    @app.get("/api/incidents", dependencies=[Depends(auth)])
    async def list_incidents(
        type: str | None = Query(None, max_length=100),
        severity: Literal["info", "warning", "critical"] | None = None,
        from_: str | None = Query(None, alias="from", max_length=40),
        to: str | None = Query(None, max_length=40),
        q: str | None = Query(None, max_length=200),
        group: Literal["toolchange", "printer", "camera", "ai", "manual"] | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict:
        for label, v in (("from", from_), ("to", to)):
            if v:
                try:
                    from_iso(v)
                except ValueError:
                    raise HTTPException(422, f"'{label}' must be an ISO time like 2026-11-08T12:00:00Z") from None
        rows, total = db.query_incidents(event_type=type, severity=severity, since=from_, until=to,
                                         q=q, group=group, limit=limit, offset=offset)
        return {"total": total, "limit": limit, "offset": offset, "incidents": [summary(r) for r in rows]}

    @app.get("/api/incidents/{uid}", dependencies=[Depends(auth)])
    async def get_incident(uid: str) -> dict:
        row = get_row(uid)
        start = from_iso(row["start_time_utc"]) if row["start_time_utc"] else None
        latency_s = cfg.camera.latency_offset_ms / 1000.0
        events = []
        for e in db.list_events(row["id"]):
            d = dict(e)
            d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else None
            d["is_trigger"] = bool(d["is_trigger"])
            # Position in the clip. The camera shows things a bit late, so add the latency offset.
            d["clip_offset_s"] = (round((from_iso(d["ts_utc"]) - start).total_seconds() + latency_s, 2)
                                  if start else None)
            events.append(d)
        first = from_iso(row["first_trigger_utc"])
        marker = round((first - start).total_seconds() + latency_s, 2) if start else None
        return {
            "incident": summary(row),
            "events": events,
            "clip": {"url": f"/api/incidents/{uid}/video" if clip_path(row) else None,
                     "marker_s": marker, "latency_offset_ms": cfg.camera.latency_offset_ms,
                     "actual_pre_s": row["actual_pre_s"], "actual_post_s": row["actual_post_s"],
                     "status": row["recording_status"], "notes": row["recording_notes"]},
            "findings": findings(row),
            "snapshots": {"available": bool(pictures(row)), "ai_pictures": pictures(row)},
        }

    @app.get("/api/incidents/{uid}/video", dependencies=[Depends(auth)])
    async def video(uid: str):
        path = clip_path(get_row(uid))
        if path is None:
            raise HTTPException(404, "no clip for this incident")
        media = "video/mp4" if path.suffix == ".mp4" else "video/x-matroska"
        return FileResponse(path, media_type=media)  # supports HTTP range requests

    def folder_of(row) -> Path:
        return incident_dir(cfg.storage, row["id"], from_iso(row["first_trigger_utc"]))

    def findings(row) -> dict:
        path = folder_of(row) / "ai_diagnosis.json"
        if path.is_file():
            try:
                return {"ai": json.loads(path.read_text(encoding="utf-8")), "available": True}
            except ValueError:
                pass
        why = ("Turn on local AI in Settings to get a diagnosis." if not cfg.ai.enabled
               else "No AI diagnosis for this incident (not needed, still running, or the AI was not available).")
        return {"available": False, "note": why}

    def pictures(row) -> list[str]:
        folder = folder_of(row)
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.glob("ai_picture_*.jpg") if AI_PICTURE_RE.match(p.name))

    @app.get("/api/incidents/{uid}/pictures/{name}", dependencies=[Depends(auth)])
    async def incident_picture(uid: str, name: str):
        row = get_row(uid)
        if not AI_PICTURE_RE.match(name):
            raise HTTPException(404, "no such picture")
        path = folder_of(row) / name
        if not path.is_file():
            raise HTTPException(404, "no such picture")
        return FileResponse(path, media_type="image/jpeg")

    def ai_service():
        ai = ctx.extra.get("ai")
        if ai is None:
            raise HTTPException(409, "AI is not available in this mode")
        return ai

    @app.get("/api/ai/status", dependencies=[Depends(auth)])
    async def ai_status() -> dict:
        ai = ai_service()
        await ai.refresh(force=True)
        installed: dict[str, int] = {}
        if ai.ollama_version:
            try:
                installed = await ai.ollama.models()
            except Exception:  # noqa: BLE001
                installed = {}
        inst = ctx.extra.get("installer")
        return {**ai.status(), "installed": installed,
                "install_help": "https://ollama.com/download",
                "install": {"plan": inst.plan(), "state": inst.state.as_dict()} if inst else None,
                "models": {k: v for k, v in AI_MODELS.items()}}

    @app.put("/api/ai/key", dependencies=[Depends(auth)])
    async def ai_key(body: AiKeyBody) -> dict:
        """Save (or with an empty key, remove) the AI API key. The key is never sent back."""
        fn = ctx.extra.get("set_ai_key")
        if fn is None:
            raise HTTPException(409, "not available in this mode")
        try:
            return await fn(body.key or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/ai/install", dependencies=[Depends(auth)])
    async def ai_install() -> dict:
        """Install Ollama for the user (they confirmed on the Settings page)."""
        inst = ctx.extra.get("installer")
        if inst is None:
            raise HTTPException(409, "installing is not available in this mode")
        if inst.state.running:
            raise HTTPException(409, "an install is already running")
        plan = inst.plan()
        if not plan.get("can_install"):
            raise HTTPException(409, plan.get("manual") or "Guardian cannot install Ollama here")
        db.log("ai", "warning", "installing Ollama at the user's request", {"method": plan.get("method")})
        task = asyncio.create_task(inst.install())
        ctx.extra.setdefault("tasks", set()).add(task)
        task.add_done_callback(lambda t: ctx.extra["tasks"].discard(t))
        return {"started": True, "method": plan.get("method")}

    @app.post("/api/ai/download", dependencies=[Depends(auth)])
    async def ai_download(body: AiModelBody) -> dict:
        ai = ai_service()
        model = body.model or cfg.ai.model
        if model not in AI_MODELS:
            raise HTTPException(400, "unknown model")
        if ai.ollama.pull_state.running:
            raise HTTPException(409, "a download is already running")
        if not await ai.ollama.version():
            raise HTTPException(409, "Ollama was not found. Install it first (https://ollama.com/download).")
        task = asyncio.create_task(ai.ollama.pull(model))
        ctx.extra.setdefault("tasks", set()).add(task)
        task.add_done_callback(lambda t: ctx.extra["tasks"].discard(t))
        return {"started": model}

    @app.delete("/api/ai/model", dependencies=[Depends(auth)])
    async def ai_delete(body: AiModelBody) -> dict:
        ai = ai_service()
        model = body.model or cfg.ai.model
        if model not in AI_MODELS:
            raise HTTPException(400, "unknown model")
        try:
            await ai.ollama.delete(model)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"could not remove the model: {exc}") from exc
        await ai.refresh(force=True)
        return {"removed": model}

    def _print_fn(key: str):
        fn = ctx.extra.get(key)
        if fn is None:
            raise HTTPException(409, "printer controls are not available in this mode")
        return fn

    @app.get("/api/printer/files", dependencies=[Depends(auth)])
    async def printer_files() -> dict:
        """G-code files on the printer (read only)."""
        try:
            return {"files": await _print_fn("printer_files")()}
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/printer/view-bed", dependencies=[Depends(auth)])
    async def printer_view_bed(body: ControlBody) -> dict:
        """Raise the bed so the camera sees the plate (before a print). Takes about a minute."""
        fn = _print_fn("view_bed")
        if not body.confirm:
            raise HTTPException(400, "confirm must be true")
        try:
            return await fn()
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/printer/start", dependencies=[Depends(auth)])
    async def printer_start(body: StartBody) -> dict:
        """Reprint (no filename) or print a file. Only runs after the page confirmed and the person said the plate is empty."""
        fn = _print_fn("start_file")
        if not body.confirm:
            raise HTTPException(400, "confirm must be true")
        try:
            return await fn(body.filename, body.plate_clear, body.override)
        except ValueError as exc:
            check = getattr(exc, "check", None)
            if check:  # the plate check stopped it: send the result too, so the page can show it
                raise HTTPException(409, {"message": str(exc), "check": check}) from exc
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/printer/check-plate", dependencies=[Depends(auth)])
    async def printer_check_plate() -> dict:
        """Look at the build plate with the AI (read only). The bed must be raised so the camera sees the plate."""
        return await _print_fn("check_plate")()

    @app.post("/api/printer/files/upload", dependencies=[Depends(auth)])
    async def printer_upload(request: Request, name: str, confirm: bool = False) -> dict:
        """Send a G-code file (the request body) to the printer. It does not start a print."""
        fn = _print_fn("upload_file")
        if not confirm:
            raise HTTPException(400, "confirm must be true")
        size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=".gcode") as tmp:
            tmp_path = Path(tmp.name)
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "That file is too big (limit 300 MB).")
                    tmp.write(chunk)
            except HTTPException:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise
        try:
            if size == 0:
                raise HTTPException(400, "The file is empty.")
            return await fn(name, tmp_path)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            tmp_path.unlink(missing_ok=True)

    @app.post("/api/printer/{action}", dependencies=[Depends(auth)])
    async def printer_control(action: Literal["pause", "resume", "cancel"], body: ControlBody) -> dict:
        """Dashboard buttons. Only runs when the page confirmed with the person first."""
        fn = ctx.extra.get("control")
        if fn is None:
            raise HTTPException(409, "printer controls are not available in this mode")
        if not body.confirm:
            raise HTTPException(400, "confirm must be true")
        try:
            return await fn(action)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/camera/stream", dependencies=[Depends(auth)])
    async def camera_stream(request: Request):
        """Live view as MJPEG (multipart). Uses the pictures Guardian already stores, so a viewer adds no
        requests to the printer. Ends when the page closes or after 10 minutes (the page then reconnects)."""
        fn = ctx.extra.get("latest_frame")
        if fn is None:
            raise HTTPException(409, "no camera in this mode")

        async def frames():
            last_sent = None
            loop = asyncio.get_running_loop()
            end = loop.time() + float(ctx.extra.get("stream_seconds", 600))
            while loop.time() < end and not await request.is_disconnected():
                latest = fn()
                if latest and latest[0] != last_sent:
                    last_sent = latest[0]
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(latest[1])).encode() + b"\r\n\r\n" + latest[1] + b"\r\n")
                await asyncio.sleep(0.25)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    # ---- camera setup ----

    def the_app():
        a = ctx.extra.get("app")
        if a is None:
            raise HTTPException(409, "not available in this mode")
        return a

    def frame_meta(a, name: str) -> dict:
        p = a.frames_dir / f"{name}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

    @app.get("/api/camera-setup", dependencies=[Depends(auth)])
    async def camera_setup_get() -> dict:
        a = the_app()
        try:
            await a._refresh_file_meta()  # so the "is the plate empty?" question can name the last print's height
        except Exception:  # noqa: BLE001
            pass
        frames = []
        if a.frames_dir.is_dir():
            for p in sorted(a.frames_dir.glob("*.json")):
                if p.stem in SETUP_SHOWN and p.with_suffix(".jpg").is_file():
                    m = json.loads(p.read_text(encoding="utf-8"))
                    frames.append({k: m.get(k) for k in ("name", "bed_z", "utc", "file", "height_now")} | {"objects": len(m.get("objects") or [])})
        st = a.tracker.status
        return {"ready": a.camera_setup.ready, "rms_px": a.camera_setup.data.get("rms_px"),
                "rotation": a.camera_setup.data.get("rotation", 0), "mirror": a.camera_setup.data.get("mirror", False),
                "clicks": a.camera_setup.data.get("clicks", []), "frames": frames, "lens": a.camera_setup.lens, "using_default": a.camera_setup.using_default,
                "bed_z_now": a._bed_z(), "homed": st.get("toolhead", {}).get("homed_axes", ""),
                "print_state": a.tracker.print_state, "bed_at_top": a.bed_at_top(), "move": a.setup_mover.status(),
                "moves_allowed": a.cfg.controls.setup_moves, "last_print": a.last_print()}

    @app.post("/api/camera-setup/move", dependencies=[Depends(auth)])
    async def camera_setup_move(body: MoveBody) -> dict:
        """Home the printer and raise the bed for the camera setup. Only when idle and the plate is confirmed empty."""
        a = the_app()
        try:
            a.setup_mover.start(body.plate_empty)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return a.setup_mover.status()

    @app.get("/api/camera-setup/frames/{name}", dependencies=[Depends(auth)])
    async def camera_setup_frame(name: str):
        a = the_app()
        if not SETUP_FRAME_RE.match(name) or not (a.frames_dir / f"{name}.jpg").is_file():
            raise HTTPException(404, "no such picture")
        return FileResponse(a.frames_dir / f"{name}.jpg", media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/camera-setup/frames", dependencies=[Depends(auth)])
    async def camera_setup_save_live() -> dict:
        a = the_app()
        jpeg = await a._fresh_frame()
        z = a._bed_z()
        if not jpeg:
            raise HTTPException(409, "No live camera picture right now. Turn on Auto camera, or open the camera in the app.")
        if z is None:
            raise HTTPException(409, "The printer did not report the bed height.")
        name = "live_" + a.clock.now_utc().strftime("%Y%m%d%H%M%S")
        return await a.save_frame(name, jpeg, z)

    @app.put("/api/camera-setup", dependencies=[Depends(auth)])
    async def camera_setup_put(body: SetupBody) -> dict:
        a = the_app()
        if body.clicks:
            for c in body.clicks:
                if not SETUP_FRAME_RE.match(str(c.get("frame", ""))):
                    raise HTTPException(400, "unknown picture in the clicks")
                m = frame_meta(a, c["frame"])
                if not m:
                    raise HTTPException(400, "a clicked picture no longer exists")
                c["bed_z"] = m["bed_z"]
            try:
                a.camera_setup.set_clicks(body.clicks, body.rotation, body.mirror, body.lens)
            except (CalibrationErrorApi, KeyError, TypeError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
        elif body.lens is not None:
            try:
                a.camera_setup.set_lens(body.lens)
            except (CalibrationErrorApi, KeyError, TypeError, ValueError) as exc:
                raise HTTPException(400, str(exc)) from exc
        elif body.rotation is not None or body.mirror is not None:
            if not a.camera_setup.ready:
                raise HTTPException(409, "Click the corners first.")
            a.camera_setup.set_orientation(body.rotation if body.rotation is not None else a.camera_setup.data.get("rotation", 0),
                                           body.mirror if body.mirror is not None else a.camera_setup.data.get("mirror", False))
        return await camera_setup_get()

    @app.delete("/api/camera-setup", dependencies=[Depends(auth)])
    async def camera_setup_delete() -> dict:
        the_app().camera_setup.clear()
        return {"cleared": True}

    @app.get("/api/camera-setup/overlay/{name}", dependencies=[Depends(auth)])
    async def camera_setup_overlay(name: str) -> dict:
        """Outlines to draw on a setup picture: the plate, and the part outlines at the plate and at their height."""
        a = the_app()
        if not SETUP_FRAME_RE.match(name):
            raise HTTPException(404, "no such picture")
        m = frame_meta(a, name)
        if not m or not a.camera_setup.ready:
            return {"lines": []}
        z, top = m["bed_z"], m.get("height_now") or 0.0
        to = a.camera_setup.to_image

        def cut(poly: list[tuple[float, float]], n: int = 12) -> list[tuple[float, float]]:
            """Edges cut in pieces, so the lens bend shows as a curve."""
            out = []
            for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
                out += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
            return out + [poly[-1]]

        lines = [{"kind": "plate", "pts": [to(x, y, 0, z) for x, y in cut([(0, 0), (270, 0), (270, 270), (0, 270), (0, 0)])]}]
        for o in m.get("objects") or []:
            poly = [(float(p[0]), float(p[1])) for p in o["polygon"] if isinstance(p, list) and len(p) >= 2]
            if len(poly) >= 3:
                lines.append({"kind": "part", "pts": [to(x, y, 0, z) for x, y in cut(poly + poly[:1])]})
                if top:
                    lines.append({"kind": "part_top", "pts": [to(x, y, top, z) for x, y in cut(poly + poly[:1])]})
        return {"lines": [ln for ln in lines if all(p is not None for p in ln["pts"])]}

    @app.get("/api/print/expected.png", dependencies=[Depends(auth)])
    async def expected_png():
        fn = ctx.extra.get("expected_picture")
        data = await asyncio.to_thread(fn) if fn else None
        if not data:
            raise HTTPException(404, "no drawing from the print file right now")
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/print/simulated.png", dependencies=[Depends(auth)])
    async def simulated_png():
        """What the camera should see now, drawn from the print file. Same viewpoint as the live picture."""
        fn = ctx.extra.get("simulated_picture")
        data = await asyncio.to_thread(fn) if fn else None
        if not data:
            raise HTTPException(404, "no simulated picture: the camera is not set up yet")
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/camera/latest", dependencies=[Depends(auth)])
    async def camera_latest():
        fn = ctx.extra.get("fresh_frame")
        data = await fn() if fn else None
        if not data:
            raise HTTPException(404, "no live camera picture right now")
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/selftest", dependencies=[Depends(auth)])
    async def selftest_run() -> dict:
        fn = ctx.extra.get("selftest")
        if fn is None:
            raise HTTPException(409, "self-test is not available in this mode")
        return await fn()

    def logs_path(row) -> Path:
        return incident_dir(cfg.storage, row["id"], from_iso(row["first_trigger_utc"])) / "printer_logs.txt"

    @app.get("/api/incidents/{uid}/logs", dependencies=[Depends(auth)])
    async def incident_logs(uid: str) -> dict:
        path = logs_path(get_row(uid))
        if not path.is_file():
            return {"available": False, "text": None,
                    "note": "No printer logs for this incident (still recording, turned off, or the printer could not be read)."}
        return {"available": True, "text": path.read_text(encoding="utf-8", errors="replace")}

    @app.get("/api/incidents/{uid}/export", dependencies=[Depends(auth)])
    async def export(uid: str):
        row = get_row(uid)
        tmp_dir = cfg.storage.resolved_data_dir() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
        zip_path = Path(name)
        with open(fd, "wb") as fh, zipfile.ZipFile(fh, "w", zipfile.ZIP_STORED) as z:
            clip = clip_path(row)
            if clip:
                z.write(clip, arcname=clip.name)
                for extra in clip.parent.glob("clip_original.*"):  # full quality original, if kept
                    z.write(extra, arcname=extra.name)
            plogs = logs_path(row)
            if plogs.is_file():
                z.write(plogs, arcname=plogs.name)
            for extra_name in ["ai_diagnosis.json", *pictures(row)]:
                extra_path = folder_of(row) / extra_name
                if extra_path.is_file():
                    z.write(extra_path, arcname=extra_name)
            detail = await get_incident(uid)
            z.writestr("report.json", json.dumps(
                {"incident": detail["incident"], "events": detail["events"], "clip": detail["clip"]},
                indent=2, sort_keys=True, default=str))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["ts_utc", "clip_offset_s", "event_type", "source", "severity", "is_trigger", "repeat_count"])
            for e in detail["events"]:
                w.writerow([e["ts_utc"], e["clip_offset_s"], e["event_type"], e["source"],
                            e["severity"], int(e["is_trigger"]), e["repeat_count"]])
            z.writestr("timeline.csv", buf.getvalue())
        return FileResponse(zip_path, media_type="application/zip", filename=f"{uid}.zip",
                            background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)))

    @app.post("/api/incidents", status_code=201, dependencies=[Depends(auth)])
    async def manual_incident(body: ManualBody) -> JSONResponse:
        meta = {"note": body.note} if body.note else {}
        result = await ctx.trigger_fn("manual", "manual", body.severity, None, meta)
        code = 201 if result.action == "created" else 200
        return JSONResponse({"uid": result.uid, "action": result.action}, status_code=code)

    @app.patch("/api/incidents/{uid}", dependencies=[Depends(auth)])
    async def patch_incident(uid: str, body: PatchBody) -> dict:
        row = get_row(uid)
        fields: dict[str, Any] = {}
        if body.protected is not None:
            fields["protected"] = int(body.protected)
        if body.user_note is not None:
            fields["user_note"] = body.user_note
        db.update_incident(row["id"], **fields)
        return summary(db.get_incident(row["id"]))

    @app.delete("/api/incidents/{uid}", dependencies=[Depends(auth)])
    async def delete_incident(uid: str, confirm: bool = False) -> dict:
        if not confirm:
            raise HTTPException(400, "add ?confirm=true to delete")
        row = get_row(uid)
        if row["protected"]:
            raise HTTPException(409, "incident is protected, unprotect it first")
        if row["recording_status"] == "recording":
            raise HTTPException(409, "incident is still recording")
        folder = incident_dir(cfg.storage, row["id"], from_iso(row["first_trigger_utc"])).resolve()
        if folder.is_relative_to(cfg.storage.incidents_dir.resolve()) and folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        db.delete_incident(row["id"])  # file and record go together (spec 10.3)
        return {"deleted": uid}

    @app.get("/api/toolchanges", dependencies=[Depends(auth)])
    async def toolchanges(limit: int = Query(100, ge=1, le=500), problems: bool = False) -> dict:
        out = []
        for r in db.list_toolchanges(limit, problems_only=problems):
            item = dict(r)
            try:
                item["transitions"] = json.loads(item["transitions"]) if item["transitions"] else []
            except ValueError:
                pass
            if item.get("incident_id"):
                inc = db.get_incident(item["incident_id"])
                item["incident_uid"] = inc["uid"] if inc else None
            out.append(item)
        return {"toolchanges": out, "counts": db.toolchange_stats()}

    @app.get("/api/toolchanges/summary", dependencies=[Depends(auth)])
    async def toolchange_summary() -> dict:
        return db.toolchange_summary()

    @app.post("/api/autopause/cancel", dependencies=[Depends(auth)])
    async def autopause_cancel() -> dict:
        fn = ctx.extra.get("cancel_autopause")
        if fn is None:
            raise HTTPException(409, "not available in this mode")
        if not await fn():
            raise HTTPException(409, "No pause is waiting right now.")
        return {"cancelled": True}

    @app.get("/api/settings", dependencies=[Depends(auth)])
    async def get_settings() -> dict:
        return {"fields": settings_mod.FIELDS, "values": settings_mod.current(cfg),
                "writable": ctx.settings_fn is not None,
                "file": str(settings_mod.settings_path(cfg))}

    @app.put("/api/settings", dependencies=[Depends(auth)])
    async def put_settings(body: SettingsBody) -> dict:
        if ctx.settings_fn is None:
            raise HTTPException(409, "settings cannot be changed in this mode")
        try:
            changed = ctx.settings_fn(body.changes)
        except settings_mod.SettingsError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"changed": changed, "values": settings_mod.current(cfg)}

    @app.put("/api/notifications/email/key", dependencies=[Depends(auth)])
    async def email_key(body: AiKeyBody) -> dict:
        """Save (or with an empty key, remove) the Resend API key. The key is never sent back."""
        a = ctx.extra.get("app")
        if a is None:
            raise HTTPException(409, "not available in this mode")
        try:
            return await a.set_email_key(body.key or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/notifications/test", dependencies=[Depends(auth)])
    async def notifications_test(body: TestBody) -> dict:
        if ctx.notifier is None:
            raise HTTPException(409, "notifications are not set up")
        result = await ctx.notifier.send_test(body.channel)
        if not result["ok"]:
            raise HTTPException(502, result["error"])
        return result

    @app.get("/api/notifications/log", dependencies=[Depends(auth)])
    async def notifications_log(limit: int = Query(50, ge=1, le=200)) -> dict:
        return {"log": [dict(r) for r in db.list_notifications(limit)]}

    @app.get("/api/events/stream", dependencies=[Depends(auth)])
    async def event_stream(request: Request) -> StreamingResponse:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)

        def on_event(topic: str, payload: Any) -> None:
            if topic.startswith(SSE_TOPICS):
                try:
                    queue.put_nowait((topic, payload))
                except asyncio.QueueFull:
                    pass  # a slow browser must never slow Guardian down

        ctx.bus.subscribe("*", on_event)

        async def gen():
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        topic, payload = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {topic}\ndata: {json.dumps(payload, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                ctx.bus.unsubscribe("*", on_event)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- dashboard (static files, no build step) ----

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(WEB_DIR / "index.html")

        @app.get("/incidents", include_in_schema=False)
        async def incidents_page() -> FileResponse:
            return FileResponse(WEB_DIR / "incidents.html")

        @app.get("/toolchanges", include_in_schema=False)
        async def toolchanges_page() -> FileResponse:
            return FileResponse(WEB_DIR / "toolchanges.html")

        @app.get("/camera-setup", include_in_schema=False)
        async def camera_setup_page() -> FileResponse:
            return FileResponse(WEB_DIR / "camera-setup.html")

        @app.get("/settings", include_in_schema=False)
        async def settings_page() -> FileResponse:
            return FileResponse(WEB_DIR / "settings.html")

        @app.get("/incident", include_in_schema=False)
        async def incident_page():
            return FileResponse(WEB_DIR / "incident.html")

    return app
