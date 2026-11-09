"""End to end: mock printer -> client -> detector -> incident manager -> clip. Real time, about 30 s."""

import asyncio
from datetime import datetime, timezone

import pytest

from guardian import sim
from guardian.camera import standin
from guardian.clock import from_iso
from guardian.main import App
from guardian.mock_moonraker import MockMoonraker

pytestmark = [pytest.mark.ffmpeg, pytest.mark.slow]


async def test_printer_failure_saves_verified_clip(tmp_path):
    cfg = sim.make_config(tmp_path, pre_s=6, post_s=4)
    failure = {"jsonrpc": "2.0", "method": "notify_status_update", "params": [{
        "print_stats": {"state": "paused", "filename": "demo.gcode",
                        "info": {"total_layer": 200, "current_layer": 50},
                        "exception": {"id": 525, "index": 2, "code": 32, "level": 2,
                                      "message": "extruder[2]: state: load_feeding, error: test"}}}, 12.0]}
    printing = {"print_stats": {"state": "printing", "filename": "demo.gcode", "exception": {},
                                "info": {"total_layer": 200, "current_layer": 50}},
                "toolhead": {"extruder": "extruder2"}}
    # The failure message arrives about 12 s after the app subscribes.
    mock = MockMoonraker([[failure]], snapshots=[printing], delay_s=12.0)
    await mock.start()
    app = App(cfg, printer_port=mock.port)
    stop = asyncio.Event()
    task = asyncio.create_task(app.run(stop))
    try:
        async with asyncio.timeout(60):
            while True:
                rows = app.db.list_incidents()
                if rows and rows[0]["recording_status"] != "recording":
                    break
                await asyncio.sleep(0.5)
    finally:
        stop.set()
        await task
        await mock.stop()

    row = rows[0]
    assert row["event_type"] == "print_paused_with_exception"
    assert row["source"] == "moonraker"
    assert row["severity"] == "critical"
    assert row["print_filename"] == "demo.gcode"
    assert row["active_toolhead"] == "extruder2"
    assert row["print_progress"] == 25.0
    assert row["recording_status"] == "complete", row["recording_notes"]
    assert row["actual_pre_s"] >= 6 and row["actual_post_s"] >= 4

    # The clip really shows the trigger moment (burned-in clock).
    clip = cfg.storage.resolved_data_dir() / row["recording_path"]
    start, trig = from_iso(row["start_time_utc"]), from_iso(row["first_trigger_utc"])
    run_start = app.buffer.first_run_start
    shown = standin.read_frame_clock(app.ffmpeg, clip, (trig - start).total_seconds())
    expected = (trig - run_start).total_seconds()
    assert abs(shown - expected) <= cfg.camera.segment_length_s + 1.0, (shown, expected)
    assert row["first_trigger_utc"] > row["start_time_utc"]
    assert datetime.now(timezone.utc) > trig
