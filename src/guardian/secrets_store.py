"""Keys the user typed on the Settings page (owner approved, 2026-11-10).

Stored in `secrets.json` in Guardian's data folder, never in config.yaml or settings.yaml, never in an export,
never sent back by the API (only "set" and the last 4 characters). On Linux the file is made readable by its owner
only. An environment variable with the same purpose still wins, so nothing changes for people who use .env.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from guardian.config import Config

log = logging.getLogger(__name__)

FILE = "secrets.json"
AI_KEY = "ai_api_key"
EMAIL_KEY = "resend_api_key"
MAX_LEN = 500


def _path(cfg: Config) -> Path:
    return cfg.storage.resolved_data_dir() / FILE


def _read(cfg: Config) -> dict:
    p = _path(cfg)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.error("could not read %s", p)
        return {}


def get(cfg: Config, name: str) -> str:
    v = _read(cfg).get(name)
    return v if isinstance(v, str) else ""


def put(cfg: Config, name: str, value: str | None) -> None:
    """Save a key, or remove it when value is empty or None."""
    if value is not None:
        value = value.strip()
        if len(value) > MAX_LEN or any(ord(ch) < 32 for ch in value):
            raise ValueError("that does not look like an API key")
    data = _read(cfg)
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def ai_key(cfg: Config) -> tuple[str, str | None]:
    """(key, where it came from: 'env', 'settings' or None)."""
    env = os.environ.get(cfg.ai.api_key_env, "").strip()
    if env:
        return env, "env"
    stored = get(cfg, AI_KEY)
    return (stored, "settings") if stored else ("", None)


def email_key(cfg: Config) -> tuple[str, str | None]:
    """The Resend key: environment variable first (GUARDIAN_RESEND_KEY or RESEND_API_KEY), then the Settings page."""
    env = cfg.notifications.email.api_key().strip()
    if env:
        return env, "env"
    stored = get(cfg, EMAIL_KEY)
    return (stored, "settings") if stored else ("", None)


def hint(key: str) -> str | None:
    return ("..." + key[-4:]) if len(key) >= 8 else ("set" if key else None)
