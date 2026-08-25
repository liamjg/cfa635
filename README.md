# CFA635 / XES635BK — 20x4 USB character LCD

Working directory for the Crystalfontz display attached to this machine.
Device identity below was established by probing the actual unit, not
assumed from the product page.

## What's actually attached

| | |
|---|---|
| Ordered as | **XES635BK-TFE-KU** — CFA635 module in a black steel enclosure |
| Reports itself as | **`CFA635:h1.1,v1.6`** — hardware v1.1, firmware v1.6 |
| USB | FTDI bridge, VID `0403` / PID `fc0d` |
| USB product string | `Crystalfontz CFA635-USB LCD` |
| USB serial | `CF124841` |
| Kernel driver | `ftdi_sio` → `/dev/ttyUSB0` |
| Stable path | `/dev/serial/by-id/usb-Crystalfontz_Crystalfontz_CFA635-USB_LCD_CF124841-if00-port0` |
| Port settings | 115200 8N1, no flow control |
| Display | 20 columns x 4 rows, FSTN positive, white LED backlight |
| Input | 6-key keypad (up/down/left/right/enter/exit) |
| Indicators | 4 bicolor red+green LEDs, PWM-dimmable |

Live settings read back from the unit: contrast **120**, backlight **100%**,
key press and release reporting both enabled for all six keys (mask `0x3f`),
user flash erased (all `0xff`).

**Use [`docs/CFA635-TFE-KU_Data_Sheet_v2.1.pdf`](docs/CFA635-TFE-KU_Data_Sheet_v2.1.pdf) as the
authoritative reference** — its hardware v1.1 / firmware v1.6 masthead is an
exact match for this unit. See [notes/DEVICE.md](notes/DEVICE.md) for why, and for
the places where the enclosure datasheet contradicts the hardware.

## Layout

```
docs/                  datasheets (PDF), docs/text/ extractions, API.md (the API reference)
notes/                 DEVICE.md (identification), PROTOCOL.md, HA-INTEGRATION.md
src/cfa635/            uv-managed package
  driver.py            driver: framing, CRC, commands (incl. CGRAM, cursor, flash)
  charmap.py           Unicode/ASCII -> CGROM translation (from datasheet Fig. 11)
  markup.py            server-side widgets: bars, sparklines, charts, icons, fills
  sim.py               protocol-faithful fake device (tests + simulator)
  probe.py             identification CLI (cfa635-probe); --store-boot-state
  server/              cfa635d: FastAPI page server (cfa635-server) + /sim viewer
deploy/                udev rule, systemd unit, install.md
tests/                 hardware-free test suite (FakeSerial) + smoke.md
```

## Quickstart

```bash
uv sync
uv run cfa635-probe            # identify the device (read-only)
uv run cfa635-server           # LAN API on :8635 (see below)
uv run pytest                  # full suite, no hardware touched

CFA635_SIM=1 uv run cfa635-server   # no hardware? full simulator at /sim
```

Driver, used directly:

```python
from cfa635 import Cfa635

with Cfa635("/dev/ttyUSB0") as lcd:
    print(lcd.version())          # 'CFA635:h1.1,v1.6'
    lcd.clear()
    lcd.write_text(0, 0, "hello")
    lcd.set_led(0, green=100)     # top LED green
    print(lcd.key_events(1.0))    # ['ENTER_PRESS', 'ENTER_RELEASE']
```

## cfa635d — the LAN page server

One long-running process owns the serial port and arbitrates the display
between any number of network clients. Clients **register once** (id +
bearer token), then publish **pages** (virtual 4x20 screens) that only they
can modify; the server decides what's on the glass: highest priority wins,
equals rotate (10 s default, per-page `duration` override), `priority >=
100` is an alert that preempts everything, and pages with a `ttl` vanish
when their owner stops refreshing them.

At rest the display shows its own **big-font clock** — block digits three
rows tall, with the date beneath — and a second built-in
**Info** page (address, uptime, firmware) sits behind it in the switcher.
Both are ordinary pages below client priority, so anything you publish
takes the glass and the clock returns when it goes; both are still "idle"
for the backlight, which turns off after 5 min without a keypress.

On the device itself: UP/DOWN rotate and pin pages, **EXIT opens the
built-in shell** (page switcher + backlight/contrast settings, persisted in
the module's user flash), and **ENTER focuses** an `interactive` page — all
six keys then route to that page's owner over WebSocket, which is how
clients build menus, dimmers, and dialogs. Page lines support server-side
**markup**: gapless bars, sparklines, multi-row charts, big block
digits, spinners, icons, `{fill}` layout, marquee and blink — see
[docs/API.md](docs/API.md).

```bash
# register once, keep the token
TOKEN=$(curl -s -X POST :8635/clients -H 'content-type: application/json' \
        -d '{"name":"music"}' | jq -r .token)

# put a page on the display (create-or-replace, idempotent, owned)
curl -X PUT :8635/pages/music -H "authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"lines":["Now playing:","Blue in Green","{bar:0.4:20}"], "ttl":30}'

# an alert with the top LED blinking red (server-side animation)
curl -X PUT :8635/pages/disk -H "authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' \
     -d '{"lines":["{bell} disk almost full"],"priority":200,
          "leds":{"0":{"red":100,"mode":"blink"}}}'

# key presses + page visibility as a live stream (reads are open)
websocat ws://host:8635/ws
```

Full surface: [docs/API.md](docs/API.md) (the reference) and interactive
docs at `http://host:8635/docs`; smoke-test walkthrough in
[tests/smoke.md](tests/smoke.md); install as a service via
[deploy/install.md](deploy/install.md) (optionally with
`cfa635-probe --store-boot-state` for a power-on splash). In production it
runs as the dedicated `cfa635` system user from `/opt/cfa635` under
systemd, so it starts at boot with nobody logged in — the port-permission
notes below only matter for running the tools ad hoc as `liam`.

**The panel in a browser**: `http://host:8635/panel` is a live visual twin
of the device face — the exact rows, glyphs, LEDs, and cursor on the glass
right now, with a clickable keypad wired into the same input pipeline as
the physical buttons. On the deployed daemon it mirrors the real hardware;
with `CFA635_SIM=1` (no hardware attached) the same page fronts a
protocol-faithful device model instead. Display *content* is byte-identical
either way; only the character shapes are lookalikes — see the fidelity
contract in [docs/API.md](docs/API.md).

Two hardware quirks the server works around, discovered on this unit:
firmware v1.6 rejects the two-byte (separate keypad) form of the backlight
command, and backlight PWM duty cycles between 1-99% make the supply
audibly whine — so the defaults hold the backlight at 100% (active) or 0%
(idle-dimmed), never in between. Same applies to LED duty cycles if you
hear ticking: use 0 or 100.

## Port permissions

`/dev/ttyUSB0` is `root:dialout`. `liam` **has been added to `dialout`** and
`/etc/group` reads `dialout:x:20:liam`, so no `sudo` is needed — but only from
a session that started after the change.

### If `groups` doesn't list dialout

That's expected, and does not mean `usermod` failed. Supplementary groups are
attached to a process at login and inherited by every child; adding a group to
the database doesn't retroactively touch sessions that are already running.
The two commands answer different questions:

```bash
id -nG liam    # fresh lookup in the group database -> includes dialout
id -nG         # this process's own credentials     -> does not, until re-login
groups         # same as `id -nG` — process credentials, so also stale
```

To pick it up permanently, log out of the **graphical session** and back in,
or reboot. Opening a new terminal is not enough — terminal windows inherit
credentials from the session manager, which is itself still running with the
old set.

Until then, `sg` runs a single command with the group attached. It does not
prompt for a password, because the account really is a member:

```bash
sg dialout -c 'uv run cfa635-probe'
newgrp dialout    # or: start an interactive subshell that has the group
```

This is how the probe was last run successfully as an unprivileged user.

### Alternative: udev rule

Grants access via `plugdev`, which this account already holds, so it needs no
re-login at all, and adds a stable `/dev/cfa635` symlink:

```bash
sudo tee /etc/udev/rules.d/99-crystalfontz.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="fc0d", \
  GROUP="plugdev", MODE="0660", SYMLINK+="cfa635"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
```

PID `fc0d` is Crystalfontz's, but VID `0403` is FTDI's and is shared with a
great many unrelated adapters — the rule is specific because of the PID, not
the VID.

The deployed rule ([deploy/99-crystalfontz.rules](deploy/99-crystalfontz.rules))
adds only the symlink and systemd tag, with no group grant — this plugdev
variant is only useful for ad-hoc access before your next re-login.

### If you do use sudo

Pass `-B` so root doesn't leave unwritable `__pycache__` directories behind.
