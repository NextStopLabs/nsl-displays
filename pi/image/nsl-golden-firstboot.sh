#!/bin/bash
# nsl-golden-firstboot — sanitize a golden-image clone on its first boot.
# Fresh SSH/machine-id/hostname, wipe pairing state so each clone registers
# as its own controller, then never run again (flag file).
set -euo pipefail
exec >>/var/log/nsl-golden-firstboot.log 2>&1
FLAG=/var/lib/nsl/.image-booted
[ -f "$FLAG" ] && { echo "already sanitized, exiting"; exit 0; }

ROLE="$(cat /etc/nsl-role 2>/dev/null || echo "${NSL_ROLE:-front}")"
echo "golden firstboot: role=$ROLE $(date -u +%FT%TZ)"

# unique machine identity
rm -f /etc/machine-id /var/lib/dbus/machine-id
systemd-machine-id-setup
rm -f /etc/ssh/ssh_host_*
ssh-keygen -A
# hostname matches role (unit personalization via nsl-set-unit afterwards)
hostnamectl set-hostname "nsl-$ROLE" || true
sed -i 's/^127\.0\.0\.1.*/127.0.0.1 nsl-'"$ROLE"'/' /etc/hosts

# fresh portal identity (front registers on boot via nsl-pairing).
# No baked bus name: clones share nothing. AP gets a unique NSL-XXXX suffix
# from machine-id until the operator sets the real unit (local UI / set-unit).
rm -f /var/lib/nsl/pairing.json /var/lib/nsl/device-token
SUFFIX="$(cat /etc/machine-id 2>/dev/null | cut -c1-4 | tr 'a-z' 'A-Z')"
SUFFIX="${SUFFIX:-0000}"
sqlite3 /var/lib/nsl/controller.db \
  "UPDATE controller_config SET controller_name='', ap_name='NSL-$SUFFIX' WHERE id=1" 2>/dev/null || true
echo "identity reset: controller_name='' ap_name='NSL-$SUFFIX'"
if [ -f /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf ]; then
  sed -i '/NSL_TOKEN/d' /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf
fi

# make sure the role's services are the ones enabled
systemctl daemon-reload
if [ "$ROLE" = "front" ]; then
  systemctl enable nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing watchdog || true
else
  systemctl disable nsl-agent nsl-local-ui nsl-player nsl-input nsl-pairing 2>/dev/null || true
  systemctl enable nsl-core nsl-display watchdog || true
fi

date -u +%FT%TZ > "$FLAG"
echo "golden firstboot done — continuing normal boot (code on boards shortly)"
