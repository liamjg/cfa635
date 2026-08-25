"""FastAPI app: pages + arbitration over one CFA635, events over WebSocket."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse, RedirectResponse

from cfa635.driver import ROWS
from cfa635.sim import KEY_CODES, FakeSerial
from cfa635.server import builtin
from cfa635.server.clients import Client, ClientRegistry
from cfa635.server.config import Config
from cfa635.server.device import DeviceWorker
from cfa635.server.events import EventBus
from cfa635.server.models import (
    ActivateBody,
    ClientCreate,
    ClientOut,
    ClientRegistered,
    CursorSpec,
    LedSpec,
    PageOut,
    PagePatch,
    PagePut,
)

CURSOR_STYLES = {"none": 0, "block": 1, "underscore": 2,
                 "block_underscore": 3, "invert": 4}

# LED state is carried as (green, red, mode, hz); duty is computed per tick.
LedState = tuple[int, int, str, float]
LED_OFF: LedState = (0, 0, "solid", 1.0)
from cfa635.server.pages import ALERT_PRIORITY, Arbiter, Page, PageStore
from cfa635.server.render import Renderer
from cfa635.server.shell import (
    Shell,
    decode_settings,
    encode_settings,
    settings_lines,
    switcher_lines,
)

log = logging.getLogger(__name__)

PAGE_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
VERSION = "0.1.0"
API_VERSION = 1


class ServerState:
    def __init__(self, config: Config, ser=None):
        self.config = config
        self.bus = EventBus()
        self.store = PageStore()
        self.clients = ClientRegistry()
        self.arbiter = Arbiter(
            self.store,
            rotation_secs=config.rotation_secs,
            nav_hold_secs=config.nav_hold_secs,
        )
        self.device = DeviceWorker(port=config.port, on_key=self._on_key, ser=ser)
        self.renderer = Renderer(self.device)
        self.visible: str | None = None
        self.visible_reason = "idle"
        # Focus: (page_id, owner_client_id, generation). While held, all six
        # keys route owner-only; the generation lets clients discard stale
        # key events from a focus they no longer hold.
        self.focus: tuple[str, str, int] | None = None
        self.focus_generation = 0
        self.focus_last_key = 0.0
        self.shell = Shell(timeout=config.nav_hold_secs)
        self.backlight = (config.backlight, config.backlight)
        self.contrast = config.contrast
        self.global_leds: dict[int, LedState] = {n: LED_OFF for n in range(4)}
        self.led_owner: dict[int, str] = {}  # claimed ambient LEDs (1-3)
        self._applied_leds: dict[int, tuple[int, int]] = {}
        self._applied_backlight: tuple[int, int] | None = None
        self._applied_contrast: int | None = None
        self._applied_cursor: tuple[int, int, str] | None = None
        self.last_activity = time.monotonic()
        self.started_at = self.last_activity
        self._refresh_lock = asyncio.Lock()
        self._tick_task: asyncio.Task | None = None
        self._ip_cache: tuple[float, str] = (0.0, "")

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await self.device.start()
        # Display settings live on the device: a valid user-flash record
        # (written by the shell's Settings screen) beats env defaults.
        try:
            stored = decode_settings(await self.device.call("read_user_flash"))
        except Exception as exc:
            log.warning("user flash read failed: %s", exc)
            stored = None
        if stored is not None:
            backlight, contrast = stored
            self.backlight = (backlight, backlight)
            self.contrast = contrast
        for page in builtin.pages(self.config):
            self.store.put(page)
        builtin.refresh(self)
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

    # --- keypad: layered routing (focus > browse/global) ----------------------

    def _on_key(self, name: str) -> None:
        key, _, action = name.rpartition("_")
        key, action = key.lower(), action.lower()
        now = time.monotonic()
        self.last_activity = now

        # Layer: shell — while open it consumes everything.
        if self.shell.is_open:
            if action == "press":
                self._shell_key(key, now)
            self.bus.publish({
                "type": "key",
                "key": key,
                "action": action,
                "consumed": True,
                "layer": "shell",
                "active_page": self.visible,
            })
            asyncio.get_running_loop().create_task(self.refresh(None))
            return

        # Layer: focus — all six keys route owner-only, refreshing the hold.
        if self.focus is not None:
            page_id, owner, generation = self.focus
            self.focus_last_key = now
            self.arbiter.pin(page_id, hold=self.config.focus_hold_secs, by=owner)
            self.bus.publish({
                "type": "key",
                "key": key,
                "action": action,
                "routed": True,
                "page": page_id,
                "generation": generation,
            }, to=owner)
            asyncio.get_running_loop().create_task(self.refresh(None))
            return

        # Layer: browse — EXIT opens the shell; ENTER focuses the visible
        # page if it opted in and its owner is listening.
        consumed = False
        reason: str | None = None
        if action == "press" and key == "exit":
            self.shell.open()
            self.bus.publish({"type": "shell", "open": True})
            consumed = True
        if (not consumed and action == "press" and key == "enter"
                and self.visible is not None):
            page = self.store.get(self.visible)
            if (page is not None and page.interactive
                    and self.bus.has_client(page.owner)):
                self._acquire_focus(page)
                consumed = True

        # Layer: global — nav keys rotate & pin.
        if not consumed and action == "press" and key in self.config.nav_keys:
            self.arbiter.nav(-1 if key == "up" else 1)
            consumed = True
            reason = "nav"

        self.bus.publish({
            "type": "key",
            "key": key,
            "action": action,
            "consumed": consumed,
            "active_page": self.visible,
        })
        asyncio.get_running_loop().create_task(self.refresh(reason))

    # --- shell ----------------------------------------------------------------

    def _shell_pages(self) -> list[Page]:
        return sorted(self.store.alive_pages(time.monotonic()),
                      key=lambda p: p.created_at)

    def _shell_key(self, key: str, now: float) -> None:
        pages = self._shell_pages()
        effect = self.shell.on_key(key, len(pages) + 1)  # +1: Settings row
        if effect is None:
            return
        if effect[0] == "select":
            index = effect[1]
            if index >= len(pages):
                self.shell.enter_settings()
            else:
                self.arbiter.pin(pages[index].id, by="keypad")
                asyncio.get_running_loop().create_task(self.close_shell())
        elif effect[0] == "close":
            asyncio.get_running_loop().create_task(self.close_shell())
        elif effect[0] == "adjust":
            _, field, direction = effect
            self.shell.dirty = True
            if field == "backlight":
                value = min(100, max(0, self.backlight[0] + 10 * direction))
                self.backlight = (value, value)
            else:
                # UI-clamped to the useful window (datasheet: ~90-140 usable,
                # factory 120); full 0-255 range remains available via env.
                value = min(160, max(60, self.contrast + 5 * direction))
                self.contrast = value

    async def close_shell(self) -> None:
        if not self.shell.is_open:
            return
        dirty = self.shell.dirty
        self.shell.close()
        self.bus.publish({"type": "shell", "open": False})
        if dirty:
            try:
                await self.device.call(
                    "write_user_flash",
                    encode_settings(self.backlight[0], self.contrast))
            except Exception as exc:
                log.warning("user flash write failed: %s", exc)
        await self.refresh(None)

    def _shell_lines(self) -> list[str]:
        if self.shell.mode == "settings":
            return settings_lines(self.backlight[0], self.contrast,
                                  self.shell.settings_row)
        pages = self._shell_pages()
        entries = []
        for page in pages:
            badge = ""
            if page.priority >= ALERT_PRIORITY:
                badge = "!"
            elif self.arbiter.pinned == page.id:
                badge = "P"
            elif page.id == self.visible:
                badge = "*"
            # client-supplied names are content, not markup
            entries.append((page.name.replace("{", "{{"), badge))
        entries.append(("Settings", ""))
        return switcher_lines(entries, self.shell.cursor)

    # --- focus ----------------------------------------------------------------

    def _acquire_focus(self, page: Page) -> None:
        self.focus_generation += 1
        self.focus = (page.id, page.owner, self.focus_generation)
        self.focus_last_key = time.monotonic()
        self.arbiter.pin(page.id, hold=self.config.focus_hold_secs,
                         by=page.owner)
        self.bus.publish({
            "type": "focus",
            "page": page.id,
            "state": "gained",
            "reason": "enter",
            "generation": self.focus_generation,
        }, to=page.owner)

    def release_focus(self, reason: str) -> None:
        """End focus (idempotent). The owner learns why via a focus event."""
        if self.focus is None:
            return
        page_id, owner, generation = self.focus
        self.focus = None
        if self.arbiter.pinned == page_id:
            self.arbiter.release()
        self.bus.publish({
            "type": "focus",
            "page": page_id,
            "state": "lost",
            "reason": reason,
            "generation": generation,
        }, to=owner)

    def _check_focus(self, now: float) -> None:
        """Focus liveness, called from the tick loop."""
        if self.focus is None:
            return
        page_id, owner, _ = self.focus
        if self.store.get(page_id) is None:
            self.release_focus("preempted")
        elif not self.bus.has_client(owner):
            self.release_focus("disconnect")
        elif now - self.focus_last_key > self.config.focus_hold_secs:
            self.release_focus("timeout")

    # --- arbitration + output ----------------------------------------------

    async def _tick_loop(self) -> None:
        while True:
            now = time.monotonic()
            for page in self.store.sweep(now):
                self.bus.publish({"type": "page_removed", "page": page.id,
                                  "reason": "expired"})
            builtin.refresh(self)  # the clock ticks here
            self._check_focus(now)
            if self.shell.expired():
                await self.close_shell()
            try:
                await self.refresh(None)
            except Exception as exc:
                log.warning("refresh failed: %s", exc)
            # An alert (or anything else) stealing the glass ends focus.
            if self.focus is not None and self.visible != self.focus[0]:
                self.release_focus("preempted")
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

            if self.shell.is_open:
                # The shell is a modal overlay: it owns the glass, and page
                # LED overlays are suspended while it is up.
                self.renderer.submit(self._shell_lines())
                await self._apply_leds(None)
            else:
                self.renderer.submit(page.lines if page else self._idle_lines())
                await self._apply_leds(page.leds if page else None)
            # Built-in pages are content, but they are not *activity*: the
            # clock must not hold the backlight on forever.
            idle = (page is None or page.builtin) and not self.shell.is_open
            await self._apply_backlight(idle=idle)
            await self._apply_contrast()
            await self._apply_cursor(None if self.shell.is_open else page)

    @staticmethod
    def _led_duty(spec: LedState, now: float) -> tuple[int, int]:
        """Instantaneous (green, red) duty for a possibly animated LED."""
        green, red, mode, hz = spec
        if mode == "solid":
            return (green, red)
        phase = (now * hz) % 1.0
        if mode == "blink":
            return (green, red) if phase < 0.5 else (0, 0)
        # pulse: triangle wave
        level = phase * 2 if phase < 0.5 else (1 - phase) * 2
        return (round(green * level), round(red * level))

    def _system_led(self, now: float) -> LedState | None:
        """LED 0 speaks for the system when something needs saying:
        solid green = keys are captured (focus); blinking red = an alert
        exists but is not on the glass."""
        if self.focus is not None:
            return (100, 0, "solid", 1.0)
        pages = self.store.alive_pages(now)
        if any(p.priority >= ALERT_PRIORITY for p in pages):
            visible = self.store.get(self.visible) if self.visible else None
            showing_alert = (not self.shell.is_open and visible is not None
                             and visible.priority >= ALERT_PRIORITY)
            if not showing_alert:
                return (0, 100, "blink", 1.0)
        return None

    async def _apply_leds(self, overlay: dict[int, LedState] | None) -> None:
        now = time.monotonic()
        for n in range(4):
            spec = self.global_leds[n]
            if overlay and n in overlay:
                spec = overlay[n]
            if n == 0:
                spec = self._system_led(now) or spec
            duty = self._led_duty(spec, now)
            if self._applied_leds.get(n) != duty:
                await self.device.call("set_led", n, green=duty[0], red=duty[1])
                self._applied_leds[n] = duty

    async def _apply_backlight(self, *, idle: bool) -> None:
        dim = idle and (time.monotonic() - self.last_activity > self.config.idle_dim_secs)
        target = (self.config.idle_backlight,) * 2 if dim else self.backlight
        if self._applied_backlight != target:
            # One-byte form only: firmware v1.6 rejects separate keypad control.
            await self.device.call("set_backlight", target[0])
            self._applied_backlight = target

    async def _apply_contrast(self) -> None:
        if self._applied_contrast != self.contrast:
            await self.device.call("set_contrast", self.contrast)
            self._applied_contrast = self.contrast

    async def _apply_cursor(self, page: Page | None) -> None:
        target = page.cursor if page is not None else None
        if target is not None and target[2] == "none":
            target = None
        if self._applied_cursor == target:
            return
        if target is None:
            await self.device.call("set_cursor_style", 0)
        else:
            row, col, style = target
            await self.device.call("set_cursor_position", col, row)
            await self.device.call("set_cursor_style", CURSOR_STYLES[style])
        self._applied_cursor = target

    def _idle_lines(self) -> list[str]:
        """Only reached with every built-in page disabled (see builtin.py)."""
        return [
            socket.gethostname()[:20],
            builtin.host_ip(self),
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
            duration=page.duration,
            cursor=None if page.cursor is None else CursorSpec(
                row=page.cursor[0], col=page.cursor[1], style=page.cursor[2]),
            leds=None if page.leds is None else {
                n: LedSpec(green=g, red=r, mode=m, hz=hz)
                for n, (g, r, m, hz) in page.leds.items()
            },
            owner=page.owner,
            interactive=page.interactive,
            visible=page.id == self.visible,
            focused=self.focus is not None and self.focus[0] == page.id,
        )


def _leds_from_spec(spec: dict[int, LedSpec] | None) -> dict[int, LedState] | None:
    if spec is None:
        return None
    bad = [n for n in spec if not 0 <= n <= 3]
    if bad:
        raise HTTPException(422, f"led index out of range: {bad}")
    return {n: (led.green, led.red, led.mode, led.hz) for n, led in spec.items()}


def create_app(config: Config | None = None, ser=None) -> FastAPI:
    config = config or Config()
    if ser is None and config.sim:
        ser = FakeSerial()
    state = ServerState(config, ser=ser)

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

    def require_client(authorization: str | None = Header(default=None)) -> Client:
        """Bearer-token auth for every mutation. Reads stay open; the token
        is arbitration identity on a trusted LAN, not a security boundary."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                401, "register with POST /clients and send its bearer token",
                headers={"WWW-Authenticate": "Bearer"})
        client = state.clients.by_token(authorization[7:].strip())
        if client is None:
            raise HTTPException(
                401, "unknown token (server restarted? re-register)",
                headers={"WWW-Authenticate": "Bearer"})
        client.last_seen = time.monotonic()
        return client

    def get_owned_page_or_403(page_id: str, client: Client) -> Page:
        page = get_page_or_404(page_id)
        if page.owner != client.id:
            raise HTTPException(
                403, f"page {page_id!r} belongs to another client")
        return page

    # --- meta ---------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "api": API_VERSION,
            "device": "ok" if state.device.healthy else "error",
            "error": state.device.last_error or None,
        }

    # USB serial (e.g. "CF124841"): a stable identity that survives IP and
    # port changes — the natural unique_id for integrations, and the
    # extension point for multi-display support (a future `display` field
    # on pages would select among several serials).
    try:
        from cfa635.probe import usb_identity
        usb_serial = (None if config.sim
                      else usb_identity(config.port).get("serial"))
    except Exception:
        usb_serial = None

    @app.get("/device")
    async def device_info():
        return {
            "version": state.device.version,
            "port": state.config.port,
            "serial": usb_serial,
            "sim": state.config.sim,
            "contrast": state.contrast,
            "backlight": {"lcd": state.backlight[0], "keypad": state.backlight[1]},
        }

    # --- clients ------------------------------------------------------------

    @app.post("/clients", response_model=ClientRegistered, status_code=201)
    async def register_client(body: ClientCreate):
        client = state.clients.register(body.name, time.monotonic())
        return ClientRegistered(id=client.id, token=client.token, name=client.name)

    @app.get("/clients")
    async def list_clients():
        now = time.monotonic()
        return [
            ClientOut(
                id=c.id,
                name=c.name,
                last_seen_ago=max(0.0, now - c.last_seen),
                pages=sorted(p.id for p in state.store.pages_for(c.id)),
            )
            for c in state.clients.all()
        ]

    @app.delete("/clients/{client_id}", status_code=204)
    async def delete_client(client_id: str,
                            client: Client = Depends(require_client)):
        if state.clients.get(client_id) is None:
            raise HTTPException(404, f"no client {client_id!r}")
        if client.id != client_id:
            raise HTTPException(403, "clients can only delete themselves")
        for page in state.store.pages_for(client_id):
            state.store.delete(page.id)
            state.bus.publish({"type": "page_removed", "page": page.id,
                               "reason": "client_deleted"})
            if state.arbiter.pinned == page.id:
                state.arbiter.release()
        if state.focus is not None and state.focus[1] == client_id:
            state.release_focus("preempted")
        for n, owner in list(state.led_owner.items()):
            if owner == client_id:
                del state.led_owner[n]
                state.global_leds[n] = LED_OFF
        state.clients.delete(client_id)
        await state.refresh(None)

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
    async def put_page(page_id: str, body: PagePut, response: Response,
                       client: Client = Depends(require_client)):
        check_page_id(page_id)
        now = time.monotonic()
        existing = state.store.get(page_id)
        if existing is not None and existing.owner != client.id:
            raise HTTPException(
                403, f"page {page_id!r} belongs to another client")
        page = Page(
            id=page_id,
            name=body.name or page_id,
            lines=body.lines,
            owner=client.id,
            interactive=body.interactive,
            priority=body.priority,
            ttl=body.ttl,
            duration=body.duration,
            cursor=(None if body.cursor is None else
                    (body.cursor.row, body.cursor.col, body.cursor.style)),
            leds=_leds_from_spec(body.leds),
            # Same-owner replace keeps its creation time (and so its slot in
            # the rotation order); a fresh id starts a new history.
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        state.store.put(page)
        if (state.focus is not None and state.focus[0] == page_id
                and not page.interactive):
            state.release_focus("preempted")
        response.status_code = 200 if existing else 201
        await state.refresh(None)
        return state.page_out(page)

    @app.patch("/pages/{page_id}", response_model=PageOut)
    async def patch_page(page_id: str, body: PagePatch,
                         client: Client = Depends(require_client)):
        page = get_owned_page_or_403(page_id, client)
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
        if body.duration is not None:
            page.duration = body.duration
        if body.leds is not None:
            page.leds = _leds_from_spec(body.leds)
        if body.cursor is not None:
            page.cursor = (body.cursor.row, body.cursor.col, body.cursor.style)
        if body.interactive is not None:
            page.interactive = body.interactive
            if (not page.interactive and state.focus is not None
                    and state.focus[0] == page_id):
                state.release_focus("preempted")
        page.updated_at = time.monotonic()
        await state.refresh(None)
        return state.page_out(page)

    @app.get("/pages/{page_id}", response_model=PageOut)
    async def get_page(page_id: str):
        return state.page_out(get_page_or_404(page_id))

    @app.delete("/pages/{page_id}", status_code=204)
    async def delete_page(page_id: str,
                          client: Client = Depends(require_client)):
        get_owned_page_or_403(page_id, client)
        state.store.delete(page_id)
        if state.focus is not None and state.focus[0] == page_id:
            state.release_focus("preempted")
        state.bus.publish({"type": "page_removed", "page": page_id, "reason": "deleted"})
        await state.refresh(None)

    @app.post("/pages/{page_id}/focus/release")
    async def focus_release(page_id: str,
                            client: Client = Depends(require_client)):
        get_owned_page_or_403(page_id, client)
        if state.focus is not None and state.focus[0] == page_id:
            state.release_focus("released")
            await state.refresh(None)
        return {"focused": state.focus[0] if state.focus else None}

    @app.post("/pages/{page_id}/activate")
    async def activate_page(page_id: str, body: ActivateBody | None = None,
                            client: Client = Depends(require_client)):
        get_owned_page_or_403(page_id, client)
        state.arbiter.pin(page_id, hold=body.hold if body else None,
                          by=client.id)
        await state.refresh("activate")
        return {"active": state.visible, "pinned": state.arbiter.pinned}

    # --- display ------------------------------------------------------------

    @app.post("/display/release")
    async def release(client: Client = Depends(require_client)):
        pinned = state.arbiter.pinned
        if pinned is not None:
            page = state.store.get(pinned)
            # You may release your own pin, or any pin of your own page —
            # but not a pin someone else (or the person at the keypad) holds
            # on somebody else's page. Pins self-expire regardless.
            owns = (state.arbiter.pinned_by == client.id
                    or (page is not None and page.owner == client.id))
            if not owns:
                raise HTTPException(403, "pin is held by another client")
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

    # --- LEDs ---------------------------------------------------------------
    # Backlight and contrast are deliberately NOT API surface: they are
    # device-local settings, adjusted at the shell's Settings screen.

    def claim_led(n: int, client: Client) -> None:
        """LED 0 is the system indicator; LEDs 1-3 are claim-on-first-write."""
        if n == 0:
            raise HTTPException(403, "led 0 is the system indicator")
        owner = state.led_owner.get(n)
        if owner is None:
            state.led_owner[n] = client.id
        elif owner != client.id:
            raise HTTPException(403, f"led {n} is claimed by another client")

    @app.get("/leds")
    async def get_leds():
        return {str(n): {"green": g, "red": r, "mode": m, "hz": hz,
                         "owner": state.led_owner.get(n)}
                for n, (g, r, m, hz) in state.global_leds.items()}

    @app.put("/leds")
    async def put_leds(body: dict[int, LedSpec],
                       client: Client = Depends(require_client)):
        leds = _leds_from_spec(body) or {}
        for n in leds:
            claim_led(n, client)
        state.global_leds.update(leds)
        await state.refresh(None)
        return await get_leds()

    @app.put("/leds/{n}")
    async def put_led(n: int, body: LedSpec,
                      client: Client = Depends(require_client)):
        if not 0 <= n <= 3:
            raise HTTPException(422, "led index must be 0-3")
        claim_led(n, client)
        state.global_leds[n] = (body.green, body.red, body.mode, body.hz)
        await state.refresh(None)
        return {"green": body.green, "red": body.red,
                "mode": body.mode, "hz": body.hz}

    # --- events -------------------------------------------------------------

    @app.websocket("/ws")
    async def ws(websocket: WebSocket, token: str | None = None):
        client: Client | None = None
        await websocket.accept()
        if token is not None:
            client = state.clients.by_token(token)
            if client is None:
                # 4401: our "unauthorized" in the WS close-code private range
                await websocket.close(code=4401, reason="unknown token")
                return
            client.last_seen = time.monotonic()
        q = state.bus.subscribe(client.id if client else None)
        try:
            now = time.monotonic()
            await websocket.send_json({
                "type": "hello",
                "server": f"cfa635d {VERSION}",
                "api": API_VERSION,
                "seq": state.bus.seq,
                "client": client.id if client else None,
                "pages": [state.page_out(p).model_dump() for p in
                          sorted(state.store.alive_pages(now),
                                 key=lambda p: p.created_at)],
                "active": state.visible,
                "pinned": state.arbiter.pinned,
                "focus": (None if state.focus is None else
                          {"page": state.focus[0],
                           "generation": state.focus[2]}),
                "reason": state.visible_reason,
                "leds": {str(n): {"green": g, "red": r, "mode": m, "hz": hz}
                         for n, (g, r, m, hz) in state.global_leds.items()},
                "backlight": state.backlight[0],
                "contrast": state.contrast,
                "device": {"version": state.device.version,
                           "healthy": state.device.healthy},
                "ts": time.time(),
            })
            while True:
                await websocket.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            state.bus.unsubscribe(q)

    # --- panel: a live mirror of the physical interface -----------------------
    #
    # Works against real hardware and the simulated device alike: the state
    # comes from the server's applied caches (the shadow buffer IS what was
    # written to the glass), and key posts feed the same _on_key pipeline the
    # physical keypad drives. Reachability on the LAN is treated as presence
    # at the device — the web keypad can do exactly what the physical one can,
    # nothing more.

    from importlib import resources

    panel_page = (resources.files("cfa635.server") / "panel.html").read_text()

    @app.get("/panel", response_class=HTMLResponse)
    async def panel_view():
        return panel_page

    @app.get("/sim", include_in_schema=False)
    async def sim_alias():
        return RedirectResponse("/panel")

    @app.get("/panel/state")
    async def panel_state():
        backlight = state._applied_backlight or state.backlight
        cursor = state._applied_cursor
        return {
            "rows": [list(row) for row in state.renderer.frame],
            "cgram": [list(g) if g is not None else [0] * 8
                      for g in state.renderer.glyphs],
            "cursor": ({"col": cursor[1], "row": cursor[0],
                        "style": CURSOR_STYLES[cursor[2]]}
                       if cursor is not None
                       else {"col": 0, "row": 0, "style": 0}),
            "leds": {str(n): {"green": state._applied_leds.get(n, (0, 0))[0],
                              "red": state._applied_leds.get(n, (0, 0))[1]}
                     for n in range(4)},
            "backlight": backlight[0],
            "contrast": (state._applied_contrast
                         if state._applied_contrast is not None
                         else state.contrast),
            "sim": state.config.sim,
        }

    @app.post("/panel/key")
    async def panel_key(body: dict):
        key = str(body.get("key", "")).lower()
        action = str(body.get("action", "tap")).lower()
        if key not in KEY_CODES:
            raise HTTPException(422, f"unknown key {key!r}")
        if action not in ("press", "release", "tap"):
            raise HTTPException(422, f"unknown action {action!r}")
        actions = ("press", "release") if action == "tap" else (action,)
        for act in actions:
            state._on_key(f"{key.upper()}_{act.upper()}")
        return {"ok": True}

    return app
