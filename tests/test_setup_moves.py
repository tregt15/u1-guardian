"""Camera setup bed moves: what may be sent, when, and that it stops at the first problem."""

import asyncio

import pytest

from guardian.bus import EventBus
from guardian.camera.setup_moves import HOME, Z5, Z15, SetupMover
from guardian.clock import ManualClock, from_iso
from guardian.mock_moonraker import MockMoonraker
from guardian.printer.moonraker_client import MoonrakerClient


async def test_client_sends_only_the_three_setup_scripts_and_waits():
    mock = MockMoonraker([[]])
    await mock.start()
    client = MoonrakerClient("127.0.0.1", mock.port, EventBus(), backoff_start_s=0.05, backoff_cap_s=0.2)
    with pytest.raises(RuntimeError):
        await client._send(None, 99, "printer.gcode.script", {"script": "G28"})  # setting off
    client.allow_setup_moves = True
    for bad in ("G28 X", "G1 Z0", "G90\nG1 Z1 F300\nM400", "M112", "SET_PIN PIN=x VALUE=1"):
        with pytest.raises(RuntimeError):
            await client._send(None, 99, "printer.gcode.script", {"script": bad})
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        async with asyncio.timeout(5):
            while not client.connected:
                await asyncio.sleep(0.02)
            for s in (HOME, Z15, Z5):
                await client.setup_move(s, timeout=3)
    finally:
        stop.set()
        await task
        await mock.stop()
    assert mock.scripts_run == [HOME, Z15, Z5] and mock.refused == []


class Rig:
    def __init__(self, state="complete", allowed=True, connected=True, fail_on=None, pictures=True):
        self.clock = ManualClock(from_iso("2026-11-11T12:00:00Z"))
        self.state, self.allowed, self.connected = state, allowed, connected
        self.fail_on, self.pictures = fail_on, pictures
        self.moves, self.saved, self.camera_calls = [], [], 0
        self.z = 200.0
        self.m = SetupMover(self.clock, allowed=lambda: self.allowed, print_state=lambda: self.state,
                            connected=lambda: self.connected, move=self._move, picture=self._picture,
                            camera_on=self._camera_on, bed_z=lambda: self.z, save=self._save,
                            settle_s=0, picture_wait_s=2)

    async def _move(self, script):
        self.moves.append(script)
        if script == self.fail_on:
            raise RuntimeError("The printer refused: Must home axis first")
        self.z = {HOME: 275.0, Z15: 15.0, Z5: 5.0}[script]

    async def _picture(self):
        return b"\xff\xd8jpeg" if self.pictures else None

    async def _camera_on(self):
        self.camera_calls += 1

    async def _save(self, name, jpeg, z):
        self.saved.append((name, z))

    async def finish(self):
        await self.m._task


async def test_runs_the_steps_and_saves_two_pictures():
    r = Rig()
    r.m.start(plate_empty=True)
    await r.finish()
    assert r.moves == [HOME, Z15, Z5] and r.saved == [("setup_15", 15.0), ("setup_05", 5.0)]
    assert r.m.status()["state"] == "done"


@pytest.mark.parametrize("kw,plate,why", [
    ({"state": "printing"}, True, "print is printing"),
    ({"state": "paused"}, True, "print is paused"),
    ({}, False, "Confirm that the build plate is empty"),
    ({"allowed": False}, True, "turned off"),
    ({"connected": False}, True, "not connected"),
])
async def test_refuses(kw, plate, why):
    r = Rig(**kw)
    with pytest.raises(ValueError, match=why):
        r.m.start(plate_empty=plate)
    assert r.moves == []


async def test_stops_at_the_first_error():
    r = Rig(fail_on=Z15)
    r.m.start(plate_empty=True)
    await r.finish()
    assert r.moves == [HOME, Z15] and r.saved == [] and r.m.status()["state"] == "error"
    assert "Must home" in r.m.status()["error"]


async def test_stops_if_a_print_starts():
    r = Rig()

    async def move(script):
        r.moves.append(script)
        r.state = "printing"  # someone started a print during homing

    r.m._move = move
    r.m.start(plate_empty=True)
    await r.finish()
    assert r.moves == [HOME] and r.m.status()["state"] == "error" and "print started" in r.m.status()["error"]


async def test_no_picture_turns_the_camera_on_then_gives_up():
    r = Rig(pictures=False)
    r.m.start(plate_empty=True)
    await r.finish()
    assert r.camera_calls == 1 and r.m.status()["state"] == "error" and "Auto camera" in r.m.status()["error"]
