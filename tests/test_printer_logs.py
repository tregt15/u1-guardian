"""Printer log excerpts. Line formats copied from a real U1 (2026-11-09)."""

from datetime import datetime, timezone

import httpx

from guardian.printer.logs import PrinterLogs, excerpt

UTC = timezone.utc

KLIPPY = """23:59:58.100:before midnight
23:59:59.900:Stats 100.0: gcodein=0 mcu: x=1 heater_bed: target=80 temp=80.2 pwm=0.240 cavity: temp=43.4 extruder: target=105 temp=220.8 pwm=0.000 extruder1: target=255 temp=255.3 pwm=0.265
00:00:01.000:Stats 102.0: gcodein=0 heater_bed: target=80 temp=80.1 pwm=0.2 extruder: target=105 temp=221.0 pwm=0
00:00:02.500:extruder[2]: state: load_feeding, error: wheel_speed!raw msg:logic error!
00:00:02.600:mcu: stepper_x:-1424
    stepper: stepper_x:154.7
00:00:12.000:Stats 112.0: heater_bed: target=80 temp=80.0 pwm=0.2
00:00:30.000:after the window
"""


def test_klippy_dates_cross_midnight_and_stats_are_shortened():
    start = datetime(2026, 11, 9, 23, 59, 58, tzinfo=UTC)
    end = datetime(2026, 11, 10, 0, 0, 20, tzinfo=UTC)
    anchor = datetime(2026, 11, 10, 0, 1, 0, tzinfo=UTC)
    out = excerpt("klippy.log", KLIPPY, start, end, anchor)
    text = "\n".join(out)
    assert out[0].startswith("23:59:58.100 before midnight")
    assert "after the window" not in text
    assert "wheel_speed" in text
    assert "    stepper: stepper_x:154.7" in text  # continuation line kept with its line
    temps = [ln for ln in out if " temps: " in ln]
    assert len(temps) == 2  # 23:59:59.9 and 00:00:12, the one at 00:00:01 is within 10 s
    assert "heater_bed: target=80 temp=80.2" in temps[0] and "pwm" not in temps[0]
    assert "mcu: x=1" not in text


def test_note_when_the_read_part_starts_too_late():
    start = datetime(2026, 11, 9, 23, 50, 0, tzinfo=UTC)
    end = datetime(2026, 11, 10, 0, 0, 20, tzinfo=UTC)
    anchor = datetime(2026, 11, 10, 0, 1, tzinfo=UTC)
    out = excerpt("klippy.log", KLIPPY, start, end, anchor, truncated=True)
    assert "earlier lines are missing" in out[0]
    assert "missing" not in excerpt("klippy.log", KLIPPY, start, end, anchor)[0]  # whole file was read


def test_moonraker_and_unisrv_formats_and_noise():
    moon = ("2026-11-09 16:45:52,731 [application.py:log_request()] - 101 GET /websocket\n"
            "2026-11-09 16:47:14,981 [file_manager.py] - later\n")
    uni = ("[2026-11-09 16:26:04.311] [unisrv] [info] [JRPC:215] Receive normal request: method is camera.start_monitor\n"
           "[2026-11-09 16:26:09.116] [unisrv] [info] [MON:429] The average time consumption for 10 capturing\n")
    start, end = datetime(2026, 11, 9, 16, 26, tzinfo=UTC), datetime(2026, 11, 9, 16, 46, tzinfo=UTC)
    assert excerpt("moonraker.log", moon, start, end) == ["16:45:52.731 [application.py:log_request()] - 101 GET /websocket"]
    out = excerpt("unisrv.log", uni, start, end)
    assert len(out) == 1 and "camera.start_monitor" in out[0]


async def test_collect_uses_ranges_and_survives_a_missing_file():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[1]
        seen[name] = request.headers.get("range")
        if name == "unisrv.log":
            return httpx.Response(404)
        if name == "klippy.log":  # as long as the range asked for, so the first line is cut in half
            body = ("cut-in-half line\n" + KLIPPY).encode()
            return httpx.Response(206, content=b"x" * (768 * 1024 - len(body)) + body)
        return httpx.Response(206, content=b"2026-11-10 00:00:02,000 hello\n")  # short file, all of it

    logs = PrinterLogs("printer", 80, transport=httpx.MockTransport(handler))
    text, problems = await logs.collect(datetime(2026, 11, 9, 23, 59, 58, tzinfo=UTC),
                                        datetime(2026, 11, 10, 0, 0, 20, tzinfo=UTC),
                                        datetime(2026, 11, 10, 0, 1, tzinfo=UTC))
    assert all(v and v.startswith("bytes=-") for v in seen.values())
    assert "cut-in-half" not in text
    assert "wheel_speed" in text and "hello" in text
    assert set(problems) == {"unisrv.log"} and "could not read" in text
