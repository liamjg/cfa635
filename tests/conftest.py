"""A real socket server, for tests that cannot use TestClient.

`TestClient` drives the ASGI app in-process, so nothing that speaks actual
HTTP — the stdlib client in `cfa635.client`, a subprocess hook script — can
reach it. This fixture runs uvicorn on an ephemeral port against the same
`create_app(config, ser=FakeSerial())` the other tests use, exercising the
real HTTP and WebSocket paths end to end.
"""

from __future__ import annotations

import threading
import time

import pytest
import uvicorn

from cfa635.server.app import create_app
from cfa635.server.config import Config
from tests.fakeserial import FakeSerial


class LiveServer:
    def __init__(self, url: str, fake: FakeSerial):
        self.url = url
        self.fake = fake

    def rows(self) -> list[str]:
        return self.fake.rows()

    def contains(self, text: str) -> bool:
        return any(text in row for row in self.fake.rows())

    def wait_for(self, predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False


@pytest.fixture
def live_server(tmp_path):
    """A cfa635d on 127.0.0.1:<ephemeral>, backed by FakeSerial."""
    fake = FakeSerial()
    config = Config(port="/dev/fake", http_host="127.0.0.1", http_port=0,
                    clock=False, info=False)
    app = create_app(config, ser=fake)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread died during startup")
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("uvicorn did not start within 10s")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield LiveServer(f"http://127.0.0.1:{port}", fake)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    """Keep token caches out of the developer's real ~/.cache/cfa635."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("CFA635_URL", raising=False)
    return tmp_path / "cache" / "cfa635"
