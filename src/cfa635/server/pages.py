"""Page store and display arbitration.

Pages are virtual 4x20 screens owned by network clients. The arbiter decides
which one is on the glass:

  1. A manual pin (keypad navigation or POST /pages/{id}/activate) wins until
     it times out, the page disappears, or an alert arrives.
  2. Alert pages (priority >= ALERT_PRIORITY) preempt pins and rotation.
  3. Otherwise the highest-priority pages rotate in created_at order.
  4. An empty store selects the built-in idle page (id None).

Pure logic over an injected clock — no device or asyncio dependencies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

ALERT_PRIORITY = 100


@dataclass
class Page:
    id: str
    name: str
    lines: list[str]
    owner: str = ""  # registered client id; only the owner may mutate
    interactive: bool = False  # ENTER may focus this page (keys route to owner)
    priority: int = 50
    ttl: float | None = None
    duration: float | None = None  # rotation dwell override while current
    leds: dict[int, tuple[int, int]] | None = None  # led index -> (green, red)
    cursor: tuple[int, int, str] | None = None  # (row, col, style)
    created_at: float = 0.0
    updated_at: float = 0.0

    def expires_at(self) -> float | None:
        return None if self.ttl is None else self.updated_at + self.ttl

    def alive(self, now: float) -> bool:
        exp = self.expires_at()
        return exp is None or now < exp


class PageStore:
    def __init__(self):
        self._pages: dict[str, Page] = {}

    def get(self, page_id: str) -> Page | None:
        return self._pages.get(page_id)

    def put(self, page: Page) -> None:
        self._pages[page.id] = page

    def delete(self, page_id: str) -> bool:
        return self._pages.pop(page_id, None) is not None

    def alive_pages(self, now: float) -> list[Page]:
        return [p for p in self._pages.values() if p.alive(now)]

    def pages_for(self, owner: str) -> list[Page]:
        return [p for p in self._pages.values() if p.owner == owner]

    def sweep(self, now: float) -> list[Page]:
        """Remove and return expired pages."""
        dead = [p for p in self._pages.values() if not p.alive(now)]
        for page in dead:
            del self._pages[page.id]
        return dead


class Arbiter:
    def __init__(self, store: PageStore, *, rotation_secs: float = 10.0,
                 nav_hold_secs: float = 30.0, clock=time.monotonic):
        self.store = store
        self.rotation_secs = rotation_secs
        self.nav_hold_secs = nav_hold_secs
        self.clock = clock
        self._pin: tuple[str, float, str] | None = None  # (page_id, expires, by)
        self._current: str | None = None
        self._rot_since: float = 0.0

    @property
    def pinned(self) -> str | None:
        return self._pin[0] if self._pin else None

    @property
    def pinned_by(self) -> str | None:
        """Who holds the pin: a client id, or "keypad" for physical nav."""
        return self._pin[2] if self._pin else None

    def pin(self, page_id: str, hold: float | None = None,
            by: str = "keypad") -> bool:
        """Pin a page (keypad nav or /activate). False if it doesn't exist."""
        now = self.clock()
        if self.store.get(page_id) is None:
            return False
        self._pin = (page_id,
                     now + (hold if hold is not None else self.nav_hold_secs),
                     by)
        return True

    def release(self) -> None:
        self._pin = None

    def nav(self, step: int) -> str | None:
        """Cycle to the next/previous page (all priorities) and pin it."""
        now = self.clock()
        ids = [p.id for p in sorted(self.store.alive_pages(now), key=lambda p: p.created_at)]
        if not ids:
            return None
        if self._current in ids:
            target = ids[(ids.index(self._current) + step) % len(ids)]
        else:
            target = ids[0]
        self.pin(target)
        self._current = target
        return target

    def select(self) -> str | None:
        """The page that should be on the glass right now (None = idle)."""
        now = self.clock()
        pages = self.store.alive_pages(now)
        if not pages:
            self._pin = None
            self._current = None
            return None

        alerts = [p for p in pages if p.priority >= ALERT_PRIORITY]
        if alerts:
            self._pin = None
            candidates = self._top_group(alerts)
        else:
            if self._pin is not None:
                page_id, expires, _by = self._pin
                if now < expires and self.store.get(page_id) is not None:
                    self._current = page_id
                    return page_id
                self._pin = None
            candidates = self._top_group(pages)

        ids = [p.id for p in sorted(candidates, key=lambda p: p.created_at)]
        if self._current not in ids:
            self._current = ids[0]
            self._rot_since = now
        else:
            # The page on the glass sets its own dwell time, if it asked to.
            current = self.store.get(self._current)
            dwell = (current.duration if current and current.duration
                     else self.rotation_secs)
            if now - self._rot_since >= dwell:
                self._current = ids[(ids.index(self._current) + 1) % len(ids)]
                self._rot_since = now
        return self._current

    @staticmethod
    def _top_group(pages: list[Page]) -> list[Page]:
        top = max(p.priority for p in pages)
        return [p for p in pages if p.priority == top]
