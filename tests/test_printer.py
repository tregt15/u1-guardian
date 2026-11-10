"""Client and detector tests against the mock Moonraker. No printer, no FFmpeg."""

import asyncio
import json
from pathlib import Path

import pytest

from guardian.bus import EventBus
from guardian.mock_moonraker import MockMoonraker
from guardian.printer.detectors import Detector
from guardian.printer.moonraker_client import ALLOWED_METHODS, MoonrakerClient
from guardian.printer.state_tracker import StateTracker

FIXTURE = Path(__file__).parent / "fixtures" / "u1_real_events.jsonl"


def real_messages():
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class Rig:
    def __init__(self, mock: MockMoonraker):
        self.mock = mock
        self.bus = EventBus()
        self.tracker = StateTracker(self.bus)  # subscribed before the detector on purpose
        self.reports: list[dict] = []
        self.detector = Detector(self.bus, self.tracker, self._report)
        self.client = MoonrakerClient("127.0.0.1", mock.port, self.bus,
                                      backoff_start_s=0.05, backoff_cap_s=0.2)
        self.stop = asyncio.Event()
        self.messages = 0
        self.bus.subscribe("printer.message", self._count)

    def _count(self, topic, payload):
        self.messages += 1

    async def _report(self, event_type, source, severity, key, metadata):
        self.reports.append({"type": event_type, "source": source, "severity": severity,
                             "key": key, "meta": metadata})

    async def run_until(self, cond, timeout=5.0):
        task = asyncio.create_task(self.client.run(self.stop))
        try:
            async with asyncio.timeout(timeout):
                while not cond():
                    await asyncio.sleep(0.02)
        finally:
            self.stop.set()
            await task


async def make(scripts, **kw):
    mock = MockMoonraker(scripts, **kw)
    await mock.start()
    return mock, Rig(mock)


async def test_real_u1_events_become_triggers():
    msgs = real_messages()
    mock, rig = await make([msgs])
    try:
        await rig.run_until(lambda: rig.messages >= len(msgs))
        await asyncio.sleep(0.1)
    finally:
        await mock.stop()
    types = [r["type"] for r in rig.reports]
    assert types.count("printer_exception") >= 2  # ids 531 and 525
    assert "klipper_error_response" in types
    paused = [r for r in rig.reports if r["type"] == "print_paused_with_exception"]
    assert len(paused) == 1
    assert paused[0]["severity"] == "critical"
    assert paused[0]["meta"]["exception"]["id"] == 525
    assert paused[0]["meta"].get("print_filename") == "sheep10_test.gcode"
    # The refused first-print error (id 531) is a warning, never critical.
    first = next(r for r in rig.reports if r["meta"].get("exception_id") == 531)
    assert first["severity"] == "warning"


async def test_client_sends_only_read_only_calls():
    mock, rig = await make([real_messages()])
    try:
        await rig.run_until(lambda: rig.messages >= 5)
    finally:
        await mock.stop()
    assert set(mock.received_methods) <= ALLOWED_METHODS
    assert mock.refused == []


async def test_client_refuses_write_calls():
    mock, rig = await make([[]])
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            await rig.client._send(None, 99, "printer.gcode.script", {"script": "M112"})
        with pytest.raises(RuntimeError):
            await rig.client._send(None, 99, "printer.print.pause")
    finally:
        await mock.stop()


async def test_reconnect_resubscribes_and_resyncs_state():
    """The printer pauses with an error while the link is down. After reconnect the new
    snapshot shows it, and Guardian still raises the incident."""
    info = {"total_layer": 100, "current_layer": 10}
    printing = {"print_stats": {"state": "printing", "filename": "a.gcode", "exception": {}, "info": info}}
    paused = {"print_stats": {"state": "paused", "filename": "a.gcode", "info": info,
                              "exception": {"id": 7, "code": 9, "message": "boom"}}}
    first_script = [{"jsonrpc": "2.0", "method": "notify_proc_stat_update", "params": [{}]}]
    mock, rig = await make([first_script, []], snapshots=[printing, paused], drop_after_script=True)
    try:
        await rig.run_until(lambda: any(r["type"] == "print_paused_with_exception" for r in rig.reports))
    finally:
        await mock.stop()
    assert rig.client.connect_count == 2
    assert mock.received_methods.count("printer.objects.subscribe") == 2
    hit = next(r for r in rig.reports if r["type"] == "print_paused_with_exception")
    assert hit["meta"]["print_progress"] == 10.0


async def test_bad_messages_do_not_stop_the_client():
    good = {"jsonrpc": "2.0", "method": "notify_gcode_response", "params": ["!! real problem"]}
    junk = ["this is not json", "[1, 2, 3]", json.dumps({"method": 5, "params": "x"}),
            json.dumps({"jsonrpc": "2.0", "method": "notify_status_update", "params": "weird"})]
    mock, rig = await make([[*junk, good]])
    try:
        await rig.run_until(lambda: any(r["type"] == "klipper_error_response" for r in rig.reports))
    finally:
        await mock.stop()
    assert rig.client.connect_count == 1


async def test_client_survives_server_down_then_up():
    mock = MockMoonraker([[]])
    await mock.start()
    port = mock.port
    await mock.stop()  # nothing is listening yet
    bus = EventBus()
    client = MoonrakerClient("127.0.0.1", port, bus, backoff_start_s=0.05, backoff_cap_s=0.1)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    await asyncio.sleep(0.3)
    assert not client.connected  # tried and failed, did not crash
    late = MockMoonraker([[]], port=port)
    await late.start()
    try:
        async with asyncio.timeout(5):
            while not client.connected:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await task
        await late.stop()


async def test_camera_start_is_refused_unless_allowed():
    mock, rig = await make([[]])
    try:
        assert rig.client.allow_camera_start is False
        with pytest.raises(RuntimeError):
            await rig.client._send(None, 99, "camera.start_monitor", {"domain": "lan", "interval": 0})
    finally:
        await mock.stop()


async def test_camera_start_only_with_exact_lan_params():
    mock = MockMoonraker([[]])
    await mock.start()
    bus = EventBus()
    client = MoonrakerClient("127.0.0.1", mock.port, bus, allow_camera_start=True,
                             backoff_start_s=0.05, backoff_cap_s=0.2)
    for method, params in [("camera.start_monitor", {"domain": "wan", "interval": 0}),
                           ("camera.stop_monitor", {"domain": "lan"}),
                           ("printer.gcode.script", {"script": "PAUSE"})]:
        with pytest.raises(RuntimeError):
            await client._send(None, 99, method, params)
    assert await client.start_camera_monitor() is False  # not connected yet
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        async with asyncio.timeout(5):
            while not client.connected:
                await asyncio.sleep(0.02)
            assert await client.start_camera_monitor() is True
            while not mock.camera_starts:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await task
        await mock.stop()
    assert mock.camera_starts == [{"domain": "lan", "interval": 0}]
    assert mock.refused == []


async def test_snapmaker_ai_exception_532_is_named():
    """From the printer's defect_detection.py: id 532, code 1 = dirty bed, then it pauses."""
    bus = EventBus()
    tracker = StateTracker(bus)
    reports = []

    async def report(event_type, source, severity, key, metadata):
        reports.append((event_type, source, severity))

    Detector(bus, tracker, report)
    await bus.publish("printer.message", {"method": "notify_exception_notification", "params": [
        {"id": 532, "index": 0, "code": 1, "level": 2, "message": "detected dirty bed"}]})
    await bus.publish("printer.message", {"method": "notify_exception_notification", "params": [
        {"id": 532, "index": 0, "code": 2, "level": 2, "message": "detected noodle"}]})
    assert reports == [("bed_not_clear", "snapmaker_ai", "critical"),
                       ("spaghetti_detected", "snapmaker_ai", "critical")]


async def test_pause_only_when_allowed_and_without_params():
    mock = MockMoonraker([[]])
    await mock.start()
    client = MoonrakerClient("127.0.0.1", mock.port, EventBus(), backoff_start_s=0.05, backoff_cap_s=0.2)
    with pytest.raises(RuntimeError):
        await client._send(None, 99, "printer.print.pause")
    client.allow_pause = True
    for method, params in [("printer.print.pause", {"x": 1}), ("printer.print.resume", None),
                           ("printer.print.cancel", None)]:
        with pytest.raises(RuntimeError):
            await client._send(None, 99, method, params)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        async with asyncio.timeout(5):
            while not client.connected:
                await asyncio.sleep(0.02)
            assert await client.pause_print() is True
            while not mock.pauses:
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await task
        await mock.stop()
    assert mock.pauses == [None] and mock.refused == []


def test_metadata_between_prints_has_no_old_file():
    tracker = StateTracker(EventBus())
    tracker.status = {"print_stats": {"state": "complete", "filename": "old.gcode",
                                      "info": {"total_layer": 10, "current_layer": 10}},
                      "toolhead": {"extruder": "extruder1"}}
    assert tracker.incident_metadata() == {"active_toolhead": "extruder1"}
    tracker.status["print_stats"]["state"] = "printing"
    assert tracker.incident_metadata()["print_filename"] == "old.gcode"
