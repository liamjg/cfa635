"""The /panel mirror: state from the server's applied caches, key injection
through the real key pipeline. make_client uses a plain (non-sim) config, so
these tests prove the panel works in hardware-mirror mode."""

from tests.test_api import auth, make_client, screen_contains, wait_for


def test_panel_state_mirrors_the_glass():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/p", json={"lines": ["mirror me", "{bar:0.5:10}"]},
                   headers=h)
        assert wait_for(screen_contains(fake, "mirror me"))
        s = client.get("/panel/state").json()
        assert s["sim"] is False  # mirroring "hardware" (the injected fake)
        # the mirror is byte-identical to what went over the wire
        assert s["rows"] == [list(row) for row in fake.screen]
        # programmed CGRAM matches the device's
        used = [g for g in s["cgram"] if any(g)]
        assert used and all(g in fake.cgram for g in used)
        assert s["backlight"] == 80
        assert s["contrast"] == 120


def test_panel_key_drives_the_real_pipeline():
    client, fake = make_client()
    with client:
        h = auth(client)
        client.put("/pages/p", json={"lines": ["a page"]}, headers=h)
        assert wait_for(screen_contains(fake, "a page"))
        r = client.post("/panel/key", json={"key": "exit"})
        assert r.status_code == 200
        assert wait_for(screen_contains(fake, "PAGES"))  # shell opened
        client.post("/panel/key", json={"key": "exit"})
        assert wait_for(screen_contains(fake, "a page"))


def test_panel_key_validation():
    client, _ = make_client()
    with client:
        assert client.post("/panel/key", json={"key": "boop"}).status_code == 422
        assert client.post("/panel/key",
                           json={"key": "up", "action": "hold"}).status_code == 422


def test_sim_path_redirects_to_panel():
    client, _ = make_client()
    with client:
        r = client.get("/sim", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/panel"
        assert "cfa635d panel" in client.get("/panel").text
