# Real-hardware smoke test

Run against the live device, in order. `websocat` and `jq` optional but
recommended. Everything here also works in the simulator
(`CFA635_SIM=1`, watch at `/sim`) **except the steps marked [HW]**, which
exist precisely because the simulator cannot verify them.

```sh
uv run cfa635-probe                    # 0. read-only sanity: CFA635:h1.1,v1.6
uv run cfa635-server &                 # 1. or: systemctl start cfa635d
curl -s :8635/health                   #    {"status":"ok","api":1,"device":"ok",...}
curl -s :8635/device                   # 2. version/port/serial/contrast/backlight
                                       #    [HW] serial should read "CF124841"

# 3. register once; all mutations below need the token
T=$(curl -s -X POST :8635/clients -H 'content-type: application/json' \
    -d '{"name":"smoke"}' | jq -r .token)
A="authorization: Bearer $T"

websocat "ws://localhost:8635/ws?token=$T" &   # 4. hello = full snapshot;
                                               #    watch seq on every event

# 5. a page appears on the glass; markup renders gapless
curl -s -X PUT :8635/pages/hello -H "$A" -H 'content-type: application/json' \
     -d '{"lines":["Hello{fill}ok","{bar:0.6:20}","{spark:0.2,0.5,0.9,0.4}","{up}{down}{check}{cross}{bell}{lock}"]}'
#    [HW] bar/spark cells must be contiguous (no vertical gaps between cells)
#    [HW] icons legible; note any that Figure 11 shows in CGROM, then move
#         them from markup.ICONS bitmaps to charmap codes (zero-slot)
#    [HW] if glyphs render mirrored/garbled the CGRAM row bit-order
#         assumption is wrong: fix markup.py's bitmaps + sim.py together

# 6. a second page starts 10 s rotation; watch page_visible events
curl -s -X PUT :8635/pages/second -H "$A" -H 'content-type: application/json' \
     -d '{"lines":["page two","{spin} spinning"],"ttl":120,"duration":5}'
#    'second' should hold the glass only ~5 s per turn (duration override);
#    the spinner must animate with zero flicker (bitmap redefinition)

# 7. keypad, browse layer: UP/DOWN pins for 30 s, then rotation resumes

# 8. the shell: press EXIT -> switcher (pages + Settings row); UP/DOWN,
#    ENTER pins; ENTER on Settings -> adjust backlight/contrast LEFT/RIGHT
#    [HW] values change visibly and instantly; close the shell (EXIT), then
#    restart the daemon: the shell-set values must survive (user flash)

# 9. focus: publish an interactive page, keep the websocat from step 4 open
curl -s -X PUT :8635/pages/dim -H "$A" -H 'content-type: application/json' \
     -d '{"lines":["Dimmer","{bar:0.5:20}"],"interactive":true,
          "cursor":{"row":0,"col":0,"style":"invert"}}'
#    press ENTER on the unit: LED 0 goes solid green, every key (incl. EXIT)
#    arrives on the socket as routed events with a generation
#    [HW] cursor style "invert" shows a blinking inverse-video cell
curl -s -X POST :8635/pages/dim/focus/release -H "$A"   # LED 0 off, browse back

# 10. alert preempts immediately; LED blinks server-side
curl -s -X PUT :8635/pages/alert -H "$A" -H 'content-type: application/json' \
     -d '{"lines":["{bell} ALERT"],"priority":200,
          "leds":{"0":{"red":100,"mode":"blink"}}}'
#    press UP to view another page: LED 0 must blink red (hidden-alert
#    indicator) until the alert is visible again
curl -s -X DELETE :8635/pages/alert -H "$A"     # back to rotation, LED restored

# 11. ownership: a second client cannot touch our pages
T2=$(curl -s -X POST :8635/clients -H 'content-type: application/json' \
     -d '{"name":"intruder"}' | jq -r .token)
curl -s -X DELETE :8635/pages/hello -H "authorization: Bearer $T2" # -> 403

curl -s :8635/display                  # 12. the literal frame on the glass
curl -s -X DELETE :8635/clients/$(curl -s :8635/clients | jq -r '.[0].id') \
     -H "$A"                           # 13. cascade: our pages vanish, idle screen
```

Charmap verification [HW]: `PUT /pages/charmap` with lines containing
`$ @ [ ] \ ^ _ ` { | } ~` and `ä ö ü Ä Ö Ü ñ Ñ § ¿ ¡ ° ² ³` and compare the
glass against `src/cfa635/charmap.py`.

One-time deploy nicety [HW]: `uv run cfa635-probe --store-boot-state`
writes a "cfa635d starting..." splash as the module's power-on state —
verify by power-cycling with the daemon stopped.
