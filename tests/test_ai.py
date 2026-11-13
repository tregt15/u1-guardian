"""Local AI pieces against a fake Ollama (httpx MockTransport). No real model is used here."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from guardian.ai import vision
from guardian.ai.diagnosis import Diagnoser, wanted
from guardian.ai.ollama import Ollama, OllamaError
from guardian.ai.vision import AiService, VisionWatcher
from guardian.clock import ManualClock, from_iso, to_iso
from guardian.config import AiConfig, Config, StorageConfig
from guardian.storage.db import Database
from guardian.storage.paths import incident_dir

T0 = from_iso("2026-11-09T20:00:00Z")
JPEG = b"\xff\xd8fake-jpeg"


class FakeOllama:
    """Answers like Ollama. `answers` is a list of JSON objects returned by /api/chat, in order."""

    def __init__(self, answers=None, models=("qwen2.5vl:7b",), up=True):
        self.answers = list(answers or [])
        self.models = list(models)
        self.up = up
        self.chats = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("nobody home")
        path = request.url.path
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.0"})
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m, "size": 6_000_000_000} for m in self.models]})
        if path == "/api/pull":
            lines = [{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 40},
                     {"status": "downloading", "total": 100, "completed": 100}, {"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        if path == "/api/delete":
            return httpx.Response(200)
        if path == "/api/generate":
            self.warmed = getattr(self, "warmed", 0) + 1
            return httpx.Response(200, json={"done": True})
        if path == "/api/chat":
            body = json.loads(request.content)
            self.chats.append(body)
            answer = self.answers.pop(0) if self.answers else {"clear": True, "failure": False, "confidence": 0.9}
            content = answer if isinstance(answer, str) else json.dumps(answer)
            return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})
        return httpx.Response(404)


def service(fake, clock=None, **cfg):
    cfg = AiConfig(enabled=True, **cfg)
    return AiService(cfg, clock or ManualClock(T0), Ollama(cfg.ollama_url, httpx.MockTransport(fake)))


# ---- client ----

async def test_client_version_models_pull_chat():
    fake = FakeOllama(answers=[{"clear": False, "confidence": 0.8}, "not json"])
    o = Ollama("http://x:11434", httpx.MockTransport(fake))
    assert await o.version() == "0.12.0"
    assert await o.has_model("qwen2.5vl:7b") and not await o.has_model("gemma3:4b")
    await o.pull("gemma3:4b")
    st = o.pull_state.as_dict()
    assert st["done"] and st["percent"] == 100.0 and st["error"] is None and not st["running"]
    assert await o.chat_json("m", "hi", [JPEG]) == {"clear": False, "confidence": 0.8}
    assert fake.chats[0]["format"] == "json" and fake.chats[0]["messages"][0]["images"]
    with pytest.raises(OllamaError):
        await o.chat_json("m", "hi")


async def test_no_ollama_is_reported_not_raised():
    o = Ollama("http://x:11434", httpx.MockTransport(FakeOllama(up=False)))
    assert await o.version() is None
    assert not await o.has_model("qwen2.5vl:7b")
    ai = AiService(AiConfig(enabled=True), ManualClock(T0), o)
    await ai.refresh(force=True)
    assert not ai.usable and ai.status()["ollama_found"] is False


# ---- picture checks ----

class Rig:
    def __init__(self, answers, monkeypatch, frame=JPEG, **cfg):
        monkeypatch.setattr(vision, "CONFIRM_BED_S", 0)
        monkeypatch.setattr(vision, "CONFIRM_PRINT_S", 0)
        self.clock = ManualClock(T0)
        self.fake = FakeOllama(answers)
        self.ai = service(self.fake, self.clock, **cfg)
        self.state = "standby"
        self.action = 136  # the printer's own bed check step
        self.frame = frame
        self.reports = []
        self.saved = []
        self.w = VisionWatcher(self.ai, self.clock, print_state=lambda: self.state, get_frame=self._frame,
                               report=self._report, save_evidence=lambda r, f: self.saved.append(f),
                               action_code=lambda: self.action)

    async def _frame(self):
        return self.frame

    async def _report(self, event_type, source, severity, key, meta):
        self.reports.append((event_type, severity, meta))
        return object()

    async def run(self, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            await self.w.tick()
            for _ in range(5):
                await asyncio.sleep(0)
        if self.w._task:
            await self.w._task


async def test_bed_not_clear_twice_reports_once(monkeypatch):
    bad = {"clear": False, "confidence": 0.9, "objects": ["scraper"], "reason": "A scraper lies on the plate."}
    rig = Rig([bad, bad], monkeypatch)
    await rig.run(2)
    rig.state = "printing"
    await rig.run(5)
    assert [r[0] for r in rig.reports] == ["ai_bed_not_clear"]
    assert rig.reports[0][1] == "critical" and "scraper" in rig.reports[0][2]["summary"]
    assert rig.saved and len(rig.saved[0]) == 2
    assert len(rig.fake.chats) == 2  # no third check: the bed check runs once per print


async def test_bed_fine_on_second_look_no_alert(monkeypatch):
    rig = Rig([{"clear": False, "confidence": 0.9}, {"clear": True, "confidence": 0.9}], monkeypatch)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(5)
    assert rig.reports == []
    assert "second look" in rig.w.status()["recent"][0]["verdict"]


async def test_low_confidence_is_ignored(monkeypatch):
    rig = Rig([{"clear": False, "confidence": 0.3}], monkeypatch)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(5)
    assert rig.reports == [] and len(rig.fake.chats) == 1


async def test_no_bed_check_when_guardian_starts_mid_print(monkeypatch):
    rig = Rig([{"clear": False, "confidence": 0.9}] * 2, monkeypatch)
    rig.state = "printing"  # first thing Guardian sees: already printing
    await rig.run(60)
    # No bed check (the print started before Guardian), but failure checks start at once.
    assert rig.reports == [] and rig.fake.chats
    assert all("print is failing" in c["messages"][-1]["content"] for c in rig.fake.chats)


async def test_print_failure_needs_three_in_a_row(monkeypatch):
    fail = {"plate_visible": True, "failure": True, "kind": "spaghetti", "confidence": 0.95, "description": "Tangled strands."}
    ok = {"plate_visible": True, "clear": True, "failure": False, "confidence": 0.9}
    rig = Rig([ok, fail, fail, fail], monkeypatch, print_check_every_s=60)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(299)
    assert rig.reports == [] and len(rig.fake.chats) == 1  # only the bed check so far
    await rig.run(50)
    assert rig.reports == [] and len(rig.fake.chats) == 3  # two positives, spaced 45 s apart
    await rig.run(50)
    assert [r[0] for r in rig.reports] == ["ai_print_failure"]
    assert "3 times in a row" in rig.reports[0][2]["summary"] and len(rig.saved[0]) == 3


async def test_one_fine_answer_resets_the_streak(monkeypatch):
    fail = {"plate_visible": True, "failure": True, "kind": "knocked_over", "confidence": 0.9}
    fine = {"plate_visible": True, "failure": False, "confidence": 0.9}
    rig = Rig([fine, fail, fail, fine, fail, fail], monkeypatch, print_check_every_s=60)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(299 + 60 * 6)
    assert rig.reports == []


async def test_quiet_after_a_report_and_warning_when_ai_cannot_pause(monkeypatch):
    fail = {"plate_visible": True, "failure": True, "kind": "knocked_over", "confidence": 0.9}
    rig = Rig([{"clear": True, "confidence": 0.9}] + [fail] * 20, monkeypatch, print_check_every_s=60)
    rig.w._failure_severity = lambda: "warning"
    await rig.run(1)
    rig.state = "printing"
    await rig.run(299 + 45 * 12)
    assert [r[1] for r in rig.reports] == ["warning"]  # once, then quiet for 30 minutes


async def test_the_question_says_what_is_printing(monkeypatch):
    rig = Rig([{"clear": True, "confidence": 0.9}, {"failure": False, "confidence": 0.9}], monkeypatch)
    rig.w._context = lambda: {"objects": ["SHEEP10.STL_ID_0_COPY_0"] * 10, "layer": 98, "total_layers": 287,
                              "height_now": 19.7, "height_total": 57.5, "colors": ["white"]}
    await rig.run(1)
    rig.state = "printing"
    await rig.run(305)
    prompt = rig.fake.chats[-1]["messages"][-1]["content"]
    assert "10 object(s): SHEEP10" in prompt and "Layer 98 of 287" in prompt and "hollow shell" in prompt


async def test_no_fresh_picture_means_no_check(monkeypatch):
    rig = Rig([], monkeypatch, frame=None)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(250)
    assert rig.fake.chats == []
    assert "skipped" in rig.w.status()["recent"][0]["verdict"]


async def test_ai_off_or_model_missing_does_nothing(monkeypatch):
    rig = Rig([{"clear": False, "confidence": 1}], monkeypatch)
    rig.ai.cfg.enabled = False
    await rig.run(1)
    rig.state = "printing"
    await rig.run(10)
    assert rig.fake.chats == []
    rig2 = Rig([{"clear": False, "confidence": 1}], monkeypatch)
    rig2.fake.models = ["gemma3:4b"]
    await rig2.run(1)
    rig2.state = "printing"
    await rig2.run(10)
    assert rig2.fake.chats == []


# ---- diagnosis ----

def make_incident(tmp_path, clock, event_type="print_paused_with_exception"):
    cfg = Config(storage=StorageConfig(data_dir=tmp_path / "data"), ai=AiConfig(enabled=True))
    db = Database(cfg.storage.db_path, clock)
    first = T0
    uid = db.next_uid(first)
    iid = db.create_incident(uid=uid, event_type=event_type, source="moonraker", severity="critical",
                             start_time_utc=to_iso(first - timedelta(seconds=30)), first_trigger_utc=to_iso(first),
                             last_trigger_utc=to_iso(first), print_filename="sheep.gcode")
    db.update_incident(iid, recording_status="missing", recording_notes="No video frames were available.")
    db.add_event(iid, ts_utc=first, event_type=event_type, source="moonraker", severity="critical", is_trigger=True,
                 metadata={"message": "extruder[2]: state: load_feeding, error: wheel_speed"})
    db.create_toolchange(requested_tool="extruder2", previous_tool="extruder1", result="ok", duration_ms=3900,
                         started_utc=to_iso(first - timedelta(seconds=12)))
    folder = incident_dir(cfg.storage, iid, first)
    folder.mkdir(parents=True)
    (folder / "printer_logs.txt").write_text("22:16:10.905 extruder[2]: state: load_feeding, error: wheel_speed!\n",
                                             encoding="utf-8")
    return cfg, db, uid, folder


async def test_diagnosis_saved_event_added_and_emailed(tmp_path):
    clock = ManualClock(T0 + timedelta(minutes=1))
    cfg, db, uid, folder = make_incident(tmp_path, clock)
    answer = {"summary": "The filament feeder for tool 2 could not push filament.", "likely_cause": "filament slipping",
              "confidence": "medium", "evidence": ["wheel_speed error"], "what_to_check": ["Check the T2 spool"],
              "safe_to_resume": "unknown"}
    fake = FakeOllama([answer])
    ai = AiService(cfg.ai, clock, Ollama(cfg.ai.ollama_url, httpx.MockTransport(fake)))
    sent = []

    async def notify(u, subject, text):
        sent.append((u, subject, text))

    d = Diagnoser(cfg, db, ai, clock, "ffmpeg-not-used", notify=notify)
    result = await d.run(uid)
    assert result and result["answer"]["likely_cause"] == "filament slipping"
    saved = json.loads((folder / "ai_diagnosis.json").read_text(encoding="utf-8"))
    assert saved["available"] and saved["pictures"] == 0
    prompt = fake.chats[0]["messages"][-1]["content"]
    assert "wheel_speed" in prompt and "extruder1 -> extruder2" in prompt and "PICTURES: none" in prompt
    kinds = [e["event_type"] for e in db.list_events(db.get_incident_by_uid(uid)["id"])]
    assert "ai_diagnosis" in kinds
    assert sent and "AI guess" in sent[0][2] and "Check the T2 spool" in sent[0][2]


async def test_diagnosis_without_ollama_says_so(tmp_path):
    clock = ManualClock(T0 + timedelta(minutes=1))
    cfg, db, uid, folder = make_incident(tmp_path, clock)
    ai = AiService(cfg.ai, clock, Ollama(cfg.ai.ollama_url, httpx.MockTransport(FakeOllama(up=False))))
    assert await Diagnoser(cfg, db, ai, clock, "ffmpeg").run(uid) is None
    saved = json.loads((folder / "ai_diagnosis.json").read_text(encoding="utf-8"))
    assert saved["available"] is False and "not available" in saved["note"]


def test_which_incidents_get_a_diagnosis():
    row = {"severity": "warning"}
    assert not wanted("stops", row, {"manual"})
    assert wanted("stops", row, {"print_paused"})
    assert not wanted("stops", row, {"toolchange_bad_state"})
    assert wanted("stops", {"severity": "critical"}, {"toolchange_wrong_tool"})
    assert wanted("all", row, {"toolchange_bad_state"})
    assert not wanted("off", {"severity": "critical"}, {"print_error"})



async def test_bed_check_waits_for_the_printers_bed_step_or_the_fallback(monkeypatch):
    bad = {"plate_visible": True, "clear": False, "confidence": 0.9}
    rig = Rig([bad, bad], monkeypatch)
    rig.action = 0
    await rig.run(1)
    rig.state = "printing"
    await rig.run(30)
    assert rig.fake.chats == [] and getattr(rig.fake, "warmed", 0) == 1  # model loaded early, no check yet
    rig.action = 136
    await rig.run(3)
    assert [r[0] for r in rig.reports] == ["ai_bed_not_clear"]
    late = Rig([{"plate_visible": True, "clear": True, "confidence": 0.9}], monkeypatch)
    late.action = 0
    await late.run(1)
    late.state = "printing"
    await late.run(119)
    assert late.fake.chats == []
    await late.run(3)
    assert len(late.fake.chats) == 1  # fallback after 120 s


async def test_plate_not_visible_is_no_verdict(monkeypatch):
    rig = Rig([{"plate_visible": False, "clear": False, "confidence": 1.0},
               {"plate_visible": False, "failure": True, "confidence": 1.0}], monkeypatch)
    await rig.run(1)
    rig.state = "printing"
    await rig.run(5)
    assert rig.reports == [] and "not visible" in rig.w.status()["recent"][0]["verdict"]


def test_known_meanings_and_tidy():
    from guardian.ai.diagnosis import known_notes, tidy
    notes = known_notes("!! extruder[2]: state: load_feeding, error: wheel_speed!raw msg:logic error!")
    assert len(notes) == 1 and "not moving" in notes[0]
    assert known_notes("everything fine") == []
    a = tidy({"evidence": ["x", "X ", "y"], "what_to_check": ["a", "a"]})
    assert a["evidence"] == ["x", "y"] and a["what_to_check"] == ["a"]


async def test_compare_sends_both_pictures_and_only_records(monkeypatch):
    answer = {"matches": False, "differences": ["part 3 is missing"], "kind": "missing_part", "confidence": 0.9}
    rig = Rig([answer], monkeypatch)

    async def sim():
        return b"\x89PNG simulated"

    rig.w._get_simulated = sim
    await rig.w._compare(JPEG)
    assert len(rig.fake.chats[0]["messages"][-1]["images"]) == 2  # the photo and the simulated picture
    assert rig.reports == []  # watch only: no incident, so no pause
    rec = rig.w.status()["recent"][0]
    assert rec["check"] == "compare" and "part 3 is missing" in rec["verdict"] and "no alert" in rec["verdict"]


async def test_compare_is_skipped_without_a_simulated_picture(monkeypatch):
    rig = Rig([{"matches": True, "confidence": 0.9}], monkeypatch)
    await rig.w._compare(JPEG)  # no provider at all
    assert rig.fake.chats == []

    async def none():
        return None

    rig.w._get_simulated = none  # camera not set up
    await rig.w._compare(JPEG)
    assert rig.fake.chats == [] and rig.w.status()["recent"] == []
