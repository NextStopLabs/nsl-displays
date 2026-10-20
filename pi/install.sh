#!/bin/bash
# Pi base image setup (README 13/15, plan.md 6.5). Run on fresh Raspberry Pi OS Lite.
set -euo pipefail
ROLE="${1:-front}"   # front|side|rear
UNIT="${2:-bus-1}"

sudo hostnamectl set-hostname "nsl-${ROLE}"
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
sudo systemctl daemon-reload
sudo systemctl enable nsl-core nsl-agent nsl-display nsl-local-ui watchdog
echo "done: role=$ROLE unit=$UNIT ip=$IP — reboot, expect pairing code on boards"
