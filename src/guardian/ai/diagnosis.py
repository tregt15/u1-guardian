"""AI diagnosis after an incident, with a local model through Ollama.

Runs after the clip is finished and the printer logs are saved. It gives the model the incident
timeline, tool change records, the printer log excerpt and up to 3 pictures from the clip, and asks
for a plain-language guess at the cause. The result is saved as ai_diagnosis.json next to the clip,
shown on the incident page and (if email is on) sent by email as text. It is always labeled as an AI
guess. Nothing leaves your network except the email text, if you turned email on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from guardian.ai.ollama import OllamaError
from guardian.ai.vision import AiService
from guardian.clock import Clock, from_iso, to_iso
from guardian.config import Config
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

log = logging.getLogger(__name__)

RESULT_FILE = "ai_diagnosis.json"
LOG_CHAR_LIMIT = 9000
KEY_LINE_RE = re.compile(r"!!|error|exception|fail|pause|timeout|shutdown|wheel|stall|abort|refus", re.I)
STOP_EVENTS = {
    "print_paused_with_exception", "print_paused", "print_error", "print_cancelled", "klippy_shutdown",
    "klippy_disconnected", "guardian_paused_print", "ai_bed_not_clear", "ai_print_failure", "bed_not_clear",
    "spaghetti_detected", "residue_detected", "dirty_nozzle_detected",
}

PROMPT = """You help the owner of a Snapmaker U1 3D printer understand why a print stopped or had a problem.
The U1 has 4 toolheads that it swaps during a print, a filament feeder for each one, and runs Klipper.
Use ONLY the evidence below. If the evidence does not show the cause, say that plainly. Do not invent details.
The print may not have failed at all. If nothing in the evidence shows a printer problem, say no problem was found.
Lines marked as about Guardian's own video are never the cause of a print problem.
Printer log lines starting with "!!" and lines with "error" or "exception" are the most important evidence.
Write short, plain sentences for someone who is not an expert.

{evidence}

Answer as JSON:
{{"summary": "2 or 3 plain sentences about what happened",
  "likely_cause": "the most likely cause, or 'not clear from the evidence'",
  "confidence": "low, medium or high",
  "evidence": ["short points, each based on a line of the evidence above"],
  "what_to_check": ["short steps the owner can take, most useful first"],
  "safe_to_resume": "yes, no or unknown"}}"""


# What some U1 messages mean, from Guardian's own investigation (docs/u1-discovery-notes.md). Given to the model
# as notes when the evidence contains the pattern, because a small model cannot know these.
KNOWN_MEANINGS = [
    (re.compile(r"wheel_speed", re.I),
     "Seen on a real U1: 'wheel_speed' during 'load_feeding' means the filament feeder's measuring wheel stopped "
     "turning while the feed motor kept running. So the filament was not moving into the toolhead. Usual causes: the "
     "spool is tangled or stuck, the filament ran out or broke, it is not pushed in far enough, or it slips in the "
     "feeder. It is not a settings problem."),
    (re.compile(r"nozzle diameter mismatch", re.I),
     "Seen on a real U1: the sliced file's nozzle size does not match the installed nozzle. Re-slice with the right "
     "nozzle size, or set the nozzle size in the slicer printer profile."),
    (re.compile(r"\"id\": 532|id:532|detected (dirty bed|noodle|residue|dirty nozzle)|bed_not_clear|spaghetti_detected", re.I),
     "Exception 532 comes from the printer's own camera check: code 1 dirty bed, 2 spaghetti, 3 residue, 4 dirty "
     "nozzle. It pauses the print by itself."),
    (re.compile(r"toolchange_(timeout|wrong_tool|bad_state|error)"),
     "Guardian watches each tool change (park the old tool, pick the new one). A stuck or wrong change usually means "
     "the tool did not lock in its dock or on the carriage: check the dock and the toolhead for plastic, strings or "
     "damage."),
    (re.compile(r"heater_(not_reaching|temperature_drop)"),
     "Guardian saw a heater that could not reach or hold its temperature. Check the heater and its temperature "
     "sensor cable, and whether a fan blows straight on the nozzle."),
]


def known_notes(text: str) -> list[str]:
    return [note for pattern, note in KNOWN_MEANINGS if pattern.search(text)]


def wanted(mode: str, row: Any, event_types: set[str]) -> bool:
    if mode == "off":
        return False
    if event_types <= {"manual"}:
        return False  # a person saved this by hand. Nothing failed.
    if mode == "all":
        return True
    return bool(event_types & STOP_EVENTS) or row["severity"] == "critical"


def _trim_logs(text: str) -> str:
    if len(text) <= LOG_CHAR_LIMIT:
        return text
    lines = text.splitlines()
    key = [ln for ln in lines if KEY_LINE_RE.search(ln)]
    keep = "\n".join(key)[: LOG_CHAR_LIMIT // 2]
    tail = "\n".join(lines)[-(LOG_CHAR_LIMIT // 2):]
    return f"(important lines)\n{keep}\n(last part)\n{tail}"


def build_evidence(db: Database, row: Any, folder: Path) -> str:
    parts = [
        "INCIDENT",
        f"type: {row['event_type']}  severity: {row['severity']}  source: {row['source']}",
        f"first trigger: {row['first_trigger_utc']}  print file: {row['print_filename'] or 'unknown'}",
        f"progress: {row['print_progress'] if row['print_progress'] is not None else 'unknown'}%  "
        f"active tool: {row['active_toolhead'] or 'unknown'}",
        f"(about Guardian's own video only, says nothing about the print) recording: {row['recording_status']} "
        f"{row['recording_notes'] or ''}".strip(),
        "",
        "TIMELINE (UTC)",
    ]
    for e in db.list_events(row["id"]):
        meta = json.loads(e["metadata"]) if e["metadata"] else {}
        what = meta.get("summary") or meta.get("message") or ""
        extra = {k: v for k, v in meta.items() if k in ("exception", "error", "channel_state", "stuck_at",
                                                         "from_tool", "to_tool", "actual_tool", "problems")}
        parts.append(f"{e['ts_utc'][11:23]} {e['event_type']} ({e['source']}) {what} "
                     f"{json.dumps(extra, default=str)[:300] if extra else ''}".rstrip())
    start = from_iso(row["first_trigger_utc"]) - timedelta(minutes=5)
    end = from_iso(row["last_trigger_utc"]) + timedelta(minutes=1)
    tcs = [r for r in db.list_toolchanges(200)
           if r["started_utc"] and to_iso(start) <= r["started_utc"] <= to_iso(end)]
    if tcs:
        parts += ["", "TOOL CHANGES NEAR THE INCIDENT (tool 0 is 'extruder', tool 1 is 'extruder1' ...)"]
        for r in reversed(tcs):
            parts.append(f"{r['started_utc'][11:19]} {r['previous_tool']} -> {r['requested_tool']}: {r['result']} "
                         f"{r['duration_ms'] or '?'} ms {r['error'] or ''}".rstrip())
    logs = folder / "printer_logs.txt"
    if logs.is_file():
        parts += ["", "PRINTER LOGS", _trim_logs(logs.read_text(encoding="utf-8", errors="replace"))]
    else:
        parts += ["", "PRINTER LOGS: not available"]
    text = "\n".join(parts)
    notes = known_notes(text)
    if notes:
        text += ("\n\nGUARDIAN NOTES (what these messages mean, from earlier tests on a real U1)\n"
                 + "\n".join(f"- {n}" for n in notes))
    return text


def tidy(answer: dict) -> dict:
    """Remove repeated list items (the model sometimes repeats one point)."""
    for key in ("evidence", "what_to_check"):
        items = answer.get(key)
        if isinstance(items, list):
            seen, out = set(), []
            for x in items:
                k = str(x).strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(x)
            answer[key] = out
    return answer


def extract_frames(ffmpeg: str, clip: Path, times_s: list[float]) -> list[bytes]:
    """A few JPEG pictures from the clip, made smaller. Missing ones are skipped."""
    out: list[bytes] = []
    for t in times_s:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(clip),
               "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout[:2] == b"\xff\xd8":
            out.append(r.stdout)
    return out


def email_text(row: Any, d: dict, link: str) -> tuple[str, str]:
    subject = f"[Guardian] AI diagnosis: {row['event_type']}" + (f" on {row['print_filename']}" if row["print_filename"] else "")
    lines = ["U1 Guardian asked the local AI what happened. This is an AI guess and can be wrong.", "",
             str(d.get("summary") or ""), "",
             f"Likely cause: {d.get('likely_cause') or 'not given'} (confidence: {d.get('confidence') or 'unknown'})",
             f"Safe to resume: {d.get('safe_to_resume') or 'unknown'}"]
    for title, key in (("Evidence", "evidence"), ("What to check", "what_to_check")):
        items = d.get(key)
        if isinstance(items, list) and items:
            lines += ["", title + ":"] + [f"- {x}" for x in items[:8]]
    lines += ["", f"View: {link}"]
    return subject, "\n".join(lines)


class Diagnoser:
    def __init__(self, cfg: Config, db: Database, ai: AiService, clock: Clock, ffmpeg: str,
                 *, notify=None) -> None:
        self.cfg, self.db, self.ai, self.clock, self.ffmpeg = cfg, db, ai, clock, ffmpeg
        self.notify = notify  # async (uid, subject, text) -> None
        self.last_error: str | None = None

    async def run(self, uid: str) -> dict | None:
        """Diagnose one finished incident if the settings want it. Never raises."""
        try:
            return await self._run(uid)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("AI diagnosis failed for %s", uid)
            return None

    async def _run(self, uid: str) -> dict | None:
        ai_cfg = self.cfg.ai
        row = self.db.get_incident_by_uid(uid)
        if row is None or not ai_cfg.enabled:
            return None
        types = {e["event_type"] for e in self.db.list_events(row["id"])}
        if not wanted(ai_cfg.diagnose, row, types):
            return None
        await self.ai.refresh(force=True)
        folder = incident_dir(self.cfg.storage, row["id"], from_iso(row["first_trigger_utc"]))
        if not self.ai.usable:
            why = ("Ollama or the model is missing" if self.ai.local
                   else (self.ai.remote_message or "the AI service could not be used"))
            self._save(folder, {"available": False, "note": f"AI was not available ({why})."})
            return None
        evidence = build_evidence(self.db, row, folder)
        frames: list[bytes] = []
        if row["recording_path"] and row["start_time_utc"]:
            clip = self.cfg.storage.resolved_data_dir() / row["recording_path"]
            marker = (from_iso(row["first_trigger_utc"]) - from_iso(row["start_time_utc"])).total_seconds()
            length = (from_iso(row["end_time_utc"]) - from_iso(row["start_time_utc"])).total_seconds() \
                if row["end_time_utc"] else marker
            if clip.is_file():
                frames = await asyncio.to_thread(extract_frames, self.ffmpeg, clip,
                                                 [marker - 10, marker, max(marker, length - 2)])
        if frames:
            evidence += (f"\n\nPICTURES: {len(frames)} camera pictures are attached, in time order: about 10 s before "
                         "the problem, at the problem, and at the end of the clip.")
        else:
            evidence += "\n\nPICTURES: none (no footage)."
        try:
            answer = tidy(await self.ai.ask(PROMPT.format(evidence=evidence), frames, num_ctx=12288))
        except OllamaError as exc:
            self.last_error = str(exc)
            self._save(folder, {"available": False, "note": f"The AI could not answer: {exc}"})
            return None
        result = {"available": True, "model": ai_cfg.active_model, "created_utc": to_iso(self.clock.now_utc()),
                  "pictures": len(frames), "answer": answer,
                  "note": "AI guess from the evidence Guardian saved. It can be wrong."}
        self._save(folder, result)
        self.db.add_event(row["id"], ts_utc=self.clock.now_utc(), event_type="ai_diagnosis", source="guardian_ai",
                          severity="info", is_trigger=False,
                          metadata={"summary": str(answer.get("summary") or "")[:500],
                                    "likely_cause": answer.get("likely_cause")})
        if self.notify:
            link = f"{self.cfg.notifications.base_url.rstrip('/')}/incident?uid={uid}"
            subject, text = email_text(row, answer, link)
            await self.notify(uid, subject, text)
        return result

    def _save(self, folder: Path, data: dict) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / RESULT_FILE).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
