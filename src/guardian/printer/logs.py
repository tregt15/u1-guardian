"""Printer log excerpts for an incident. Read only.

Moonraker serves the printer's log folder as the read-only file root `logs`
(GET /server/files/logs/<name>, HTTP range requests work). Verified on a real U1, 2026-11-09:
the files are klippy.log, moonraker.log, unisrv.log, syslog, gui.log and rotated copies.

Line formats seen on the U1 (printer clock is UTC):
- klippy.log:    `06:11:19.722:text` (time only. The date is taken from the incident.)
- moonraker.log: `2026-11-09 16:45:52,731 text`
- unisrv.log:    `[2026-11-09 16:26:04.311] [unisrv] [info] text`

Only the end of each file is fetched (a range request), so this stays light on a Pi.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from guardian.clock import ensure_utc

log = logging.getLogger(__name__)

KLIPPY_RE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}):(.*)$")
MOONRAKER_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) (.*)$")
UNISRV_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.(\d{3})\] (.*)$")
STATS_TEMPS_RE = re.compile(r"(heater_bed: target=\S+ temp=\S+|cavity: temp=\S+|extruder\d?: target=\S+ temp=\S+)")

TAIL_BYTES = {"klippy.log": 768 * 1024, "moonraker.log": 256 * 1024, "unisrv.log": 256 * 1024}
MAX_LINES = 400
STATS_EVERY_S = 10.0
# unisrv logs one picture timing line every 5 s while the camera is on. It says nothing useful here.
NOISE = ("MON:429", "notify_system_check_remote", "Check remote firmware result",
         "\"method\":\"printer.objects.query\",\"params\":{\"objects\":{\"machine_state_manager\":null}}")


@dataclass
class LogLine:
    ts: datetime
    text: str


def _parse(name: str, text: str, anchor: datetime) -> list[LogLine]:
    """Turn raw text into timed lines. Lines without a time stick to the line before them.

    klippy.log has only the time of day. The file is in time order and its last line is from
    just before `anchor` (when it was read), so the dates are filled in walking back from the
    end, going back one day each time the clock jumps backward."""
    anchor = ensure_utc(anchor)
    rows: list[list] = []  # [tod or full datetime, text, is_tod]
    for raw in text.splitlines():
        if name.startswith("klippy"):
            m = KLIPPY_RE.match(raw)
            if m:
                tod = timedelta(hours=int(m[1]), minutes=int(m[2]), seconds=int(m[3]), milliseconds=int(m[4]))
                rows.append([tod, m[5], True])
                continue
        else:
            m = (MOONRAKER_RE if name.startswith("moonraker") else UNISRV_RE).match(raw)
            if m:
                ts = datetime.fromisoformat(m[1]).replace(tzinfo=anchor.tzinfo, microsecond=int(m[2]) * 1000)
                rows.append([ts, m[3], False])
                continue
        if rows:
            rows[-1][1] += "\n    " + raw
    day = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    if rows and rows[-1][2] and day + rows[-1][0] > anchor + timedelta(minutes=10):
        day -= timedelta(days=1)  # the last line is from before midnight
    out: list[LogLine] = []
    later: timedelta | None = None
    for value, body, is_tod in reversed(rows):
        if is_tod:
            if later is not None and value > later + timedelta(hours=1):
                day -= timedelta(days=1)
            later = value
            out.append(LogLine(day + value, body))
        else:
            out.append(LogLine(value, body))
    out.reverse()
    return out


def excerpt(name: str, text: str, start: datetime, end: datetime, anchor: datetime | None = None,
            truncated: bool = False) -> list[str]:
    """Lines between start and end, with Stats lines cut down to temperatures (one per 10 s).
    `anchor` is when the text was read (default: end). `truncated`: only the end of the file was read."""
    parsed = _parse(name, text, anchor or end)
    lines = [ln for ln in parsed if start <= ln.ts <= end]
    out: list[str] = []
    if truncated and parsed and parsed[0].ts > start:
        out.append(f"(the part of {name} that was read starts at {parsed[0].ts.strftime('%H:%M:%S')}, "
                   "so earlier lines are missing)")
    last_stats: datetime | None = None
    for ln in lines:
        body = ln.text
        if any(n in body for n in NOISE):
            continue
        if body.startswith("Stats "):
            if last_stats and (ln.ts - last_stats).total_seconds() < STATS_EVERY_S:
                continue
            last_stats = ln.ts
            temps = STATS_TEMPS_RE.findall(body)
            body = "temps: " + ", ".join(temps) if temps else body[:200]
        out.append(f"{ln.ts.strftime('%H:%M:%S.%f')[:-3]} {body}")
    if len(out) > MAX_LINES:
        cut = len(out) - MAX_LINES
        out = out[:MAX_LINES // 2] + [f"... {cut} lines left out ..."] + out[-MAX_LINES // 2:]
    return out


class PrinterLogs:
    def __init__(self, host: str, port: int, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = f"http://{host}:{port}/server/files/logs/"
        self._transport = transport

    async def _tail(self, client: httpx.AsyncClient, name: str) -> tuple[str, bool]:
        """(text, truncated). truncated: the file is longer than what was read."""
        r = await client.get(self.base + name, headers={"Range": f"bytes=-{TAIL_BYTES[name]}"})
        if r.status_code not in (200, 206):
            raise RuntimeError(f"HTTP {r.status_code}")
        text = r.content.decode("utf-8", errors="replace")
        truncated = r.status_code == 206 and len(r.content) >= TAIL_BYTES[name]
        if truncated and "\n" in text:
            text = text.split("\n", 1)[1]  # the first line is cut in half by the range
        return text, truncated

    async def collect(self, start: datetime, end: datetime, anchor: datetime) -> tuple[str, dict[str, str]]:
        """Return (report text, {log name: problem}) for the window. Never raises."""
        parts: list[str] = [
            f"Printer logs from {start.strftime('%Y-%m-%d %H:%M:%S')} to {end.strftime('%H:%M:%S')} UTC.",
            "Read from the printer over Moonraker (read only). Times are the printer's clock (UTC).",
            "",
        ]
        problems: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
            for name in TAIL_BYTES:
                try:
                    text, truncated = await self._tail(client, name)
                except Exception as exc:  # noqa: BLE001
                    problems[name] = f"could not read ({type(exc).__name__}: {exc})"
                    parts += [f"===== {name} =====", f"(could not read: {exc})", ""]
                    continue
                lines = excerpt(name, text, start, end, anchor, truncated)
                parts += [f"===== {name} ({len(lines)} lines) =====", *(lines or ["(nothing in this window)"]), ""]
        return "\n".join(parts), problems
