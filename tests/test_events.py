"""WebSocket auth, hello snapshot, and event sequence numbers."""

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.test_api import auth, make_client, recv_until


def test_hello_is_a_full_snapshot():
    client, _ = make_client()
    with client:
        h = auth(client, "snap")
        client.put("/pages/pg", json={"lines": ["hi"], "priority": 60}, headers=h)
        client.put("/leds/1", json={"green": 42}, headers=h)
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello"
            assert hello["api"] == 1
            assert isinstance(hello["seq"], int)
            assert hello["client"] is None  # observer connection
            assert [p["id"] for p in hello["pages"]] == ["pg"]
            assert hello["pages"][0]["priority"] == 60
            assert hello["active"] == "pg"
            assert hello["leds"]["1"] == {"green": 42, "red": 0,
                                          "mode": "solid", "hz": 1.0}
            assert hello["backlight"] == 80
            assert hello["contrast"] == 120
            assert hello["device"]["healthy"] is True


def test_ws_token_identifies_client():
    client, _ = make_client()
    with client:
        r = client.post("/clients", json={"name": "ha"}).json()
        with client.websocket_connect(f"/ws?token={r['token']}") as ws:
            hello = ws.receive_json()
            assert hello["client"] == r["id"]


def test_ws_bad_token_closed_4401():
    client, _ = make_client()
    with client:
        with pytest.raises(WebSocketDisconnect) as err:
            with client.websocket_connect("/ws?token=bogus") as ws:
                ws.receive_json()
        assert err.value.code == 4401


def test_seq_strictly_increases():
    client, _ = make_client()
    with client:
        h = auth(client)
        with client.websocket_connect("/ws") as ws:
            hello = ws.receive_json()
            last = hello["seq"]
            client.put("/pages/a", json={"lines": ["a"]}, headers=h)
            client.put("/pages/b", json={"lines": ["b"], "priority": 150},
                       headers=h)
            client.delete("/pages/b", headers=h)
            for _ in range(4):
                event = ws.receive_json()
                assert event["seq"] > last
                last = event["seq"]


def test_health_reports_api_version():
    client, _ = make_client()
    with client:
        assert client.get("/health").json()["api"] == 1
