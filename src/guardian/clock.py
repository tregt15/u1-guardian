"""Time handling. Everything is UTC inside Guardian.

Convert to local time only when showing it to a person.
Times are stored as ISO 8601 strings that end in Z, for example
2026-11-02T13:05:22.123Z.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol

UTC = timezone.utc


class Clock(Protocol):
    """Anything that can tell the time. Tests swap in a fake one."""

    def now_utc(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock you move by hand. Used in tests and simulations."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = ensure_utc(start) if start else datetime(2026, 11, 1, tzinfo=UTC)
        self._mono = 0.0

    def now_utc(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def set(self, when: datetime) -> None:
        when = ensure_utc(when)
        self._mono += (when - self._now).total_seconds()
        self._now = when

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._mono += seconds


def ensure_utc(dt: datetime) -> datetime:
    """Return dt as UTC. A naive datetime is rejected, because it hides bugs."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed, use a timezone-aware UTC value")
    return dt.astimezone(UTC)


def to_iso(dt: datetime) -> str:
    """Format as 2026-11-02T13:05:22.123Z (millisecond precision)."""
    dt = ensure_utc(dt)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def from_iso(text: str) -> datetime:
    """Parse the format made by to_iso. Also accepts +00:00 endings."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def uid_stamp(dt: datetime) -> str:
    """Time part of an incident uid, safe in file names: 2026-11-02T13-05-22Z."""
    return ensure_utc(dt).strftime("%Y-%m-%dT%H-%M-%SZ")
