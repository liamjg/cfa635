"""Client registry: who is talking to the server.

A client registers once (POST /clients) and gets an id plus a bearer token.
The token is arbitration identity, not security: it decides who may modify
which resources (pages, LEDs, pins) so clients cannot clobber each other on
a trusted LAN. Registry state is in-memory, like pages — after a server
restart clients re-register and re-publish.

Pure logic over an injected clock — no web or asyncio dependencies.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass


@dataclass
class Client:
    id: str            # server-generated, e.g. "c-4f2a91d0"
    token: str         # bearer token; returned exactly once, at registration
    name: str          # display label chosen by the client; not unique
    created_at: float = 0.0
    last_seen: float = 0.0


class ClientRegistry:
    def __init__(self):
        self._by_id: dict[str, Client] = {}
        self._by_token: dict[str, Client] = {}

    def register(self, name: str, now: float) -> Client:
        while True:
            client_id = "c-" + secrets.token_hex(4)
            if client_id not in self._by_id:
                break
        client = Client(
            id=client_id,
            token=secrets.token_urlsafe(16),
            name=name,
            created_at=now,
            last_seen=now,
        )
        self._by_id[client.id] = client
        self._by_token[client.token] = client
        return client

    def get(self, client_id: str) -> Client | None:
        return self._by_id.get(client_id)

    def by_token(self, token: str) -> Client | None:
        return self._by_token.get(token)

    def delete(self, client_id: str) -> bool:
        client = self._by_id.pop(client_id, None)
        if client is None:
            return False
        del self._by_token[client.token]
        return True

    def all(self) -> list[Client]:
        return sorted(self._by_id.values(), key=lambda c: c.created_at)
