"""Fan-out of server events to WebSocket subscribers.

Each subscriber gets a bounded queue; a slow consumer loses its oldest
events rather than ever blocking the publisher. Every event carries a
monotonically increasing `seq`, so a consumer that sees a gap knows it
lost events and should resync over REST.

Subscribers may be tagged with a registered client id; `publish(to=...)`
then delivers to that client's connections only (used for routed keys and
focus events).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 64):
        self._maxsize = maxsize
        self._subscribers: dict[asyncio.Queue, str | None] = {}
        self._seq = 0

    def subscribe(self, client_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[q] = client_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def has_client(self, client_id: str) -> bool:
        """Is at least one connection authenticated as this client?"""
        return client_id in self._subscribers.values()

    @property
    def seq(self) -> int:
        """Sequence number of the most recently published event."""
        return self._seq

    def publish(self, event: dict[str, Any], *, to: str | None = None) -> None:
        self._seq += 1
        event.setdefault("ts", time.time())
        event["seq"] = self._seq
        for q, client_id in self._subscribers.items():
            if to is not None and client_id != to:
                continue
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(event)
