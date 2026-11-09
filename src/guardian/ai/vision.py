"""AI picture checks during prints, using a local vision model through Ollama.

- Bed check: in the first BED_WINDOW_S of a new print, before anything is printed. Two pictures
  5 s apart must both say "not clear" before Guardian reports `ai_bed_not_clear`.
- Print check: every `print_check_every_s` once the print has run PRINT_CHECK_AFTER_S. A positive
  answer is checked again with a new picture 10 s later before `ai_print_failure` is reported.

Both are `critical` and can pause the print through auto pause (owner's choice, 2026-11-09).
How well the models do on U1 pictures is UNVERIFIED. Every check result is kept for review.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from guardian.ai.ollama import Ollama, OllamaError
from guardian.clock import Clock, to_iso
from guardian.config import AiConfig

log = logging.getLogger(__name__)

BED_WINDOW_S = 90.0
PRINT_CHECK_AFTER_S = 300.0
CONFIRM_BED_S = 5.0
CONFIRM_PRINT_S = 10.0
AVAILABILITY_EVERY_S = 60.0

SYSTEM = ("You check pictures from the camera inside a Snapmaker U1 3D printer. "
          "Be careful and only report a problem you can clearly see. Answer only with JSON.")

BED_PROMPT = (
    "This picture was taken at the start of a print, before anything was printed. "
    "Is the build plate (the flat print surface) clear? It is NOT clear if something lies on it: a leftover print, "
    "loose parts, tools, a scraper, pieces of filament, purge blobs or any other object. "
    "Do not count the printer's own parts (frame, toolhead, nozzles, cables, plate edges) or marks, scratches, "
    "reflections and the plate texture. "
    'Answer as JSON: {"clear": true or false, "confidence": a number from 0 to 1, '
    '"objects": [short names of things on the plate], "reason": "one short sentence"}'
)

PRINT_PROMPT = (
    "This picture was taken during a 3D print. Is the print failing? Failures are: spaghetti (loose tangled "
    "strands of plastic), a part that broke loose or fell over, a big blob of plastic stuck on the nozzle, or "
    "plastic being laid down in the air. A normal print in progress, supports, a purge tower and the printer's own "
    "parts are fine. "
    'Answer as JSON: {"failure": true or false, "kind": "spaghetti, detached, blob, air, other or none", '
    '"confidence": a number from 0 to 1, "reason": "one short sentence"}'
)


def _conf(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "yes"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "no"):
        return False
    return None


class AiService:
    """Shared by the picture checks and the diagnosis: one Ollama, one request at a time."""

    def __init__(self, cfg: AiConfig, clock: Clock, ollama: Ollama | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.ollama = ollama or Ollama(cfg.ollama_url)
        self.lock = asyncio.Lock()
        self.ollama_version: str | None = None
        self.model_ready = False
        self._checked_mono: float | None = None
        self._checked_for: tuple[str, str] | None = None

    async def refresh(self, force: bool = False) -> None:
        if self.ollama.url != self.cfg.ollama_url:  # the address was changed in settings
            self.ollama = Ollama(self.cfg.ollama_url, self.ollama.transport)
        key = (self.cfg.ollama_url, self.cfg.model)
        now = self.clock.monotonic()
        if not force and self._checked_for == key and self._checked_mono is not None \
                and now - self._checked_mono < AVAILABILITY_EVERY_S:
            return
        self._checked_mono, self._checked_for = now, key
        self.ollama_version = await self.ollama.version()
        self.model_ready = bool(self.ollama_version) and await self.ollama.has_model(self.cfg.model)

    @property
    def usable(self) -> bool:
        return self.cfg.enabled and bool(self.ollama_version) and self.model_ready

    async def ask(self, prompt: str, images: list[bytes] | None = None, *, num_ctx: int = 8192) -> dict:
        async with self.lock:
            return await self.ollama.chat_json(self.cfg.model, prompt, images, system=SYSTEM,
                                               timeout=self.cfg.timeout_s, num_ctx=num_ctx)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.cfg.enabled, "ollama_url": self.cfg.ollama_url, "model": self.cfg.model,
                "ollama_version": self.ollama_version, "ollama_found": bool(self.ollama_version),
                "model_ready": self.model_ready, "usable": self.usable,
                "download": self.ollama.pull_state.as_dict()}


ReportFn = Callable[..., Awaitable[Any]]


class VisionWatcher:
    def __init__(self, ai: AiService, clock: Clock, *, print_state: Callable[[], str | None],
                 get_frame: Callable[[], Awaitable[bytes | None]], report: ReportFn,
                 save_evidence: Callable[[Any, list[bytes]], Any] | None = None) -> None:
        self.ai = ai
        self.cfg = ai.cfg
        self.clock = clock
        self._print_state = print_state
        self._get_frame = get_frame
        self._report = report
        self._save_evidence = save_evidence
        self._prev_state: str | None = None
        self._seen_state = False
        self._print_start: float | None = None
        self._bed_done = False
        self._last_print_check: float | None = None
        self._task: asyncio.Task | None = None
        self.results: deque[dict] = deque(maxlen=30)
        self.checks_run = 0

    # ---- decisions, once a second ----

    async def tick(self) -> None:
        state = self._print_state()
        if self._seen_state and state == "printing" and self._prev_state not in ("printing", "paused"):
            self._print_start, self._bed_done, self._last_print_check = self.clock.monotonic(), False, None
        if state not in ("printing", "paused"):
            self._print_start = None
        if state is not None:
            self._seen_state = True
        self._prev_state = state

        if not self.cfg.enabled or state != "printing" or (self._task and not self._task.done()):
            return
        await self.ai.refresh()
        if not self.ai.usable:
            return
        now = self.clock.monotonic()
        since_start = now - self._print_start if self._print_start is not None else None
        if self.cfg.bed_check and not self._bed_done and since_start is not None:
            if since_start > BED_WINDOW_S:
                self._bed_done = True
                self._remember("bed", None, "skipped: no camera picture in the first 90 s of the print")
            else:
                self._task = asyncio.create_task(self._bed_check())
                return
        if self.cfg.print_check and (since_start is None or since_start >= PRINT_CHECK_AFTER_S):
            if self._last_print_check is None or now - self._last_print_check >= self.cfg.print_check_every_s:
                self._last_print_check = now
                self._task = asyncio.create_task(self._print_check())

    # ---- checks ----

    async def _frame(self) -> bytes | None:
        try:
            return await self._get_frame()
        except Exception:  # noqa: BLE001
            log.exception("could not get a camera picture for the AI check")
            return None

    async def _ask(self, kind: str, frame: bytes) -> dict | None:
        try:
            self.checks_run += 1
            return await self.ai.ask(BED_PROMPT if kind == "bed" else PRINT_PROMPT, [frame])
        except OllamaError as exc:
            self._remember(kind, None, f"AI error: {exc}")
            return None

    async def _bed_check(self) -> None:
        first = await self._frame()
        if first is None:
            return  # no fresh picture yet. Try again next tick (still inside the window).
        self._bed_done = True
        a = await self._ask("bed", first)
        if a is None:
            return
        bad = _bool(a.get("clear")) is False and _conf(a.get("confidence")) >= self.cfg.min_confidence
        self._remember("bed", a, "not clear, checking again" if bad else "clear")
        if not bad:
            return
        await asyncio.sleep(CONFIRM_BED_S)
        second = await self._frame()
        b = await self._ask("bed", second) if second else None
        if b is None:
            return
        confirmed = _bool(b.get("clear")) is False and _conf(b.get("confidence")) >= self.cfg.min_confidence
        self._remember("bed", b, "not clear (confirmed)" if confirmed else "clear on the second look, no alert")
        if confirmed:
            objects = b.get("objects") or a.get("objects") or []
            what = ", ".join(str(o) for o in objects[:5]) if isinstance(objects, list) else str(objects)
            await self._fire("ai_bed_not_clear", f"The AI sees something on the build plate"
                             + (f" ({what})" if what else "") + f": {b.get('reason') or a.get('reason')}",
                             [first, second], a, b)

    async def _print_check(self) -> None:
        first = await self._frame()
        if first is None:
            self._remember("print", None, "skipped: no fresh camera picture")
            return
        a = await self._ask("print", first)
        if a is None:
            return
        bad = _bool(a.get("failure")) is True and _conf(a.get("confidence")) >= self.cfg.min_confidence
        self._remember("print", a, "failure seen, checking again" if bad else "looks fine")
        if not bad:
            return
        await asyncio.sleep(CONFIRM_PRINT_S)
        if self._print_state() != "printing":
            return
        second = await self._frame()
        b = await self._ask("print", second) if second else None
        if b is None:
            return
        confirmed = _bool(b.get("failure")) is True and _conf(b.get("confidence")) >= self.cfg.min_confidence
        self._remember("print", b, "failure (confirmed)" if confirmed else "fine on the second look, no alert")
        if confirmed:
            await self._fire("ai_print_failure", f"The AI sees a print failure ({b.get('kind') or a.get('kind')}): "
                             f"{b.get('reason') or a.get('reason')}", [first, second], a, b)

    async def _fire(self, event_type: str, summary: str, frames: list[bytes], a: dict, b: dict) -> None:
        meta = {"summary": summary, "model": self.cfg.model, "first_answer": a, "second_answer": b,
                "note": "AI result. It can be wrong."}
        try:
            result = await self._report(event_type, "guardian_ai", "critical", event_type, meta)
        except Exception:  # noqa: BLE001
            log.exception("could not report %s", event_type)
            return
        if self._save_evidence and result is not None:
            try:
                self._save_evidence(result, frames)
            except Exception:  # noqa: BLE001
                log.exception("could not save the AI pictures")

    def _remember(self, kind: str, answer: dict | None, verdict: str) -> None:
        self.results.appendleft({"ts_utc": to_iso(self.clock.now_utc()), "check": kind, "verdict": verdict,
                                 "answer": answer})
        if "confirmed" in verdict or "error" in verdict.lower():
            log.warning("AI %s check: %s %s", kind, verdict, answer or "")

    def status(self) -> dict[str, Any]:
        return {"checks_run": self.checks_run, "recent": list(self.results)[:10],
                "busy": bool(self._task and not self._task.done())}

