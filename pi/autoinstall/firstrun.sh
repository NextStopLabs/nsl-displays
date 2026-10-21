#!/bin/bash
# NSL autoinstall firstrun — ZERO-TOUCH flash for stock Raspberry Pi OS Lite.
#
# 1. Flash stock Raspberry Pi OS Lite 64-bit to the SD (no Imager customization needed).
# 2. Edit the CONFIG block below (ROLE per Pi, UNIT per bus).
# 3. Copy THIS FILE onto the SD boot drive as:  firstrun.sh
#    (the small FAT partition your computer shows after flashing — just the
#    drive root, next to config.txt/cmdline.txt)
# 4. Boot the Pi with wired net (needs internet). Walk away.
# 5. ~15-25 min later (Pi 4; longer on 3B) it reboots by itself.
#    Front Pi: pairing code appears on the boards. Boards: signs follow front.
#    Progress any time: SSH in (user pi, password nsl-setup — change it) and
#    tail /var/log/nsl-autoinstall.log, or re-mount the SD and read nsl-status.txt.
#
# How it works: first boot runs this file as root. It creates the pi user,
# enables SSH, and installs a one-shot nsl-autoinstall service that pulls the
# normal installer from GitHub and runs it (same code as the manual
# copy-paste). The service deletes itself when done. Safe to re-image: every
# SD installs fresh (unique SSH keys, fresh pairing registration).
#
# ===== CONFIG — edit these 5 lines per Pi, then copy the file =====
NSL_ROLE="front"    # front | side | rear  (one front + one side + one rear per bus)
NSL_UNIT="bus-1"    # must MATCH on all 3 Pis of one bus (AP becomes NSL-<unit>)
NSL_AUDIO="jack"    # jack (3.5mm onboard, default) | usb (adapter)
NSL_PORTAL="https://nsldisplays.com/portal"
NSL_REPO="https://github.com/NextStopLabs/nsl-displays.git"
# ===== end CONFIG =====
RAW_BASE="https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main"

# Never break first boot: log everything, always exit 0.
exec >>/var/log/nsl-firstrun.log 2>&1 || true
echo "nsl-firstrun: role=$NSL_ROLE unit=$NSL_UNIT audio=$NSL_AUDIO $(date -u +%FT%TZ)"

main() {
  set -euo pipefail

  # --- login + SSH (stock Lite has neither) ---
  if ! id pi >/dev/null 2>&1; then
    useradd -m -s /bin/bash -G sudo pi
    echo 'pi:nsl-setup' | chpasswd
    echo "pi ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/nsl-pi
    chmod 0440 /etc/sudoers.d/nsl-pi
  fi
  # force a password change on first SSH login (default creds must not stay)
  chage -d 0 pi || true
  # optional: drop your key on the boot drive as nsl-ssh.pub for key login
  for bd in /boot/firmware /boot; do
    if [ -f "$bd/nsl-ssh.pub" ]; then
      mkdir -p /home/pi/.ssh && cat "$bd/nsl-ssh.pub" >> /home/pi/.ssh/authorized_keys
      chmod 700 /home/pi/.ssh && chmod 600 /home/pi/.ssh/authorized_keys
      chown -R pi:pi /home/pi/.ssh
      echo "nsl-firstrun: installed nsl-ssh.pub"
      break
    fi
  done
  systemctl enable ssh || true

  # --- config the autoinstall service will read ---
  cat > /etc/nsl-autoinstall.conf <<EOF
NSL_ROLE="$NSL_ROLE"
NSL_UNIT="$NSL_UNIT"
NSL_AUDIO="$NSL_AUDIO"
NSL_PORTAL="$NSL_PORTAL"
NSL_REPO="$NSL_REPO"
RAW_BASE="$RAW_BASE"
EOF
  chmod 600 /etc/nsl-autoinstall.conf

  # --- the actual installer (runs on boot, after network) ---
  cat > /usr/local/sbin/nsl-autoinstall.sh <<'AUTOINSTALL_EOF'
#!/bin/bash
# First-boot autoinstall worker. Installed by firstrun.sh. Idempotent:
# re-runs the normal installer until it succeeds, then removes itself.
set -euo pipefail
source /etc/nsl-autoinstall.conf
exec >>/var/log/nsl-autoinstall.log 2>&1
boot_dir() { for d in /boot/firmware /boot; do [ -d "$d" ] && { echo "$d"; return; }; done; echo /boot; }
stat() { echo "$(date -u +%FT%TZ) $1" | tee -a "$(boot_dir)/nsl-status.txt"; }
wait_apt() {
  for _ in $(seq 1 180); do
    if ! fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       && ! fuser /var/lib/apt/lists/lock >/dev/null 2>&1; then return 0; fi
    sleep 10
  done
  return 1
}
wait_net() {
  for _ in $(seq 1 60); do
    if curl -fsSL -m 10 -o /dev/null "$RAW_BASE/pi/install.sh"; then return 0; fi
    sleep 60
  done
  return 1
}
stat "autoinstall start role=$NSL_ROLE unit=$NSL_UNIT audio=$NSL_AUDIO"
wait_apt || { stat "apt lock stuck, retrying via service restart"; exit 1; }
wait_net || { stat "no internet after ~1h, retrying via service restart"; exit 1; }
if [ "$NSL_ROLE" = "front" ]; then
  stat "running install-controller"
  curl -fsSL "$RAW_BASE/pi/install-controller.sh" \
    | bash -s -- --unit "$NSL_UNIT" --audio "$NSL_AUDIO" --portal "$NSL_PORTAL" --repo "$NSL_REPO"
else
  stat "running install-board role=$NSL_ROLE"
  curl -fsSL "$RAW_BASE/pi/install-board.sh" \
    | bash -s -- --role "$NSL_ROLE" --unit "$NSL_UNIT" --repo "$NSL_REPO"
fi
stat "installer done"
systemctl disable nsl-autoinstall.service || true
echo "$(date -u +%FT%TZ) installed role=$NSL_ROLE unit=$NSL_UNIT" > "$(boot_dir)/nsl-installed.txt"
stat "rebooting into normal operation"
sleep 5
reboot
AUTOINSTALL_EOF
  chmod +x /usr/local/sbin/nsl-autoinstall.sh

  cat > /etc/systemd/system/nsl-autoinstall.service <<'EOF'
[Unit]
Description=NSL first-boot autoinstall (self-removing)
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nsl-autoinstall.sh
Restart=on-failure
RestartSec=120
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable nsl-autoinstall.service
  systemctl start nsl-autoinstall.service || true
  echo "nsl-firstrun: done, autoinstall service handed off"
}

main || echo "nsl-firstrun FAILED — SSH in and check this log"
exit 0
