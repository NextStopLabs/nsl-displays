#!/bin/bash
# Pi base image setup (README 13/15, plan.md 6.5). Run on fresh Raspberry Pi OS Lite.
set -euo pipefail
ROLE="${1:-front}"   # front|side|rear
UNIT="${2:-bus-1}"
AUDIO="${3:-${NSL_AUDIO:-jack}}"   # jack|usb - jack keeps the 3.5mm onboard output
if [ "$AUDIO" != "usb" ] && [ "$AUDIO" != "jack" ]; then echo "audio must be usb|jack"; exit 1; fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# keep sudo/hostname lookups working (else "unable to resolve host nsl-${ROLE}");
# hosts entry BEFORE the rename so sudo never warns mid-install.
if ! grep -q "nsl-${ROLE}" /etc/hosts 2>/dev/null; then
  echo "127.0.0.1 nsl-${ROLE}" | sudo tee -a /etc/hosts >/dev/null
fi
sudo hostnamectl set-hostname "nsl-${ROLE}"
# audio: 3.5mm jack is the default (onboard stays on - needs software pulsing).
# --audio jack keeps the 3.5mm onboard jack: needs software pulsing
# (the default no-hardware-pulse) and a reboot after install.
if [ "$AUDIO" = "jack" ]; then
  echo "audio=jack: leaving onboard audio enabled for 3.5mm output"
  for cfg in /boot/firmware/config.txt /boot/config.txt; do
    [ -f "$cfg" ] || continue
    if grep -q "^dtparam=audio=off" "$cfg" 2>/dev/null; then
      sudo sed -i 's/^dtparam=audio=off/#dtparam=audio=off # NSL jack mode/' "$cfg" || true
      echo "commented out dtparam=audio=off in $cfg (reboot to apply)"
    fi
  done
else
  # onboard audio off (HUB75 clash), USB audio on front only
  if ! grep -q "dtparam=audio=off" /boot/firmware/config.txt 2>/dev/null; then
    echo "dtparam=audio=off" | sudo tee -a /boot/firmware/config.txt >/dev/null || \
    echo "dtparam=audio=off" | sudo tee -a /boot/config.txt >/dev/null
  fi
fi
# blank panels at boot: GPIO18 (OE) driven high until the display driver
# takes over, so the boards aren't full-bright white from power-on.
if ! grep -q "^gpio=18=op,dh" /boot/firmware/config.txt 2>/dev/null; then
  echo "gpio=18=op,dh" | sudo tee -a /boot/firmware/config.txt >/dev/null || \
  echo "gpio=18=op,dh" | sudo tee -a /boot/config.txt >/dev/null
  echo "set gpio=18=op,dh (reboot to apply)"
fi
sudo apt-get update
sudo apt-get install -y python3-pip python3-pil python3-fastapi python3-uvicorn \
  sqlite3 flac alsa-utils network-manager evtest watchdog
sudo mkdir -p /var/lib/nsl /opt/nsl /etc/nsl
# Release signing public key. Replace NSL_RELEASE_PUBLIC_KEY in the environment
# with the real key before imaging; otherwise OTA updates will fail verification.
if [ -n "${NSL_RELEASE_PUBLIC_KEY:-}" ] && [ -f "$NSL_RELEASE_PUBLIC_KEY" ]; then
  sudo cp "$NSL_RELEASE_PUBLIC_KEY" /etc/nsl/release.pub
  sudo chmod 644 /etc/nsl/release.pub
elif [ -n "${NSL_RELEASE_PUBLIC_KEY_PEM:-}" ]; then
  printf '%s' "$NSL_RELEASE_PUBLIC_KEY_PEM" | sudo tee /etc/nsl/release.pub >/dev/null
  sudo chmod 644 /etc/nsl/release.pub
fi
# NOTE: plain `sudo python3` (no env tricks): sudo strips PYTHONPATH even with
# -E, so the repo paths go on sys.path inside the script itself.
sudo python3 -c "import sys; sys.path.insert(0, '$REPO'); sys.path.insert(0, '$REPO/pi'); from nsl_pi.store import init_controller_db; init_controller_db('/var/lib/nsl/controller.db', controller_name='$UNIT')"
# static bus LAN
case "$ROLE" in
  front) IP=10.9.0.1/24;; side) IP=10.9.0.2/24;; rear) IP=10.9.0.3/24;;
esac
if sudo nmcli -t -f NAME con show | grep -qx "bus-lan"; then
  echo "bus-lan connection already exists, leaving it"
else
  sudo nmcli con add type ethernet ifname eth0 con-name bus-lan ip4 "$IP" ipv4.never-default yes || true
fi
# static bus LAN wins over DHCP when both are visible (isolated switch = no DHCP)
sudo nmcli con mod bus-lan connection.autoconnect-priority 10 2>/dev/null || true
sudo cp "$REPO"/deploy/nsl-*.service /etc/systemd/system/
# Code location + role: units run `python3 -m nsl_pi.*`, so both the repo root
# (for `nslprog`) and `pi/` (for `nsl_pi`) must be on PYTHONPATH. Drop-ins keep
# this working wherever the repo was cloned from (REPO computed at top).
# Hanover bitmap fonts for the on-bus renderer (ships with the repo)
if [ -d "$REPO/HANOVERFONTS" ]; then
  sudo mkdir -p /var/lib/nsl/fonts
  sudo cp "$REPO"/HANOVERFONTS/*.Fnt /var/lib/nsl/fonts/ 2>/dev/null || true
fi
for svc in nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing; do
  sudo mkdir -p "/etc/systemd/system/${svc}.service.d"
  sudo tee "/etc/systemd/system/${svc}.service.d/nsl-path.conf" >/dev/null <<EOF
[Service]
Environment="NSL_ROLE=$ROLE"
Environment="NSL_UNIT=$UNIT"
Environment="PYTHONPATH=$REPO:$REPO/pi"
EOF
done
sudo systemctl daemon-reload
sudo systemctl enable nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing watchdog
echo "done: role=$ROLE unit=$UNIT audio=$AUDIO ip=$IP - reboot, expect pairing code on boards"
