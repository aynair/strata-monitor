#!/usr/bin/env bash
# Install the strata web monitor as an Ubuntu 24.04 systemd daemon.
#   sudo ./install.sh [--https-port N] [--yes] [--no-ufw]
# Firewall: checks ufw for the nginx HTTPS port (default 8443) and offers to
# open it. The monitor's own 8090 is loopback-only and is never opened.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP=/opt/strata-monitor
CONF_DIR=/etc/strata-monitor
UNIT=strata-monitor.service

HTTPS_PORT=8443 UFW_YES=0 UFW_SKIP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --https-port) HTTPS_PORT="${2:?--https-port needs a value}"; shift 2 ;;
    --https-port=*) HTTPS_PORT="${1#*=}"; shift ;;
    --yes) UFW_YES=1; shift ;;
    --no-ufw) UFW_SKIP=1; shift ;;
    -h|--help) sed -n '2,5p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

maybe_open_firewall() {
  local port="$1"
  [[ $UFW_SKIP -eq 1 ]] && { echo "firewall: skipped (--no-ufw)"; return; }
  command -v ufw >/dev/null || { echo "firewall: ufw not installed; skipping"; return; }
  local status; status="$(ufw status 2>/dev/null || true)"
  grep -qi '^Status: *active' <<<"$status" \
      || { echo "firewall: ufw inactive; nothing to do"; return; }
  local already=0
  grep -Eq "(^|[^0-9])${port}(/tcp)?([^0-9]|$)" <<<"$status" && already=1
  if [[ $already -eq 0 && ( "$port" = 443 || "$port" = 80 ) ]] \
      && grep -Eiq 'nginx' <<<"$status"; then already=1; fi
  if [[ $already -eq 1 ]]; then echo "firewall: ${port}/tcp already allowed"; return; fi
  if [[ $UFW_YES -eq 1 ]]; then
    ufw allow "${port}/tcp"; echo "firewall: allowed ${port}/tcp"
  elif [[ -t 0 ]]; then
    local a
    read -r -p "firewall: ufw allow ${port}/tcp? [y/N] " a
    if [[ $a =~ ^[Yy] ]]; then ufw allow "${port}/tcp"; echo "firewall: allowed ${port}/tcp"
    else echo "firewall: skipped; manual: sudo ufw allow ${port}/tcp"; fi
  else
    echo "firewall: not allowed; manual: sudo ufw allow ${port}/tcp"
  fi
}

[[ $EUID -eq 0 ]] || { echo "run me as root:  sudo $0" >&2; exit 1; }

if ! python3 -c 'import psutil' 2>/dev/null; then
    echo "installing python3-psutil ..."
    apt-get update -qq
    apt-get install -y -qq python3-psutil
fi

# migrate away from the old flat layout (monitor.py directly in $APP)
rm -f "$APP"/*.py
rm -rf "$APP/static" "$APP/__pycache__"

install -d -m 0755 "$APP/src" "$APP/src/static" "$CONF_DIR"
install -m 0644 "$SRC"/src/*.py "$APP/src/"
install -m 0644 "$SRC"/src/static/* "$APP/src/static/"
chmod 0755 "$APP/src/monitor.py"

if [[ -f "$CONF_DIR/config.json" ]]; then
    # upgrade shipped defaults in place: a value that still equals the old
    # shipped default is not a user customization and may be replaced;
    # anything else the admin changed is kept as-is
    python3 - "$CONF_DIR/config.json" "$SRC/config.json" <<'PY'
import json, sys

cur_path, new_path = sys.argv[1], sys.argv[2]
with open(cur_path) as f:
    cur = json.load(f)
with open(new_path) as f:
    new = json.load(f)

OLD_DEFAULTS = {"unit": "strata.service"}
changed, added = [], []
for k, v in new.items():
    if k in cur:
        if cur[k] == OLD_DEFAULTS.get(k) and cur[k] != v:
            cur[k] = v
            changed.append("%s: %s -> %s" % (k, OLD_DEFAULTS[k], v))
    else:
        cur[k] = v
        added.append(k)
if changed or added:
    import shutil
    shutil.copy2(cur_path, cur_path + ".bak")
    with open(cur_path, "w") as f:
        json.dump(cur, f, indent=2)
        f.write("\n")
    for c in changed:
        print("config migrated: %s (backup: %s.bak)" % (c, cur_path))
    for a in added:
        print("config added key: %s" % a)
else:
    print("keeping existing %s (nothing to migrate)" % cur_path)
PY
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

if compgen -G "/etc/systemd/system/$UNIT.d/*.conf" > /dev/null; then
    echo "note: drop-ins in /etc/systemd/system/$UNIT.d/ override the shipped"
    echo "      unit file (e.g. ExecStart); the shipped file IS reinstalled:"
    ls -1 "/etc/systemd/system/$UNIT.d/" | sed 's/^/        /'
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

echo
echo "  firewall:   checking ufw for the nginx HTTPS port ${HTTPS_PORT}/tcp"
echo "              (the monitor's own 8090 is loopback-only and is not opened)"
maybe_open_firewall "$HTTPS_PORT"
