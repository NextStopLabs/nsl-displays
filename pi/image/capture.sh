#!/bin/bash
# capture - SD → compressed golden image. Run on a Linux/Mac workstation
# with the GOLDEN Pi powered off and its SD inserted (via reader).
# Usage: sudo bash capture.sh /dev/sdX nsl-front   (Linux)
#        sudo bash capture.sh /dev/rdisk4 nsl-front (macOS)
# Refuses to run against a mounted root disk. Output: <name>.img + .img.xz.
set -euo pipefail
DEV="${1:?usage: sudo bash capture.sh <device> <name>}"; NAME="${2:?usage: sudo bash capture.sh <device> <name>}"
if [ "$(id -u)" -ne 0 ]; then echo "run as root (dd needs it)"; exit 1; fi
case "$DEV" in /dev/*) ;; *) echo "refusing: $DEV is not a device path"; exit 1;; esac
if mount | grep -q "^$DEV"; then echo "refusing: $DEV is mounted"; exit 1; fi
if [ "$(uname)" = "Darwin" ]; then diskutil unmountDisk "$DEV" || true; fi
# never image the disk we booted from
if [ "$(uname)" = "Linux" ] && [ -b "$DEV" ]; then
  rootdev="$(findmnt -no SOURCE / || true)"
  case "$rootdev" in "$DEV"*) echo "refusing: $DEV holds this machine's /"; exit 1;; esac
fi
echo "capturing $DEV -> $NAME.img (this takes a while)"
dd if="$DEV" of="$NAME.img" bs=4M status=progress conv=fsync
echo "compressing"
xz -T0 -9 -f "$NAME.img"
sha256sum "$NAME.img.xz" | tee "$NAME.img.xz.sha256"
ls -lh "$NAME.img.xz" "$NAME.img.xz.sha256"
echo "done: publish both files to https://nsldisplays.com/images/ (see README2)."
echo "flash with Raspberry Pi Imager (Use custom) or: xzcat $NAME.img.xz | sudo dd of=/dev/sdY bs=4M status=progress"
