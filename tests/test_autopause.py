"""Auto pause decisions. The pause sender is fake. No printer."""

from guardian.clock import ManualClock, from_iso
from guardian.config import AutopauseConfig
from guardian.printer.autopause import AutoPause


class Rig:
    def __init__(self, state="printing", connected=True, **cfg):
        self.clock = ManualClock(from_iso("2026-11-09T17:00:00Z"))
        self.state = state
        self.connected = connected
        self.sent = 0
        self.notes = []
        self.cfg = AutopauseConfig(**cfg)
        self.ap = AutoPause(self.cfg, self.clock, print_state=lambda: self.state, send_pause=self._send,
                            note=self._note, audit=lambda m, meta: None)

    async def _send(self):
        if not self.connected:
            return False
        self.sent += 1
        self.state = "paused"
        return True

    async def _note(self, kind, meta):
        self.notes.append(kind)

    async def run(self, seconds):
        for _ in range(int(seconds * 2)):
            self.clock.advance(0.5)
            await self.ap.tick()


async def test_pauses_after_grace_when_printer_did_not():
    rig = Rig()
    await rig.ap.on_trigger("toolchange_wrong_tool", "wrong tool")
    assert rig.ap.status()["pending_in_s"] == 10
    await rig.run(9)
    assert rig.sent == 0
    await rig.run(2)
    assert rig.sent == 1 and rig.notes == ["autopause_armed", "guardian_paused_print"]
    assert "wrong tool" in rig.ap.status()["last_reason"]


async def test_printer_paused_itself_so_guardian_does_nothing():
    rig = Rig()
    await rig.ap.on_trigger("bed_not_clear")
    await rig.run(3)
    rig.state = "paused"  # the printer's own AI check paused it
    await rig.run(10)
    assert rig.sent == 0 and rig.notes == ["autopause_armed", "autopause_not_needed"]


async def test_many_triggers_one_pause():
    rig = Rig()
    for t in ("toolchange_timeout", "filament_feed_error", "toolchange_error"):
        await rig.ap.on_trigger(t)
    await rig.run(11)
    assert rig.sent == 1
    assert rig.ap.status()["last_reason"].count(";") == 2


async def test_not_a_trigger_or_disabled_or_not_printing():
    rig = Rig()
    await rig.ap.on_trigger("klipper_error_response")
    await rig.ap.on_trigger("toolchange_bad_state")
    off = Rig(enabled=False)
    await off.ap.on_trigger("bed_not_clear")
    idle = Rig(state="standby")
    await idle.ap.on_trigger("bed_not_clear")
    custom = Rig(triggers=["bed_not_clear"])
    await custom.ap.on_trigger("toolchange_timeout")
    for r in (rig, off, idle, custom):
        await r.run(15)
        assert r.sent == 0 and r.notes == []


async def test_turned_off_during_grace_cancels():
    rig = Rig()
    await rig.ap.on_trigger("toolchange_error")
    rig.cfg.enabled = False
    await rig.run(11)
    assert rig.sent == 0 and rig.notes[-1] == "autopause_cancelled"


async def test_retries_while_disconnected_then_gives_up():
    rig = Rig(connected=False)
    await rig.ap.on_trigger("toolchange_error")
    await rig.run(11)
    rig.connected = True
    await rig.run(3)
    assert rig.sent == 1
    never = Rig(connected=False)
    await never.ap.on_trigger("toolchange_error")
    await never.run(30)
    assert never.sent == 0 and never.notes[-1] == "autopause_failed"


async def test_zero_grace_pauses_on_next_tick():
    rig = Rig(grace_s=0)
    await rig.ap.on_trigger("spaghetti_detected")
    await rig.ap.tick()
    assert rig.sent == 1


async def test_cancel_from_the_dashboard():
    rig = Rig()
    assert await rig.ap.cancel() is False
    await rig.ap.on_trigger("toolchange_timeout")
    assert await rig.ap.cancel() is True
    await rig.run(15)
    assert rig.sent == 0 and rig.notes == ["autopause_armed", "autopause_cancelled"]
