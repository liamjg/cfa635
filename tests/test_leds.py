"""LED ownership, patterns, and pin ownership."""

import time

from tests.test_api import auth, make_client, screen_contains, wait_for


def test_led0_reserved_for_system():
    client, _ = make_client()
    with client:
        h = auth(client)
        assert client.put("/leds/0", json={"green": 50},
                          headers=h).status_code == 403
        assert client.put("/leds", json={"0": {"green": 50}},
                          headers=h).status_code == 403


def test_led_claim_on_first_write_and_cross_owner_403():
    client, fake = make_client()
    with client:
        a = auth(client, "alpha")
        b = auth(client, "beta")
        assert client.put("/leds/2", json={"green": 60},
                          headers=a).status_code == 200
        assert wait_for(lambda: fake.led(2) == (60, 0))
        assert client.put("/leds/2", json={"red": 90},
                          headers=b).status_code == 403
        assert client.put("/leds/3", json={"red": 30},
                          headers=b).status_code == 200
        leds = client.get("/leds").json()
        a_id = next(c["id"] for c in client.get("/clients").json()
                    if c["name"] == "alpha")
        assert leds["2"]["owner"] == a_id
        assert leds["0"]["owner"] is None


def test_client_delete_releases_leds():
    client, fake = make_client()
    with client:
        a = auth(client, "quitter")
        a_id = client.get("/clients").json()[0]["id"]
        client.put("/leds/1", json={"green": 100}, headers=a)
        assert wait_for(lambda: fake.led(1) == (100, 0))
        client.delete(f"/clients/{a_id}", headers=a)
        assert wait_for(lambda: fake.led(1) == (0, 0))
        assert client.get("/leds").json()["1"]["owner"] is None


def test_blink_mode_animates_server_side():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/leds/1", json={"green": 80, "mode": "blink", "hz": 1},
                   headers=h)
        # the tick loop must drive the LED through both halves of the cycle
        assert wait_for(lambda: fake.led(1) == (80, 0), timeout=2)
        assert wait_for(lambda: fake.led(1) == (0, 0), timeout=2)
        assert wait_for(lambda: fake.led(1) == (80, 0), timeout=2)


def test_pin_ownership_on_release():
    client, fake = make_client()
    with client:
        a = auth(client, "alpha")
        b = auth(client, "beta")
        client.put("/pages/apage", json={"lines": ["alpha page"]}, headers=a)
        assert wait_for(screen_contains(fake, "alpha page"))
        client.post("/pages/apage/activate", headers=a)
        # beta neither holds the pin nor owns the page
        assert client.post("/display/release", headers=b).status_code == 403
        assert client.get("/display").json()["pinned"] == "apage"
        assert client.post("/display/release", headers=a).status_code == 200
        assert client.get("/display").json()["pinned"] is None


def test_page_owner_may_release_keypad_pin_of_their_page():
    from cfa635.driver import KEY_UP_PRESS

    client, fake = make_client()
    with client:
        a = auth(client, "alpha")
        b = auth(client, "beta")
        client.put("/pages/only", json={"lines": ["the page"]}, headers=a)
        assert wait_for(screen_contains(fake, "the page"))
        fake.press_key(KEY_UP_PRESS)  # human pins alpha's page
        assert wait_for(
            lambda: client.get("/display").json()["pinned"] == "only")
        assert client.post("/display/release", headers=b).status_code == 403
        assert client.post("/display/release", headers=a).status_code == 200
