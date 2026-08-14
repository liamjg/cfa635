# Installing cfa635d

```sh
cd /home/liam/dev/cfa635
uv sync                                            # build the venv + entry points
sudo cp deploy/99-crystalfontz.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger   # creates /dev/cfa635
sudo cp deploy/cfa635d.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cfa635d
curl -s localhost:8635/health
```

The service stops when the display is unplugged and restarts when it
reappears (BindsTo the udev-tagged device unit).

## Configuration

Set `CFA635_*` environment variables in the unit (see
`src/cfa635/server/config.py` for the full list and defaults):
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
