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

# nginx reverse-proxy templates (HTTPS). Not applied automatically: copy one to
# /etc/nginx/sites-available/, edit the cert paths, then reload nginx.
#   strata-monitor.nginx.conf          -> dedicated host on 443
#   strata-monitor.nginx-port8443.conf -> extra site on port 8443 (no new DNS)
if compgen -G "$SRC/deploy/*.conf" > /dev/null; then
    install -m 0644 "$SRC"/deploy/*.conf "$CONF_DIR/"
    echo "nginx templates -> $CONF_DIR/strata-monitor.nginx*.conf (edit cert paths)"
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
echo "  dashboard:  https://${HOSTN}/   (via nginx; see $CONF_DIR/strata-monitor.nginx.conf)"
echo "  local only: http://127.0.0.1:${PORT}/   (the monitor binds loopback HTTP)"
echo "  config:     $CONF_DIR/config.json   (restart: systemctl restart $UNIT)"
echo "  logs:       journalctl -u $UNIT -f"
