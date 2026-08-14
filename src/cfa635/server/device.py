"""Single-owner bridge between asyncio and the blocking CFA635 driver.

Exactly one thread ever touches the serial port. It multiplexes two duties
in one loop: draining unsolicited key reports (forwarded to the event loop)
and executing queued commands (resolved via asyncio futures). The 20 ms
command-queue timeout doubles as the report poll tick.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from cfa635.driver import KEY_NAMES, Cfa635


@dataclass
class _Call:
    fn: str
    args: tuple
    kwargs: dict
    future: asyncio.Future


@dataclass
class DeviceWorker:
    port: str = "/dev/ttyUSB0"
    baudrate: int = 115200
    on_key: Callable[[str], None] | None = None  # called in the event loop with e.g. "UP_PRESS"
    ser: Any = None  # injected fake for tests

    version: str = ""
    healthy: bool = False
    last_error: str = ""

    _queue: queue.Queue = field(default_factory=queue.Queue)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Start the worker thread; raises if the device can't be opened."""
        self._loop = asyncio.get_running_loop()
        started: asyncio.Future = self._loop.create_future()
        self._thread = threading.Thread(
            target=self._run, args=(started,), name="cfa635-device", daemon=True
        )
        self._thread.start()
        await started

    async def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join, 5.0)

    async def call(self, fn: str, *args, **kwargs) -> Any:
        """Run a driver method on the worker thread, e.g.
        await worker.call("write_text", 0, 1, b"hello")."""
        assert self._loop is not None, "worker not started"
        future: asyncio.Future = self._loop.create_future()
        self._queue.put(_Call(fn, args, kwargs, future))
        return await future

    # --- worker thread ------------------------------------------------------

    def _resolve(self, future: asyncio.Future, result: Any, exc: BaseException | None) -> None:
        if future.cancelled():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _run(self, started: asyncio.Future) -> None:
        loop = self._loop
        assert loop is not None
        try:
            dev = Cfa635(self.port, self.baudrate, ser=self.ser)
            dev.ping()
            self.version = dev.version()
            dev.clear()
            dev.configure_key_reporting()
            self.healthy = True
        except Exception as exc:
            self.last_error = str(exc)
            loop.call_soon_threadsafe(self._resolve, started, None, exc)
            return
        loop.call_soon_threadsafe(self._resolve, started, None, None)

        while not self._stop.is_set():
            for pkt in dev.drain_reports_nonblocking():
                if pkt.command == 0x00 and pkt.data and self.on_key is not None:
                    name = KEY_NAMES.get(pkt.data[0], f"UNKNOWN_{pkt.data[0]}")
                    loop.call_soon_threadsafe(self.on_key, name)
            try:
                call = self._queue.get(timeout=0.02)
            except queue.Empty:
                continue
            try:
                result = getattr(dev, call.fn)(*call.args, **call.kwargs)
                self.healthy = True
            except Exception as exc:
                self.healthy = False
                self.last_error = str(exc)
                loop.call_soon_threadsafe(self._resolve, call.future, None, exc)
            else:
                loop.call_soon_threadsafe(self._resolve, call.future, result, None)

        # Leave the glass blank and the LEDs dark on clean shutdown.
        try:
            dev.clear()
            for led in range(4):
                dev.set_led(led, 0, 0)
        except Exception:
            pass
        dev.close()
