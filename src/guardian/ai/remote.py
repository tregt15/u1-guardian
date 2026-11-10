"""Non-local AI through an API key (owner asked, 2026-11-10). Off unless the user picks it.

Two kinds of service:
- "openai": anything that speaks the OpenAI chat completions API (OpenAI, OpenRouter, Groq, Google Gemini's
  OpenAI-compatible endpoint, LM Studio, and more). POST {url}/chat/completions, images as data URLs.
- "anthropic": Anthropic's Messages API (Claude). POST https://api.anthropic.com/v1/messages.

The key comes from an environment variable (or the .env file), never from a config file.
PRIVACY: with these, the camera pictures and printer logs Guardian sends for a check go to that company.
The settings page says so before it is turned on. Request shapes follow each company's public API docs.
They have only been tested here against a fake server (UNVERIFIED against the real services).
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

import httpx

from guardian.ai.ollama import OllamaError

ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class RemoteAiError(OllamaError):
    """Same handling as a local AI error: the check is skipped and the reason is shown."""


def api_key(env_name: str) -> str:
    return os.environ.get(env_name, "").strip()


def _parse_json(text: str) -> dict:
    text = FENCE_RE.sub("", (text or "").strip())
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)  # some models add a sentence around the JSON
        if not m:
            raise RemoteAiError("the model did not answer with JSON") from None
        try:
            data = json.loads(m.group(0))
        except ValueError as exc:
            raise RemoteAiError("the model did not answer with JSON") from exc
    if not isinstance(data, dict):
        raise RemoteAiError("the model did not answer with a JSON object")
    return data


@dataclass
class RemoteAI:
    kind: str  # "openai" or "anthropic"
    url: str
    key: str
    transport: httpx.AsyncBaseTransport | None = None

    def _base(self) -> str:
        return (ANTHROPIC_URL if self.kind == "anthropic" else self.url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        if self.kind == "anthropic":
            return {"x-api-key": self.key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        return {"Authorization": f"Bearer {self.key}", "content-type": "application/json"}

    async def check(self, model: str) -> tuple[bool, str]:
        """(ok, message). Lists the service's models with the key. Never raises."""
        if not self.key:
            return False, "no API key found"
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self.transport) as c:
                r = await c.get(self._base() + "/models", headers=self._headers())
        except httpx.HTTPError as exc:
            return False, f"could not reach the AI service ({type(exc).__name__})"
        if r.status_code in (401, 403):
            return False, "the AI service refused the API key"
        if r.status_code == 404:
            return True, "connected (this service does not list its models)"
        if r.status_code != 200:
            return False, f"the AI service answered HTTP {r.status_code}"
        try:
            ids = {m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)}
        except (ValueError, AttributeError):
            ids = set()
        if ids and model not in ids:
            return False, f"the service does not offer the model '{model}'"
        return True, "connected"

    async def chat_json(self, model: str, prompt: str, images: list[bytes] | None = None, *,
                        system: str | None = None, timeout: float = 180.0, num_ctx: int = 8192) -> dict:
        b64 = [base64.b64encode(b).decode("ascii") for b in images or []]
        if self.kind == "anthropic":
            content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d}}
                       for d in b64] + [{"type": "text", "text": prompt}]
            body: dict = {"model": model, "max_tokens": 1024, "temperature": 0,
                          "messages": [{"role": "user", "content": content}]}
            if system:
                body["system"] = system
            path = "/messages"
        else:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{d}"}} for d in b64]
            messages = ([{"role": "system", "content": system}] if system else []) + \
                [{"role": "user", "content": content}]
            body = {"model": model, "messages": messages, "temperature": 0,
                    "response_format": {"type": "json_object"}}
            path = "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as c:
                r = await c.post(self._base() + path, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise RemoteAiError(f"could not reach the AI service ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise RemoteAiError(f"the AI service answered HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            if self.kind == "anthropic":
                text = "".join(part.get("text", "") for part in data["content"] if part.get("type") == "text")
            else:
                text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RemoteAiError("the AI service sent an answer Guardian could not read") from exc
        return _parse_json(text)
