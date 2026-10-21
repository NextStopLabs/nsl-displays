#!/bin/bash
# nsl-set-unit — personalize a golden-image front clone for its bus.
# Usage: sudo nsl-set-unit bus-2
# Updates hostname, controller name/AP name, service env, then reboots.
# Pair AFTER this (each bus gets its own code).
set -euo pipefail
NOREBOOT=0
for a in "$@"; do [ "$a" = "--no-reboot" ] && NOREBOOT=1; done
ARGS="$(echo " $* " | sed 's/ --no-reboot / /g')"
set -- $ARGS
UNIT="${1:?usage: sudo nsl-set-unit <unit> [--no-reboot]   e.g. sudo nsl-set-unit bus-2}"
if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi
ROLE="$(cat /etc/nsl-role 2>/dev/null || echo front)"

hostnamectl set-hostname "nsl-$ROLE"
sed -i 's/^127\.0\.0\.1.*/127.0.0.1 nsl-'"$ROLE"'/' /etc/hosts
sqlite3 /var/lib/nsl/controller.db \
  "UPDATE controller_config SET controller_name='$UNIT', ap_name='NSL-$UNIT' WHERE id=1"
for svc in nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing; do
  d="/etc/systemd/system/${svc}.service.d/nsl-path.conf"
  [ -f "$d" ] && sed -i "s/NSL_UNIT=[^\" ]*/NSL_UNIT=$UNIT/" "$d"
done
pa="/etc/systemd/system/nsl-agent.service.d/nsl-portal.conf"
[ -f "$pa" ] && sed -i "s/NSL_UNIT=[^\" ]*/NSL_UNIT=$UNIT/" "$pa"
systemctl daemon-reload
if [ "$NOREBOOT" = "1" ]; then
  echo "unit=$UNIT applied — reboot to finish (hostname + services)"
  exit 0
fi
echo "unit=$UNIT applied — rebooting"
reboot
