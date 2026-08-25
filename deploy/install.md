# Installing cfa635d

The service runs as a dedicated `cfa635` system user (no login, no
password) from a root-owned copy of the repo in `/opt/cfa635`, so it
starts at boot with nobody logged in. The working copy stays in
`~/dev/cfa635`; the installer copies it to `/opt` and builds the venv
there.

```sh
sudo sh deploy/install.sh      # from ~/dev/cfa635
curl -s localhost:8635/health
```

The script is idempotent — after pulling changes into `~/dev/cfa635`,
re-run it to redeploy.

What it does:

1. Creates the `cfa635` system user (home `/var/lib/cfa635`). Serial
   access is granted per-unit via `SupplementaryGroups=dialout`
   (tty devices are `root:dialout 0660` by default), not by editing the
   group database.
2. Copies the repo to `/opt/cfa635` (excluding `.git`/`.venv`) and runs
   `uv sync --frozen` there.
3. Installs `99-crystalfontz.rules` → stable `/dev/cfa635` symlink and a
   systemd device unit.
4. Installs `cfa635d.service`, stops any manually-started server, then
   enables + starts the service.

The service stops when the display is unplugged and restarts when it
reappears (`BindsTo` the udev-tagged device unit).

## Configuration

Set `CFA635_*` environment variables via `sudo systemctl edit cfa635d`
(see `src/cfa635/server/config.py` for the full list and defaults):
`CFA635_PORT`, `CFA635_HTTP_HOST`, `CFA635_HTTP_PORT` (8635),
`CFA635_ROTATION_SECS`, `CFA635_NAV_HOLD_SECS`, `CFA635_NAV_KEYS`,
`CFA635_BACKLIGHT`, `CFA635_IDLE_BACKLIGHT`, `CFA635_IDLE_DIM_SECS`,
`CFA635_CONTRAST`.

## Optional tuning

The FTDI latency timer defaults to 16 ms, which is the floor on keypress
round-trip latency. If key response feels sluggish:

```sh
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

(or add `ATTR{device/latency_timer}="1"` to the udev rule to make it stick).
