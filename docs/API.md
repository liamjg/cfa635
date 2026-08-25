# cfa635d API

HTTP + WebSocket on port 8635 (`CFA635_HTTP_PORT`). One daemon owns the
CFA635 display and arbitrates it between any number of network clients.

**API version: 1** (reported in `/health` and the WebSocket hello).

## Identity and authorization

Every *mutation* requires a registered client. Reads are open.

```
POST /clients {"name": "my-app"}       -> 201 {"id": "c-…", "token": "…", "name": "my-app"}
```

The token is returned **exactly once** — store it. Send it on every mutation:

```
Authorization: Bearer <token>
```

- `401` — missing/unknown token (the server may have restarted: re-register).
- `403` — valid token, but the resource belongs to another client.
- Tokens are arbitration identity on a trusted LAN, **not** a security
  boundary: there is no TLS and reads are unauthenticated.
- State is in-memory. After a server restart, re-register and re-publish.
- Registering the same `name` again creates a *new* identity; the old
  client's pages linger until their TTLs expire. Set TTLs, or `DELETE
  /clients/{id}` on clean shutdown (which removes all your pages).

```
GET  /clients                 -> [{"id", "name", "last_seen_ago", "pages": [...]}]  (never tokens)
DELETE /clients/{id}          -> 204; self only; deletes your pages, releases your pin
```

## Pages

A page is a virtual 4×20 screen. The server decides what is on the glass:
highest priority wins, equals rotate (default 10 s), `priority >= 100` is an
alert and preempts everything.

```
GET    /pages                  -> {"pages": [...], "active", "pinned"}
PUT    /pages/{id}             -> 201 create / 200 replace (owner only)
PATCH  /pages/{id}             -> partial update; {} is a pure TTL heartbeat
GET    /pages/{id}
DELETE /pages/{id}             -> 204
POST   /pages/{id}/activate    {"hold": secs?}  -> pin to the glass
POST   /display/release        -> clear the pin
```

Page ids: `[a-z0-9-]{1,32}`, global namespace, first-writer owns.

**Built-in pages.** The server publishes two of its own, owned by the
reserved id `system`, so `clock` and `info` are taken: writing to either
returns 403 like any other client's page.

| id | priority | what |
|---|---|---|
| `clock` | 10 | the resting screen: `{big:HH:MM}`, ticking seconds, date footer |
| `info` | 5 | hostname + version, `IP:port`, uptime + client count, display firmware |

Both sit below the client default (50), so anything you publish takes the
glass and they return when it goes. `info` sits below `clock` so it never
rotates in on its own — reach it from the shell switcher or the nav keys.
They appear in `GET /pages` and rotate/pin like any other page, but they do
not count as activity: the idle backlight timer still runs while one is up.
Disable either with `CFA635_CLOCK=0` / `CFA635_INFO=0`; the clock's
`strftime` formats are `CFA635_CLOCK_TIME_FMT` and `CFA635_CLOCK_DATE_FMT`
(the date format may contain `{fill}`, e.g. `%a %d %b{fill}%Y`), and
`CFA635_CLOCK_SECONDS=0` drops the seconds.

`PUT` body:

```json
{
  "lines": ["up to 4 rows of text"],
  "name": "human label",
  "priority": 50,
  "ttl": 30.0,
  "duration": 15.0,
  "leds": {"0": {"green": 100, "red": 0, "mode": "blink", "hz": 1}},
  "interactive": false
}
```

- `ttl` — page vanishes this many seconds after its last PUT/PATCH.
  The only garbage collection; use it for anything that should not
  outlive its publisher.
- `duration` — this page's own rotation dwell (seconds); the global
  `rotation_secs` applies when unset.
- `leds` — overlay on the physical LEDs *while this page is visible*
  (page LEDs may use `mode`/`hz` like global LEDs, below).
- `interactive` — this page can take keypad focus (below).
- Replacing your own page keeps its creation time (and rotation slot).
- `PATCH` accepts `lines` as a full list or `{"1": "row one only"}`.

**Pins**: `activate` pins are held by your client; the keypad's UP/DOWN
pins are held by "keypad". `POST /display/release` succeeds if you hold
the pin *or* you own the pinned page — otherwise 403. All pins self-expire
(default 30 s).

## Markup (v1)

Page lines may contain tokens, expanded server-side at render time. The
display's cells are contiguous in both axes, so meters are gapless.

| Token | Renders | Glyph slots |
|---|---|---|
| `{bar:0.65:12}` | horizontal bar, fraction × width cells, 6 px/cell | ≤5 partials + full |
| `{vbar:0.6}` | one-cell vertical fill, 8 levels | ≤7 (shared family) |
| `{spark:0.2,0.7,…}` | one vbar cell per sample | ≤7 (shared family) |
| `{chart:…:rows=2}` | multi-row fill chart, 8·N levels — the widget owns the cells it covers on the rows below | ≤7 (shared family) |
| `{big:09:47}` | seven-segment characters 3 rows tall, 3 cells per digit — the widget owns the cells it covers on the two rows below | 8 (the whole font) |
| `{spin}` | one-cell spinner, bitmap-animated ~4 Hz | 1 |
| `{hr}` | solid rule filling the remaining width | 1 |
| `{fill}` `{fill:.}` | expands to consume leftover space: `CPU{fill}42%` right-justifies, `{fill}T{fill}` centers, `{fill:.}` dot leaders | 0 |
| `{scroll}` | line prefix: overflowing text marquees ~2 Hz | 0 |
| `{blink:TEXT}` | text alternates with blanks at 1 Hz | 0 |
| `{up} {down} {left} {right} {check} {cross} {bell} {lock} {unlock}` | icons | ≤1 each |

- **Budget: 8 distinct custom glyphs per frame**, counted on the bitmaps
  actually used. `{vbar}`/`{spark}`/`{chart}` share one family of row-fill
  partials; `{bar}` has its own column-fill family. Over budget, each
  widget degrades per-cell to a documented ASCII fallback (`=` fills,
  ` .:-=#` ramps, `|/-\` spinner, mnemonic icons) — never an error.
- `{{` renders a literal `{`. Unknown/malformed tokens render literally
  (through the CGROM character map). Lines never fail validation.
- Selection and buttons are *conventions*: a `>` marker (or the hardware
  `cursor` field) for the selected row; `[ OK ]` for buttons. Zero slots,
  works over any text.

**Hardware cursor**: pages may set
`"cursor": {"row": 0-3, "col": 0-19, "style": "none"|"block"|"underscore"|"block_underscore"|"invert"}`
— applied while the page is visible, cleared otherwise. `invert` is a
hardware inverse-video blinking block: the zero-slot highlight for PIN
entry and field editing.

**Big characters**: `{big:...}` draws digits 18 x 24 px, as three rows of
three cells. One blank column separates adjacent digits (their 2 px
verticals would otherwise merge); `:` and any character with no big form
take a single column and render normally on the *middle* row, i.e.
vertically centred against the digits — which is what makes the colon in
`{big:09:47}` land dead centre for free, and what lets `{big:9 AM}` work.
`{big:HH:MM}` is 15 columns wide.

The font is 3 rows tall rather than 2 because that is what fits: the ten
digits then share exactly 8 bitmaps (four bars/verticals and their four
corners), so no value can exhaust CGRAM mid-frame. A 2-row font needs 9-11.
The flip side is that the budget is fully spent — nothing else on the frame
gets a slot, and `{big:}` itself degrades to the plain string on its top
row if something else claimed the slots first.

*Planned (v1.1, not yet implemented)*: `{inv:text}` inverted spans (short
designed spans only — one slot per distinct character), sliders/scrollbars,
battery/wifi levels, more icons.
*Planned live tokens*: `{ago:epoch}`, `{countdown:epoch}`.

## Keypad input: browse and focus

Keys resolve through layers; a layer either consumes a key or lets it fall
through:

1. **Focus** (when held) — all six keys route to the focused page's owner.
2. **Browse** — universal bindings: `UP`/`DOWN` rotate & pin (30 s),
   `ENTER` focuses the visible page if it is `interactive` *and* its owner
   has an authenticated WebSocket connected.
3. Everything else is broadcast to observers as an unconsumed `key` event.

While focused, the owner receives every key as
`{"type": "key", "routed": true, "page", "key", "action", "generation"}`
on its authenticated socket only — observers see none of them — and the
page stays pinned. Ignore routed events whose `generation` differs from
your current focus (they are stale).

Focus ends when any of these happens (the owner gets
`{"type": "focus", "state": "lost", "reason": …}`):

- `released` — you called `POST /pages/{id}/focus/release`
  (convention: do this when EXIT is pressed at your UI's top level);
- `timeout` — no keypress for `focus_hold_secs` (default 30);
- `disconnect` — your WebSocket dropped;
- `preempted` — an alert took the glass, or the page went away.

A human can therefore never be trapped in a focused page.

## Display & LEDs

```
GET /display                   -> {"active", "pinned", "reason", "rotation_secs", "frame": [4 strings]}
GET /device                    -> {"version", "port", "serial", "sim", "contrast", "backlight"}
GET /health                    -> {"status", "api", "device", "error"}
GET /leds                      -> {"0": {"green", "red", "mode", "hz", "owner"}, ...}
PUT /leds                      / PUT /leds/{n}
    {"green": 0-100, "red": 0-100, "mode": "solid"|"blink"|"pulse", "hz": <=2}
```

**Backlight and contrast are not API surface** — they are device-local
settings adjusted at the shell's Settings screen on the unit itself
(current values are still readable via `GET /device`).

`device.serial` is the module's USB serial (e.g. `CF124841`): a stable
identity for integrations that survives IP/port changes, and the reserved
extension point for multi-display support (a future `display` field on
pages would select among serials; today one daemon drives one display).

## The on-device shell

Pressing **EXIT** (in browse mode) opens the server's own UI, which
consumes all keys while open (`key` events carry `layer: "shell"`):

- **Switcher** — every page by name with badges (P pinned, ! alert,
  * visible) plus a trailing **Settings** row. UP/DOWN highlight, ENTER
  pins and closes, EXIT closes. Auto-closes after 30 s.
- **Settings** — backlight (0–100, step 10) and contrast (60–160, step 5),
  applied live and persisted to the module's 16-byte user flash when the
  shell closes; a stored record beats `CFA635_*` env defaults on startup.

`shell` events (`{"open": true|false}`) mark it opening and closing.

**LED ownership.** LED 0 (top) is the system indicator and cannot be
written. LEDs 1–3 are ambient indicators: your first write claims one,
after which only you can write it (`403` otherwise); deleting your client
releases and darkens it. Two idioms:

- *Ambient role*: a claimed global LED as a long-lived, across-the-room
  signal ("heating on", "door unlocked").
- *Row annotation*: the four LEDs sit one beside each display row — a
  page's `leds` overlay can severity-color its own lines while visible.

`mode: "blink"|"pulse"` animates server-side (survives the client dying —
usually what you want from an alarm). Precedence per LED: system indicator
(LED 0) → visible page overlay → global claim.

## Events (WebSocket)

```
ws://host:8635/ws              observer: all broadcast events
ws://host:8635/ws?token=…      authenticated: broadcasts + events addressed to you
```

An invalid token closes the socket with code **4401**. The stream is
send-only. The first frame is a **full state snapshot**:

```json
{"type": "hello", "api": 1, "seq": 41, "client": "c-…|null",
 "pages": [...], "active": "…", "pinned": null, "reason": "rotation",
 "leds": {...}, "backlight": 100, "contrast": 120,
 "device": {"version": "CFA635:h1.1,v1.6", "healthy": true}}
```

Then events:

- `key` — broadcast: `{"key", "action", "consumed", "active_page"}`;
  routed (owner-only, during focus): `{"key", "action", "routed": true,
  "page", "generation"}`
- `focus` — owner-only: `{"page", "state": "gained"|"lost", "reason",
  "generation"}`
- `page_visible` — `{"page", "previous", "reason"}`
  (`reason` ∈ `idle|alert|nav|rotation|activate`)
- `page_removed` — `{"page", "reason": "expired"|"deleted"|"client_deleted"}`

The hello snapshot includes `focus: {"page", "generation"} | null`.

**Sequence numbers**: every event carries a monotonically increasing `seq`
(the hello carries the current value). Per-connection queues are bounded and
drop oldest under pressure — if you observe a gap in `seq`, you lost events:
resync via REST.

## Panel — a live mirror of the physical interface

`GET /panel` is always available and serves a visual twin of the device
face (geometry per the datasheet's mechanical drawings): the same rows,
custom glyphs, LEDs, cursor, and backlight the glass is showing *right
now*, plus a clickable keypad (arrow/Enter/Esc keys mapped) that feeds the
same input pipeline as the physical buttons. On a deployed daemon this is
the display in a browser tab — page rotation, the shell, focus mode, and
alerts all appear and can be driven remotely.

```
GET  /panel        the visual panel (status line shows "hardware mirror"
                   or "simulated device")
GET  /panel/state  raw mirror state: rows (byte codes), cgram, cursor,
                   leds, backlight, contrast, sim
POST /panel/key    {"key": "up", "action": "tap"|"press"|"release"}
                   — injected exactly like a physical key press
```

Key injection is unauthenticated by design: LAN reachability is treated as
presence at the device, and the web keypad can do exactly what the
physical one can — nothing more. (`/sim` redirects to `/panel`.)

### Simulator mode

Run without hardware:

```
CFA635_SIM=1 cfa635-server
```

The server drives the real driver/renderer against an in-process device
model — the fake sits where the serial port would, and stores exactly what
the byte stream writes. Everything else, including `/panel`, behaves
identically.

**Fidelity contract** (applies to the panel in both modes): the *content*
is byte-identical to the device state — the same 80 DDRAM character codes,
CGRAM bitmaps, LED duties, cursor, and backlight, produced by the
identical server → driver → protocol path. Only the *drawing*
approximates: ROM characters use a lookalike 5×7 font (CGRAM glyphs are
rendered from their actual programmed bitmaps), the rare CGROM/ASCII
divergences (e.g. `¤`) show their ASCII lookalikes, and the `invert`
cursor style draws as a plain blinking block. Anything the datasheet
mis-documents would make the mirror and the driver wrong *together* — the
`[HW]` steps in `tests/smoke.md` exist to catch exactly that on real
glass.
