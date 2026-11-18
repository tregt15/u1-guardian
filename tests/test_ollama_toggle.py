from types import SimpleNamespace

from guardian import hostinfo
from guardian.main import App as Guardian


def _fake(enabled, provider="local", url="http://127.0.0.1:11434"):
    return SimpleNamespace(cfg=SimpleNamespace(ai=SimpleNamespace(enabled=enabled, provider=provider, ollama_url=url)))


def test_local_address_check():
    assert hostinfo.ollama_is_local("http://127.0.0.1:11434")
    assert hostinfo.ollama_is_local("http://localhost:11434")
    assert not hostinfo.ollama_is_local("http://192.168.1.50:11434")


def test_ai_off_stops_and_on_starts(monkeypatch):
    calls = []
    monkeypatch.setattr(hostinfo, "stop_ollama", lambda: calls.append("stop"))
    monkeypatch.setattr(hostinfo, "ollama_start_command", lambda: ["ollama", "serve"])
    monkeypatch.setattr(hostinfo, "start_detached", lambda cmd: calls.append("start"))
    Guardian._sync_ollama_service(_fake(False))
    Guardian._sync_ollama_service(_fake(True))
    assert calls == ["stop", "start"]


def test_remote_ollama_is_left_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(hostinfo, "stop_ollama", lambda: calls.append("stop"))
    Guardian._sync_ollama_service(_fake(False, url="http://192.168.1.50:11434"))
    Guardian._sync_ollama_service(_fake(False, provider="openai"))
    assert calls == []
