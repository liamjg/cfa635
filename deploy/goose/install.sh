#!/bin/sh
# Install the cfa635 status plugin for goose.
#
#   CFA635_URL=http://192.168.99.20:8635 deploy/goose/install.sh
#
# Idempotent. Vendors the two stdlib-only modules the hook needs, so the
# target host needs neither pyserial nor a venv — just python3.
set -eu

PLUGIN_DIR="${GOOSE_PLUGIN_DIR:-$HOME/.agents/plugins/cfa635-status}"
SRC="$(cd "$(dirname "$0")/../../src" && pwd)"
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "installing to $PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR/hooks" "$PLUGIN_DIR/scripts" "$PLUGIN_DIR/vendor/cfa635/goose"

cp "$HERE/cfa635-status/hooks/hooks.json"        "$PLUGIN_DIR/hooks/"
cp "$HERE/cfa635-status/scripts/cfa635_status.py" "$PLUGIN_DIR/scripts/"
chmod +x "$PLUGIN_DIR/scripts/cfa635_status.py"

# The hook path imports only these; cfa635/__init__.py re-exports the driver
# lazily, so vendoring it does not drag in pyserial.
cp "$SRC/cfa635/__init__.py"        "$PLUGIN_DIR/vendor/cfa635/"
cp "$SRC/cfa635/client.py"          "$PLUGIN_DIR/vendor/cfa635/"
cp "$SRC/cfa635/goose/__init__.py"  "$PLUGIN_DIR/vendor/cfa635/goose/"
cp "$SRC/cfa635/goose/status.py"    "$PLUGIN_DIR/vendor/cfa635/goose/"

# goose runs hooks with its own environment, so CFA635_URL will not reach the
# hook unless it was exported before goose started. Persist it where the hook
# looks (see resolve_url in cfa635/goose/status.py).
if [ -n "${CFA635_URL:-}" ]; then
    CONF="${XDG_CONFIG_HOME:-$HOME/.config}/cfa635"
    mkdir -p "$CONF"
    printf '%s\n' "$CFA635_URL" > "$CONF/url"
    echo "display: $CFA635_URL  (recorded in $CONF/url)"
else
    echo "note: CFA635_URL unset; the hook will default to http://127.0.0.1:8635"
fi

echo "verifying the hook is silent and exits 0..."
echo '{"event":"SessionStart","session_id":"install-check"}' \
    | "$PLUGIN_DIR/scripts/cfa635_status.py" > /tmp/cfa635-hook-out 2>/dev/null
test ! -s /tmp/cfa635-hook-out || { echo "FAIL: hook wrote to stdout"; exit 1; }
rm -f /tmp/cfa635-hook-out
echo "ok - restart any running goose session to load the plugin"
