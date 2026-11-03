"""A small internal async event bus.

Parts of Guardian talk through topics, not by calling each other.
A handler that raises an error is logged and skipped. One broken
handler must never stop the others (spec section 2, principle 6).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[str, Any], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Subscribe to a topic. Use "*" to get every topic."""
        self._handlers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        if handler in self._handlers.get(topic, []):
            self._handlers[topic].remove(handler)

    async def publish(self, topic: str, payload: Any = None) -> None:
        """Deliver to all handlers and wait for them. Errors are logged."""
        handlers = [*self._handlers.get(topic, []), *self._handlers.get("*", [])]
        if not handlers:
            return
        await asyncio.gather(*(self._call(h, topic, payload) for h in handlers))

    async def _call(self, handler: Handler, topic: str, payload: Any) -> None:
        try:
            result = handler(topic, payload)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - by design, nothing may escape a handler
            log.exception("event handler failed for topic %r", topic)
