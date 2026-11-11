"""AI key typed on the Settings page: stored apart from settings, never sent back."""

import json
import os

import pytest

from guardian import secrets_store
from guardian.config import AiConfig, Config, StorageConfig


def make(tmp_path):
    return Config(storage=StorageConfig(data_dir=tmp_path / "data"))


def test_put_get_remove_and_file_mode(tmp_path):
    cfg = make(tmp_path)
    assert secrets_store.ai_key(cfg) == ("", None)
    secrets_store.put(cfg, secrets_store.AI_KEY, "  sk-abcdefgh1234 ")
    assert secrets_store.ai_key(cfg) == ("sk-abcdefgh1234", "settings")
    path = cfg.storage.resolved_data_dir() / "secrets.json"
    assert json.loads(path.read_text()) == {"ai_api_key": "sk-abcdefgh1234"}
    if os.name != "nt":
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not (cfg.storage.resolved_data_dir() / "settings.yaml").exists()  # never mixed with settings
    secrets_store.put(cfg, secrets_store.AI_KEY, "")
    assert secrets_store.ai_key(cfg) == ("", None)
    with pytest.raises(ValueError):
        secrets_store.put(cfg, secrets_store.AI_KEY, "bad\nkey")
    assert secrets_store.hint("sk-abcdefgh1234") == "...1234" and secrets_store.hint("") is None


def test_env_var_wins(tmp_path, monkeypatch):
    cfg = make(tmp_path)
    secrets_store.put(cfg, secrets_store.AI_KEY, "from-settings-1234")
    monkeypatch.setenv(cfg.ai.api_key_env, "from-env-9999")
    assert secrets_store.ai_key(cfg) == ("from-env-9999", "env")


def test_api_saves_key_and_never_returns_it(tmp_path):
    from fastapi.testclient import TestClient

    from guardian.api.app import create_app
    from tests.test_api import make_env  # noqa: F401 - reuse the test app setup

    env = make_env(tmp_path)
    saved = {}

    async def set_key(key):
        saved["key"] = key
        return {"set": bool(key), "status": {"api_key_hint": secrets_store.hint(key or "")}}

    env.ctx.extra["set_ai_key"] = set_key
    client = TestClient(create_app(env.ctx))
    r = client.put("/api/ai/key", json={"key": "sk-secret-value-5678"})
    assert r.status_code == 200 and "sk-secret-value-5678" not in r.text and "...5678" in r.text
    assert saved["key"] == "sk-secret-value-5678"
    assert client.put("/api/ai/key", json={"key": "x" * 600}).status_code == 422
    assert "sk-secret" not in client.get("/api/settings").text


async def test_ai_service_uses_the_saved_key(tmp_path):
    import httpx

    from guardian.ai.vision import AiService
    from guardian.clock import ManualClock, from_iso

    cfg = make(tmp_path)
    cfg.ai = AiConfig(enabled=True, provider="openai", remote_model="m1", remote_url="https://api.example/v1")
    secrets_store.put(cfg, secrets_store.AI_KEY, "sk-typed-in-page-0001")
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    ai = AiService(cfg.ai, ManualClock(from_iso("2026-11-10T00:00:00Z")), remote_transport=httpx.MockTransport(handler),
                   key_fn=lambda: secrets_store.ai_key(cfg))
    await ai.refresh(force=True)
    assert ai.usable and seen == ["Bearer sk-typed-in-page-0001"]
    st = ai.status()
    assert st["api_key_source"] == "settings" and st["api_key_hint"] == "...0001"
    assert "sk-typed" not in json.dumps(st)


def test_resend_key_from_settings_and_env_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARDIAN_RESEND_KEY", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    cfg = make(tmp_path)
    assert secrets_store.email_key(cfg) == ("", None)
    secrets_store.put(cfg, secrets_store.EMAIL_KEY, "re_typed_in_page_42")
    assert secrets_store.email_key(cfg) == ("re_typed_in_page_42", "settings")
    assert secrets_store.ai_key(cfg) == ("", None)  # the two keys are kept apart
    monkeypatch.setenv("GUARDIAN_RESEND_KEY", "re_from_env_77")
    assert secrets_store.email_key(cfg) == ("re_from_env_77", "env")


async def test_email_channel_sends_with_the_saved_key(tmp_path):
    import httpx

    from guardian.config import EmailConfig
    from guardian.notify.email import ResendEmailChannel

    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": "x"})

    ch = ResendEmailChannel(EmailConfig(enabled=True, to_addrs=["me@example.com"]), transport=httpx.MockTransport(handler),
                            key_fn=lambda: "re_saved_1234")
    assert ch.configured
    await ch.send("hi", "text")
    assert seen == ["Bearer re_saved_1234"]


def test_email_key_api_never_returns_the_key(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from guardian.api.app import create_app
    from tests.test_api import make_env

    env = make_env(tmp_path)
    saved = {}

    async def set_email_key(key):
        saved["k"] = key
        return {"set": bool(key), "source": "settings", "hint": secrets_store.hint(key or "")}

    env.ctx.extra["app"] = SimpleNamespace(set_email_key=set_email_key)
    client = TestClient(create_app(env.ctx))
    r = client.put("/api/notifications/email/key", json={"key": "re_secret_value_9999"})
    assert r.status_code == 200 and "re_secret_value_9999" not in r.text and "...9999" in r.text
    assert saved["k"] == "re_secret_value_9999"
