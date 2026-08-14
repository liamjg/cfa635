# Real-hardware smoke test

Run against the live device, in order. `websocat` optional but recommended.

```sh
uv run cfa635-probe                    # 0. read-only sanity: CFA635:h1.1,v1.6
uv run cfa635-server &                 # 1. or: systemctl start cfa635d
curl -s :8635/health                   #    {"status":"ok","device":"ok",...}
curl -s :8635/device                   # 2. version/port/contrast/backlight
websocat ws://localhost:8635/ws &      # 3. hold open: press keys, see events

# 4. a page appears on the glass
curl -s -X PUT :8635/pages/hello -H 'content-type: application/json' \
     -d '{"lines":["Hello","from cfa635d"]}'

# 5. a second page starts 10 s rotation; watch page_visible events
curl -s -X PUT :8635/pages/second -H 'content-type: application/json' \
     -d '{"lines":["page two"],"ttl":60}'

# 6. press UP/DOWN on the unit: nav pin for 30 s, then rotation resumes

# 7. alert preempts immediately, top LED goes red
curl -s -X PUT :8635/pages/alert -H 'content-type: application/json' \
     -d '{"lines":["!! ALERT !!"],"priority":200,"leds":{"0":{"red":100}}}'

curl -s -X DELETE :8635/pages/alert    # 8. back to rotation, LED restored

# 9. wait for 'second' to expire (TTL 60) -> page_removed event

curl -s -X PUT :8635/display/backlight -H 'content-type: application/json' \
     -d '{"lcd":40}'                   # 10. visibly dims

curl -s :8635/display                  # 11. shows the literal frame on the glass
curl -s -X DELETE :8635/pages/hello    # 12. idle page (hostname/IP/clock)
```

Charmap verification: `PUT /pages/charmap` with lines containing
`$ @ [ ] \ ^ _ ` { | } ~` and `ä ö ü Ä Ö Ü ñ Ñ § ¿ ¡ ° ² ³` and compare the
glass against `src/cfa635/charmap.py`.
