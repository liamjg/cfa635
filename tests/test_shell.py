"""The on-device shell: switcher, settings, flash persistence."""

from cfa635.driver import (
    KEY_DOWN_PRESS,
    KEY_ENTER_PRESS,
    KEY_EXIT_PRESS,
    KEY_LEFT_PRESS,
    KEY_RIGHT_PRESS,
    KEY_UP_PRESS,
)
from cfa635.server.shell import decode_settings, encode_settings
from tests.test_api import auth, make_client, screen_contains, wait_for


def test_exit_opens_switcher_and_lists_pages():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/aaa", json={"lines": ["content a"]}, headers=h)
        client.put("/pages/bbb", json={"lines": ["content b"]}, headers=h)
        assert wait_for(screen_contains(fake, "content a"))
        fake.press_key(KEY_EXIT_PRESS)
        assert wait_for(screen_contains(fake, "PAGES"))
        assert wait_for(screen_contains(fake, "> aaa"))
        assert wait_for(screen_contains(fake, "Settings"))
        # EXIT again closes and the page comes back
        fake.press_key(KEY_EXIT_PRESS)
        assert wait_for(screen_contains(fake, "content a"))


def test_switcher_enter_pins_selected_page():
    client, fake = make_client(rotation_secs=0.3)
    with client:
        h = auth(client)
        client.put("/pages/one", json={"lines": ["page one"]}, headers=h)
        client.put("/pages/two", json={"lines": ["page two"]}, headers=h)
        assert wait_for(screen_contains(fake, "page one"))
        fake.press_key(KEY_EXIT_PRESS)
        assert wait_for(screen_contains(fake, "PAGES"))
        fake.press_key(KEY_DOWN_PRESS)  # highlight 'two'
        assert wait_for(screen_contains(fake, "> two"))
        fake.press_key(KEY_ENTER_PRESS)
        assert wait_for(screen_contains(fake, "page two"))
        assert client.get("/display").json()["pinned"] == "two"


def test_settings_adjust_live_and_persist_to_flash():
    client, fake = make_client()
    with client:
        assert wait_for(lambda: fake.backlight == (80, 80))
        fake.press_key(KEY_EXIT_PRESS)
        assert wait_for(screen_contains(fake, "PAGES"))
        # no pages: Settings is the only (and highlighted) row
        fake.press_key(KEY_ENTER_PRESS)
        assert wait_for(screen_contains(fake, "SETTINGS"))
        fake.press_key(KEY_LEFT_PRESS)  # backlight 80 -> 70, applied live
        assert wait_for(lambda: fake.backlight == (70, 70))
        fake.press_key(KEY_DOWN_PRESS)  # highlight contrast
        fake.press_key(KEY_RIGHT_PRESS)  # contrast 120 -> 125
        assert wait_for(lambda: fake.contrast == 125)
        fake.press_key(KEY_EXIT_PRESS)  # back to switcher
        fake.press_key(KEY_EXIT_PRESS)  # close shell -> persist
        assert wait_for(lambda: decode_settings(fake.user_flash) == (70, 125))


def test_valid_flash_record_beats_env_defaults_on_start():
    from fastapi.testclient import TestClient

    from cfa635.server.app import create_app
    from cfa635.server.config import Config
    from tests.fakeserial import FakeSerial

    fake = FakeSerial()
    fake.user_flash = encode_settings(30, 140)
    app = create_app(Config(port="/dev/fake", backlight=80), ser=fake)
    with TestClient(app) as client:
        assert wait_for(lambda: fake.backlight == (30, 30))
        assert wait_for(lambda: fake.contrast == 140)
        info = client.get("/device").json()
        assert info["backlight"]["lcd"] == 30
        assert info["contrast"] == 140


def test_corrupt_flash_record_falls_back_to_defaults():
    from fastapi.testclient import TestClient

    from cfa635.server.app import create_app
    from cfa635.server.config import Config
    from tests.fakeserial import FakeSerial

    fake = FakeSerial()
    fake.user_flash = b"\x12garbage\x00\x00\x00\x00\x00\x00\x00\x00"
    app = create_app(Config(port="/dev/fake", backlight=80), ser=fake)
    with TestClient(app) as client:
        assert wait_for(lambda: fake.backlight == (80, 80))
        assert wait_for(lambda: fake.contrast == 120)


def test_shell_keys_never_reach_owners_or_nav():
    client, fake = make_client()
    with client:
        r = client.post("/clients", json={"name": "owner"}).json()
        h = {"Authorization": f"Bearer {r['token']}"}
        client.put("/pages/i", json={"lines": ["interactive pg"],
                                     "interactive": True}, headers=h)
        client.put("/pages/j", json={"lines": ["other pg"]}, headers=h)
        assert wait_for(screen_contains(fake, "interactive pg"))
        with client.websocket_connect(f"/ws?token={r['token']}") as ws:
            ws.receive_json()
            fake.press_key(KEY_EXIT_PRESS)  # open shell
            assert wait_for(screen_contains(fake, "PAGES"))
            fake.press_key(KEY_UP_PRESS)   # shell nav, not page rotation
            fake.press_key(KEY_ENTER_PRESS)  # selects, does NOT focus
            # the opening EXIT is a browse-layer key (consumed); everything
            # after belongs to the shell layer, and nothing routes to owners
            opened = False
            shell_keys = []
            for _ in range(8):
                event = ws.receive_json()
                if event["type"] == "shell" and event["open"] is True:
                    opened = True
                elif event["type"] == "key":
                    assert "routed" not in event
                    assert event["consumed"] is True
                    if opened and event["key"] != "exit":
                        assert event.get("layer") == "shell"
                        shell_keys.append(event["key"])
                if len(shell_keys) == 2:
                    break
            assert shell_keys == ["up", "enter"]


def test_shell_auto_closes_on_timeout():
    client, fake = make_client(nav_hold_secs=0.4)
    with client:
        h = auth(client)
        client.put("/pages/p", json={"lines": ["back again"]}, headers=h)
        assert wait_for(screen_contains(fake, "back again"))
        fake.press_key(KEY_EXIT_PRESS)
        assert wait_for(screen_contains(fake, "PAGES"))
        assert wait_for(screen_contains(fake, "back again"), timeout=3)
