#!/bin/bash
# prepare-golden — turn THIS Pi into a capture-ready golden master.
#
# Flow: install one Pi per role with the normal installer (unpaired!),
# verify it, then run:  sudo bash prepare-golden.sh
# Power off, pull the SD, capture it (see capture.sh), flash the .img to the
# fleet. Each clone sanitizes itself on first boot (fresh SSH/machine-id,
# fresh pairing registration) and comes up showing its own pairing code.
#
# Run ON the golden Pi, from the repo checkout. One image per ROLE
# (front/side/rear) — clones of one image serve many buses of that role,
# then each bus gets its unit via nsl-set-unit (front only).
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "run as root: sudo bash prepare-golden.sh"; exit 1; fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROLE="${NSL_ROLE:-front}"

if [ -f /var/lib/nsl/device-token ]; then
  echo "REFUSING: this Pi is paired (/var/lib/nsl/device-token exists)."
  echo "Golden images must be UNPAIRED or every clone shares one identity."
  echo "Unpair in the portal first, delete the token, then re-run."
  exit 1
fi

echo "==> role=$ROLE — installing first-boot sanitizer"
cp "$REPO/pi/image/nsl-golden-firstboot.service" /etc/systemd/system/
cp "$REPO/pi/image/nsl-golden-firstboot.sh" /usr/local/sbin/
cp "$REPO/pi/image/nsl-set-unit.sh" /usr/local/sbin/nsl-set-unit
chmod +x /usr/local/sbin/nsl-golden-firstboot.sh /usr/local/sbin/nsl-set-unit
echo "$ROLE" > /etc/nsl-role
systemctl daemon-reload
systemctl enable nsl-golden-firstboot.service

echo "==> wiping install-time identity (fresh per clone)"
rm -f /var/lib/nsl/pairing.json /var/lib/nsl/device-token
rm -f /var/lib/nsl/.image-booted /boot/nsl-status.txt /boot/nsl-installed.txt
if [ -f /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf ]; then
  sed -i '/NSL_TOKEN/d' /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf
fi

echo "==> cleaning logs/history/caches"
journalctl --vacuum-time=1s || true
rm -rf /var/log/nsl-*.log /root/.bash_history /home/*/.bash_history /home/*/.ssh/known_hosts*
apt-get clean || true
rm -rf /root/.cache /home/*/.cache

echo "================================================================"
echo "  Golden master ready (role=$ROLE, UNPAIRED)."
echo "  1. sudo poweroff — pull the SD."
echo "  2. Capture: sudo bash pi/image/capture.sh /dev/sdX nsl-$ROLE.img"
echo "  3. Flash nsl-$ROLE.img.xz to fleet SDs, boot with wired net."
echo "  4. Front clones: each shows its OWN pairing code → pair per bus."
echo "     Then on each front: sudo nsl-set-unit bus-N (per bus name)."
echo "================================================================"
