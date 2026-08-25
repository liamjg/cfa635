#!/bin/sh
# Idempotent installer for cfa635d. Run as root: sudo sh deploy/install.sh
# Copies the repo to /opt/cfa635 (the working copy in /home is 0700,
# unreadable by the service user), builds the venv there, and installs
# the udev rule + system unit running as the `cfa635` user.
set -eu

SRC=/home/liam/dev/cfa635
UV=/home/liam/.local/bin/uv
DEST=/opt/cfa635

[ "$(id -u)" = 0 ] || { echo "run as root: sudo sh $0" >&2; exit 1; }

getent passwd cfa635 >/dev/null || \
    adduser --system --group --home /var/lib/cfa635 cfa635

mkdir -p "$DEST"
# '._*' and .DS_Store ride along from macOS copies; .pytest_cache is test
# scratch. The tar extracts over the destination without deleting, so also
# prune anything already there from an earlier copy.
(cd "$SRC" && tar --exclude=.git --exclude=.venv --exclude='__pycache__' \
    --exclude='._*' --exclude=.DS_Store --exclude=.pytest_cache -cf - .) \
    | tar -xf - -C "$DEST"
find "$DEST" -path "$DEST/.venv" -prune -o \
    \( -name '._*' -o -name .DS_Store \) -print0 2>/dev/null | xargs -0r rm -f
rm -rf "$DEST/.pytest_cache"
(cd "$DEST" && "$UV" sync --frozen)

install -m 644 "$DEST/deploy/99-crystalfontz.rules" /etc/udev/rules.d/
udevadm control --reload
udevadm trigger --subsystem-match=tty

install -m 644 "$DEST/deploy/cfa635d.service" /etc/systemd/system/
systemctl daemon-reload

# stop any previous copy (manual or service) so the port and :8635 are free
systemctl stop cfa635d 2>/dev/null || true
pkill -f cfa635-server 2>/dev/null || true

systemctl enable --now cfa635d
sleep 2
systemctl --no-pager --lines=5 status cfa635d
