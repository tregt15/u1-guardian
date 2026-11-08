"""Simulation helpers: fire fake triggers at known times and check the clips.

Used by tools/simulate_trigger.py and by the tests.

Fast mode: the stand-in video is made as fast as FFmpeg can, then a fake
clock walks through it second by second. Segments that end after the fake
"now" are hidden, so the incident manager behaves as it would in real time.

Live mode: real clock, real-time stand-in video. Slow, but it shows true timing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from guardian.blackbox.incident_manager import IncidentManager, TriggerResult
from guardian.bus import EventBus
from guardian.camera import standin
from guardian.camera.capture import Capture, check_ffmpeg
from guardian.camera.segment_buffer import SegmentBuffer
from guardian.clock import Clock, ManualClock, SystemClock, from_iso
from guardian.config import BlackboxConfig, CameraConfig, Config, StorageConfig
from guardian.storage.db import Database

SIM_START = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class SimTrigger:
    at_s: float  # seconds after the stand-in video started
    event_type: str = "sim_trigger"
    dedup_key: str | None = None
    severity: str = "warning"


@dataclass
class SimEnv:
    cfg: Config
    db: Database
    buffer: SegmentBuffer
    manager: IncidentManager
    clock: Clock
    capture_start: datetime
    ffmpeg: str
    ffprobe: str
    bus: EventBus
    results: list[TriggerResult] = field(default_factory=list)


def make_config(workdir: Path, *, codec: str = "h264", pre_s: float = 30, post_s: float = 15,
                max_incident_s: float = 300, duplicate_window_s: float = 5,
                segment_length_s: float = 2) -> Config:
    return Config(
        camera=CameraConfig(standin_codec=codec, segment_length_s=segment_length_s),
        blackbox=BlackboxConfig(pre_trigger_s=pre_s, post_trigger_s=post_s,
                                max_incident_s=max_incident_s, duplicate_window_s=duplicate_window_s),
        storage=StorageConfig(data_dir=workdir / "data", buffer_dir=workdir / "buffer"),
    )


def _build(cfg: Config, clock: Clock) -> tuple[Database, SegmentBuffer, IncidentManager, EventBus, str, str]:
    ffmpeg, ffprobe = check_ffmpeg(cfg.camera)
    db = Database(cfg.storage.db_path, clock)
    buffer = SegmentBuffer(cfg.storage.resolved_buffer_dir(), cfg.camera.segment_length_s, clock)
    buffer.clear_files()
    bus = EventBus()
    manager = IncidentManager(cfg, db, buffer, clock, ffmpeg, ffprobe, bus)
    return db, buffer, manager, bus, ffmpeg, ffprobe


async def build_fast_env(cfg: Config, total_s: float) -> SimEnv:
    """Make `total_s` seconds of stand-in video as fast as possible, with a fake clock."""
    clock = ManualClock(SIM_START)
    db, buffer, manager, bus, ffmpeg, ffprobe = _build(cfg, clock)
    capture = Capture(cfg.camera, buffer, clock, realtime=False, duration_s=total_s)
    rc = await capture.run_to_completion()
    if rc != 0:
        raise RuntimeError(f"stand-in capture failed with exit code {rc}")
    return SimEnv(cfg, db, buffer, manager, clock, SIM_START, ffmpeg, ffprobe, bus)


async def run_fast(env: SimEnv, triggers: list[SimTrigger], total_s: float) -> None:
    """Walk the fake clock through the video, firing triggers at their exact times."""
    clock = env.clock
    assert isinstance(clock, ManualClock)
    sec = 0
    pending = sorted(triggers, key=lambda t: t.at_s)

    async def advance_to(limit: float) -> None:
        nonlocal sec
        while sec + 1 <= limit:
            sec += 1
            clock.set(env.capture_start + timedelta(seconds=sec))
            await env.manager.tick()

    for trig in pending:
        await advance_to(trig.at_s)
        clock.set(env.capture_start + timedelta(seconds=trig.at_s))
        env.results.append(await env.manager.report_incident(
            trig.event_type, "manual", trig.severity, trig.dedup_key, {"sim": True}))
    await advance_to(total_s)
    # Make sure everything still open gets finalized.
    for _ in range(int(env.cfg.blackbox.max_incident_s) + 10):
        if env.manager.open_count == 0:
            break
        sec += 1
        clock.set(env.capture_start + timedelta(seconds=sec))
        await env.manager.tick()


def verify_incident(env: SimEnv, uid: str) -> tuple[list[str], dict]:
    """Check one finished incident against the burned-in clock.

    Returns (problems, measurements). No problems means the incident passed.
    """
    cfg = env.cfg
    seg = cfg.camera.segment_length_s
    row = env.db.get_incident_by_uid(uid)
    problems: list[str] = []
    stats: dict = {}
    if row["recording_status"] != "complete":
        problems.append(f"status is {row['recording_status']}: {row['recording_notes']}")
    if not row["recording_path"]:
        return problems + ["no clip path recorded"], stats
    clip = cfg.storage.resolved_data_dir() / row["recording_path"]
    if not clip.is_file():
        return problems + [f"clip file missing: {clip}"], stats

    from guardian.blackbox.assembler import AssemblyError, probe
    try:
        info = probe(env.ffprobe, clip)
    except AssemblyError as exc:
        return problems + [str(exc)], stats

    start, end = from_iso(row["start_time_utc"]), from_iso(row["end_time_utc"])
    first, last = from_iso(row["first_trigger_utc"]), from_iso(row["last_trigger_utc"])
    if abs(info.duration_s - (end - start).total_seconds()) > 0.5:
        problems.append(f"clip is {info.duration_s:.1f}s but metadata says {(end - start).total_seconds():.1f}s")
    if row["actual_pre_s"] is None or row["actual_pre_s"] < cfg.blackbox.pre_trigger_s - 0.01:
        problems.append(f"actual_pre_s {row['actual_pre_s']} < {cfg.blackbox.pre_trigger_s}")
    if row["actual_post_s"] is None or row["actual_post_s"] < cfg.blackbox.post_trigger_s - 0.01:
        problems.append(f"actual_post_s {row['actual_post_s']} < {cfg.blackbox.post_trigger_s}")

    def stream_s(dt: datetime) -> float:
        return (dt - env.capture_start).total_seconds()

    c_start = standin.read_frame_clock(env.ffmpeg, clip, 0.0)
    c_trig = standin.read_frame_clock(env.ffmpeg, clip, (first - start).total_seconds())
    c_end = standin.read_frame_clock(env.ffmpeg, clip, max(info.duration_s - 0.3, 0))
    trig_err = c_trig - stream_s(first)
    stats.update(trigger_error_s=trig_err, clip_s=info.duration_s,
                 actual_pre_s=row["actual_pre_s"], actual_post_s=row["actual_post_s"])
    if abs(c_start - stream_s(start)) > seg:
        problems.append(f"clip starts at video clock {c_start:.1f}s, expected {stream_s(start):.1f}s")
    if abs(trig_err) > seg:
        problems.append(f"trigger frame shows clock {c_trig:.1f}s, expected {stream_s(first):.1f}s (off by {trig_err:.1f}s)")
    if c_start > stream_s(first) - cfg.blackbox.pre_trigger_s + seg:
        problems.append(f"clip starts too late: video clock {c_start:.1f}s")
    if c_end < stream_s(last) + cfg.blackbox.post_trigger_s - seg:
        problems.append(f"clip ends too early: video clock {c_end:.1f}s")
    return problems, stats


async def run_live(cfg: Config, trigger_after_s: float = 40.0) -> tuple[SimEnv, TriggerResult]:
    """Real clock, real-time stand-in video, one trigger. Takes about a minute."""
    clock = SystemClock()
    db, buffer, manager, bus, ffmpeg, ffprobe = _build(cfg, clock)
    capture = Capture(cfg.camera, buffer, clock, realtime=True)
    stop = asyncio.Event()
    task = asyncio.create_task(capture.supervise(stop))
    try:
        await asyncio.sleep(0.5)
        env = SimEnv(cfg, db, buffer, manager, clock, buffer.first_run_start or clock.now_utc(),
                     ffmpeg, ffprobe, bus)
        deadline = clock.monotonic() + trigger_after_s
        while clock.monotonic() < deadline:
            await manager.tick()
            await asyncio.sleep(0.5)
        result = await manager.report_incident("live_sim", "manual", "info")
        env.results.append(result)
        while manager.open_count:
            await manager.tick()
            await asyncio.sleep(0.5)
        return env, result
    finally:
        stop.set()
        await task
