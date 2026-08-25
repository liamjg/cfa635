"""The server's own clock and info pages: seeding, priority, reservation."""

from datetime import datetime

from cfa635.server import builtin
from cfa635.server.config import Config
from tests.test_api import auth, make_client, screen_contains, wait_for


def make_default_client(**config_kw):
    """A server with the built-ins on, i.e. the shipped default."""
    return make_client(clock=True, info=True, **config_kw)


# --- the clock ---------------------------------------------------------------


def test_clock_is_the_resting_screen():
    client, fake = make_default_client()
    with client:
        # the date line is plain text, so it is readable straight off the glass
        expected = datetime.now().strftime("%a %d %b")
        assert wait_for(screen_contains(fake, expected))
        # ...and the three rows above it are custom glyphs (CGRAM codes 0-7)
        assert wait_for(lambda: all(
            any(b < 8 for b in fake.screen[row]) for row in range(3)))
        assert client.get("/pages").json()["active"] == "clock"


def seconds_cell(fake):
    """Row 1 to the right of the {big:} span, where the small seconds live."""
    return fake.rows()[1][builtin.SECONDS_COL:].strip()


def test_clock_ticks():
    client, fake = make_default_client()
    with client:
        assert wait_for(lambda: seconds_cell(fake).isdigit())
        seconds = seconds_cell(fake)
        assert wait_for(lambda: seconds_cell(fake) != seconds, timeout=3.0)


def test_time_format_and_seconds_are_configurable():
    client, fake = make_default_client(clock_time_fmt="%H%M",
                                       clock_seconds=False,
                                       clock_date_fmt="day %j")
    with client:
        assert wait_for(screen_contains(fake, datetime.now().strftime("day %j")))
        assert wait_for(lambda: seconds_cell(fake) == "")


def test_clock_lines_lay_out_as_designed():
    config = Config(port="/dev/fake", clock=True, info=True)
    lines = builtin.clock_lines(config, datetime(2026, 8, 24, 9, 47, 23))
    assert lines[0] == "{big:09:47}"
    assert lines[1] == " " * builtin.SECONDS_COL + "23"
    assert lines[3] == "Mon 24 Aug{fill}2026"


# --- the info page -----------------------------------------------------------


def test_info_page_carries_the_address():
    client, fake = make_default_client()
    with client:
        info = client.get("/pages/info").json()
        assert any(":8635" in line for line in info["lines"])
        assert any("up " in line for line in info["lines"])
        # never selected on its own, but the nav keys reach it
        assert wait_for(lambda: client.get("/pages").json()["active"] == "clock")
        client.post("/panel/key", json={"key": "down"})
        assert wait_for(screen_contains(fake, ":8635"))


def test_info_reports_client_count():
    client, _ = make_default_client()
    with client:
        auth(client, "one")
        def counted():
            return any("1 client" in line
                       for line in client.get("/pages/info").json()["lines"])
        assert wait_for(counted)


# --- arbitration and ownership -----------------------------------------------


def test_client_pages_preempt_the_builtins_and_release_back():
    client, fake = make_default_client()
    with client:
        h = auth(client)
        client.put("/pages/mine", json={"lines": ["a client page"]}, headers=h)
        assert wait_for(screen_contains(fake, "a client page"))
        client.delete("/pages/mine", headers=h)
        assert wait_for(screen_contains(fake, datetime.now().strftime("%a %d %b")))


def test_alerts_still_preempt_the_clock():
    client, fake = make_default_client()
    with client:
        h = auth(client)
        client.put("/pages/fire", json={"lines": ["ALERT"], "priority": 200},
                   headers=h)
        assert wait_for(screen_contains(fake, "ALERT"))


def test_builtin_ids_are_reserved():
    client, _ = make_default_client()
    with client:
        h = auth(client)
        assert client.put("/pages/clock", json={"lines": ["mine now"]},
                          headers=h).status_code == 403
        assert client.patch("/pages/info", json={"lines": ["mine"]},
                            headers=h).status_code == 403
        assert client.delete("/pages/clock", headers=h).status_code == 403


def test_switcher_lists_the_builtins_first():
    client, fake = make_default_client()
    with client:
        h = auth(client)
        client.put("/pages/mine", json={"lines": ["x"], "name": "Mine"},
                   headers=h)
        assert wait_for(screen_contains(fake, "x"))
        client.post("/panel/key", json={"key": "exit"})
        assert wait_for(screen_contains(fake, "> Clock"))
        assert wait_for(screen_contains(fake, "Info"))


def test_builtins_do_not_hold_the_backlight_on():
    # a clock on the glass is content, but it is not activity: the idle dim
    # timer must still run, or the display never goes dark overnight
    client, fake = make_default_client(idle_dim_secs=0.0, idle_backlight=0)
    with client:
        assert wait_for(lambda: fake.backlight[0] == 0)


def test_disabling_both_falls_back_to_the_idle_screen():
    client, fake = make_client()  # helper defaults both off
    with client:
        assert wait_for(screen_contains(fake, "no pages"))
        assert client.get("/pages").json()["pages"] == []


def test_info_keeps_the_address_when_the_hostname_is_long(monkeypatch):
    monkeypatch.setattr(builtin.socket, "gethostname",
                        lambda: "a-very-long-hostname.local")
    client, fake = make_default_client()
    with client:
        lines = client.get("/pages/info").json()["lines"]
        assert lines[0].endswith("{fill}v0.1.0")     # version survives
        assert len(lines[0].replace("{fill}", "")) <= 20
        client.post("/panel/key", json={"key": "down"})
        assert wait_for(screen_contains(fake, "v0.1.0"))
