"""FastAPI app: pages + arbitration over one CFA635, events over WebSocket."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect

from cfa635.driver import ROWS
from cfa635.server.config import Config
from cfa635.server.device import DeviceWorker
from cfa635.server.events import EventBus
from cfa635.server.models import (
    ActivateBody,
    BacklightPut,
    ContrastPut,
    LedSpec,
    PageOut,
    PagePatch,
    PagePut,
)
from cfa635.server.pages import ALERT_PRIORITY, Arbiter, Page, PageStore
from cfa635.server.render import Renderer

log = logging.getLogger(__name__)

PAGE_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
VERSION = "0.1.0"


class ServerState:
    def __init__(self, config: Config, ser=None):
        self.config = config
        self.bus = EventBus()
        self.store = PageStore()
        self.arbiter = Arbiter(
            self.store,
            rotation_secs=config.rotation_secs,
            nav_hold_secs=config.nav_hold_secs,
        )
        self.device = DeviceWorker(port=config.port, on_key=self._on_key, ser=ser)
        self.renderer = Renderer(self.device)
        self.visible: str | None = None
        self.visible_reason = "idle"
        self.backlight = (config.backlight, config.backlight)
        self.contrast = config.contrast
        self.global_leds: dict[int, tuple[int, int]] = {n: (0, 0) for n in range(4)}
        self._applied_leds: dict[int, tuple[int, int]] = {}
        self._applied_backlight: tuple[int, int] | None = None
        self.last_activity = time.monotonic()
        self._refresh_lock = asyncio.Lock()
        self._tick_task: asyncio.Task | None = None
        self._ip_cache: tuple[float, str] = (0.0, "")

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.device.start()
        await self.device.call("set_contrast", self.contrast)
        self.renderer.start()
        self._tick_task = asyncio.create_task(self._tick_loop(), name="arbiter-tick")
        await self.refresh("idle")

    async def stop(self) -> None:
        if self._tick_task is not None:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        await self.renderer.stop()
        await self.device.stop()

    # --- keypad -------------------------------------------------------------

    def _on_key(self, name: str) -> None:
        key, _, action = name.rpartition("_")
        key, action = key.lower(), action.lower()
        self.last_activity = time.monotonic()
        consumed = False
        if action == "press" and key in self.config.nav_keys:
            self.arbiter.nav(-1 if key == "up" else 1)
            consumed = True
        self.bus.publish({
            "type": "key",
            "key": key,
            "action": action,
            "consumed": consumed,
            "active_page": self.visible,
        })
        asyncio.get_running_loop().create_task(
            self.refresh("nav" if consumed else None)
        )

    # --- arbitration + output ----------------------------------------------

    async def _tick_loop(self) -> None:
        while True:
            now = time.monotonic()
            for page in self.store.sweep(now):
                self.bus.publish({"type": "page_removed", "page": page.id,
                                  "reason": "expired"})
            try:
                await self.refresh(None)
            except Exception as exc:
                log.warning("refresh failed: %s", exc)
            await asyncio.sleep(0.25)

    async def refresh(self, reason: str | None) -> None:
        async with self._refresh_lock:
            selected = self.arbiter.select()
            page = self.store.get(selected) if selected else None

            if selected != self.visible:
                if reason is None:
                    if page is None:
                        reason = "idle"
                    elif page.priority >= ALERT_PRIORITY:
                        reason = "alert"
                    elif self.arbiter.pinned == selected:
                        reason = "nav"
                    else:
                        reason = "rotation"
                previous = self.visible
                self.visible = selected
                self.visible_reason = reason
                self.bus.publish({"type": "page_visible", "page": selected,
                                  "previous": previous, "reason": reason})

            self.renderer.submit(page.lines if page else self._idle_lines())
            await self._apply_leds(page.leds if page else None)
            await self._apply_backlight(idle=page is None)

    async def _apply_leds(self, overlay: dict[int, tuple[int, int]] | None) -> None:
        for n in range(4):
            target = self.global_leds[n]
            if overlay and n in overlay:
                target = overlay[n]
            if self._applied_leds.get(n) != target:
                await self.device.call("set_led", n, green=target[0], red=target[1])
                self._applied_leds[n] = target

    async def _apply_backlight(self, *, idle: bool) -> None:
        dim = idle and (time.monotonic() - self.last_activity > self.config.idle_dim_secs)
        target = (self.config.idle_backlight,) * 2 if dim else self.backlight
        if self._applied_backlight != target:
            # One-byte form only: firmware v1.6 rejects separate keypad control.
            await self.device.call("set_backlight", target[0])
            self._applied_backlight = target

    def _idle_lines(self) -> list[str]:
        now = time.monotonic()
        cached_at, ip = self._ip_cache
        if now - cached_at > 60:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(0.2)
                    s.connect(("8.8.8.8", 53))
                    ip = s.getsockname()[0]
            except OSError:
                ip = ""
            self._ip_cache = (now, ip)
        return [
            socket.gethostname()[:20],
            ip,
            datetime.now().strftime("%b %d  %H:%M:%S"),
            "cfa635d - no pages",
        ]

    # --- helpers ------------------------------------------------------------

    def page_out(self, page: Page) -> PageOut:
        exp = page.expires_at()
        return PageOut(
            id=page.id,
            name=page.name,
            lines=page.lines,
            priority=page.priority,
            ttl=page.ttl,
            expires_in=None if exp is None else max(0.0, exp - time.monotonic()),
            leds=None if page.leds is None else {
                n: LedSpec(green=g, red=r) for n, (g, r) in page.leds.items()
            },
            visible=page.id == self.visible,
        )


def _leds_from_spec(spec: dict[int, LedSpec] | None) -> dict[int, tuple[int, int]] | None:
    if spec is None:
        return None
    bad = [n for n in spec if not 0 <= n <= 3]
    if bad:
        raise HTTPException(422, f"led index out of range: {bad}")
    return {n: (led.green, led.red) for n, led in spec.items()}


def create_app(config: Config | None = None, ser=None) -> FastAPI:
    state = ServerState(config or Config(), ser=ser)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await state.start()
        yield
        await state.stop()

    app = FastAPI(title="cfa635d", version=VERSION, lifespan=lifespan)
    app.state.s = state

    def get_page_or_404(page_id: str) -> Page:
        page = state.store.get(page_id)
        if page is None:
            raise HTTPException(404, f"no page {page_id!r}")
        return page

    def check_page_id(page_id: str) -> None:
        if not PAGE_ID_RE.match(page_id):
            raise HTTPException(422, "page id must match [a-z0-9-]{1,32}")

    # --- meta ---------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "device": "ok" if state.device.healthy else "error",
            "error": state.device.last_error or None,
        }

    @app.get("/device")
    async def device_info():
        return {
            "version": state.device.version,
            "port": state.config.port,
            "contrast": state.contrast,
            "backlight": {"lcd": state.backlight[0], "keypad": state.backlight[1]},
        }

    # --- pages --------------------------------------------------------------

    @app.get("/pages")
    async def list_pages():
        now = time.monotonic()
        return {
            "pages": [state.page_out(p) for p in
                      sorted(state.store.alive_pages(now), key=lambda p: p.created_at)],
            "active": state.visible,
            "pinned": state.arbiter.pinned,
        }

    @app.put("/pages/{page_id}", response_model=PageOut)
    async def put_page(page_id: str, body: PagePut, response: Response):
        check_page_id(page_id)
        now = time.monotonic()
        existing = state.store.get(page_id)
        page = Page(
            id=page_id,
            name=body.name or page_id,
            lines=body.lines,
            priority=body.priority,
            ttl=body.ttl,
            leds=_leds_from_spec(body.leds),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        state.store.put(page)
        response.status_code = 200 if existing else 201
        await state.refresh(None)
        return state.page_out(page)

    @app.patch("/pages/{page_id}", response_model=PageOut)
    async def patch_page(page_id: str, body: PagePatch):
        page = get_page_or_404(page_id)
        if isinstance(body.lines, list):
            page.lines = body.lines
        elif isinstance(body.lines, dict):
            lines = (page.lines + [""] * ROWS)[:ROWS]
            for row, text in body.lines.items():
                if not 0 <= row < ROWS:
                    raise HTTPException(422, f"row {row} out of range")
                lines[row] = text
            page.lines = lines
        if body.name is not None:
            page.name = body.name
        if body.priority is not None:
            page.priority = body.priority
        if body.ttl is not None:
            page.ttl = body.ttl
        if body.leds is not None:
            page.leds = _leds_from_spec(body.leds)
        page.updated_at = time.monotonic()
        await state.refresh(None)
        return state.page_out(page)

    @app.get("/pages/{page_id}", response_model=PageOut)
    async def get_page(page_id: str):
        return state.page_out(get_page_or_404(page_id))

    @app.delete("/pages/{page_id}", status_code=204)
    async def delete_page(page_id: str):
        get_page_or_404(page_id)
        state.store.delete(page_id)
        state.bus.publish({"type": "page_removed", "page": page_id, "reason": "deleted"})
        await state.refresh(None)

    @app.post("/pages/{page_id}/activate")
    async def activate_page(page_id: str, body: ActivateBody | None = None):
        get_page_or_404(page_id)
        state.arbiter.pin(page_id, hold=body.hold if body else None)
        await state.refresh("activate")
        return {"active": state.visible, "pinned": state.arbiter.pinned}

    # --- display ------------------------------------------------------------

    @app.post("/display/release")
    async def release():
        state.arbiter.release()
        await state.refresh(None)
        return {"active": state.visible, "pinned": None}

    @app.get("/display")
    async def display():
        return {
            "active": state.visible,
            "pinned": state.arbiter.pinned,
            "reason": state.visible_reason,
            "rotation_secs": state.config.rotation_secs,
            "frame": [row.decode("latin-1") for row in state.renderer.frame],
        }

    @app.put("/display/backlight")
    async def set_backlight(body: BacklightPut):
        if body.keypad is not None and body.keypad != body.lcd:
            raise HTTPException(
                422, "firmware v1.6 sets both backlights together; omit 'keypad'")
        state.backlight = (body.lcd, body.lcd)
        state._applied_backlight = None
        await state.refresh(None)
        return {"lcd": state.backlight[0], "keypad": state.backlight[1]}

    @app.put("/display/contrast")
    async def set_contrast(body: ContrastPut):
        await state.device.call("set_contrast", body.value)
        state.contrast = body.value
        return {"value": body.value}

    # --- LEDs ---------------------------------------------------------------

    @app.get("/leds")
    async def get_leds():
        return {str(n): {"green": g, "red": r}
                for n, (g, r) in state.global_leds.items()}

    @app.put("/leds")
    async def put_leds(body: dict[int, LedSpec]):
        leds = _leds_from_spec(body) or {}
        state.global_leds.update(leds)
        await state.refresh(None)
        return await get_leds()

    @app.put("/leds/{n}")
    async def put_led(n: int, body: LedSpec):
        if not 0 <= n <= 3:
            raise HTTPException(422, "led index must be 0-3")
        state.global_leds[n] = (body.green, body.red)
        await state.refresh(None)
        return {"green": body.green, "red": body.red}

    # --- events -------------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = state.bus.subscribe()
        try:
            await websocket.send_json({
                "type": "hello",
                "server": f"cfa635d {VERSION}",
                "active_page": state.visible,
                "ts": time.time(),
            })
            while True:
                await websocket.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            state.bus.unsubscribe(q)

    return app
