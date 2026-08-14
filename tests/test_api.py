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
        assert health == {"status": "ok", "device": "ok", "error": None}
        info = client.get("/device").json()
        assert info["version"] == "CFA635:h1.1,v1.6"
        assert info["backlight"] == {"lcd": 80, "keypad": 80}
        # startup applied key reporting and contrast to the hardware
        assert fake.press_mask == 0x3F
        assert fake.contrast == 120


def test_page_appears_on_screen():
    client, fake = make_client()
    with client:
        r = client.put("/pages/hello", json={"lines": ["Hello", "world"]})
        assert r.status_code == 201
        assert wait_for(screen_contains(fake, "Hello"))
        assert wait_for(screen_contains(fake, "world"))
        body = client.get("/pages/hello").json()
        assert body["visible"] is True
        # replace is 200 and repaints
        assert client.put("/pages/hello", json={"lines": ["Bye"]}).status_code == 200
        assert wait_for(screen_contains(fake, "Bye"))


def test_idle_page_when_empty():
    client, fake = make_client()
    with client:
        assert wait_for(screen_contains(fake, "no pages"))
        assert client.get("/display").json()["active"] is None


def test_missing_page_404():
    client, _ = make_client()
    with client:
        assert client.get("/pages/nope").status_code == 404
        assert client.delete("/pages/nope").status_code == 404
        assert client.patch("/pages/nope", json={}).status_code == 404


def test_bad_page_id_rejected():
    client, _ = make_client()
    with client:
        assert client.put("/pages/Not_Valid!", json={"lines": []}).status_code == 422


def test_alert_preempts_and_led_overlay():
    client, fake = make_client()
    with client:
        client.put("/pages/calm", json={"lines": ["calm page"]})
        assert wait_for(screen_contains(fake, "calm page"))
        client.put("/pages/alarm", json={
            "lines": ["!! ALERT !!"],
            "priority": 200,
            "leds": {"0": {"red": 100}},
        })
        assert wait_for(screen_contains(fake, "!! ALERT !!"))
        assert wait_for(lambda: fake.led(0) == (0, 100))
        assert client.get("/display").json()["reason"] == "alert"
        client.delete("/pages/alarm")
        assert wait_for(screen_contains(fake, "calm page"))
        assert wait_for(lambda: fake.led(0) == (0, 0))


def test_rotation():
    client, fake = make_client(rotation_secs=0.4)
    with client:
        client.put("/pages/one", json={"lines": ["page one"]})
        client.put("/pages/two", json={"lines": ["page two"]})
        assert wait_for(screen_contains(fake, "page one"))
        assert wait_for(screen_contains(fake, "page two"), timeout=2)
        assert wait_for(screen_contains(fake, "page one"), timeout=2)


def test_activate_pins_and_release_resumes():
    client, fake = make_client(rotation_secs=0.3)
    with client:
        client.put("/pages/one", json={"lines": ["page one"]})
        client.put("/pages/two", json={"lines": ["page two"]})
        r = client.post("/pages/two/activate", json={"hold": 30}).json()
        assert r == {"active": "two", "pinned": "two"}
        time.sleep(0.8)  # several rotation periods
        assert client.get("/display").json()["active"] == "two"
        client.post("/display/release")
        assert wait_for(
            lambda: client.get("/display").json()["pinned"] is None)


def test_ttl_expiry_removes_page():
    client, fake = make_client()
    with client:
        client.put("/pages/flash", json={"lines": ["short-lived"], "ttl": 0.4})
        assert wait_for(screen_contains(fake, "short-lived"))
        assert wait_for(lambda: client.get("/pages/flash").status_code == 404,
                        timeout=3)


def test_patch_partial_row_and_heartbeat():
    client, fake = make_client()
    with client:
        client.put("/pages/p", json={"lines": ["row zero", "row one"], "ttl": 5})
        assert wait_for(screen_contains(fake, "row zero"))
        client.patch("/pages/p", json={"lines": {"1": "changed"}})
        assert wait_for(screen_contains(fake, "changed"))
        assert wait_for(screen_contains(fake, "row zero"))  # row 0 untouched
        before = client.get("/pages/p").json()["expires_in"]
        time.sleep(0.2)
        client.patch("/pages/p", json={})  # pure TTL heartbeat
        after = client.get("/pages/p").json()["expires_in"]
        assert after >= before - 0.1


def test_backlight_contrast_and_leds():
    client, fake = make_client()
    with client:
        r = client.put("/display/backlight", json={"lcd": 40}).json()
        assert r == {"lcd": 40, "keypad": 40}
        assert wait_for(lambda: fake.backlight == (40, 40))
        client.put("/display/contrast", json={"value": 130})
        assert wait_for(lambda: fake.contrast == 130)
        client.put("/leds/2", json={"green": 55, "red": 0})
        assert wait_for(lambda: fake.led(2) == (55, 0))
        assert client.get("/leds").json()["2"] == {"green": 55, "red": 0}


# --- key events and navigation ----------------------------------------------


def test_key_event_streamed_with_annotation():
    client, fake = make_client()
    with client:
        client.put("/pages/menu", json={"lines": ["menu"]})
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
        client.put("/pages/one", json={"lines": ["page one"]})
        client.put("/pages/two", json={"lines": ["page two"]})
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
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            fake.press_key_mid_command(KEY_ENTER_PRESS)
            # Force traffic so the report gets interleaved with a command.
            client.put("/pages/busy", json={"lines": ["traffic"]})
            event = recv_until(ws, "key")
            assert event["key"] == "enter"


def test_page_visible_events():
    client, fake = make_client()
    with client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            client.put("/pages/first", json={"lines": ["x"]})
            event = recv_until(ws, "page_visible")
            assert event["page"] == "first"
            client.put("/pages/urgent", json={"lines": ["y"], "priority": 150})
            event = recv_until(ws, "page_visible")
            assert event == {**event, "page": "urgent", "reason": "alert"}
            client.delete("/pages/urgent")
            removed = recv_until(ws, "page_removed")
            assert removed["reason"] == "deleted"
