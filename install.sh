#!/usr/bin/env bash
# Install the strata web monitor as an Ubuntu 24.04 systemd daemon.
#   sudo ./install.sh
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/strata-monitor
CONF_DIR=/etc/strata-monitor
UNIT=strata-monitor.service

[[ $EUID -eq 0 ]] || { echo "run me as root:  sudo $0" >&2; exit 1; }

if ! python3 -c 'import psutil' 2>/dev/null; then
    echo "installing python3-psutil ..."
    apt-get update -qq
    apt-get install -y -qq python3-psutil
fi

install -d -m 0755 "$APP" "$APP/static" "$CONF_DIR"
install -m 0644 "$SRC"/src/*.py "$APP/"
install -m 0644 "$SRC"/src/static/* "$APP/static/"
chmod 0755 "$APP/monitor.py"

if [[ -f "$CONF_DIR/config.json" ]]; then
    echo "keeping existing $CONF_DIR/config.json"
else
    install -m 0644 "$SRC/config.json" "$CONF_DIR/config.json"
    echo "installed default config -> $CONF_DIR/config.json"
fi

install -m 0644 "$SRC/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT"
sleep 1.5

echo
systemctl --no-pager -l status "$UNIT" | sed -n '1,12p' || true
PORT="$(python3 -c "import json;print(json.load(open('$CONF_DIR/config.json')).get('port', 8090))")"
HOSTN="$(hostname -f 2>/dev/null || hostname)"
echo
echo "  dashboard:  https://${HOSTN}:${PORT}/   (self-signed cert auto-created"
echo "              in /var/lib/strata-monitor/tls/; set tls_cert/tls_key in"
echo "              config.json for a real CA cert)"
echo "  config:     $CONF_DIR/config.json   (restart: systemctl restart $UNIT)"
echo "  logs:       journalctl -u $UNIT -f"
