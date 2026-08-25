"""Client registration, auth, and ownership isolation."""

import time

from tests.test_api import auth, make_client, screen_contains, wait_for


def test_register_returns_token_once_and_never_lists_it():
    client, _ = make_client()
    with client:
        r = client.post("/clients", json={"name": "ha"})
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "ha"
        assert body["id"].startswith("c-")
        assert len(body["token"]) >= 16
        listing = client.get("/clients").json()
        assert [c["id"] for c in listing] == [body["id"]]
        assert "token" not in listing[0]
        assert listing[0]["pages"] == []


def test_unauthed_mutations_401():
    client, _ = make_client()
    with client:
        assert client.put("/pages/x", json={"lines": []}).status_code == 401
        assert client.patch("/pages/x", json={}).status_code == 401
        assert client.delete("/pages/x").status_code == 401
        assert client.post("/pages/x/activate").status_code == 401
        assert client.post("/display/release").status_code == 401
        assert client.put("/leds/1", json={"green": 5}).status_code == 401
        bad = {"Authorization": "Bearer nope"}
        assert client.put("/pages/x", json={"lines": []},
                          headers=bad).status_code == 401
        # reads stay open
        assert client.get("/pages").status_code == 200
        assert client.get("/display").status_code == 200


def test_cross_owner_mutations_403_and_target_untouched():
    client, fake = make_client()
    with client:
        a = auth(client, "alpha")
        b = auth(client, "beta")
        client.put("/pages/mine", json={"lines": ["alpha content"]}, headers=a)
        assert wait_for(screen_contains(fake, "alpha content"))
        assert client.put("/pages/mine", json={"lines": ["stolen"]},
                          headers=b).status_code == 403
        assert client.patch("/pages/mine", json={"lines": ["stolen"]},
                            headers=b).status_code == 403
        assert client.delete("/pages/mine", headers=b).status_code == 403
        assert client.post("/pages/mine/activate", headers=b).status_code == 403
        page = client.get("/pages/mine").json()
        assert page["lines"] == ["alpha content"]
        # owner still fully in control
        assert client.patch("/pages/mine", json={"lines": ["updated"]},
                            headers=a).status_code == 200


def test_same_owner_replace_preserves_created_at_ordering():
    client, fake = make_client(rotation_secs=0.3)
    with client:
        h = auth(client)
        client.put("/pages/first", json={"lines": ["first page"]}, headers=h)
        time.sleep(0.05)
        client.put("/pages/second", json={"lines": ["second page"]}, headers=h)
        # replacing 'first' must not move it to the end of the rotation order
        client.put("/pages/first", json={"lines": ["first v2"]}, headers=h)
        pages = client.get("/pages").json()["pages"]
        assert [p["id"] for p in pages] == ["first", "second"]


def test_client_delete_cascades():
    client, fake = make_client()
    with client:
        a = auth(client, "alpha")
        b = auth(client, "beta")
        a_id = client.get("/clients").json()[0]["id"]
        client.put("/pages/gone", json={"lines": ["from alpha"]}, headers=a)
        client.put("/pages/stays", json={"lines": ["from beta"]}, headers=b)
        client.post("/pages/gone/activate", headers=a)
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()  # hello
            # beta cannot delete alpha
            assert client.delete(f"/clients/{a_id}", headers=b).status_code == 403
            assert client.delete(f"/clients/{a_id}", headers=a).status_code == 204
            for _ in range(20):
                event = ws.receive_json()
                if event["type"] == "page_removed":
                    break
            assert event["page"] == "gone"
            assert event["reason"] == "client_deleted"
        assert client.get("/pages/gone").status_code == 404
        assert client.get("/pages/stays").status_code == 200
        assert client.get("/display").json()["pinned"] is None
        # alpha's token is dead now
        assert client.put("/pages/again", json={"lines": []},
                          headers=a).status_code == 401
        assert [c["name"] for c in client.get("/clients").json()] == ["beta"]


def test_owner_visible_in_page_out_and_client_listing():
    client, _ = make_client()
    with client:
        h = auth(client, "solo")
        cid = client.get("/clients").json()[0]["id"]
        client.put("/pages/pg", json={"lines": []}, headers=h)
        assert client.get("/pages/pg").json()["owner"] == cid
        assert client.get("/clients").json()[0]["pages"] == ["pg"]
