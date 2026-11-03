from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from guardian.bus import EventBus
from guardian.clock import ManualClock, from_iso, to_iso, uid_stamp
from guardian.config import Config, ConfigError, load_config
from guardian.storage.db import Database

UTC = timezone.utc
REPO = Path(__file__).resolve().parent.parent


def test_iso_roundtrip_and_naive_rejected():
    dt = datetime(2026, 11, 2, 13, 5, 22, 123000, tzinfo=UTC)
    assert to_iso(dt) == "2026-11-02T13:05:22.123Z"
    assert from_iso(to_iso(dt)) == dt
    assert uid_stamp(dt) == "2026-11-02T13-05-22Z"
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 11, 2))


def test_manual_clock():
    c = ManualClock(datetime(2026, 11, 2, tzinfo=UTC))
    c.advance(5)
    assert c.now_utc() == datetime(2026, 11, 2, 0, 0, 5, tzinfo=UTC)
    assert c.monotonic() == 5


def test_example_config_loads():
    cfg = load_config(REPO / "config.example.yaml")
    assert cfg.blackbox.pre_trigger_s == 30
    assert cfg.blackbox.post_trigger_s == 15
    assert cfg.camera.segment_length_s == 2


def test_defaults_and_paths():
    cfg = Config()
    assert cfg.storage.resolved_buffer_dir().parent == cfg.storage.resolved_data_dir()


def test_config_rejects_typos_and_bad_values(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("blackbox:\n  pre_trigger_sec: 30\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text("blackbox:\n  pre_trigger_s: 400\n", encoding="utf-8")
    with pytest.raises(ConfigError):  # longer than max_incident_s
        load_config(bad)
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


async def test_bus_isolates_failing_handler():
    bus = EventBus()
    seen = []

    def boom(topic, payload):
        raise RuntimeError("bad handler")

    async def good(topic, payload):
        seen.append((topic, payload))

    bus.subscribe("x", boom)
    bus.subscribe("x", good)
    bus.subscribe("*", good)
    await bus.publish("x", 1)
    assert seen == [("x", 1), ("x", 1)]


def test_db_incident_and_events(tmp_path):
    db = Database(tmp_path / "g.db")
    t = datetime(2026, 11, 2, 13, 5, 22, tzinfo=UTC)
    uid = db.next_uid(t)
    assert uid == "2026-11-02T13-05-22Z_0001"
    iid = db.create_incident(
        uid=uid, event_type="manual", source="manual", severity="info",
        start_time_utc=to_iso(t - timedelta(seconds=30)),
        first_trigger_utc=to_iso(t), last_trigger_utc=to_iso(t),
    )
    row = db.get_incident(iid)
    assert row["recording_status"] == "recording"
    eid = db.add_event(iid, ts_utc=t, event_type="manual", source="manual",
                       is_trigger=True, metadata={"a": 1})
    db.bump_event_repeat(eid)
    assert db.list_events(iid)[0]["repeat_count"] == 2
    db.update_incident(iid, recording_status="partial", actual_pre_s=12.5)
    assert db.get_incident(iid)["actual_pre_s"] == 12.5
    with pytest.raises(ValueError):
        db.update_incident(iid, nonsense=1)
    assert db.get_incident_by_uid(uid)["id"] == iid
    db.log("test", "info", "hello")
    db.close()
