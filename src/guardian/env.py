"""Tiny .env loader, so a person can keep their own secrets in a file instead of the shell.

Rules: lines look like KEY=value. Comments start with #. Values already set in the real
environment win, so a .env file never overrides what you set on purpose. Values are never
printed or logged. The file itself is git-ignored in this project.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_dotenv(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_dotenv(*paths: Path) -> list[Path]:
    """Load each existing file. Returns the files that were read (never the values)."""
    loaded: list[Path] = []
    for path in paths:
        try:
            values = parse_dotenv(Path(path).read_text(encoding="utf-8"))
        except OSError:
            continue
        for k, v in values.items():
            os.environ.setdefault(k, v)
        loaded.append(Path(path))
    return loaded
