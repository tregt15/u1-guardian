"""Camera keepalive decisions. No printer: the send function is fake."""


from guardian.bus import EventBus
from guardian.camera.keepalive import CameraKeepalive
from guardian.camera.session_watch import CameraSessionWatch
from guardian.clock import ManualClock, from_iso

T0 = from_iso("2026-11-09T16:00:00Z")


class Rig:
    def __init__(self, mode="printing", state="printing", connected=True):
        self.clock = ManualClock(T0)
        self.bus = EventBus()
        self.session = CameraSessionWatch(self.bus, self.clock)
        self.state = state
        self.frame = None
        self.watched = True
        self.connected = connected
        self.sent = 0
        self.audit = []
        self.ka = CameraKeepalive(mode, self.session, self.clock, print_state=lambda: self.state,
                                  last_frame=lambda: self.frame, frames_watched=lambda: self.watched,
                                  send=self._send, audit=lambda m, meta: self.audit.append(meta["reason"]))

    async def _send(self):
        if not self.connected:
            return False
        self.sent += 1
        return True

    async def camera(self, on):
        await self.bus.publish("printer.message", {"method": "notify_camera_status_change",
                                                   "params": [{"monitor_domain": "lan", "monitoring": on}],
                                                   "recv_utc": self.clock.now_utc()})

    async def run(self, seconds, frames=False):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            if frames:
                self.frame = self.clock.now_utc()
            await self.ka.tick()


async def test_idle_does_nothing_by_default():
    rig = Rig(state="standby")
    await rig.run(60)
    assert rig.sent == 0 and not rig.ka.wanted()


async def test_off_never_sends():
    rig = Rig(mode="off")
    await rig.camera(False)
    await rig.run(60)
    assert rig.sent == 0


async def test_print_start_turns_camera_on_once():
    rig = Rig(state="standby")
    await rig.camera(False)
    await rig.run(5)
    assert rig.sent == 0
    rig.state = "printing"
    await rig.ka.tick()
    assert rig.sent == 1 and rig.audit == ["the printer camera is off"]
    await rig.camera(True)
    await rig.run(60, frames=True)  # pictures flow: nothing more to do
    assert rig.sent == 1


async def test_cooldown_while_camera_stays_off():
    rig = Rig()
    await rig.camera(False)
    await rig.run(31)
    assert rig.sent == 3  # at 1 s, 16 s, 31 s


async def test_countdown_renews():
    rig = Rig()
    await rig.camera(True)
    await rig.run(20, frames=True)
    await rig.bus.publish("printer.message", {"method": "notify_camera_monitoring_to_stop",
                                              "params": [{"remain": 50}], "recv_utc": rig.clock.now_utc()})
    await rig.ka.tick()
    assert rig.sent == 1 and "about to stop" in rig.ka.last_reason


async def test_stale_pictures_restart_with_long_cooldown():
    """The camera service crashed once on the real U1 with no message. Pictures just stopped."""
    rig = Rig()
    await rig.camera(True)
    await rig.run(20, frames=True)
    await rig.run(9)
    assert rig.sent == 0
    await rig.run(2)
    assert rig.sent == 1 and "no new picture" in rig.ka.last_reason
    await rig.run(25)
    assert rig.sent == 1
    await rig.run(10)
    assert rig.sent == 2


async def test_stale_rule_skipped_when_guardian_is_not_watching_frames():
    rig = Rig(mode="always", state="standby")
    rig.watched = False
    await rig.camera(True)
    await rig.run(60)
    assert rig.sent == 0


async def test_not_connected_retries_next_tick():
    rig = Rig(connected=False)
    await rig.camera(False)
    await rig.run(3)
    assert rig.sent == 0
    rig.connected = True
    await rig.run(1)
    assert rig.sent == 1


async def test_paused_print_keeps_camera_on():
    rig = Rig(state="paused")
    await rig.camera(False)
    await rig.ka.tick()
    assert rig.sent == 1


async def test_unknown_state_but_pictures_coming_means_on():
    """Seen on the real U1: the camera was already on when Guardian started, so the printer sent no
    status message, and Guardian asked again every 15 s. Fresh pictures now count as proof."""
    rig = Rig(mode="always", state="standby")
    await rig.run(60, frames=True)
    assert rig.sent == 0


async def test_unknown_state_without_pictures_asks_once_a_minute():
    rig = Rig(mode="always", state="standby")
    rig.watched = False
    await rig.run(61)
    assert rig.sent == 2  # at 1 s and 61 s
