"""Shadow-buffer renderer with markup, glyph allocation, and span diffs.

A single task consumes a latest-frame-wins slot, so bursts of updates
coalesce instead of queueing. Each render:

  1. parses the submitted lines' markup into cells (pure, time-derived
     animation state — the tick loop resubmits ~4x/s so spinners advance),
  2. allocates CGRAM slots for the frame's distinct glyph bitmaps through
     a bitmap-keyed LRU cache (over the 8-slot budget, cells degrade to
     their ASCII fallbacks — never an error),
  3. programs changed glyphs *before* the text that references them,
     preferring slots the on-glass frame doesn't currently use
     (redefining a visible slot repaints its cells instantly), and
  4. writes only the changed span of each row (command 31 takes 1-20
     chars at any column, so a spinner tick costs ~9 bytes, not 26).
"""

from __future__ import annotations

import asyncio
import logging

from cfa635 import markup
from cfa635.driver import COLUMNS, ROWS
from cfa635.markup import MAX_GLYPHS, Cell
from cfa635.server.device import DeviceWorker

log = logging.getLogger(__name__)


class Renderer:
    def __init__(self, device: DeviceWorker):
        self._device = device
        self._shadow = [b" " * COLUMNS for _ in range(ROWS)]
        self._want: list[str] | None = None
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._slots: dict[tuple, int] = {}      # bitmap -> CGRAM slot
        self._slot_order: list[tuple] = []      # LRU, oldest first
        self._programmed: list[tuple | None] = [None] * MAX_GLYPHS

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="renderer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def submit(self, lines: list[str]) -> None:
        """Request that these (up to 4) lines be on the glass."""
        self._want = list(lines)
        self._dirty.set()

    @property
    def frame(self) -> list[bytes]:
        """What is currently on the glass (per the shadow buffer)."""
        return list(self._shadow)

    @property
    def glyphs(self) -> list[tuple[int, ...] | None]:
        """CGRAM contents as programmed (slot -> 8-row bitmap or None)."""
        return list(self._programmed)

    # --- glyph slot allocation ------------------------------------------------

    def _allocate(self, cells: list[list[Cell]]) -> dict[tuple, int]:
        """Map this frame's distinct bitmaps to slots; unmapped -> fallback."""
        used: list[tuple] = []
        for row in cells:
            for cell in row:
                if cell.glyph is not None and cell.glyph not in used:
                    used.append(cell.glyph)

        mapping = {bm: self._slots[bm] for bm in used if bm in self._slots}
        wanted = [bm for bm in used[:MAX_GLYPHS] if bm not in mapping]
        if wanted:
            taken = set(mapping.values())
            on_glass = {b for row in self._shadow for b in row if b < MAX_GLYPHS}
            candidates = [s for s in range(MAX_GLYPHS) if s not in taken]
            # program into invisible slots first to avoid transition artifacts,
            # breaking ties by evicting the least-recently-used bitmap
            def eviction_rank(slot: int) -> tuple:
                bitmap = self._programmed[slot]
                age = (self._slot_order.index(bitmap)
                       if bitmap in self._slot_order else -1)
                return (slot in on_glass, age)
            candidates.sort(key=eviction_rank)
            for bitmap in wanted:
                if not candidates:
                    break
                slot = candidates.pop(0)
                evicted = next((b for b, s in self._slots.items() if s == slot),
                               None)
                if evicted is not None:
                    del self._slots[evicted]
                    self._slot_order.remove(evicted)
                self._slots[bitmap] = slot
                mapping[bitmap] = slot

        for bitmap in used:  # LRU touch
            if bitmap in self._slot_order:
                self._slot_order.remove(bitmap)
            if bitmap in self._slots:
                self._slot_order.append(bitmap)
        return mapping

    @staticmethod
    def _encode(cells: list[list[Cell]], slots: dict[tuple, int]) -> list[bytes]:
        rows = []
        for row in cells:
            out = bytearray()
            for cell in row:
                if cell.glyph is not None and cell.glyph in slots:
                    out.append(slots[cell.glyph])
                elif cell.glyph is not None:
                    out.append(cell.fallback)
                else:
                    out.append(cell.char if cell.char is not None else 0x20)
            rows.append(bytes(out))
        return rows

    # --- render loop ----------------------------------------------------------

    async def _run(self) -> None:
        import time

        while True:
            await self._dirty.wait()
            self._dirty.clear()
            lines = self._want
            if lines is None:
                continue
            try:
                cells = markup.parse_frame(lines, time.monotonic())
                slots = self._allocate(cells)
                # glyphs first: a cell must never reference a stale bitmap
                for bitmap, slot in slots.items():
                    if self._programmed[slot] != bitmap:
                        await self._device.call("set_special_char", slot,
                                                bytes(bitmap))
                        self._programmed[slot] = bitmap
                frame = self._encode(cells, slots)
                for row in range(ROWS):
                    if frame[row] == self._shadow[row]:
                        continue
                    # only the changed span goes over the wire
                    old, new = self._shadow[row], frame[row]
                    first = next(i for i in range(COLUMNS) if old[i] != new[i])
                    last = next(i for i in range(COLUMNS - 1, -1, -1)
                                if old[i] != new[i])
                    await self._device.call("write_text", first, row,
                                            new[first:last + 1])
                    self._shadow[row] = new
            except Exception as exc:
                # Transient serial trouble: keep the target frame, retry shortly.
                log.warning("render failed, retrying: %s", exc)
                await asyncio.sleep(0.5)
                self._dirty.set()
