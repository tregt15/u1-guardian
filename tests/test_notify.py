"""Notification tests. A fake Resend server stands in for the network, so nothing real is sent."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from guardian.api.app import ApiContext, create_app
from guardian.bus import EventBus
from guardian.clock import ManualClock, to_iso
from guardian.config import ApiConfig, Config, EmailConfig, NotificationsConfig, StorageConfig
from guardian.env import load_dotenv, parse_dotenv
from guardian.notify.email import RESEND_URL, NotifyError, ResendEmailChannel
from guardian.notify.manager import NotificationManager
from guardian.storage.db import Database

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
KEY = "re_TESTKEY_1234567890"


class FakeResend:
    """Records requests and answers from a list of (status, json) replies, then repeats the last."""

    def __init__(self, replies=None):
        self.requests: list[httpx.Request] = []
        self.replies = list(replies or [(200, {"id": "abc"})])
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(status, json=body)

    @property
    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("GUARDIAN_RESEND_KEY", KEY)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)


def email_cfg(**kw):
    return EmailConfig(enabled=True, to_addrs=["me@example.com"], **kw)


class Rig:
    def __init__(self, tmp_path, replies=None, *, cfg=None, retry=(0.0, 0.0)):
        self.clock = ManualClock(T0)
        self.db = Database(tmp_path / "g.db", self.clock)
        self.bus = EventBus()
        self.fake = FakeResend(replies)
        self.channel = ResendEmailChannel(email_cfg(), transport=self.fake.transport)
        self.cfg = cfg or NotificationsConfig(email=email_cfg(), base_url="http://guardian.local:8080/")
        self.mgr = NotificationManager(self.cfg, self.db, self.bus, [self.channel], self.clock, retry_delays_s=retry)
        self.n = 0

    def incident(self, event_type="print_paused_with_exception", severity="critical", **kw):
        self.n += 1
        first = T0 + timedelta(seconds=10 * self.n)
        uid = self.db.next_uid(first)
        iid = self.db.create_incident(
            uid=uid, event_type=event_type, source="moonraker", severity=severity,
            start_time_utc=to_iso(first - timedelta(seconds=30)), first_trigger_utc=to_iso(first),
            last_trigger_utc=to_iso(first), print_filename=kw.get("print_filename", "sheep.gcode"),
            print_progress=12.5, active_toolhead="extruder2")
        self.db.add_event(iid, ts_utc=first, event_type=event_type, source="moonraker", severity=severity,
                          is_trigger=True, metadata={"message": kw.get("message", "feed error")})
        return uid, iid

    async def created(self, uid, event_type="print_paused_with_exception", severity="critical"):
        await self.bus.publish("incident.created", {"uid": uid, "event_type": event_type,
                                                    "source": "moonraker", "severity": severity})
        await self.mgr.drain()

    def log(self):
        return [dict(r) for r in self.db.list_notifications()][::-1]


# ---- .env loader ----

def test_dotenv_parsing_and_no_override(tmp_path, monkeypatch):
    assert parse_dotenv('﻿# comment\nA=1\nexport B="two words"\nC=\'x\'\n\nbad line\nD=a=b\n') == {
        "A": "1", "B": "two words", "C": "x", "D": "a=b"}
    f = tmp_path / ".env"
    f.write_text("GUARDIAN_TEST_VAR=from_file\nGUARDIAN_TEST_SET=from_file\n", encoding="utf-8")
    monkeypatch.setenv("GUARDIAN_TEST_SET", "from_shell")
    monkeypatch.delenv("GUARDIAN_TEST_VAR", raising=False)
    assert load_dotenv(f, tmp_path / "missing.env") == [f]
    import os
    assert os.environ["GUARDIAN_TEST_VAR"] == "from_file"
    assert os.environ["GUARDIAN_TEST_SET"] == "from_shell"  # real environment wins
    monkeypatch.delenv("GUARDIAN_TEST_VAR")


def test_key_lookup_accepts_resend_standard_name(monkeypatch):
    monkeypatch.delenv("GUARDIAN_RESEND_KEY")
    assert email_cfg().api_key() == ""
    monkeypatch.setenv("RESEND_API_KEY", "other")
    assert email_cfg().api_key() == "other"


def test_config_rules():
    with pytest.raises(ValueError):
        EmailConfig(enabled=True)  # needs a recipient
    with pytest.raises(ValueError):
        NotificationsConfig(attach_clip=True)  # clips never leave the network
    with pytest.raises(ValueError):
        EmailConfig(to_addrs=["a@b.com\r\nBcc: x@y.com"])
    assert EmailConfig().enabled is False  # off by default


def test_no_secrets_in_tracked_files():
    """No Resend-style key, and no value from a local .env, in any file git tracks.
    Failures name the file only. They never print the secret."""
    import re
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not available")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    secrets = []
    env_file = root / ".env"
    if env_file.is_file():
        secrets = [v for v in parse_dotenv(env_file.read_text(encoding="utf-8")).values() if len(v) >= 8]
    pattern = re.compile(r"re_[A-Za-z0-9_]{20,}")
    bad = []
    for name in out.stdout.splitlines():
        path = root / name
        if not path.is_file() or path.suffix in (".png", ".jpg", ".mp4", ".db", ".jsonl"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if (pattern.search(text) and name != "tests/test_notify.py") or any(sec in text for sec in secrets):
            bad.append(name)
    assert not bad, f"possible secret in tracked files: {bad}"


# ---- the channel ----

async def test_email_request_shape():
    fake = FakeResend()
    ch = ResendEmailChannel(email_cfg(), transport=fake.transport)
    await ch.send("Hello\r\nBcc: evil@example.com", "body text")
    req = fake.requests[0]
    assert str(req.url) == RESEND_URL and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = fake.bodies[0]
    assert body["to"] == ["me@example.com"] and body["from"] == "Guardian <onboarding@resend.dev>"
    assert body["text"] == "body text" and "\n" not in body["subject"] and "\r" not in body["subject"]
    assert "attachments" not in body  # text only


async def test_missing_key_message_says_what_to_do(monkeypatch):
    monkeypatch.delenv("GUARDIAN_RESEND_KEY")
    ch = ResendEmailChannel(email_cfg(), transport=FakeResend().transport)
    assert ch.configured is False
    with pytest.raises(NotifyError, match="GUARDIAN_RESEND_KEY") as e:
        await ch.send("s", "t")
    assert e.value.retryable is False


async def test_error_text_never_contains_the_key():
    fake = FakeResend([(401, {"message": f"API key {KEY} is invalid"})])
    ch = ResendEmailChannel(email_cfg(), transport=fake.transport)
    with pytest.raises(NotifyError) as e:
        await ch.send("s", "t")
    assert KEY not in str(e.value) and "Check your API key" in str(e.value) and e.value.retryable is False


# ---- manager ----

async def test_created_then_clip_ready_complete(tmp_path):
    r = Rig(tmp_path)
    uid, iid = r.incident()
    await r.created(uid)
    assert len(r.fake.bodies) == 1
    b = r.fake.bodies[0]
    assert b["subject"] == "[Guardian] CRITICAL: print_paused_with_exception on sheep.gcode"
    for needle in ("feed error", "critical", "sheep.gcode", "12.5% (by layer)", "extruder2",
                   f"http://guardian.local:8080/incident?uid={uid}", "A second message follows"):
        assert needle in b["text"], needle
    r.db.update_incident(iid, recording_status="complete", actual_pre_s=30.2, actual_post_s=15.1)
    await r.bus.publish("incident.finalized", {"uid": uid, "status": "complete"})
    await r.mgr.drain()
    clip = r.fake.bodies[1]
    assert "COMPLETE" in clip["subject"] and "30.2 s" in clip["text"] and "NOT COMPLETE" not in clip["text"]
    assert [x["status"] for x in r.log()] == ["sent", "sent"]


async def test_clip_ready_says_so_when_footage_is_not_complete(tmp_path):
    r = Rig(tmp_path)
    uid, iid = r.incident()
    await r.created(uid)
    r.db.update_incident(iid, recording_status="missing",
                         recording_notes="Nobody was viewing the camera.")
    await r.bus.publish("incident.finalized", {"uid": uid, "status": "missing"})
    await r.mgr.drain()
    text = r.fake.bodies[1]["text"]
    assert "MISSING" in r.fake.bodies[1]["subject"]
    assert "THE FOOTAGE IS NOT COMPLETE" in text and "Nobody was viewing the camera." in text


async def test_merges_do_not_resend_unless_severity_rises(tmp_path):
    r = Rig(tmp_path)
    uid, iid = r.incident(severity="warning")
    await r.created(uid, severity="warning")
    assert len(r.fake.bodies) == 1
    for sev in ("info", "warning"):  # same or lower: no new message
        await r.bus.publish("incident.merged", {"uid": uid, "severity": sev})
    await r.mgr.drain()
    assert len(r.fake.bodies) == 1
    r.db.update_incident(iid, severity="critical")
    await r.bus.publish("incident.merged", {"uid": uid, "severity": "critical"})
    await r.mgr.drain()
    assert len(r.fake.bodies) == 2 and r.fake.bodies[1]["subject"].startswith("[Guardian] RAISED to CRITICAL")


async def test_cooldown_per_event_type(tmp_path):
    r = Rig(tmp_path, cfg=NotificationsConfig(email=email_cfg(), cooldown_s=60))
    a, _ = r.incident("klipper_error_response", "warning")
    b, _ = r.incident("klipper_error_response", "warning")
    c, _ = r.incident("print_error", "critical")
    await r.created(a, "klipper_error_response", "warning")
    await r.created(b, "klipper_error_response", "warning")   # same type inside 60 s: skipped
    await r.created(c, "print_error")                          # different type: sent
    assert len(r.fake.bodies) == 2
    skipped = [x for x in r.log() if x["status"] == "skipped"]
    assert len(skipped) == 1 and "cooldown" in skipped[0]["error"]
    r.clock.advance(61)
    d, _ = r.incident("klipper_error_response", "warning")
    await r.created(d, "klipper_error_response", "warning")
    assert len(r.fake.bodies) == 3


async def test_min_severity_and_send_on_filters(tmp_path):
    r = Rig(tmp_path, cfg=NotificationsConfig(email=email_cfg(), min_severity="warning",
                                              send_on=["incident_created"]))
    uid, _ = r.incident("manual", "info")
    await r.created(uid, "manual", "info")
    assert r.fake.requests == [] and "min_severity" in r.log()[0]["error"]
    uid2, _ = r.incident()
    await r.created(uid2)
    assert len(r.fake.requests) == 1
    await r.bus.publish("incident.finalized", {"uid": uid2, "status": "complete"})
    await r.mgr.drain()
    assert len(r.fake.requests) == 1  # clip_ready is not in send_on


async def test_clip_ready_needs_a_created_message(tmp_path):
    r = Rig(tmp_path)
    uid, _ = r.incident("manual", "info")  # below min_severity, so never announced
    await r.created(uid, "manual", "info")
    await r.bus.publish("incident.finalized", {"uid": uid, "status": "complete"})
    await r.mgr.drain()
    assert r.fake.requests == []


async def test_retries_then_succeeds(tmp_path):
    r = Rig(tmp_path, replies=[(500, {"message": "oops"}), (429, {"message": "slow down"}), (200, {"id": "x"})])
    uid, _ = r.incident()
    await r.created(uid)
    assert len(r.fake.requests) == 3
    assert r.log()[0]["status"] == "sent" and r.log()[0]["attempts"] == 3


async def test_gives_up_after_three_tries_and_logs_failure(tmp_path):
    r = Rig(tmp_path, replies=[(503, {"message": "down"})])
    uid, iid = r.incident()
    await r.created(uid)
    assert len(r.fake.requests) == 3
    row = r.log()[0]
    assert row["status"] == "failed" and row["attempts"] == 3 and "503" in row["error"]
    assert r.db.get_incident(iid)["recording_status"] == "recording"  # the incident is unaffected


async def test_bad_key_is_not_retried_and_never_logged(tmp_path):
    r = Rig(tmp_path, replies=[(401, {"message": f"bad key {KEY}"})])
    uid, _ = r.incident()
    await r.created(uid)
    assert len(r.fake.requests) == 1
    row = r.log()[0]
    assert row["status"] == "failed" and row["attempts"] == 1 and "API key" in row["error"]
    assert all(KEY not in json.dumps(x) for x in r.log())


async def test_no_key_means_skipped_not_crash(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    monkeypatch.delenv("GUARDIAN_RESEND_KEY")
    uid, _ = r.incident()
    await r.created(uid)
    assert r.fake.requests == []
    row = r.log()[0]
    assert row["status"] == "skipped" and "credentials" in row["error"]


async def test_bus_handlers_never_wait_on_the_network(tmp_path):
    """A slow email server must not slow down the incident manager."""
    class Slow:
        name = "email"
        configured = True

        def __init__(self):
            self.sent = 0

        async def send(self, subject, text):
            await asyncio.sleep(0.5)
            self.sent += 1

    r = Rig(tmp_path)
    slow = Slow()
    mgr = NotificationManager(r.cfg, r.db, r.bus, [slow], r.clock)  # adds a second set of handlers
    uid, _ = r.incident()
    t = time.monotonic()
    await r.bus.publish("incident.created", {"uid": uid, "event_type": "x", "source": "s", "severity": "critical"})
    assert time.monotonic() - t < 0.2 and slow.sent == 0
    stop = asyncio.Event()
    worker = asyncio.create_task(mgr.run(stop))
    await asyncio.sleep(0.9)
    stop.set()
    await worker
    assert slow.sent >= 1


# ---- API ----

def api_client(tmp_path, replies=None, token=""):
    r = Rig(tmp_path, replies)
    cfg = Config(storage=StorageConfig(data_dir=tmp_path / "data", buffer_dir=tmp_path / "buf"),
                 api=ApiConfig(token=token), notifications=r.cfg)
    ctx = ApiContext(cfg=cfg, db=r.db, bus=r.bus, status_fn=lambda: {}, trigger_fn=None,
                     started_utc=T0, notifier=r.mgr)
    return r, TestClient(create_app(ctx))


def test_api_test_endpoint_and_log(tmp_path):
    r, c = api_client(tmp_path)
    resp = c.post("/api/notifications/test", json={"channel": "email"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert r.fake.bodies[0]["subject"] == "[Guardian] Test message"
    log = c.get("/api/notifications/log").json()["log"]
    assert log[0]["kind"] == "test" and log[0]["status"] == "sent"
    assert c.post("/api/notifications/test", json={"channel": "sms"}).status_code == 422


def test_api_test_endpoint_reports_failure_without_the_key(tmp_path):
    r, c = api_client(tmp_path, replies=[(403, {"message": f"nope {KEY}"})])
    resp = c.post("/api/notifications/test", json={"channel": "email"})
    assert resp.status_code == 502 and KEY not in resp.text and "API key" in resp.text
    assert KEY not in c.get("/api/notifications/log").text


def test_api_notification_endpoints_need_the_token(tmp_path):
    r, c = api_client(tmp_path, token="s3cret")
    assert c.post("/api/notifications/test", json={}).status_code == 401
    assert c.get("/api/notifications/log").status_code == 401
    assert c.get("/api/notifications/log", headers={"X-Guardian-Token": "s3cret"}).status_code == 200


async def test_no_clip_email_when_the_ai_diagnosis_follows(tmp_path):
    """Owner got 3 emails per incident (2026-11-11). With a diagnosis coming, the clip email is skipped."""
    r = Rig(tmp_path)
    r.mgr.diagnosis_coming = lambda uid: True
    uid, iid = r.incident()
    await r.created(uid)
    r.db.update_incident(iid, recording_status="complete")
    await r.bus.publish("incident.finalized", {"uid": uid, "status": "complete"})
    await r.mgr.drain()
    assert len(r.fake.bodies) == 1
    assert r.log()[-1]["status"] == "skipped" and "diagnosis email follows" in r.log()[-1]["error"]
    await r.mgr.send_diagnosis(uid, "[Guardian] AI diagnosis", "Footage: saved.")
    await r.mgr.drain()
    assert len(r.fake.bodies) == 2
