#!/usr/bin/env bash
# Uninstall the strata web monitor (reverse of install.sh).
#   sudo ./uninstall.sh [--purge] [--purge-deps] [--keep-config] [--dry-run]
# Default: stop+disable unit, remove app + config; KEEP /var/lib/strata-monitor.
set -euo pipefail

APP=/opt/strata-monitor
CONF_DIR=/etc/strata-monitor
STATE_DIR=/var/lib/strata-monitor
UNIT=strata-monitor.service

PURGE=0 PURGE_DEPS=0 KEEP_CONFIG=0 DRYRUN=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    --purge-deps) PURGE_DEPS=1 ;;
    --keep-config) KEEP_CONFIG=1 ;;
    --dry-run) DRYRUN=1 ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run me as root:  sudo $0" >&2; exit 1; }

run() { if [[ $DRYRUN -eq 1 ]]; then echo "DRY: $*"; else "$@"; fi; }

run systemctl disable --now "$UNIT" 2>/dev/null || true
run rm -f "/etc/systemd/system/$UNIT"
run rm -rf "/etc/systemd/system/$UNIT.d"
run systemctl daemon-reload
run systemctl reset-failed "$UNIT" 2>/dev/null || true

run rm -rf "$APP"

if [[ $KEEP_CONFIG -eq 1 ]]; then
  echo "keeping $CONF_DIR"
else
  run rm -rf "$CONF_DIR"
fi

if [[ $PURGE -eq 1 ]]; then
  echo "purging data: $STATE_DIR"
  run rm -rf "$STATE_DIR"
else
  echo "kept data: $STATE_DIR   (re-add with --purge to delete)"
fi

if [[ $PURGE_DEPS -eq 1 ]]; then
  run apt-get remove -y python3-psutil
fi

echo
echo "done. manual finish (not automated):"
echo "  nginx:  sudo rm /etc/nginx/sites-{available,enabled}/strata-monitor && sudo nginx -t && sudo systemctl reload nginx"
echo "  ufw:    sudo ufw delete allow 8443/tcp   (or whichever port you opened)"
echo "  logs:   sudo journalctl --rotate && sudo journalctl --vacuum-time=1s   (optional)"
