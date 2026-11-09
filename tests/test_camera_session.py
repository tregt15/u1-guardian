"""Camera session countdown tests. `u1_camera_countdown.jsonl` is a real countdown from a real U1 (print 2)."""

import json
from datetime import timedelta
from pathlib import Path

from guardian.blackbox.incident_manager import IncidentManager
from guardian.bus import EventBus
from guardian.camera.session_watch import CameraSessionWatch
from guardian.camera.snapshot import FrameBuffer
from guardian.clock import ManualClock, from_iso
from guardian.config import BlackboxConfig, CameraConfig, Config, StorageConfig
from guardian.storage.db import Database

FIX = Path(__file__).parent / "fixtures" / "u1_camera_countdown.jsonl"


def real_messages():
    return [json.loads(line) for line in FIX.read_text(encoding="utf-8").splitlines() if line.strip()]


class Rig:
    def __init__(self, start):
        self.clock = ManualClock(start)
        self.bus = EventBus()
        self.notes = []
        self.watch = CameraSessionWatch(self.bus, self.clock, note=self._note)

    async def _note(self, kind, meta):
        self.notes.append((kind, meta))

    async def send(self, method, params, at=None):
        if at is not None:
            self.clock.set(at)
        await self.bus.publish("printer.message", {"method": method, "params": params,
                                                   "recv_utc": self.clock.now_utc(), "recv_mono": 0.0})


async def test_real_countdown_then_stop():
    msgs = real_messages()
    rig = Rig(from_iso(msgs[0]["t_utc"]) - timedelta(seconds=1))
    assert rig.watch.status()["session"] is None and rig.watch.status()["stops_in_s"] is None
    first = msgs[0]
    await rig.send(first["msg"]["method"], first["msg"]["params"], at=from_iso(first["t_utc"]))
    assert rig.watch.status()["stops_in_s"] == 49
    rig.clock.advance(1)
    assert rig.watch.status()["stops_in_s"] == 48  # counts down between messages too
    for m in msgs[1:]:
        await rig.send(m["msg"]["method"], m["msg"]["params"], at=max(rig.clock.now_utc(), from_iso(m["t_utc"])))
    st = rig.watch.status()
    assert st["session"] == "off" and st["stops_in_s"] is None and st["session_domain"] == "lan"
    # One warning for the whole countdown, then one stop.
    assert [k for k, _ in rig.notes] == ["camera_stop_warning", "camera_stopped"]


async def test_countdown_that_stops_without_a_stop_is_dropped():
    """UNVERIFIED case: a viewer renews in time. The countdown messages just stop."""
    rig = Rig(from_iso("2026-11-09T02:00:00Z"))
    await rig.send("notify_camera_monitoring_to_stop", [{"remain": 30}])
    rig.clock.advance(2)
    await rig.send("notify_camera_monitoring_to_stop", [{"remain": 28}])
    assert rig.watch.status()["stops_in_s"] == 28
    rig.clock.advance(6)
    assert rig.watch.status()["stops_in_s"] is None
    # A later countdown warns again.
    await rig.send("notify_camera_monitoring_to_stop", [{"remain": 50}])
    assert [k for k, _ in rig.notes] == ["camera_stop_warning", "camera_stop_warning"]


async def test_start_and_bad_messages():
    rig = Rig(from_iso("2026-11-09T05:45:00Z"))
    await rig.send("notify_camera_status_change", [{"monitor_domain": "lan", "monitoring": True}])
    await rig.send("notify_camera_status_change", [{"monitor_domain": "lan", "monitoring": True}])  # no change
    await rig.send("notify_camera_status_change", [{"monitoring": "yes"}])
    await rig.send("notify_camera_monitoring_to_stop", [{"remain": "soon"}])
    await rig.send("notify_camera_monitoring_to_stop", "junk")
    assert rig.watch.status()["session"] == "on"
    assert [k for k, _ in rig.notes] == ["camera_started"]


async def test_camera_stop_lands_in_incident_and_explains_missing_footage(tmp_path):
    t0 = from_iso("2026-11-09T02:01:00Z")
    clock = ManualClock(t0)
    cfg = Config(camera=CameraConfig(snapshot_url="http://printer/monitor.jpg"),
                 blackbox=BlackboxConfig(pre_trigger_s=10, post_trigger_s=5),
                 storage=StorageConfig(data_dir=tmp_path / "data", buffer_dir=tmp_path / "buf"))
    db = Database(cfg.storage.db_path, clock)
    buf = FrameBuffer(cfg.storage.resolved_buffer_dir(), cfg.camera.max_frame_gap_s, clock)
    mgr = IncidentManager(cfg, db, buf, clock, "ffmpeg-not-used", "ffprobe-not-used", EventBus())
    assert await mgr.add_note("camera_stopped", "printer_camera", {}) is None  # nothing open: dropped
    result = await mgr.report_incident("manual", "manual", "info")
    clock.advance(2)
    assert await mgr.add_note("camera_stopped", "printer_camera", {"summary": "off"}) == result.uid
    clock.advance(30)
    await mgr.tick()
    row = db.get_incident_by_uid(result.uid)
    assert row["recording_status"] == "missing"
    assert "The printer turned the camera off at 2026-11-09T02:01:02.000Z" in row["recording_notes"]
    events = db.list_events(row["id"])
    note = next(e for e in events if e["event_type"] == "camera_stopped")
    assert note["is_trigger"] == 0
    assert row["last_trigger_utc"] == row["first_trigger_utc"]  # a note does not extend the incident
