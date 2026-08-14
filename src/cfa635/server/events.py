"""Fan-out of server events to WebSocket subscribers.

Each subscriber gets a bounded queue; a slow consumer loses its oldest
events rather than ever blocking the publisher.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 64):
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(event)
