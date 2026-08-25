"""Integration tests: full FastAPI app + DeviceWorker thread + FakeSerial."""

import time

import pytest
from fastapi.testclient import TestClient

from cfa635.driver import KEY_ENTER_PRESS, KEY_UP_PRESS
from cfa635.server.app import create_app
from cfa635.server.config import Config
from tests.fakeserial import FakeSerial


def make_client(**config_kw) -> tuple[TestClient, FakeSerial]:
    fake = FakeSerial()
    config = Config(port="/dev/fake", backlight=80, idle_dim_secs=300, **config_kw)
    app = create_app(config, ser=fake)
    return TestClient(app), fake


def auth(client, name="tester") -> dict:
    """Register a client and return the Authorization header for it."""
    r = client.post("/clients", json={"name": name})
    assert r.status_code == 201
    return {"Authorization": f"Bearer {r.json()['token']}"}


def wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def screen_contains(fake, text):
    return lambda: any(text in row for row in fake.rows())


def recv_until(ws, event_type, tries=20):
    for _ in range(tries):
        event = ws.receive_json()
        if event["type"] == event_type:
            return event
    raise AssertionError(f"no {event_type} event received")


# --- basic lifecycle and endpoints ------------------------------------------


def test_startup_health_and_device():
    client, fake = make_client()
    with client:
        health = client.get("/health").json()
        assert health == {"status": "ok", "api": 1, "device": "ok", "error": None}
        info = client.get("/device").json()
        assert info["version"] == "CFA635:h1.1,v1.6"
        assert info["backlight"] == {"lcd": 80, "keypad": 80}
        # startup applied key reporting and contrast to the hardware
        assert fake.press_mask == 0x3F
        assert fake.contrast == 120


def test_page_appears_on_screen():
    client, fake = make_client()
    with client:
        h = auth(client)
        r = client.put("/pages/hello", json={"lines": ["Hello", "world"]}, headers=h)
        assert r.status_code == 201
        assert wait_for(screen_contains(fake, "Hello"))
        assert wait_for(screen_contains(fake, "world"))
        body = client.get("/pages/hello").json()
        assert body["visible"] is True
        # replace is 200 and repaints
        assert client.put("/pages/hello", json={"lines": ["Bye"]},
                          headers=h).status_code == 200
        assert wait_for(screen_contains(fake, "Bye"))


def test_idle_page_when_empty():
    client, fake = make_client()
    with client:
        assert wait_for(screen_contains(fake, "no pages"))
        assert client.get("/display").json()["active"] is None


def test_missing_page_404():
    client, _ = make_client()
    with client:
        h = auth(client)
        assert client.get("/pages/nope").status_code == 404
        assert client.delete("/pages/nope", headers=h).status_code == 404
        assert client.patch("/pages/nope", json={}, headers=h).status_code == 404


def test_bad_page_id_rejected():
    client, _ = make_client()
    with client:
        h = auth(client)
        assert client.put("/pages/Not_Valid!", json={"lines": []},
                          headers=h).status_code == 422


def test_alert_preempts_and_led_overlay():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/calm", json={"lines": ["calm page"]}, headers=h)
        assert wait_for(screen_contains(fake, "calm page"))
        client.put("/pages/alarm", json={
            "lines": ["!! ALERT !!"],
            "priority": 200,
            "leds": {"0": {"red": 100}},
        }, headers=h)
        assert wait_for(screen_contains(fake, "!! ALERT !!"))
        assert wait_for(lambda: fake.led(0) == (0, 100))
        assert client.get("/display").json()["reason"] == "alert"
        client.delete("/pages/alarm", headers=h)
        assert wait_for(screen_contains(fake, "calm page"))
        assert wait_for(lambda: fake.led(0) == (0, 0))


def test_rotation():
    client, fake = make_client(rotation_secs=0.4)
    with client:
        h = auth(client)
        client.put("/pages/one", json={"lines": ["page one"]}, headers=h)
        client.put("/pages/two", json={"lines": ["page two"]}, headers=h)
        assert wait_for(screen_contains(fake, "page one"))
        assert wait_for(screen_contains(fake, "page two"), timeout=2)
        assert wait_for(screen_contains(fake, "page one"), timeout=2)


def test_activate_pins_and_release_resumes():
    client, fake = make_client(rotation_secs=0.3)
    with client:
        h = auth(client)
        client.put("/pages/one", json={"lines": ["page one"]}, headers=h)
        client.put("/pages/two", json={"lines": ["page two"]}, headers=h)
        r = client.post("/pages/two/activate", json={"hold": 30}, headers=h).json()
        assert r == {"active": "two", "pinned": "two"}
        time.sleep(0.8)  # several rotation periods
        assert client.get("/display").json()["active"] == "two"
        client.post("/display/release", headers=h)
        assert wait_for(
            lambda: client.get("/display").json()["pinned"] is None)


def test_ttl_expiry_removes_page():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/flash", json={"lines": ["short-lived"], "ttl": 0.4},
                   headers=h)
        assert wait_for(screen_contains(fake, "short-lived"))
        assert wait_for(lambda: client.get("/pages/flash").status_code == 404,
                        timeout=3)


def test_patch_partial_row_and_heartbeat():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/p", json={"lines": ["row zero", "row one"], "ttl": 5},
                   headers=h)
        assert wait_for(screen_contains(fake, "row zero"))
        client.patch("/pages/p", json={"lines": {"1": "changed"}}, headers=h)
        assert wait_for(screen_contains(fake, "changed"))
        assert wait_for(screen_contains(fake, "row zero"))  # row 0 untouched
        before = client.get("/pages/p").json()["expires_in"]
        time.sleep(0.2)
        client.patch("/pages/p", json={}, headers=h)  # pure TTL heartbeat
        after = client.get("/pages/p").json()["expires_in"]
        assert after >= before - 0.1


def test_leds_and_display_settings_not_api():
    client, fake = make_client()
    with client:
        h = auth(client)
        # backlight/contrast are shell-owned now; the routes are gone
        assert client.put("/display/backlight", json={"lcd": 40},
                          headers=h).status_code in (404, 405)
        assert client.put("/display/contrast", json={"value": 130},
                          headers=h).status_code in (404, 405)
        # startup applied the configured defaults through refresh()
        assert wait_for(lambda: fake.contrast == 120)
        assert wait_for(lambda: fake.backlight == (80, 80))
        client.put("/leds/2", json={"green": 55, "red": 0}, headers=h)
        assert wait_for(lambda: fake.led(2) == (55, 0))
        led = client.get("/leds").json()["2"]
        assert led["green"] == 55 and led["red"] == 0
        assert led["mode"] == "solid"


# --- key events and navigation ----------------------------------------------


def test_key_event_streamed_with_annotation():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/menu", json={"lines": ["menu"]}, headers=h)
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            fake.press_key(KEY_ENTER_PRESS)
            event = recv_until(ws, "key")
            assert event["key"] == "enter"
            assert event["action"] == "press"
            assert event["consumed"] is False
            assert event["active_page"] == "menu"


def test_nav_key_consumed_and_switches_page():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/one", json={"lines": ["page one"]}, headers=h)
        client.put("/pages/two", json={"lines": ["page two"]}, headers=h)
        assert wait_for(screen_contains(fake, "page one"))
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            fake.press_key(KEY_UP_PRESS)
            event = recv_until(ws, "key")
            assert event["key"] == "up"
            assert event["consumed"] is True
        assert wait_for(screen_contains(fake, "page two"))
        assert client.get("/display").json()["pinned"] == "two"


def test_no_keys_lost_during_command_interleaving():
    """The regression test for the reset_input_buffer fix, end to end."""
    client, fake = make_client()
    with client:
        h = auth(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            fake.press_key_mid_command(KEY_ENTER_PRESS)
            # Force traffic so the report gets interleaved with a command.
            client.put("/pages/busy", json={"lines": ["traffic"]}, headers=h)
            event = recv_until(ws, "key")
            assert event["key"] == "enter"


def test_page_visible_events():
    client, fake = make_client()
    with client:
        h = auth(client)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            client.put("/pages/first", json={"lines": ["x"]}, headers=h)
            event = recv_until(ws, "page_visible")
            assert event["page"] == "first"
            client.put("/pages/urgent", json={"lines": ["y"], "priority": 150},
                       headers=h)
            event = recv_until(ws, "page_visible")
            assert event == {**event, "page": "urgent", "reason": "alert"}
            client.delete("/pages/urgent", headers=h)
            removed = recv_until(ws, "page_removed")
            assert removed["reason"] == "deleted"
