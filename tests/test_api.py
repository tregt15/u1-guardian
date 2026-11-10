"""API tests. Fake clip file, real database, no FFmpeg and no printer."""

import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from guardian.api.app import ApiContext, create_app
from guardian.blackbox.incident_manager import TriggerResult
from guardian.bus import EventBus
from guardian.clock import to_iso
from guardian.config import ApiConfig, BlackboxConfig, CameraConfig, Config, ConfigError, StorageConfig, load_config
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
CLIP = bytes(range(256)) * 8  # 2048 bytes, so range requests are easy to check


def make_env(tmp_path, token="", latency_ms=0):
    cfg = Config(
        camera=CameraConfig(latency_offset_ms=latency_ms),
        storage=StorageConfig(data_dir=tmp_path / "data", buffer_dir=tmp_path / "buf"),
        api=ApiConfig(token=token),
    )
    db = Database(cfg.storage.db_path)
    calls = []

    async def trigger(event_type, source, severity, key, metadata):
        calls.append((event_type, source, severity, key, metadata))
        return TriggerResult("created", "2026-11-08T12-30-00Z_0099", 99)

    ctx = ApiContext(cfg=cfg, db=db, bus=EventBus(), started_utc=T0,
                     status_fn=lambda: {"printer": {"connected": True}, "camera": {"mode": "standin"},
                                        "incidents": {"open": 0}},
                     trigger_fn=trigger)
    return SimpleNamespace(cfg=cfg, db=db, ctx=ctx, calls=calls, client=TestClient(create_app(ctx)))


def add_incident(env, n=1, *, event_type="print_paused_with_exception", severity="critical",
                 filename="sheep.gcode", status="complete", with_clip=True, protected=0):
    first = T0 + timedelta(minutes=n)
    uid = env.db.next_uid(first)
    iid = env.db.create_incident(
        uid=uid, event_type=event_type, source="moonraker", severity=severity,
        start_time_utc=to_iso(first - timedelta(seconds=30)), first_trigger_utc=to_iso(first),
        last_trigger_utc=to_iso(first), end_time_utc=to_iso(first + timedelta(seconds=15)),
        print_filename=filename, recording_status=status, actual_pre_s=30.1, actual_post_s=15.2,
        protected=protected)
    env.db.add_event(iid, ts_utc=first, event_type=event_type, source="moonraker", severity=severity,
                     is_trigger=True, metadata={"message": "boom"})
    env.db.add_event(iid, ts_utc=first + timedelta(seconds=5), event_type="later", source="klipper")
    folder = incident_dir(env.cfg.storage, iid, first)
    if with_clip:
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(CLIP)
        env.db.update_incident(iid, recording_path=f"incidents/{first:%Y}/{first:%m}/incident_{iid:04d}/clip.mp4")
    return uid, iid, folder


def test_status_and_health_are_open_even_with_token(tmp_path):
    env = make_env(tmp_path, token="s3cret")
    r = env.client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["guardian"]["auth_required"] is True
    assert "storage" in body and body["printer"]["connected"] is True
    assert env.client.get("/api/health").status_code == 200


def test_token_required_for_incidents(tmp_path):
    env = make_env(tmp_path, token="s3cret")
    uid, _, _ = add_incident(env)
    assert env.client.get("/api/incidents").status_code == 401
    assert env.client.get("/api/incidents", headers={"X-Guardian-Token": "wrong"}).status_code == 401
    assert env.client.get("/api/incidents", headers={"X-Guardian-Token": "s3cret"}).status_code == 200
    assert env.client.get("/api/incidents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert env.client.get(f"/api/incidents/{uid}/video?token=s3cret").status_code == 200  # for <video>
    assert env.client.post("/api/incidents", json={}).status_code == 401
    assert env.client.delete(f"/api/incidents/{uid}?confirm=true").status_code == 401


def test_non_local_bind_needs_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token is required"):
        ApiConfig(bind="0.0.0.0")
    assert ApiConfig(bind="0.0.0.0", token="x").is_local is False
    assert ApiConfig().is_local is True
    monkeypatch.setenv("GUARDIAN_API_TOKEN", "from-env")
    assert ApiConfig(bind="0.0.0.0").effective_token == "from-env"
    bad = tmp_path / "c.yaml"
    monkeypatch.delenv("GUARDIAN_API_TOKEN")
    bad.write_text("api:\n  bind: 0.0.0.0\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_list_filters_and_paging(tmp_path):
    env = make_env(tmp_path)
    add_incident(env, 1, event_type="manual", severity="info", filename="a_b.gcode")
    add_incident(env, 2, event_type="print_error", severity="critical", filename="sheep.gcode")
    add_incident(env, 3, event_type="manual", severity="warning", filename="50%_done.gcode")
    c = env.client
    assert c.get("/api/incidents").json()["total"] == 3
    assert [i["event_type"] for i in c.get("/api/incidents").json()["incidents"]][0] == "manual"  # newest first
    assert c.get("/api/incidents?type=manual").json()["total"] == 2
    assert c.get("/api/incidents?severity=critical").json()["total"] == 1
    assert c.get("/api/incidents?q=sheep").json()["total"] == 1
    assert c.get("/api/incidents?q=%25").json()["total"] == 1       # a literal "%" is not a wildcard
    assert c.get("/api/incidents?q=_").json()["total"] == 2         # a literal "_" is not a wildcard
    page = c.get("/api/incidents?limit=2&offset=2").json()
    assert page["total"] == 3 and len(page["incidents"]) == 1
    since = to_iso(T0 + timedelta(minutes=2))
    assert c.get(f"/api/incidents?from={since}").json()["total"] == 2
    assert c.get("/api/incidents?from=yesterday").status_code == 422
    assert c.get("/api/incidents?limit=0").status_code == 422
    assert c.get("/api/incidents?limit=1000").status_code == 422
    assert c.get("/api/incidents?severity=bogus").status_code == 422


def test_sql_injection_attempt_is_harmless(tmp_path):
    env = make_env(tmp_path)
    add_incident(env)
    r = env.client.get("/api/incidents", params={"q": "'; DROP TABLE incidents; --", "type": "x' OR '1'='1"})
    assert r.status_code == 200 and r.json()["total"] == 0
    assert env.client.get("/api/incidents").json()["total"] == 1


def test_detail_has_offsets_marker_and_honest_findings(tmp_path):
    env = make_env(tmp_path, latency_ms=500)
    uid, _, _ = add_incident(env)
    d = env.client.get(f"/api/incidents/{uid}").json()
    assert d["clip"]["marker_s"] == 30.5  # 30 s of pre-roll plus the 0.5 s camera delay
    offsets = [e["clip_offset_s"] for e in d["events"]]
    assert offsets == [30.5, 35.5]
    assert d["events"][0]["metadata"] == {"message": "boom"}
    assert d["findings"]["available"] is False  # no fake "no problems found"
    assert d["incident"]["clip_available"] is True
    assert d["incident"]["protected"] is False


def test_bad_uids_and_unknown_incident(tmp_path):
    env = make_env(tmp_path)
    assert env.client.get("/api/incidents/2026-11-08T12-00-00Z_9999").status_code == 404
    assert env.client.get("/api/incidents/..%2F..%2Fetc").status_code == 404
    assert env.client.get("/api/incidents/not-a-uid/video").status_code == 404


def test_video_range_requests(tmp_path):
    env = make_env(tmp_path)
    uid, _, _ = add_incident(env)
    full = env.client.get(f"/api/incidents/{uid}/video")
    assert full.status_code == 200 and full.content == CLIP
    assert full.headers["content-type"] == "video/mp4"
    part = env.client.get(f"/api/incidents/{uid}/video", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206
    assert part.content == CLIP[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(CLIP)}"


def test_video_path_comes_from_database_and_cannot_escape(tmp_path):
    env = make_env(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    uid, iid, _ = add_incident(env, with_clip=False)
    env.db.update_incident(iid, recording_path="../secret.txt")
    assert env.client.get(f"/api/incidents/{uid}/video").status_code == 404
    env.db.update_incident(iid, recording_path=str(secret))  # absolute path outside the data folder
    assert env.client.get(f"/api/incidents/{uid}/video").status_code == 404
    assert env.client.get(f"/api/incidents/{uid}").json()["incident"]["clip_available"] is False


def test_export_zip(tmp_path):
    env = make_env(tmp_path)
    uid, _, _ = add_incident(env)
    r = env.client.get(f"/api/incidents/{uid}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["clip.mp4", "report.json", "timeline.csv"]
    assert z.read("clip.mp4") == CLIP
    assert "print_paused_with_exception" in z.read("timeline.csv").decode()
    assert not list((env.cfg.storage.resolved_data_dir() / "tmp").glob("*.zip"))  # temp file cleaned up


def test_manual_incident(tmp_path):
    env = make_env(tmp_path)
    r = env.client.post("/api/incidents", json={"note": "looks odd", "severity": "warning"})
    assert r.status_code == 201 and r.json()["uid"].endswith("_0099")
    assert env.calls == [("manual", "manual", "warning", None, {"note": "looks odd"})]
    assert env.client.post("/api/incidents", json={"severity": "huge"}).status_code == 422
    assert env.client.post("/api/incidents", json={"event_type": "evil"}).status_code == 422  # no extra fields


def test_patch_and_delete_rules(tmp_path):
    env = make_env(tmp_path)
    uid, iid, folder = add_incident(env)
    c = env.client
    assert c.patch(f"/api/incidents/{uid}", json={"user_note": "mine", "protected": True}).json()["protected"] is True
    assert c.patch(f"/api/incidents/{uid}", json={"recording_status": "complete"}).status_code == 422
    assert c.patch(f"/api/incidents/{uid}", json={"user_note": "x" * 3000}).status_code == 422
    assert c.delete(f"/api/incidents/{uid}").status_code == 400          # needs confirm
    assert c.delete(f"/api/incidents/{uid}?confirm=true").status_code == 409  # protected
    assert folder.exists()
    c.patch(f"/api/incidents/{uid}", json={"protected": False})
    assert c.delete(f"/api/incidents/{uid}?confirm=true").json() == {"deleted": uid}
    assert not folder.exists() and env.db.get_incident(iid) is None  # file and record go together
    assert c.get(f"/api/incidents/{uid}").status_code == 404


def test_cannot_delete_while_recording(tmp_path):
    env = make_env(tmp_path)
    uid, _, _ = add_incident(env, status="recording", with_clip=False)
    assert env.client.delete(f"/api/incidents/{uid}?confirm=true").status_code == 409


def test_toolchanges_list_and_problem_filter(tmp_path):
    env = make_env(tmp_path)
    assert env.client.get("/api/toolchanges").json()["toolchanges"] == []
    env.db.create_toolchange(requested_tool="extruder1", previous_tool="extruder", started_utc="2026-11-09T01:00:00.000Z",
                             result="ok", duration_ms=4000, transitions='[{"what": "start"}]')
    env.db.create_toolchange(requested_tool="extruder", previous_tool="extruder1", started_utc="2026-11-09T01:01:00.000Z",
                             result="timeout", error="stuck")
    body = env.client.get("/api/toolchanges").json()
    assert len(body["toolchanges"]) == 2 and body["counts"] == {"ok": 1, "timeout": 1}
    assert body["toolchanges"][1]["transitions"] == [{"what": "start"}]
    problems = env.client.get("/api/toolchanges?problems=true").json()["toolchanges"]
    assert [p["result"] for p in problems] == ["timeout"]


def test_dashboard_pages_are_served(tmp_path):
    env = make_env(tmp_path)
    for path, needle in (("/", "U1 Guardian"), ("/incident", "Incident"), ("/static/common.js", "createTextNode"),
                         ("/static/style.css", "--bg")):
        r = env.client.get(path)
        assert r.status_code == 200 and needle in r.text, path
    assert env.client.get("/").headers["x-content-type-options"] == "nosniff"


def test_dashboard_never_uses_innerhtml():
    from guardian.api.app import WEB_DIR
    for f in WEB_DIR.glob("*.js"):
        assert "innerHTML" not in f.read_text(encoding="utf-8"), f.name


def test_settings_get_and_put(tmp_path):
    from guardian import settings as settings_mod
    env = make_env(tmp_path)
    assert env.client.get("/api/settings").json()["writable"] is False
    assert env.client.put("/api/settings", json={"changes": {}}).status_code == 409
    env.ctx.settings_fn = lambda changes: settings_mod.save(env.cfg, changes)
    env.client = TestClient(create_app(env.ctx))
    body = env.client.get("/api/settings").json()
    assert body["writable"] and body["values"]["camera.keepalive"] == "printing"
    assert any(f["key"] == "autopause.triggers" for f in body["fields"])
    r = env.client.put("/api/settings", json={"changes": {"camera.keepalive": "off"}})
    assert r.status_code == 200 and r.json()["changed"] == ["camera.keepalive"]
    assert env.cfg.camera.keepalive == "off"
    bad = env.client.put("/api/settings", json={"changes": {"printer.host": "x"}})
    assert bad.status_code == 400
    assert env.client.get("/settings").status_code == 200


def test_settings_need_token_when_set(tmp_path):
    env = make_env(tmp_path, token="s3cret")
    assert env.client.get("/api/settings").status_code == 401
    assert env.client.put("/api/settings", json={"changes": {}}).status_code == 401



def test_printer_controls_need_confirm_and_report_refusals(tmp_path):
    env = make_env(tmp_path)
    assert env.client.post("/api/printer/pause", json={"confirm": True}).status_code == 409  # no app here
    calls = []

    async def control(action):
        calls.append(action)
        if action == "resume":
            raise ValueError("The printer is printing, so resume does not apply.")
        return {"sent": action}

    env.ctx.extra["control"] = control
    env.client = TestClient(create_app(env.ctx))
    assert env.client.post("/api/printer/pause", json={}).status_code == 400
    assert env.client.post("/api/printer/pause", json={"confirm": True}).json() == {"sent": "pause"}
    r = env.client.post("/api/printer/resume", json={"confirm": True})
    assert r.status_code == 409 and "does not apply" in r.json()["detail"]
    assert env.client.post("/api/printer/gcode", json={"confirm": True}).status_code == 422
    assert calls == ["pause", "resume"]


def test_camera_stream_sends_jpeg_parts(tmp_path):
    from datetime import datetime, timezone
    env = make_env(tmp_path)
    env.ctx.extra["latest_frame"] = lambda: (datetime(2026, 11, 10, tzinfo=timezone.utc), b"\xff\xd8jpeg")
    env.ctx.extra["stream_seconds"] = 0.6
    env.client = TestClient(create_app(env.ctx))
    r = env.client.get("/api/camera/stream")
    assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert r.content.count(b"--frame") == 1 and b"\xff\xd8jpeg" in r.content  # same picture is sent once
