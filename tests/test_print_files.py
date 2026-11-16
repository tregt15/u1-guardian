"""Reprint, print a file, upload a file: what may be sent, when, and how the API guards it."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from guardian.api.app import create_app
from guardian.bus import EventBus
from guardian.main import App
from guardian.mock_moonraker import MockMoonraker
from guardian.printer.moonraker_client import MoonrakerClient
from tests.test_api import make_env


async def test_client_sends_start_only_with_a_file_name_and_the_setting_on():
    mock = MockMoonraker([[]])
    await mock.start()
    client = MoonrakerClient("127.0.0.1", mock.port, EventBus(), backoff_start_s=0.05, backoff_cap_s=0.2)
    with pytest.raises(RuntimeError):
        await client._send(None, 99, "printer.print.start", {"filename": "a.gcode"})  # setting off
    client.allow_start_print = True
    for bad in (None, {}, {"filename": ""}, {"filename": 5}, {"filename": "a.gcode", "x": 1}):
        with pytest.raises(RuntimeError):
            await client._send(None, 99, "printer.print.start", bad)
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        async with asyncio.timeout(5):
            while not client.connected:
                await asyncio.sleep(0.02)
            await client.start_print("sub/a.gcode")
    finally:
        stop.set()
        await task
        await mock.stop()
    assert mock.starts == [{"filename": "sub/a.gcode"}] and mock.refused == []


def test_gcode_names_are_checked():
    ok = App.clean_gcode_name
    assert ok("a.gcode") == "a.gcode" and ok("sub" + chr(92) + "b.gcode") == "sub/b.gcode"
    for bad in ("", "../a.gcode", "/etc/a.gcode", "C:/a.gcode", "a/../b.gcode", "a.txt", "a//b.gcode"):
        with pytest.raises(ValueError):
            ok(bad)


def test_start_api_needs_confirm_and_reports_refusals(tmp_path):
    env = make_env(tmp_path)
    assert env.client.post("/api/printer/start", json={"confirm": True}).status_code == 409  # no app here
    calls = []

    async def start_file(name, plate_clear, override=False):
        calls.append((name, plate_clear))
        if not plate_clear:
            raise ValueError("Confirm that the plate is empty first.")
        return {"started": name or "last.gcode"}

    env.ctx.extra["start_file"] = start_file
    env.client = TestClient(create_app(env.ctx))
    assert env.client.post("/api/printer/start", json={}).status_code == 400
    r = env.client.post("/api/printer/start", json={"confirm": True})
    assert r.status_code == 409 and "plate" in r.json()["detail"]
    assert env.client.post("/api/printer/start", json={"confirm": True, "plate_clear": True}).json() == {"started": "last.gcode"}
    assert env.client.post("/api/printer/start", json={"confirm": True, "plate_clear": True, "filename": "a.gcode"}).json() == {"started": "a.gcode"}
    assert calls == [(None, False), (None, True), ("a.gcode", True)]


def test_upload_api_needs_confirm_and_a_body(tmp_path):
    env = make_env(tmp_path)
    got = []

    async def upload_file(name, path):
        got.append((name, path.read_bytes()))
        return {"uploaded": name}

    env.ctx.extra["upload_file"] = upload_file
    env.client = TestClient(create_app(env.ctx))
    assert env.client.post("/api/printer/files/upload?name=a.gcode", content=b"G1").status_code == 400
    assert env.client.post("/api/printer/files/upload?name=a.gcode&confirm=true", content=b"").status_code == 400
    r = env.client.post("/api/printer/files/upload?name=a.gcode&confirm=true", content=b"G1 X1")
    assert r.json() == {"uploaded": "a.gcode"} and got == [("a.gcode", b"G1 X1")]


async def test_start_file_guards_state_plate_and_setting():
    from types import SimpleNamespace

    sent, logged = [], []

    async def start_print(name):
        sent.append(name)

    def make(state, on=True, last="last.gcode"):
        me = SimpleNamespace(cfg=SimpleNamespace(controls=SimpleNamespace(print_files=on, plate_check=False)),
                             tracker=SimpleNamespace(print_state=state), client=SimpleNamespace(start_print=start_print),
                             db=SimpleNamespace(log=lambda *a: logged.append(a)), last_print=lambda: {"file": last},
                             IDLE_STATES=App.IDLE_STATES, clean_gcode_name=App.clean_gcode_name)
        me._check_can_start = lambda: App._check_can_start(me)
        return me

    for bad in (make("printing"), make("paused"), make("error"), make(None), make("complete", on=False)):
        with pytest.raises(ValueError):
            await App.start_file(bad, "a.gcode", True)
    with pytest.raises(ValueError, match="plate"):
        await App.start_file(make("complete"), "a.gcode", False)
    with pytest.raises(ValueError, match="last file"):
        await App.start_file(make("complete", last=None), None, True)
    assert sent == []
    assert await App.start_file(make("complete"), None, True) == {"started": "last.gcode"}
    assert await App.start_file(make("cancelled"), "x/b.gcode", True) == {"started": "x/b.gcode"}
    assert sent == ["last.gcode", "x/b.gcode"] and len(logged) == 2


async def test_view_bed_runs_home_park_z5_only_when_idle_and_allowed():
    from types import SimpleNamespace

    from guardian.camera.setup_moves import HOME, PARK, Z5

    moves, logged = [], []

    async def setup_move(script):
        moves.append(script)
        if script == "FAIL":
            raise RuntimeError("The printer refused: x")

    def make(state, files=True, moves_on=True):
        me = SimpleNamespace(cfg=SimpleNamespace(controls=SimpleNamespace(print_files=files, setup_moves=moves_on)),
                             tracker=SimpleNamespace(print_state=state), client=SimpleNamespace(setup_move=setup_move),
                             db=SimpleNamespace(log=lambda *a: logged.append(a)), _bed_view_busy=False,
                             IDLE_STATES=App.IDLE_STATES)
        me._check_can_start = lambda: App._check_can_start(me)
        return me

    for bad in (make("printing"), make("paused"), make("error"), make("complete", files=False), make("complete", moves_on=False)):
        with pytest.raises(ValueError):
            await App.view_bed(bad)
    assert moves == []
    ok = make("complete")
    assert await App.view_bed(ok) == {"moved": True}
    assert moves == [HOME, PARK, Z5] and ok._bed_view_busy is False


def test_view_bed_api_needs_confirm(tmp_path):
    env = make_env(tmp_path)
    assert env.client.post("/api/printer/view-bed", json={"confirm": True}).status_code == 409  # no app here
    calls = []

    async def view_bed():
        calls.append(1)
        return {"moved": True}

    env.ctx.extra["view_bed"] = view_bed
    env.client = TestClient(create_app(env.ctx))
    assert env.client.post("/api/printer/view-bed", json={}).status_code == 400
    assert env.client.post("/api/printer/view-bed", json={"confirm": True}).json() == {"moved": True} and calls == [1]


# ---- the plate check before the start command (owner asked, 2026-11-16) ----

def _checker(*, z=5.0, usable=True, frames=2, answers=None, minc=0.7):
    """An App stand-in with just what check_plate and start_file use."""
    import asyncio
    from types import SimpleNamespace

    asked = []
    queue = list(answers if answers is not None else [])

    async def look(frame):
        asked.append(frame)
        return queue.pop(0) if queue else None

    left = [frames]

    async def fresh():
        if left[0] <= 0:
            return None
        left[0] -= 1
        return b"jpeg"

    sent, logged = [], []

    async def start_print(name):
        sent.append(name)

    me = SimpleNamespace(cfg=SimpleNamespace(controls=SimpleNamespace(print_files=True, plate_check=True),
                                            ai=SimpleNamespace(min_confidence=minc)),
                         tracker=SimpleNamespace(print_state="complete"), client=SimpleNamespace(start_print=start_print),
                         db=SimpleNamespace(log=lambda *a: logged.append(a)), last_print=lambda: {"file": "last.gcode"},
                         IDLE_STATES=App.IDLE_STATES, PLATE_VISIBLE_Z=App.PLATE_VISIBLE_Z, clean_gcode_name=App.clean_gcode_name,
                         _bed_z=lambda: z, ai=SimpleNamespace(usable=usable), _fresh_frame=fresh,
                         vision=SimpleNamespace(look_at_plate=look))
    me._check_can_start = lambda: App._check_can_start(me)
    me.check_plate = lambda: App.check_plate(me)
    me.sent, me.logged, me.asked = sent, logged, asked
    return me


CLEAR = {"plate_visible": True, "clear": True, "confidence": 0.9, "description": "an empty plate"}
BUSY = {"plate_visible": True, "clear": False, "confidence": 0.9, "objects": ["a sheep print"], "description": "a print on the plate"}


@pytest.fixture
def no_sleep(monkeypatch):
    import asyncio

    async def quick(_):
        return None
    monkeypatch.setattr(asyncio, "sleep", quick)


async def test_plate_check_says_clear_only_after_two_sure_looks(no_sleep):
    r = await App.check_plate(_checker(answers=[CLEAR, CLEAR]))
    assert r["state"] == "clear"
    assert (await App.check_plate(_checker(answers=[CLEAR, BUSY])))["state"] == "not_clear"   # one bad look is enough
    r = await App.check_plate(_checker(answers=[BUSY, CLEAR]))
    assert r["state"] == "not_clear" and "sheep" in r["message"]
    unsure = dict(CLEAR, confidence=0.4)
    assert (await App.check_plate(_checker(answers=[CLEAR, unsure])))["state"] == "cannot_check"  # not sure is not clear


async def test_plate_check_cannot_check_when_it_cannot_see_or_the_ai_is_off(no_sleep):
    assert (await App.check_plate(_checker(z=200.0, answers=[CLEAR, CLEAR])))["state"] == "cannot_check"  # bed lowered
    assert "Raise the bed" in (await App.check_plate(_checker(z=200.0)))["message"]
    assert (await App.check_plate(_checker(usable=False, answers=[CLEAR, CLEAR])))["state"] == "cannot_check"
    assert (await App.check_plate(_checker(frames=0, answers=[CLEAR, CLEAR])))["state"] == "cannot_check"
    assert (await App.check_plate(_checker(answers=[])))["state"] == "cannot_check"            # the AI gave no answer
    hidden = dict(CLEAR, plate_visible=False)
    assert (await App.check_plate(_checker(answers=[hidden, CLEAR])))["state"] == "cannot_check"
    assert (await App.check_plate(_checker(z=None, answers=[CLEAR, CLEAR])))["state"] == "cannot_check"


async def test_start_is_not_sent_unless_the_plate_check_says_clear(no_sleep):
    from guardian.main import PlateCheckFailed

    for answers in ([BUSY, BUSY], [], [CLEAR, dict(CLEAR, confidence=0.1)]):
        me = _checker(answers=answers)
        with pytest.raises(PlateCheckFailed) as exc:
            await App.start_file(me, None, True)
        assert me.sent == [] and exc.value.check["state"] in ("not_clear", "cannot_check")
    me = _checker(z=200.0, answers=[CLEAR, CLEAR])
    with pytest.raises(PlateCheckFailed):
        await App.start_file(me, "a.gcode", True)
    assert me.sent == []
    me = _checker(answers=[CLEAR, CLEAR])
    assert await App.start_file(me, "a.gcode", True) == {"started": "a.gcode"} and me.sent == ["a.gcode"]


async def test_override_only_helps_when_the_ai_saw_something_not_when_it_could_not_check(no_sleep):
    from guardian.main import PlateCheckFailed

    me = _checker(answers=[BUSY, BUSY])
    assert await App.start_file(me, "a.gcode", True, override=True) == {"started": "a.gcode"}
    assert me.sent == ["a.gcode"] and any("plate check saw something" in str(x) for x in me.logged)
    for kw in (dict(z=200.0, answers=[CLEAR, CLEAR]), dict(usable=False), dict(answers=[])):
        me = _checker(**kw)
        with pytest.raises(PlateCheckFailed):
            await App.start_file(me, "a.gcode", True, override=True)
        assert me.sent == []


async def test_the_check_can_be_turned_off_in_settings():
    me = _checker(answers=[])
    me.cfg.controls.plate_check = False
    assert await App.start_file(me, "a.gcode", True) == {"started": "a.gcode"} and me.asked == []


def test_start_api_sends_the_plate_check_result_and_has_a_check_endpoint(tmp_path):
    from guardian.main import PlateCheckFailed

    env = make_env(tmp_path)
    seen = []

    async def start_file(name, plate_clear, override=False):
        seen.append(override)
        raise PlateCheckFailed({"state": "not_clear", "message": "The AI sees something on the plate (a sheep)."})

    async def check_plate():
        return {"state": "clear", "message": "The AI looked twice and the plate looks clear."}

    env.ctx.extra["start_file"] = start_file
    env.ctx.extra["check_plate"] = check_plate
    env.client = TestClient(create_app(env.ctx))
    r = env.client.post("/api/printer/start", json={"confirm": True, "plate_clear": True, "override": True})
    assert r.status_code == 409 and r.json()["detail"]["check"]["state"] == "not_clear" and "sheep" in r.json()["detail"]["message"]
    assert seen == [True]
    assert env.client.post("/api/printer/check-plate").json()["state"] == "clear"
