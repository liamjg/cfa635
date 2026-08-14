"""Shadow-buffer renderer: only rows that changed are rewritten.

A single task consumes a latest-frame-wins slot, so bursts of updates
coalesce instead of queueing (a client PUTting at 50 Hz gets back-to-back
renders at whatever rate the device sustains, never a backlog).
"""

from __future__ import annotations

import asyncio
import logging

from cfa635 import charmap
from cfa635.driver import COLUMNS, ROWS
from cfa635.server.device import DeviceWorker

log = logging.getLogger(__name__)


class Renderer:
    def __init__(self, device: DeviceWorker):
        self._device = device
        self._shadow = [b" " * COLUMNS for _ in range(ROWS)]
        self._want: list[bytes] | None = None
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None

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
        padded = (list(lines) + [""] * ROWS)[:ROWS]
        self._want = [charmap.encode_line(line) for line in padded]
        self._dirty.set()

    @property
    def frame(self) -> list[bytes]:
        """What is currently on the glass (per the shadow buffer)."""
        return list(self._shadow)

    async def _run(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            frame = self._want
            if frame is None:
                continue
            try:
                for row in range(ROWS):
                    if frame[row] != self._shadow[row]:
                        await self._device.call("write_text", 0, row, frame[row])
                        self._shadow[row] = frame[row]
            except Exception as exc:
                # Transient serial trouble: keep the target frame, retry shortly.
                log.warning("render failed, retrying: %s", exc)
                await asyncio.sleep(0.5)
                self._dirty.set()
