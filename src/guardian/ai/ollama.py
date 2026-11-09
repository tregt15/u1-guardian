"""Small client for a local Ollama server (https://ollama.com). Ollama is an outside program the
user installs, like FFmpeg. Guardian never installs it and never sends anything to the internet.

Endpoints used (Ollama's documented REST API): GET /api/version, GET /api/tags, POST /api/pull
(streamed progress), DELETE /api/delete, POST /api/chat (images as base64, JSON output).
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OllamaError(Exception):
    pass


@dataclass
class PullState:
    model: str | None = None
    running: bool = False
    status: str | None = None
    completed: int = 0
    total: int = 0
    error: str | None = None
    done: bool = False

    def as_dict(self) -> dict[str, Any]:
        pct = round(100.0 * self.completed / self.total, 1) if self.total else None
        return {"model": self.model, "running": self.running, "status": self.status, "percent": pct,
                "completed_bytes": self.completed, "total_bytes": self.total, "error": self.error, "done": self.done}


@dataclass
class Ollama:
    url: str
    transport: httpx.AsyncBaseTransport | None = None
    pull_state: PullState = field(default_factory=PullState)

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url.rstrip("/"), timeout=timeout, transport=self.transport)

    async def version(self) -> str | None:
        """Ollama's version, or None if nothing answers at the address."""
        try:
            async with self._client(3.0) as c:
                r = await c.get("/api/version")
                r.raise_for_status()
                return str(r.json().get("version"))
        except (httpx.HTTPError, ValueError):
            return None

    async def models(self) -> dict[str, int]:
        """Installed models: name -> size in bytes."""
        async with self._client(10.0) as c:
            r = await c.get("/api/tags")
            r.raise_for_status()
            out = {}
            for m in r.json().get("models", []):
                name = m.get("name") or m.get("model")
                if name:
                    out[name] = int(m.get("size") or 0)
            return out

    async def has_model(self, model: str) -> bool:
        try:
            names = await self.models()
        except (httpx.HTTPError, ValueError):
            return False
        return model in names or f"{model}:latest" in names

    async def pull(self, model: str) -> None:
        """Download a model, keeping progress in pull_state. Never raises."""
        st = self.pull_state = PullState(model=model, running=True, status="starting")
        try:
            async with self._client(None) as c:  # a big download can take a long time
                async with c.stream("POST", "/api/pull", json={"model": model, "stream": True}) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if msg.get("error"):
                            raise OllamaError(str(msg["error"]))
                        st.status = msg.get("status", st.status)
                        if msg.get("total"):
                            st.total, st.completed = int(msg["total"]), int(msg.get("completed") or 0)
                        if st.status == "success":
                            st.done = True
        except Exception as exc:  # noqa: BLE001
            st.error = f"{type(exc).__name__}: {exc}"
            log.warning("model download failed: %s", st.error)
        finally:
            st.running = False

    async def delete(self, model: str) -> None:
        async with self._client(30.0) as c:
            r = await c.request("DELETE", "/api/delete", json={"model": model})
            if r.status_code not in (200, 404):
                raise OllamaError(f"HTTP {r.status_code}")

    async def chat_json(self, model: str, prompt: str, images: list[bytes] | None = None, *,
                        system: str | None = None, timeout: float = 180.0, num_ctx: int = 8192) -> dict:
        """Ask the model and get a JSON object back. Raises OllamaError."""
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user["images"] = [base64.b64encode(b).decode("ascii") for b in images]
        messages.append(user)
        body = {"model": model, "messages": messages, "stream": False, "format": "json",
                "options": {"temperature": 0, "num_ctx": num_ctx}}
        try:
            async with self._client(timeout) as c:
                r = await c.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise OllamaError(f"could not reach Ollama ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise OllamaError(f"Ollama answered HTTP {r.status_code}: {r.text[:200]}")
        try:
            content = r.json()["message"]["content"]
            data = json.loads(content)
        except (ValueError, KeyError, TypeError) as exc:
            raise OllamaError("the model did not answer with JSON") from exc
        if not isinstance(data, dict):
            raise OllamaError("the model did not answer with a JSON object")
        return data
