# Home Assistant integration for cfa635d — design

Design for a full-featured Home Assistant integration that drives the CFA635
through the **cfa635d LAN server** (`:8635`), not the serial driver. The server
already owns the port and arbitrates between clients; Home Assistant should be
one more client, publishing pages and reacting to keys, never bypassing the
arbiter.

Status: design only, nothing implemented yet.

## Shape and distribution

- **Custom integration, distributed via HACS.** Domain `cfa635`, one config
  entry per cfa635d server. Core inclusion would require the API client to be
  a published PyPI package and a long review cycle; HACS gets the same UX
  (config flow, devices, entities) without either. The client code is written
  as a self-contained `api.py` so it could be extracted to PyPI later.
- `manifest.json`: `iot_class: local_push` (WebSocket-driven), no external
  `requirements` — everything uses HA's bundled `aiohttp`.

## Architecture

```
config entry ──► Cfa635dClient (api.py, aiohttp)
                   │  REST: pages / display / leds / backlight / contrast
                   │  WS:   /ws events
                   ▼
               Cfa635dCoordinator (DataUpdateCoordinator)
                   │  push: WS events patch coordinator data immediately
                   │  pull: full resync via GET /pages + /display + /leds +
                   │        /device on connect, reconnect, and every 60 s
                   ▼
               entities (light, number, select, sensor, event, text,
                         button, binary_sensor, notify)
```

- **WS listener task** per entry: connect to `ws://host:port/ws` with
  `aiohttp` heartbeat pings (~30 s); on any event, update coordinator state
  and fire HA bus events. On disconnect, retry with exponential backoff
  (1 s → 60 s cap) and mark entities unavailable after the first failed
  reconnect.
- **Resync on (re)connect** is mandatory: the `hello` frame only carries
  `active_page`, and the EventBus drops oldest events for slow consumers, so
  the WS stream alone is not a reliable source of truth. Every (re)connect
  pulls the full REST state, then the stream keeps it fresh.
- Coordinator data is one snapshot: `pages` (list of PageOut), `active`,
  `pinned`, `reason`, `backlight`, `contrast`, `leds`, `device` (version,
  health).

## Config flow

1. User step: `host` (required), `port` (default 8635).
2. Validate by calling `GET /health` and `GET /device`; abort with
   `cannot_connect` / show `device_error` if `health.device != "ok"` (still
   allow setup — the server may recover, entities just start unavailable).
3. `unique_id`: `f"{host}:{port}"` for now. **Server enhancement worth
   making:** expose the USB serial (`CF124841`) in `GET /device` so the
   unique_id survives IP changes; see "Server-side work" below.
4. Options flow (per entry):
   - notify defaults: priority (default 200), ttl (default 20 s), LED flash
     on/off
   - status page: page id (default `hass`), priority (default 50)
   - resync interval (default 60 s)
   - `allow_pwm` (default **off**): unlocks 1–99 % backlight/LED duty. Off by
     default because this unit's supply audibly whines at intermediate PWM —
     the same reason cfa635d defaults to 0/100.

No discovery initially: cfa635d doesn't advertise itself. Zeroconf is a
server-side addition (below); once it exists, add a `zeroconf` matcher for
`_cfa635d._tcp.local.`.

## Device and entities

One HA device per config entry: manufacturer Crystalfontz, model CFA635,
`sw_version` from `/device` (`CFA635:h1.1,v1.6` + server version).

| Entity | Platform | Backing API | Notes |
|---|---|---|---|
| Backlight | `light` | `PUT /display/backlight` | ON/OFF color mode by default (0/100 only, PWM whine). With `allow_pwm`: brightness mode, 0–100 mapped to duty. State from coordinator, not optimistic. |
| Contrast | `number` | `PUT /display/contrast` | 0–255, `entity_category: config`, default hidden-ish (config section). |
| LED 1–4 | `select` | `PUT /leds/{n}` | Options `off / green / red / amber` → (0,0)/(100,0)/(0,100)/(100,100). With `allow_pwm`, add two `number` entities per LED (green %, red %) instead. These drive the **global** LED state; page-level LED overlays still win while their page is visible, which is exactly the server's model. |
| Active page | `sensor` | coordinator | State = active page id (or `idle`); attributes: `name`, `reason`, `pinned`, `priority`, `expires_in`, `frame` (4 strings from `/display`). |
| Pages | `sensor` | coordinator | State = count of live pages; attribute: list of `{id, name, priority, ttl, visible}`. |
| Connectivity | `binary_sensor` | `GET /health` + WS liveness | `device_class: connectivity`. ON only when HTTP reachable **and** `health.device == "ok"`. |
| Key ×6 | `event` | WS `key` events | One event entity per key (`up down left right enter exit`), event types `press` / `release`, attributes `consumed`, `active_page`. |
| Release display | `button` | `POST /display/release` | Unpins whatever is pinned. |
| Line 1–4 | `text` | integration-owned page | See "Status page" below. `max=20`. |
| Notify | `notify` | `PUT /pages/...` | See "Notifications" below. |

Entity naming follows the modern pattern (`has_entity_name`, device name
"CFA635" from config title).

### Notifications (the headline feature)

`notify.send_message` on the notify entity publishes an **alert page**:

- page id `ha-notify` (fixed; repeat notifications replace, not stack)
- `title` → line 0, `message` word-wrapped into lines 1–3 (20 cols)
- `priority` from options (default 200 ≥ ALERT_PRIORITY, so it preempts pins
  and rotation), `ttl` from options (default 20 s → self-cleans)
- optional LED flash: page-level `leds: {0: {red: 100}}` while visible

This gives automations `action: notify.cfa635` and the message physically
appears on the desk. Messages longer than 3×20 chars are truncated (log a
debug line); a later iteration could paginate.

### Status page (`text` entities)

The integration owns one persistent page (id from options, default `hass`,
priority 50, **no ttl**). Four `text` entities map to its rows via
`PATCH /pages/hass {"lines": {row: text}}`; the page is created lazily on
first non-empty line (PATCH 404 → PUT) and deleted when all four lines are
empty. This is the "dashboard" surface: templates/automations write whatever
they like per-row without composing service calls, and it rotates with other
clients' pages like any citizen.

## Services (for full page control)

Domain services targeting the config entry, thin wrappers over REST — these
expose the entire page model to power users:

| Service | Fields |
|---|---|
| `cfa635.set_page` | `page_id` (required, `[a-z0-9-]{1,32}`), `lines` (1–4 strings), `name?`, `priority?` 0–255, `ttl?`, `leds?` (map 0–3 → `{green, red}`) — maps to PUT (create-or-replace) |
| `cfa635.update_page` | same fields, all optional; `lines` may be a row→text map — maps to PATCH |
| `cfa635.delete_page` | `page_id` |
| `cfa635.activate_page` | `page_id`, `hold?` seconds — maps to `/activate` (pin) |
| `cfa635.release` | — |

All services `translation`'d and schema'd so they're usable from the
automation UI. `set_page` docs should spell out the arbitration rules
(≥100 = alert, equals rotate at 10 s, ttl needs refreshing).

## Automation surface

- **HA bus events**: every WS `key` event fires `cfa635_key`
  (`{key, action, consumed, active_page, entry_id}`); `page_visible` fires
  `cfa635_page_visible` (`{page, previous, reason}`). Bus events + event
  entities cover both YAML and UI users.
- **Device triggers**: "Up pressed", "Enter released", etc., built on the
  event entities, so keys appear directly in the automation editor's device
  picker. This is what makes the 6-key keypad a usable wall-controller:
  ENTER toggles lights, EXIT runs a scene, LEFT/RIGHT change volume
  (UP/DOWN stay server-consumed for page nav by default — `consumed: true`
  events are still fired, so automations *can* use them, but the trigger UI
  should note the server also acts on them).

## Failure handling

- HTTP errors on entity commands → `HomeAssistantError` with the server's
  detail string (e.g. the 422 "firmware v1.6 sets both backlights together").
- WS down or `health.device == "error"` → all entities except Connectivity
  become unavailable; a `repairs` issue is raised after 5 min of continuous
  unavailability pointing at the systemd unit (`deploy/install.md`).
- Server restart wipes pages (in-memory store): on reconnect the integration
  re-PUTs its own pages (status page + any live notify page) from local
  state, then resyncs. Other clients' pages are their own problem.
- **Diagnostics** platform dumps `/health`, `/device`, `/display`, `/pages`,
  `/leds`, redaction-free (nothing sensitive in any of them).

## Repo layout

```
custom_components/cfa635/
  manifest.json  hacs.json (repo root)
  __init__.py          setup/unload, WS task lifecycle
  api.py               Cfa635dClient — REST + WS, no HA imports
  coordinator.py       snapshot + event fan-out
  config_flow.py       user + options steps
  entity.py            base entity (device info, availability)
  light.py number.py select.py sensor.py binary_sensor.py
  event.py button.py text.py notify.py
  services.py services.yaml
  diagnostics.py
  device_trigger.py
  strings.json translations/en.json
tests/                 pytest-homeassistant-custom-component;
                       aioresponses for REST, fake WS server for events
```

Testing mirrors the main repo's philosophy: everything runs hardware-free —
the integration tests never need the device, only a faked cfa635d, and a
`tests/fixtures/` snapshot of real responses keeps the fake honest.

## Server-side work that would help (small, optional)

1. **`GET /device` gains the USB serial** (`CF124841`) → stable unique_id and
   multi-display support done right.
2. **Zeroconf**: advertise `_cfa635d._tcp.local.` with serial + version in
   TXT → discovery flow, zero-config setup.
3. **WS `state` frame on connect** (full pages/display/leds snapshot in
   `hello`) → removes one REST round-trip per reconnect. Nice, not needed.

## Build order

1. **MVP**: api.py, config flow, coordinator + WS listener, backlight light,
   connectivity, active-page sensor, notify entity. (Already useful daily.)
2. Key event entities + bus events + device triggers.
3. Services, LED selects, contrast number, release button.
4. Status-page text entities, options flow, diagnostics, repairs.
5. HACS polish: hacs.json, brands PR (logo), README with automation recipes.
