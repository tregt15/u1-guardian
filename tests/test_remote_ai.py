"""Online AI services with an API key, against fake servers. Request shapes follow the public API docs."""

import base64
import json

import httpx
import pytest

from guardian.ai.remote import RemoteAI, RemoteAiError
from guardian.ai.vision import AiService
from guardian.clock import ManualClock, from_iso
from guardian.config import AiConfig

JPEG = b"\xff\xd8fake"


class FakeService:
    def __init__(self, kind, answer_text='{"clear": true, "confidence": 0.9}', models=("m1",), status=200):
        self.kind, self.answer_text, self.models, self.status = kind, answer_text, models, status
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "nope"})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if self.kind == "anthropic":
            return httpx.Response(200, json={"content": [{"type": "text", "text": self.answer_text}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": self.answer_text}}]})


async def test_openai_compatible_request_and_answer():
    fake = FakeService("openai")
    ai = RemoteAI("openai", "https://openrouter.example/api/v1", "sk-test", httpx.MockTransport(fake))
    assert await ai.check("m1") == (True, "connected")
    assert (await ai.check("other"))[0] is False
    out = await ai.chat_json("m1", "is it clear?", [JPEG], system="be careful")
    assert out == {"clear": True, "confidence": 0.9}
    req = fake.requests[-1]
    assert str(req.url) == "https://openrouter.example/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["response_format"] == {"type": "json_object"} and body["messages"][0]["role"] == "system"
    image = body["messages"][1]["content"][1]["image_url"]["url"]
    assert image == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


async def test_anthropic_request_and_fenced_answer():
    fake = FakeService("anthropic", answer_text='Here you go:\n```json\n{"failure": false, "confidence": 0.8}\n```')
    ai = RemoteAI("anthropic", "ignored", "ak-test", httpx.MockTransport(fake))
    out = await ai.chat_json("claude-x", "failing?", [JPEG], system="be careful")
    assert out == {"failure": False, "confidence": 0.8}
    req = fake.requests[-1]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "ak-test" and req.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(req.content)
    assert body["system"] == "be careful" and body["messages"][0]["content"][0]["type"] == "image"


async def test_bad_key_missing_key_and_bad_answers():
    assert await RemoteAI("openai", "https://x/v1", "", None).check("m") == (False, "no API key found")
    refused = RemoteAI("openai", "https://x/v1", "k", httpx.MockTransport(FakeService("openai", status=401)))
    assert (await refused.check("m"))[1] == "the AI service refused the API key"
    with pytest.raises(RemoteAiError):
        await refused.chat_json("m", "x")
    junk = RemoteAI("openai", "https://x/v1", "k", httpx.MockTransport(FakeService("openai", answer_text="no json here")))
    with pytest.raises(RemoteAiError):
        await junk.chat_json("m", "x")


async def test_service_uses_the_remote_provider_and_the_key_from_env(monkeypatch):
    monkeypatch.setenv("MY_AI_KEY", "sk-env")
    fake = FakeService("openai", models=("gpt-test",))
    cfg = AiConfig(enabled=True, provider="openai", remote_model="gpt-test", api_key_env="MY_AI_KEY",
                   remote_url="https://api.example/v1")
    ai = AiService(cfg, ManualClock(from_iso("2026-11-10T00:00:00Z")), remote_transport=httpx.MockTransport(fake))
    await ai.refresh(force=True)
    assert ai.usable and ai.status()["api_key_found"] and ai.status()["model"] == "gpt-test"
    assert await ai.ask("hi", [JPEG]) == {"clear": True, "confidence": 0.9}
    assert fake.requests[-1].headers["authorization"] == "Bearer sk-env"
    monkeypatch.delenv("MY_AI_KEY")
    await ai.refresh(force=True)
    assert not ai.usable and ai.status()["remote_message"] == "no API key found"


def test_remote_needs_a_model_name():
    with pytest.raises(ValueError):
        AiConfig(enabled=True, provider="anthropic", remote_model="")
    with pytest.raises(ValueError):
        AiConfig(api_key_env="bad name; rm")
    assert AiConfig(enabled=False, provider="anthropic").active_model == ""
