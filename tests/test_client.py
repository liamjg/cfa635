"""The client library against a real socket server (see conftest.live_server)."""

import json

import pytest

from cfa635.client import (
    AsyncCfa635Client,
    Cfa635Client,
    Cfa635Error,
    Cfa635Unreachable,
)


def make(live_server, name="tester", **kw) -> Cfa635Client:
    return Cfa635Client(live_server.url, name, timeout=5.0, **kw)


def test_publishes_a_page(live_server, token_dir):
    client = make(live_server)
    client.put_page("hello", ["Hello", "world"])
    assert live_server.wait_for(lambda: live_server.contains("Hello"))
    assert live_server.contains("world")


def test_reads_need_no_registration(live_server, token_dir):
    client = make(live_server)
    assert client.health()["status"] == "ok"
    assert client.device()["sim"] is False
    # A read-only client never claimed an identity on the server.
    assert client.id is None
    assert live_server.wait_for(lambda: client.pages()["pages"] == [])


def test_token_is_cached_and_reused(live_server, token_dir):
    first = make(live_server)
    first.put_page("p", ["one"])
    assert first.token_path.exists()

    second = make(live_server)
    assert second.token == first.token
    assert second.id == first.id
    # Same identity, so it may modify the first client's page.
    second.patch_page("p", ["two"])
    assert live_server.wait_for(lambda: live_server.contains("two"))


def test_markup_is_expanded_server_side(live_server, token_dir):
    client = make(live_server)
    client.put_page("m", ["CPU{fill}42%"])
    assert live_server.wait_for(lambda: live_server.contains("CPU"))
    row = next(r for r in live_server.rows() if "CPU" in r)
    assert row.rstrip().endswith("42%")
    assert len(row) == 20


def test_401_reregisters_transparently(live_server, token_dir):
    client = make(live_server)
    client.register()
    dead = client.token
    # The server restarted and forgot us: the cached token is now meaningless.
    client.token = "not-a-real-token"
    client.put_page("after", ["survived"])
    assert client.token not in (dead, "not-a-real-token")
    assert live_server.wait_for(lambda: live_server.contains("survived"))


def test_401_replays_published_pages(live_server, token_dir):
    client = make(live_server)
    client.put_page("status", ["before"], ttl=60)
    assert live_server.wait_for(lambda: live_server.contains("before"))

    # Deregistering server-side is exactly what a restart looks like to us:
    # our token dies and our pages go with it.
    client._request("DELETE", f"/clients/{client.id}", retry=False)
    assert live_server.wait_for(lambda: not live_server.contains("before"))

    client.patch_page("status", ["after"])
    assert live_server.wait_for(lambda: live_server.contains("after"))


def test_403_on_another_clients_page(live_server, token_dir):
    owner = make(live_server, name="owner")
    owner.put_page("mine", ["hands off"])

    intruder = make(live_server, name="intruder", token_path=token_dir / "other.json")
    intruder.register()
    with pytest.raises(Cfa635Error) as exc:
        intruder.patch_page("mine", ["clobbered"])
    assert exc.value.status == 403


def test_close_removes_our_pages(live_server, token_dir):
    client = make(live_server)
    client.put_page("temp", ["here"])
    assert live_server.wait_for(lambda: live_server.contains("here"))

    client.close()
    assert live_server.wait_for(lambda: not live_server.contains("here"))
    assert not client.token_path.exists()
    assert client.id is None


def test_led_zero_is_reserved(live_server, token_dir):
    client = make(live_server)
    with pytest.raises(Cfa635Error) as exc:
        client.set_led(0, green=100)
    assert exc.value.status == 403

    client.set_led(1, green=100, red=100, mode="blink")
    assert live_server.wait_for(lambda: live_server.fake.led(1)[0] > 0)


def test_bad_page_id_fails_before_the_network(live_server, token_dir):
    client = make(live_server)
    with pytest.raises(ValueError, match=r"\[a-z0-9-\]"):
        client.put_page("Not A Valid Id", ["x"])


def test_unreachable_server_raises_its_own_error(token_dir):
    # Port 1 is reserved and never listening.
    client = Cfa635Client("http://127.0.0.1:1", "nobody", timeout=1.0)
    with pytest.raises(Cfa635Unreachable):
        client.health()


def test_token_cache_is_private(live_server, token_dir):
    client = make(live_server)
    client.register()
    assert client.token_path.stat().st_mode & 0o077 == 0
    assert json.loads(client.token_path.read_text())["id"] == client.id


async def test_event_stream_starts_with_a_full_snapshot(live_server, token_dir):
    client = AsyncCfa635Client(live_server.url, "watcher", timeout=5.0)
    await client.put_page("watched", ["visible"], interactive=True)

    events = client.events(reconnect=False)
    hello = await anext(events)
    assert hello["type"] == "hello"
    assert hello["client"] == client.id
    assert any(p["id"] == "watched" for p in hello["pages"])
    assert "leds" in hello and "device" in hello

    # A physical keypress reaches us over the same socket.
    live_server.fake.tap("down")
    for _ in range(20):
        event = await anext(events)
        if event["type"] == "key":
            assert event["key"] == "down"
            break
    else:
        pytest.fail("no key event arrived")

    await events.aclose()
    await client.close()
