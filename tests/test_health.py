"""Stall, heater, tool change trend and self-test checks. Made-up data. The real prints are replayed
by hand and gave no false alarms."""

import httpx

from guardian import selftest
from guardian.bus import EventBus
from guardian.clock import ManualClock, from_iso, to_iso
from guardian.config import CameraConfig, Config, PrinterConfig, ToolchangerConfig
from guardian.printer.health import HealthWatch
from guardian.printer.state_tracker import StateTracker
from guardian.printer.toolchanger import ToolchangeMonitor
from guardian.storage.db import Database

T0 = from_iso("2026-11-09T20:00:00Z")


class Rig:
    def __init__(self):
        self.clock = ManualClock(T0)
        self.status = {"print_stats": {"state": "printing"}, "virtual_sdcard": {"file_position": 100},
                       "toolhead": {"extruder": "extruder"}, "machine_state_manager": {"action_code": 0},
                       "heater_bed": {"temperature": 80.0, "target": 80.0},
                       "extruder": {"temperature": 220.0, "target": 220.0},
                       "extruder1": {"temperature": 105.0, "target": 105.0}}
        self.reports = []
        self.h = HealthWatch(self.clock, lambda: self.status, self._report)

    async def _report(self, event_type, source, severity, key, meta):
        self.reports.append((event_type, meta))

    async def run(self, seconds, every=None):
        for i in range(int(seconds)):
            self.clock.advance(1)
            if every:
                every(i)
            await self.h.tick()

    def types(self):
        return [r[0] for r in self.reports]


async def test_stall_after_ten_minutes_once():
    rig = Rig()
    await rig.run(599)
    assert rig.reports == []
    await rig.run(2)
    assert rig.types() == ["print_stalled"]
    await rig.run(1200)
    assert rig.types() == ["print_stalled"]


async def test_moving_print_is_not_a_stall():
    rig = Rig()

    def move(i):
        rig.status["virtual_sdcard"]["file_position"] += 10

    await rig.run(1200, every=move)
    assert rig.reports == []


async def test_bed_heating_holds_the_stall_clock_but_preheating_another_tool_does_not():
    rig = Rig()
    rig.status["heater_bed"] = {"temperature": 30.0, "target": 80.0}
    rig.status["extruder1"] = {"temperature": 30.0, "target": 255.0}  # next tool preheating

    def warm(i):  # the bed warms up slowly over 15 minutes
        rig.status["heater_bed"]["temperature"] = min(80.0, 30.0 + i * 0.06)
        rig.status["extruder1"]["temperature"] = min(255.0, 30.0 + i * 2)

    await rig.run(900, every=warm)
    assert "print_stalled" not in rig.types()
    rig2 = Rig()
    rig2.status["extruder1"] = {"temperature": 30.0, "target": 255.0}
    await rig2.run(601)
    assert "print_stalled" in rig2.types()  # only a non-active tool was heating


async def test_heater_not_reaching_target():
    rig = Rig()

    def move(i):
        rig.status["virtual_sdcard"]["file_position"] += 1

    rig.status["extruder"] = {"temperature": 150.0, "target": 150.0}
    await rig.run(5, every=move)
    rig.status["extruder"] = {"temperature": 150.0, "target": 250.0}  # new target, heater stuck at 180
    rig.status["extruder"]["temperature"] = 180.0
    await rig.run(299, every=move)
    assert rig.reports == []
    await rig.run(3, every=move)
    assert rig.types() == ["heater_not_reaching"]
    assert "T0" in rig.reports[0][1]["summary"]


async def test_heater_drop_after_reaching():
    rig = Rig()

    def move(i):
        rig.status["virtual_sdcard"]["file_position"] += 1

    await rig.run(10, every=move)
    rig.status["heater_bed"]["temperature"] = 60.0
    await rig.run(29, every=move)
    assert rig.reports == []
    await rig.run(3, every=move)
    assert rig.types() == ["heater_temperature_drop"]


async def test_nothing_when_not_printing():
    rig = Rig()
    rig.status["print_stats"]["state"] = "standby"
    rig.status["heater_bed"] = {"temperature": 30.0, "target": 80.0}
    await rig.run(2000)
    assert rig.reports == []


async def test_toolchange_slowing_trend(tmp_path):
    clock = ManualClock(T0)
    db = Database(tmp_path / "g.db", clock)
    for i in range(50):  # 40 normal changes, then 10 slow ones
        db.create_toolchange(previous_tool="extruder", requested_tool="extruder1", result="ok",
                             duration_ms=4000 if i < 40 else 6500, started_utc=to_iso(T0) + f"{i:04d}")
    reports = []

    async def report(*a):
        reports.append(a)

    mon = ToolchangeMonitor(ToolchangerConfig(), db, EventBus(), StateTracker(EventBus()), clock, report)
    from guardian.printer.toolchanger import _Change
    ch = _Change(1, "extruder", "extruder1", T0, 0.0)
    await mon._check_trend(ch)
    await mon._check_trend(ch)  # quiet time: only one warning
    assert len(reports) == 1 and reports[0][0] == "toolchange_slowing"
    assert reports[0][4]["recent_median_ms"] == 6500


async def test_selftest_against_a_fake_printer(tmp_path):
    objects = list(selftest.REQUIRED_OBJECTS)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/server/info":
            return httpx.Response(200, json={"result": {"moonraker_version": "1.4.1", "klippy_state": "ready"}})
        if path == "/printer/info":
            return httpx.Response(200, json={"result": {"software_version": "1.5.0.1_new"}})
        if path == "/printer/objects/list":
            return httpx.Response(200, json={"result": {"objects": objects}})
        if path == "/server/files/list":
            return httpx.Response(200, json={"result": [{"path": "klippy.log"}]})
        if path.endswith("monitor.jpg"):
            return httpx.Response(200, content=b"\xff\xd8jpeg")
        return httpx.Response(404)

    cfg = Config(printer=PrinterConfig(host="u1", moonraker_port=80),
                 camera=CameraConfig(snapshot_url="http://u1/server/files/camera/monitor.jpg"))
    r = await selftest.run_checks(cfg, httpx.MockTransport(handler))
    assert r["ok"] and r["firmware"] == "1.5.0.1_new"
    assert "checked on" in next(c for c in r["checks"] if c["name"] == "Printer firmware")["detail"]
    objects.remove("filament_feed right")
    r = await selftest.run_checks(cfg, httpx.MockTransport(handler))
    assert not r["ok"] and "filament_feed right" in str(r["checks"])
    assert selftest.remember_firmware(tmp_path, "1.4.1") is None
    assert selftest.remember_firmware(tmp_path, "1.4.1") is None
    assert selftest.remember_firmware(tmp_path, "1.5.0") == "1.4.1"


async def test_selftest_camera_off_is_not_a_problem_but_a_wrong_address_is():
    roots = [{"name": "camera"}]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/server/info":
            return httpx.Response(200, json={"result": {"moonraker_version": "1", "klippy_state": "ready"}})
        if path == "/server/files/roots":
            return httpx.Response(200, json={"result": roots})
        return httpx.Response(404)  # no monitor.jpg: the camera is off

    cfg = Config(printer=PrinterConfig(host="u1", moonraker_port=80),
                 camera=CameraConfig(snapshot_url="http://u1/server/files/camera/monitor.jpg"))
    r = await selftest.run_checks(cfg, httpx.MockTransport(handler))
    cam = next(c for c in r["checks"] if c["name"] == "Camera picture")
    assert cam["ok"] and "camera is on" in cam["detail"]
    roots.clear()  # no camera folder at all: the address is wrong
    r = await selftest.run_checks(cfg, httpx.MockTransport(handler))
    cam = next(c for c in r["checks"] if c["name"] == "Camera picture")
    assert not cam["ok"] and "404" in cam["detail"]
