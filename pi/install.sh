#!/bin/bash
# Pi base image setup (README 13/15, plan.md 6.5). Run on fresh Raspberry Pi OS Lite.
set -euo pipefail
ROLE="${1:-front}"   # front|side|rear
UNIT="${2:-bus-1}"

sudo hostnamectl set-hostname "nsl-${ROLE}"
# keep sudo/hostname lookups working (else "unable to resolve host nsl-${ROLE}")
if ! grep -q "nsl-${ROLE}" /etc/hosts 2>/dev/null; then
  echo "127.0.0.1 nsl-${ROLE}" | sudo tee -a /etc/hosts >/dev/null
fi
# onboard audio off (HUB75 clash), USB audio on front only
if ! grep -q "dtparam=audio=off" /boot/firmware/config.txt 2>/dev/null; then
  echo "dtparam=audio=off" | sudo tee -a /boot/firmware/config.txt >/dev/null || \
  echo "dtparam=audio=off" | sudo tee -a /boot/config.txt >/dev/null
fi
sudo apt-get update
sudo apt-get install -y python3-pip python3-pil python3-fastapi python3-uvicorn \
  sqlite3 flac alsa-utils network-manager evtest watchdog
sudo mkdir -p /var/lib/nsl /opt/nsl
NSL_ROLE="$ROLE" NSL_UNIT="$UNIT" python3 -m nsl_pi.store_init 2>/dev/null || \
  python3 -c "import os,sys; sys.path.insert(0,'.'); from pi.nsl_pi.store import init_controller_db; init_controller_db('/var/lib/nsl/controller.db', controller_name='$UNIT')"
# static bus LAN
case "$ROLE" in
  front) IP=10.9.0.1/24;; side) IP=10.9.0.2/24;; rear) IP=10.9.0.3/24;;
esac
sudo nmcli con add type ethernet ifname eth0 con-name bus-lan ip4 "$IP" ipv4.never-default yes || true
sudo cp deploy/nsl-*.service /etc/systemd/system/
# Code location + role: units run `python3 -m nsl_pi.*`, so both the repo root
# (for `nslprog`) and `pi/` (for `nsl_pi`) must be on PYTHONPATH. Drop-ins keep
# this working wherever the repo was cloned from.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Hanover bitmap fonts for the on-bus renderer (ships with the repo)
if [ -d "$REPO/HANOVERFONTS" ]; then
  sudo mkdir -p /var/lib/nsl/fonts
  sudo cp "$REPO"/HANOVERFONTS/*.Fnt /var/lib/nsl/fonts/ 2>/dev/null || true
fi
for svc in nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input; do
  sudo mkdir -p "/etc/systemd/system/${svc}.service.d"
  sudo tee "/etc/systemd/system/${svc}.service.d/nsl-path.conf" >/dev/null <<EOF
[Service]
Environment="NSL_ROLE=$ROLE"
Environment="NSL_UNIT=$UNIT"
Environment="PYTHONPATH=$REPO:$REPO/pi"
EOF
done
sudo systemctl daemon-reload
sudo systemctl enable nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input watchdog
echo "done: role=$ROLE unit=$UNIT ip=$IP — reboot, expect pairing code on boards"
