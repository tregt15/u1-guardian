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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from guardian import __version__, hostinfo
from guardian.bus import EventBus
from guardian.clock import from_iso, to_iso
from guardian.config import Config
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
    extra: dict = field(default_factory=dict)


class PatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protected: bool | None = None
    user_note: str | None = Field(default=None, max_length=2000)


class TestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email"] = "email"


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
                                         q=q, limit=limit, offset=offset)
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
            # Honest: these are not built yet, so say so instead of showing empty "no problems".
            "findings": {"available": False, "note": "The diagnostic engine is not built yet."},
            "snapshots": {"available": False},
        }

    @app.get("/api/incidents/{uid}/video", dependencies=[Depends(auth)])
    async def video(uid: str):
        path = clip_path(get_row(uid))
        if path is None:
            raise HTTPException(404, "no clip for this incident")
        media = "video/mp4" if path.suffix == ".mp4" else "video/x-matroska"
        return FileResponse(path, media_type=media)  # supports HTTP range requests

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

        @app.get("/incident", include_in_schema=False)
        async def incident_page():
            return FileResponse(WEB_DIR / "incident.html")

    return app
