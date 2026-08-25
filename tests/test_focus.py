"""Focus mode: ENTER captures the keypad for a page's owner."""

import time

from cfa635.driver import (
    KEY_DOWN_PRESS,
    KEY_ENTER_PRESS,
    KEY_EXIT_PRESS,
    KEY_UP_PRESS,
)
from tests.test_api import auth, make_client, recv_until, screen_contains, wait_for


def register(client, name):
    r = client.post("/clients", json={"name": name}).json()
    return r["id"], r["token"], {"Authorization": f"Bearer {r['token']}"}


def test_enter_gains_focus_and_routes_all_keys_owner_only():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "dimmer")
        client.put("/pages/light", json={"lines": ["Kitchen"],
                                         "interactive": True}, headers=h)
        assert wait_for(screen_contains(fake, "Kitchen"))
        with client.websocket_connect(f"/ws?token={token}") as owner_ws, \
                client.websocket_connect("/ws") as observer_ws:
            owner_ws.receive_json()
            observer_ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            gained = recv_until(owner_ws, "focus")
            assert gained["state"] == "gained"
            assert gained["page"] == "light"
            generation = gained["generation"]
            # every key, including EXIT, now routes to the owner
            # (skipping the broadcast of the consumed ENTER press itself)
            fake.press_key(KEY_UP_PRESS)
            fake.press_key(KEY_EXIT_PRESS)
            routed = []
            while len(routed) < 2:
                event = recv_until(owner_ws, "key")
                if event.get("routed"):
                    routed.append(event)
            assert [e["key"] for e in routed] == ["up", "exit"]
            assert all(e["generation"] == generation for e in routed)
            # the observer saw the consumed ENTER but none of the routed keys
            enter = recv_until(observer_ws, "key")
            assert enter["key"] == "enter"
            assert enter["consumed"] is True
            page = client.get("/pages/light").json()
            assert page["focused"] is True


def test_focused_page_does_not_rotate_away():
    client, fake = make_client(rotation_secs=0.2)
    with client:
        cid, token, h = register(client, "app")
        client.put("/pages/a", json={"lines": ["page aaa"],
                                     "interactive": True}, headers=h)
        client.put("/pages/b", json={"lines": ["page bbb"]}, headers=h)
        assert wait_for(screen_contains(fake, "page aaa"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            recv_until(ws, "focus")
            time.sleep(0.6)  # several rotation periods
            assert client.get("/display").json()["active"] == "a"
            # routed keys refresh the hold; up does not rotate
            fake.press_key(KEY_UP_PRESS)
            time.sleep(0.3)
            assert client.get("/display").json()["active"] == "a"


def test_enter_without_owner_connection_stays_global():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "offline")
        client.put("/pages/solo", json={"lines": ["alone"],
                                        "interactive": True}, headers=h)
        assert wait_for(screen_contains(fake, "alone"))
        with client.websocket_connect("/ws") as observer:
            observer.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            event = recv_until(observer, "key")
            assert event["consumed"] is False  # no focus happened
        assert client.get("/pages/solo").json()["focused"] is False


def test_enter_on_non_interactive_page_does_nothing():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "passive")
        client.put("/pages/plain", json={"lines": ["static"]}, headers=h)
        assert wait_for(screen_contains(fake, "static"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            event = recv_until(ws, "key")
            assert event["key"] == "enter"
            assert event["consumed"] is False


def test_release_endpoint_and_ownership():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "owner")
        _, _, other = register(client, "other")
        client.put("/pages/ui", json={"lines": ["ui"], "interactive": True},
                   headers=h)
        assert wait_for(screen_contains(fake, "ui"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            recv_until(ws, "focus")
            assert client.post("/pages/ui/focus/release",
                               headers=other).status_code == 403
            r = client.post("/pages/ui/focus/release", headers=h)
            assert r.status_code == 200
            assert r.json()["focused"] is None
            lost = recv_until(ws, "focus")
            assert lost["state"] == "lost"
            assert lost["reason"] == "released"
        assert client.get("/display").json()["pinned"] is None


def test_focus_idle_timeout():
    client, fake = make_client(focus_hold_secs=0.3)
    with client:
        cid, token, h = register(client, "sleepy")
        client.put("/pages/nap", json={"lines": ["zzz"], "interactive": True},
                   headers=h)
        assert wait_for(screen_contains(fake, "zzz"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            recv_until(ws, "focus")
            lost = recv_until(ws, "focus", tries=40)
            assert lost["state"] == "lost"
            assert lost["reason"] == "timeout"


def test_focus_dropped_when_owner_disconnects():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "flaky")
        client.put("/pages/x", json={"lines": ["xx"], "interactive": True},
                   headers=h)
        assert wait_for(screen_contains(fake, "xx"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            recv_until(ws, "focus")
        # owner socket is gone; the tick loop notices and un-focuses
        assert wait_for(
            lambda: client.get("/pages/x").json()["focused"] is False)
        # keys are global again: DOWN pins the next page rather than routing
        client.put("/pages/y", json={"lines": ["yy"]}, headers=h)
        fake.press_key(KEY_DOWN_PRESS)
        assert wait_for(lambda: client.get("/display").json()["pinned"] is not None)


def test_alert_preempts_focus():
    client, fake = make_client()
    with client:
        cid, token, h = register(client, "calm")
        client.put("/pages/menu", json={"lines": ["menu here"],
                                        "interactive": True}, headers=h)
        assert wait_for(screen_contains(fake, "menu here"))
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.receive_json()
            fake.press_key(KEY_ENTER_PRESS)
            recv_until(ws, "focus")
            client.put("/pages/fire", json={"lines": ["FIRE"], "priority": 200},
                       headers=h)
            lost = recv_until(ws, "focus", tries=40)
            assert lost["state"] == "lost"
            assert lost["reason"] == "preempted"
        assert wait_for(screen_contains(fake, "FIRE"))
