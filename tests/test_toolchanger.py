"""Tool change monitor tests.

The `u1_*` fixtures are real messages from a real U1 (two prints, trimmed to the fields that matter).
Failure cases are made up, because no tool change has failed on a real U1 yet.
"""

import json
from datetime import timedelta
from pathlib import Path

from guardian.bus import EventBus
from guardian.clock import ManualClock, from_iso
from guardian.config import ToolchangerConfig
from guardian.printer.detectors import Detector
from guardian.printer.state_tracker import StateTracker
from guardian.printer.toolchanger import ToolchangeMonitor, tool_label
from guardian.storage.db import Database

FIX = Path(__file__).parent / "fixtures"


class Rig:
    def __init__(self, tmp_path, start, **cfg):
        self.clock = ManualClock(start)
        self.bus = EventBus()
        self.db = Database(tmp_path / "g.db", self.clock)
        self.tracker = StateTracker(self.bus)
        self.reports = []
        self.detector = Detector(self.bus, self.tracker, self._report)
        self.mon = ToolchangeMonitor(ToolchangerConfig(**cfg), self.db, self.bus, self.tracker, self.clock,
                                     self._report)

    async def _report(self, event_type, source, severity, key, metadata):
        self.reports.append({"type": event_type, "source": source, "severity": severity, "key": key,
                             "meta": metadata})

    async def msg(self, method, params, at=None):
        if at is not None:
            self.clock.set(at)
        await self.mon.tick()
        await self.bus.publish("printer.message", {"method": method, "params": params,
                                                   "recv_utc": self.clock.now_utc(),
                                                   "recv_mono": self.clock.monotonic(), "printer_ts": None})

    async def gcode(self, line, after_s=0.0):
        self.clock.advance(after_s)
        await self.msg("notify_gcode_response", [line])

    async def status(self, update, after_s=0.0):
        self.clock.advance(after_s)
        await self.msg("notify_status_update", [update, 0.0])

    async def wait(self, seconds):
        for _ in range(int(seconds * 4)):
            self.clock.advance(0.25)
            await self.mon.tick()

    async def replay(self, name):
        for line in (FIX / name).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            await self.msg(r["msg"]["method"], r["msg"]["params"], at=from_iso(r["t_utc"]))
        await self.wait(10)

    def records(self):
        return [dict(r) for r in reversed(self.db.list_toolchanges(500))]


def first_time(name):
    line = (FIX / name).read_text(encoding="utf-8").splitlines()[0]
    return from_iso(json.loads(line)["t_utc"]) - timedelta(seconds=1)


async def test_real_print2_changes_are_all_ok(tmp_path):
    name = "u1_toolchanges_print2.jsonl"
    rig = Rig(tmp_path, first_time(name))
    await rig.replay(name)
    recs = rig.records()
    assert [(r["previous_tool"], r["requested_tool"]) for r in recs] == [
        ("extruder", "extruder1"), ("extruder1", "extruder"), ("extruder", "extruder1"), ("extruder1", "extruder")]
    assert all(r["result"] == "ok" for r in recs), recs
    assert all(3000 <= r["duration_ms"] <= 5000 for r in recs)  # real U1: 3 to 5 s
    assert rig.reports == []
    steps = json.loads(recs[0]["transitions"])
    assert steps[0]["what"] == "start" and any(s["value"] == "// pick extruder1 !!!" for s in steps)


async def test_real_first_t2_change_with_pin_flicker_is_ok(tmp_path):
    name = "u1_toolchange_first_t2.jsonl"
    rig = Rig(tmp_path, first_time(name))
    await rig.replay(name)
    (rec,) = rig.records()
    assert (rec["previous_tool"], rec["requested_tool"], rec["result"]) == ("extruder1", "extruder2", "ok")
    assert rig.reports == []


async def test_real_print1_overlapping_changes_and_feed_failure(tmp_path):
    """Print 1: fast back to back changes (the next `A -> B` line arrives before the last change's
    toolhead update), then a filament load failure on T2 that is NOT a tool change failure."""
    name = "u1_print1_start_and_feed_fail.jsonl"
    rig = Rig(tmp_path, first_time(name))
    await rig.replay(name)
    recs = rig.records()
    assert len(recs) >= 6
    assert all(r["result"] == "ok" for r in recs), [(r["requested_tool"], r["result"], r["error"]) for r in recs]
    assert recs[0]["print_filename"] == "sheep10_test.gcode"
    types = [r["type"] for r in rig.reports]
    assert not any(t.startswith("toolchange_") for t in types)
    feed = [r for r in rig.reports if r["type"] == "filament_feed_error"]
    assert len(feed) == 1  # fired once, not again for the load_fail update with the same error
    assert feed[0]["meta"]["tool"] == "T2" and feed[0]["meta"]["error"] == "wheel_speed"
    assert "print_paused_with_exception" in types


async def test_learned_timeout_from_real_changes(tmp_path):
    name = "u1_toolchanges_print2.jsonl"
    rig = Rig(tmp_path, first_time(name), learn_count=3, timeout_floor_s=6.0)
    assert rig.mon.timeout_s() == 6.0  # not enough data yet: the floor
    await rig.replay(name)
    st = rig.mon.status()
    assert st["timeout_source"] == "learned" and st["good_changes_measured"] == 4
    slowest = max(r["duration_ms"] for r in rig.records()) / 1000
    assert rig.mon.timeout_s() == max(6.0, slowest * 1.5)
    # Learned values survive a restart (read back from the database).
    again = ToolchangeMonitor(ToolchangerConfig(learn_count=3, timeout_floor_s=6.0), rig.db, EventBus(),
                              rig.tracker, rig.clock, rig._report)
    assert again.timeout_s() == rig.mon.timeout_s()


async def test_fixed_timeout_overrides(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"), timeout_s=7.0)
    assert rig.mon.timeout_s() == 7.0 and rig.mon.status()["timeout_source"] == "fixed"


# ---- made-up failures ----

async def start_change(rig, a="extruder", b="extruder1"):
    await rig.status({"toolhead": {"extruder": a}})
    await rig.gcode(f"// {a} -> {b}")
    await rig.gcode(f"// park {a} !!!")
    await rig.status({b: {"activating_move": True}})


async def test_stuck_at_pickup_times_out_then_finishes_late(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await start_change(rig)
    await rig.status({"extruder": {"state": "PARKED", "park_pin": True, "active_pin": False}}, after_s=1)
    await rig.gcode("// pick extruder1 !!!", after_s=1)
    await rig.wait(12)  # 14 s since the start
    assert rig.reports == []
    await rig.wait(2)  # past the 15 s floor
    (rep,) = rig.reports
    assert rep["type"] == "toolchange_timeout" and rep["severity"] == "warning"
    assert rep["meta"]["stuck_at"] == "picking up T1"
    assert rep["meta"]["from_tool"] == "T0" and rep["meta"]["to_tool"] == "T1"
    (rec,) = rig.records()
    assert rec["result"] == "timeout" and rec["ended_utc"] is None
    # It finishes 10 s later: the result stays timeout, the real end time is kept.
    await rig.status({"extruder1": {"state": "ACTIVATE", "grab_valid_pin": True}}, after_s=10)
    await rig.status({"toolhead": {"extruder": "extruder1"}})
    (rec,) = rig.records()
    assert rec["result"] == "timeout" and rec["duration_ms"] > 25000
    assert len(rig.reports) == 1


async def test_stuck_at_parking(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"), timeout_s=5)
    await start_change(rig)
    await rig.wait(6)
    assert rig.reports[0]["meta"]["stuck_at"] == "parking T0"


async def test_wrong_tool(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await start_change(rig)
    await rig.status({"toolhead": {"extruder": "extruder2"}}, after_s=3)
    (rep,) = rig.reports
    assert rep["type"] == "toolchange_wrong_tool" and rep["severity"] == "critical"
    (rec,) = rig.records()
    assert rec["result"] == "wrong_tool" and rec["actual_tool"] == "extruder2"


async def test_activated_line_but_tool_states_wrong(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await start_change(rig)
    await rig.status({"extruder": {"state": "PARKED", "park_pin": True}}, after_s=1)
    await rig.status({"extruder1": {"state": "ACTIVATE", "active_pin": True, "park_pin": False}}, after_s=1)
    await rig.gcode("// Activating extruder extruder1", after_s=1)
    await rig.status({"toolhead": {"extruder": "extruder1"}})
    await rig.wait(4)  # settle time passes, grab_valid_pin never came
    (rep,) = rig.reports
    assert rep["type"] == "toolchange_bad_state"
    assert "grab" in rep["meta"]["problems"][0]
    assert rig.records()[0]["result"] == "bad_state"


async def test_printer_error_during_change(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await start_change(rig)
    await rig.gcode("!! toolhead pickup failed", after_s=2)
    types = [r["type"] for r in rig.reports]
    assert "toolchange_error" in types and "klipper_error_response" in types
    assert rig.records()[0]["result"] == "error"


async def test_link_lost_marks_interrupted_and_restart_recovery(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await start_change(rig)
    await rig.bus.publish("printer.snapshot", {"status": {}})
    assert rig.records()[0]["result"] == "interrupted"
    await start_change(rig, "extruder1", "extruder")  # Guardian stops in the middle of this one
    assert rig.records()[1]["result"] is None
    fresh = ToolchangeMonitor(ToolchangerConfig(), rig.db, EventBus(), rig.tracker, rig.clock, rig._report)
    assert fresh.recover_interrupted() == 1
    assert rig.records()[1]["result"] == "interrupted"
    assert not any(r["type"].startswith("toolchange_") for r in rig.reports)


async def test_filament_feed_snapshot_is_a_baseline(tmp_path):
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    bad = {"filament_feed right": {"extruder2": {"channel_error": "wheel_speed", "channel_state": "load_fail"}}}
    await rig.bus.publish("printer.snapshot", {"status": bad})
    assert rig.reports == []  # already broken before Guardian connected: no new event
    await rig.status({"filament_feed right": {"extruder2": {"channel_error": "ok", "channel_state": "idle"}}})
    await rig.status(bad)
    assert [r["type"] for r in rig.reports] == ["filament_feed_error"]


def test_tool_label():
    assert [tool_label(n) for n in ("extruder", "extruder1", "extruder3", None, "x")] == ["T0", "T1", "T3", "unknown", "x"]


async def test_real_end_of_print_park_is_not_a_timeout(tmp_path):
    """Print 2 ended with `// extruder1 -> extruder` and a park, but no pickup."""
    name = "u1_print2_end_park_only.jsonl"
    rig = Rig(tmp_path, first_time(name))
    await rig.replay(name)
    await rig.wait(30)
    (rec,) = rig.records()
    assert rec["result"] == "park_only"
    assert rig.reports == []
    assert rig.mon.status()["good_changes_measured"] == 0  # not used to learn the timeout
    assert rig.db.list_toolchanges(problems_only=True) == []


async def test_same_tool_line_is_not_a_change(tmp_path):
    """Firmware 2.1.0 on the real U1 printed `extruder1 -> extruder1` during a phase offset step. Guardian took
    it as a change, timed out and paused a good print (2026-11-10)."""
    rig = Rig(tmp_path, first_time("u1_toolchanges_print2.jsonl"))
    await rig.status({"toolhead": {"extruder": "extruder1"}})
    await rig.gcode("// Pausing toolhead to calculate extruder1 phase offset")
    await rig.gcode("// extruder1 -> extruder1")
    await rig.status({"extruder1": {"activating_move": True}})
    await rig.wait(60)
    assert rig.records() == [] and rig.reports == []
