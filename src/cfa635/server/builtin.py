"""The server's own pages: the resting clock and an info screen.

These are ordinary `Page`s in the ordinary `PageStore`, owned by the reserved
id "system". That buys the whole feature set for free: they appear in the
shell's switcher, UP/DOWN navigates to them, and the existing ownership check
on PUT/PATCH/DELETE makes their ids reserved without any new code.

They sit *below* the client default priority (50), so anything a client
publishes preempts them, and they come back when it goes away. Info sits below
the clock so it never rotates in on its own — you reach it from the switcher
or with the nav keys.

Their lines are rebuilt from the tick loop rather than being static, which is
what makes the clock tick and keeps GET /pages honest about what is on glass.
"""

from __future__ import annotations

import socket
import time
from datetime import datetime

from cfa635.driver import COLUMNS
from cfa635.server.pages import Page

OWNER = "system"
CLOCK_ID = "clock"
INFO_ID = "info"
CLOCK_PRIORITY = 10
INFO_PRIORITY = 5

# Big digits are 3 cells wide with a blank column between them, so HH:MM is
# 15 columns; the small seconds go in the right-hand margin, vertically
# centred against the 24 px digits.
SECONDS_COL = 17

_IP_CACHE_SECS = 60.0


def pages(config) -> list[Page]:
    """The built-in pages this config enables, in switcher order."""
    made = []
    if config.clock:
        made.append(Page(id=CLOCK_ID, name="Clock", lines=["", "", "", ""],
                         owner=OWNER, priority=CLOCK_PRIORITY, builtin=True))
    if config.info:
        made.append(Page(id=INFO_ID, name="Info", lines=["", "", "", ""],
                         owner=OWNER, priority=INFO_PRIORITY, builtin=True))
    now = time.monotonic()
    for index, page in enumerate(made):
        # created_at orders both the switcher and the nav cycle; seeding from
        # the same clock client pages use keeps the built-ins first however
        # long the host has been up
        page.created_at = page.updated_at = now + index * 1e-6
    return made


def clock_lines(config, now: datetime | None = None) -> list[str]:
    now = now or datetime.now()
    seconds = ""
    if config.clock_seconds:
        seconds = " " * SECONDS_COL + now.strftime("%S")
    return [
        "{big:" + now.strftime(config.clock_time_fmt) + "}",
        seconds,
        "",  # owned by the {big:} span
        now.strftime(config.clock_date_fmt),
    ]


def info_lines(state) -> list[str]:
    """Hostname, address, uptime, and what the display itself reports."""
    from cfa635.server.app import VERSION

    host = socket.gethostname().split(".")[0]
    clients = len(state.clients.all())
    return [
        _row(host, "v" + VERSION),
        _text(f"{host_ip(state)}:{state.config.http_port}"),
        _row(f"up {_uptime(time.monotonic() - state.started_at)}",
             f"{clients} client" + ("" if clients == 1 else "s")),
        _text(state.device.version or "no device version"),
    ]


def host_ip(state) -> str:
    """This host's LAN address, cached for a minute. No traffic is sent —
    a UDP connect just asks the kernel which interface it would route out of."""
    now = time.monotonic()
    cached_at, ip = state._ip_cache
    if now - cached_at > _IP_CACHE_SECS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect(("8.8.8.8", 53))
                ip = s.getsockname()[0]
        except OSError:
            ip = "no network"
        state._ip_cache = (now, ip)
    return ip


def refresh(state) -> None:
    """Rebuild the enabled built-ins' lines. Called from the tick loop."""
    clock = state.store.get(CLOCK_ID)
    if clock is not None and clock.builtin:
        clock.lines = clock_lines(state.config)
    info = state.store.get(INFO_ID)
    if info is not None and info.builtin:
        info.lines = info_lines(state)


def _uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours:02d}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _row(label: str, value: str) -> str:
    """label ... value, right-aligned. The label gives way when space is tight
    — a truncated hostname still beats a truncated address or version."""
    return _text(label[:COLUMNS - len(value) - 1]) + "{fill}" + _text(value)


def _text(text: str) -> str:
    """Hostnames and firmware strings are content, not markup."""
    return text[:COLUMNS].replace("{", "{{")
