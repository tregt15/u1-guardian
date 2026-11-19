"""Failed AI calls say what went wrong and what to do."""

import httpx
import pytest

from guardian.ai.ollama import Ollama, OllamaError, explain_failure
from guardian.config import AI_MODELS, AiConfig
from guardian.settings import FIELDS


def test_timeout_message_says_what_to_do():
    msg = explain_failure(httpx.ReadTimeout("x"), 180)
    assert "180 seconds" in msg and "smaller model" in msg and "Settings" in msg


def test_connect_message_points_at_ollama():
    assert "not be running" in explain_failure(httpx.ConnectError("x"), 10)
    assert "internet" in explain_failure(httpx.ConnectError("x"), 10, "the AI service")


@pytest.mark.asyncio
async def test_chat_timeout_gives_plain_error():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    o = Ollama("http://x", transport=httpx.MockTransport(boom))
    with pytest.raises(OllamaError, match="did not answer within 20 seconds"):
        await o.chat_json("m", "hi", timeout=20)


@pytest.mark.asyncio
async def test_missing_model_message():
    o = Ollama("http://x", transport=httpx.MockTransport(lambda r: httpx.Response(404, text="nope")))
    with pytest.raises(OllamaError, match="Download it"):
        await o.chat_json("m", "hi")


def test_timeout_and_small_model_are_settings():
    assert any(f["key"] == "ai.timeout_s" for f in FIELDS)
    assert "qwen2.5vl:3b" in AI_MODELS
    assert AiConfig(model="qwen2.5vl:3b", timeout_s=600).timeout_s == 600
