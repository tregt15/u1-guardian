"""Black Box tests. They run real FFmpeg on the stand-in video with a fake clock."""

import json
import random
from datetime import timedelta

import pytest

from guardian import sim
from guardian.blackbox.incident_manager import IncidentManager
from guardian.sim import SimTrigger

pytestmark = pytest.mark.ffmpeg


def _rows(env):
    return env.db.list_incidents()


async def test_100_triggers_h264(tmp_path):
    """Spec Milestone 2 primary test: 100 triggers, each clip checked against the burned-in clock."""
    cfg = sim.make_config(tmp_path)
    rng = random.Random(7)
    triggers = [
        SimTrigger(at_s=40 + i * 20 + rng.uniform(0, 1.9), dedup_key=f"k{i}") for i in range(100)
    ]
    total = triggers[-1].at_s + 35
    env = await sim.build_fast_env(cfg, total)
    await sim.run_fast(env, triggers, total)

    assert [r.action for r in env.results] == ["created"] * 100
    uids = sorted({r.uid for r in env.results})
    assert len(uids) == 100
    failures = {}
    worst = 0.0
    for uid in uids:
        problems, stats = sim.verify_incident(env, uid)
        if problems:
            failures[uid] = problems
        else:
            worst = max(worst, abs(stats["trigger_error_s"]))
    assert not failures, json.dumps(failures, indent=1)[:2000]
    assert worst <= cfg.camera.segment_length_s
    assert env.manager.open_count == 0


async def test_mjpeg_path(tmp_path):
    cfg = sim.make_config(tmp_path, codec="mjpeg")
    triggers = [SimTrigger(at_s=45.3 + i * 22, dedup_key=str(i)) for i in range(5)]
    env = await sim.build_fast_env(cfg, 190)
    await sim.run_fast(env, triggers, 190)
    for uid in {r.uid for r in env.results}:
        problems, _ = sim.verify_incident(env, uid)
        assert not problems, problems
    for row in _rows(env):  # MJPEG is re-encoded once to a browser-playable MP4, original kept
        assert row["recording_path"].endswith("clip.mp4")
        assert (cfg.storage.resolved_data_dir() / row["recording_path"]).with_name("clip_original.mkv").is_file()


async def test_dedupe_and_merge_make_one_incident(tmp_path):
    cfg = sim.make_config(tmp_path)
    triggers = [
        SimTrigger(60.0, "spaghetti", "a"),
        SimTrigger(62.0, "spaghetti", "a"),                     # same key inside 5 s: duplicate
        SimTrigger(66.0, "bed_adhesion", "b"),                  # open incident: merge
        SimTrigger(75.0, "estop", "c", severity="critical"),   # merge, raises severity, extends deadline
        SimTrigger(130.0, "spaghetti", "a"),                    # long after: new incident
    ]
    env = await sim.build_fast_env(cfg, 200)
    await sim.run_fast(env, triggers, 200)

    assert [r.action for r in env.results] == ["created", "duplicate", "merged", "merged", "created"]
    rows = _rows(env)
    assert len(rows) == 2
    first = rows[0]
    assert first["severity"] == "critical"
    events = env.db.list_events(first["id"])
    assert len(events) == 3  # the duplicate is not a new event
    assert events[0]["repeat_count"] == 2
    # Deadline moved to last trigger + 15 s, so post-roll counts from the last trigger (75 s).
    assert first["actual_post_s"] >= 15
    assert first["actual_pre_s"] >= 30
    problems, _ = sim.verify_incident(env, first["uid"])
    assert not problems, problems


async def test_max_incident_length_splits(tmp_path):
    cfg = sim.make_config(tmp_path, pre_s=10, post_s=5, max_incident_s=60)
    triggers = [SimTrigger(30 + i * 4, "tick", f"k{i}") for i in range(26)]  # one every 4 s for 100 s
    env = await sim.build_fast_env(cfg, 160)
    await sim.run_fast(env, triggers, 160)
    rows = _rows(env)
    assert len(rows) >= 2
    from guardian.clock import from_iso
    for r in rows:
        span = (from_iso(r["last_trigger_utc"]) - from_iso(r["first_trigger_utc"])).total_seconds()
        assert span <= 60


async def test_early_trigger_is_partial_and_says_why(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 80)
    await sim.run_fast(env, [SimTrigger(12.0)], 80)
    row = _rows(env)[0]
    assert row["recording_status"] == "partial"
    assert "Pre-roll" in row["recording_notes"]
    assert row["actual_pre_s"] < 30
    assert 11 <= row["actual_pre_s"] <= 14  # about 12 s existed
    assert row["recording_path"]  # we still keep what we have


async def test_missing_segment_is_never_complete(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 110)
    gone = env.buffer.all_segments()[25]  # about 50 s to 52 s
    gone.path.unlink()
    del env.buffer._segments[gone.index]
    await sim.run_fast(env, [SimTrigger(60.0)], 110)
    row = _rows(env)[0]
    assert row["recording_status"] == "partial"
    assert "Gap" in row["recording_notes"]


async def test_no_footage_is_missing(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 20)
    for s in env.buffer.all_segments():
        s.path.unlink()
    env.buffer._segments.clear()
    await sim.run_fast(env, [SimTrigger(10.0)], 20)
    assert _rows(env)[0]["recording_status"] == "missing"


async def test_corrupt_segment_gives_failed_and_keeps_raw(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 110)
    mgr = env.manager
    env.clock.set(env.capture_start + timedelta(seconds=60))
    await mgr.report_incident("x", "manual", "warning")
    for _, dest in mgr._open[0].pinned.values():
        dest.write_bytes(b"this is not video")
    inc_dir = mgr._open[0].dir
    await sim.run_fast(env, [], 110)
    row = _rows(env)[0]
    assert row["recording_status"] == "failed"
    assert (inc_dir / "segments").is_dir()  # raw segments kept when assembly fails


async def test_restart_recovery_never_complete(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 110)
    env.clock.set(env.capture_start + timedelta(seconds=60))
    await env.manager.report_incident("x", "manual", "warning")  # pinned, never finalized
    # Simulate a restart: a fresh manager on the same database.
    fresh = IncidentManager(cfg, env.db, env.buffer, env.clock, env.ffmpeg, env.ffprobe)
    assert fresh.recover_interrupted() == [_rows(env)[0]["uid"]]
    row = _rows(env)[0]
    assert row["recording_status"] == "partial"
    assert "restarted" in row["recording_notes"]


async def test_bus_events_published(tmp_path):
    cfg = sim.make_config(tmp_path)
    env = await sim.build_fast_env(cfg, 110)
    seen = []
    env.bus.subscribe("*", lambda topic, payload: seen.append(topic))
    await sim.run_fast(env, [SimTrigger(60.0)], 110)
    assert seen == ["incident.created", "incident.finalized"]
