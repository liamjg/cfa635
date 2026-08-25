"""Client library for cfa635d: the other side of the LAN API.

Everything a well-behaved client owes the server, in one place, so no caller
has to remember it:

- the bearer token is returned exactly once, so it is cached on disk;
- server state is in-memory, so a `401` mid-run means "it restarted and
  forgot you" — re-register, re-publish, retry;
- event queues are bounded and drop oldest under pressure, so a gap in `seq`
  means events were lost and REST is the source of truth.

The sync half is stdlib-only: it has to run from a bare script on a host with
no venv (a goose hook, a cron job). `websockets` is imported lazily by the
async half so the sync path never pays for it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator

DEFAULT_URL = "http://127.0.0.1:8635"

# Page ids are validated server-side; fail here with a useful message rather
# than eating a 422 from the far end.
PAGE_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


class Cfa635Error(RuntimeError):
    """An error response from the server. `status` is the HTTP code: 401 is a
    dead token, 403 is someone else's resource, 422 is a malformed id."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Cfa635Unreachable(Cfa635Error):
    """The server could not be contacted at all (down, wrong host, no route)."""

    def __init__(self, detail: str):
        super().__init__(0, detail)


def _cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(root) / "cfa635"


def _token_path(base_url: str, name: str) -> Path:
    parts = urllib.parse.urlsplit(base_url)
    host = (parts.hostname or "unknown").replace(":", "-")
    slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", name) or "client"
    return _cache_dir() / f"{host}-{parts.port or 80}-{slug}.json"


def _detail(exc: urllib.error.HTTPError) -> str:
    """FastAPI puts the human-readable reason in {"detail": ...}."""
    try:
        return str(json.loads(exc.read()).get("detail", exc.reason))
    except Exception:
        return str(exc.reason)


def _fields(**kwargs: Any) -> dict[str, Any]:
    """Drop unset keys so the server's own defaults apply."""
    return {k: v for k, v in kwargs.items() if v is not None}


class Cfa635Client:
    """Synchronous client. Blocking, stdlib-only, safe to construct cheaply.

    Registration is lazy: the first mutating call registers (or reuses a
    cached token) so that read-only use never creates a client on the server.
    """

    def __init__(
        self,
        base_url: str | None = None,
        name: str = "client",
        *,
        token_path: str | Path | None = None,
        timeout: float = 2.0,
        replay_pages: bool = True,
    ):
        self.base_url = (
            base_url or os.environ.get("CFA635_URL") or DEFAULT_URL
        ).rstrip("/")
        self.name = name
        self.timeout = timeout
        self.replay_pages = replay_pages
        self.token_path = (
            Path(token_path) if token_path else _token_path(self.base_url, name)
        )
        self.id: str | None = None
        self.token: str | None = None
        # What we have published, so a 401 can be recovered from transparently.
        self._pages: dict[str, dict[str, Any]] = {}
        self._load_token()

    # --- identity -----------------------------------------------------

    def _load_token(self) -> None:
        try:
            cached = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return
        self.id, self.token = cached.get("id"), cached.get("token")

    def _save_token(self) -> None:
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(json.dumps({"id": self.id, "token": self.token}))
            self.token_path.chmod(0o600)
        except OSError:
            pass  # a cache we cannot write just means we re-register next time

    def register(self) -> None:
        """Claim a fresh identity. Any previously published pages are orphaned
        on the server until their TTLs expire — set TTLs."""
        out = self._request("POST", "/clients", {"name": self.name}, auth=False)
        self.id, self.token = out["id"], out["token"]
        self._save_token()

    # --- transport ----------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        body: Any | None = None,
        *,
        auth: bool = True,
        retry: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if auth:
            if self.token is None:
                self.register()
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and auth and retry:
                # In-memory state: the server restarted and forgot us.
                self.register()
                self._replay()
                return self._request(method, path, body, auth=auth, retry=False)
            raise Cfa635Error(exc.code, _detail(exc)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise Cfa635Unreachable(f"{self.base_url}: {exc}") from None

    def _replay(self) -> None:
        """Re-publish our pages under the new identity, best effort."""
        if not self.replay_pages:
            return
        for page_id, fields in list(self._pages.items()):
            try:
                self._request("PUT", f"/pages/{page_id}", fields, retry=False)
            except Cfa635Error:
                pass

    # --- pages --------------------------------------------------------

    def put_page(
        self,
        page_id: str,
        lines: list[str] | None = None,
        *,
        name: str | None = None,
        priority: int | None = None,
        ttl: float | None = None,
        duration: float | None = None,
        leds: dict[int, dict[str, Any]] | None = None,
        interactive: bool | None = None,
        cursor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or wholly replace a page. Replacing our own keeps its
        creation time, and therefore its rotation slot."""
        if not PAGE_ID_RE.match(page_id):
            raise ValueError(f"page id must match [a-z0-9-]{{1,32}}: {page_id!r}")
        body = _fields(
            lines=lines,
            name=name,
            priority=priority,
            ttl=ttl,
            duration=duration,
            leds=leds,
            interactive=interactive,
            cursor=cursor,
        )
        out = self._request("PUT", f"/pages/{page_id}", body)
        self._pages[page_id] = body
        return out

    def patch_page(
        self,
        page_id: str,
        lines: list[str] | dict[int | str, str] | None = None,
        **rest: Any,
    ) -> dict[str, Any]:
        """Partial update. `patch_page(id)` with nothing set is a pure TTL
        heartbeat. `lines` may be a full list or {row: text}."""
        body = _fields(lines=lines, **rest)
        out = self._request("PATCH", f"/pages/{page_id}", body)
        # Keep the replay copy current, but only for whole-line replacements —
        # a {row: text} patch cannot be merged into a list sensibly.
        tracked = self._pages.setdefault(page_id, {})
        tracked.update({k: v for k, v in body.items() if k != "lines" or isinstance(v, list)})
        return out

    def delete_page(self, page_id: str) -> None:
        self._request("DELETE", f"/pages/{page_id}")
        self._pages.pop(page_id, None)

    def activate(self, page_id: str, hold: float | None = None) -> dict[str, Any]:
        """Pin this page to the glass. Pins self-expire; alerts still preempt."""
        return self._request("POST", f"/pages/{page_id}/activate", _fields(hold=hold))

    def release_focus(self, page_id: str) -> dict[str, Any]:
        return self._request("POST", f"/pages/{page_id}/focus/release", {})

    def release_display(self) -> dict[str, Any]:
        return self._request("POST", "/display/release", {})

    # --- leds ---------------------------------------------------------

    def set_led(
        self,
        n: int,
        green: int = 0,
        red: int = 0,
        mode: str = "solid",
        hz: float = 1.0,
    ) -> dict[str, Any]:
        """Claim-on-first-write. LED 0 is the system indicator and always 403s.
        `blink`/`pulse` animate server-side, surviving our own death."""
        return self._request(
            "PUT", f"/leds/{n}", {"green": green, "red": red, "mode": mode, "hz": hz}
        )

    # --- reads (unauthenticated) --------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", auth=False)

    def device(self) -> dict[str, Any]:
        return self._request("GET", "/device", auth=False)

    def display(self) -> dict[str, Any]:
        return self._request("GET", "/display", auth=False)

    def pages(self) -> dict[str, Any]:
        return self._request("GET", "/pages", auth=False)

    def leds(self) -> dict[str, Any]:
        return self._request("GET", "/leds", auth=False)

    # --- teardown -----------------------------------------------------

    def close(self) -> None:
        """Deregister: removes our pages, releases our pin and LEDs. Best
        effort — an unreachable server will TTL the pages out anyway."""
        if self.id is None or self.token is None:
            return
        try:
            self._request("DELETE", f"/clients/{self.id}", retry=False)
        except Cfa635Error:
            pass
        self._pages.clear()
        self.id = self.token = None
        try:
            self.token_path.unlink()
        except OSError:
            pass


class AsyncCfa635Client:
    """Async facade for long-lived clients that need the event stream.

    Wraps the sync client rather than reimplementing it: the REST calls are
    short LAN round-trips, so they go to a thread instead of pulling in an
    async HTTP dependency. The value here is `events()`.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        self._sync = Cfa635Client(*args, **kwargs)

    @property
    def id(self) -> str | None:
        return self._sync.id

    @property
    def base_url(self) -> str:
        return self._sync.base_url

    def _ws_url(self) -> str:
        parts = urllib.parse.urlsplit(self._sync.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}/ws?token={self._sync.token}"

    async def _call(self, fn: str, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(getattr(self._sync, fn), *args, **kwargs)

    async def register(self) -> None:
        await self._call("register")

    async def put_page(self, page_id: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call("put_page", page_id, *args, **kwargs)

    async def patch_page(self, page_id: str, *args: Any, **kwargs: Any) -> Any:
        return await self._call("patch_page", page_id, *args, **kwargs)

    async def delete_page(self, page_id: str) -> Any:
        return await self._call("delete_page", page_id)

    async def activate(self, page_id: str, hold: float | None = None) -> Any:
        return await self._call("activate", page_id, hold)

    async def release_focus(self, page_id: str) -> Any:
        return await self._call("release_focus", page_id)

    async def release_display(self) -> Any:
        return await self._call("release_display")

    async def set_led(self, n: int, *args: Any, **kwargs: Any) -> Any:
        return await self._call("set_led", n, *args, **kwargs)

    async def health(self) -> Any:
        return await self._call("health")

    async def device(self) -> Any:
        return await self._call("device")

    async def pages(self) -> Any:
        return await self._call("pages")

    async def close(self) -> None:
        await self._call("close")

    async def events(
        self, *, reconnect: bool = True, max_backoff: float = 60.0
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield events from the authenticated socket, starting with `hello`.

        The hello frame is full state, so connecting *is* the resync — parse
        it rather than polling. A gap in `seq` yields a synthetic
        `{"type": "desync", "missed": n}` before the event that revealed it,
        because bounded queues drop oldest and REST is then authoritative.
        """
        import websockets  # lazy: the sync path must not need it

        if self._sync.token is None:
            await self.register()
        backoff = 1.0
        seq: int | None = None
        while True:
            try:
                async with websockets.connect(self._ws_url()) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        event = json.loads(raw)
                        got = event.get("seq")
                        if seq is not None and isinstance(got, int) and got > seq + 1:
                            yield {"type": "desync", "missed": got - seq - 1}
                        if isinstance(got, int):
                            seq = got
                        yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 4401 is the server telling us the token is dead, not a
                # transport failure: a fresh identity is the only way back.
                if getattr(exc, "code", None) == 4401:
                    await self.register()
                    seq = None
                    continue
                if not reconnect:
                    raise
            if not reconnect:
                return
            await asyncio.sleep(backoff)
            seq = None  # we were away; the next hello re-establishes the baseline
            backoff = min(backoff * 2, max_backoff)
