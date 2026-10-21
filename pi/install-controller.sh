#!/bin/bash
# install-controller — one-command setup for the FRONT controller Pi (Pi 4B).
#
# Single copy-paste on a flashed Raspberry Pi OS Lite 64-bit:
#
#   curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-controller.sh \
#     | sudo bash -s -- --unit bus-1
#
# Optional flags:
#   --portal https://nslboards.com/portal   portal base URL (default shown)
#   --unit bus-1                             bus name: AP becomes NSL-<unit>
#   --repo https://github.com/NextStopLabs/nsl-displays.git
#   --dir /opt/nsl-displays                  checkout location
#   --skip-matrix                            skip rpi-rgb-led-matrix build
#   --audio usb|jack                         usb adapter (default) or 3.5mm onboard jack
#
# Flow: install -> reboot if asked -> code shows on the front board AND in
# the terminal -> portal Controllers -> Pair controller -> enter code + name
# -> click Pair. The Pi polls the portal, fetches its token itself, restarts
# its agent and starts syncing. No manual token copy needed.
set -euo pipefail

PORTAL="https://nslboards.com/portal"
UNIT=""
REPO="https://github.com/NextStopLabs/nsl-displays.git"
DIR="/opt/nsl-displays"
SKIP_MATRIX=0
AUDIO="usb"

while [ $# -gt 0 ]; do
  case "$1" in
    --portal) PORTAL="${2:?}"; shift 2;;
    --unit) UNIT="${2:?}"; shift 2;;
    --repo) REPO="${2:?}"; shift 2;;
    --dir) DIR="${2:?}"; shift 2;;
    --skip-matrix) SKIP_MATRIX=1; shift;;
    --audio) AUDIO="${2:?}"; shift 2;;
    -h|--help) sed -n '1,20p' "$0"; exit 0;;
    *) echo "unknown flag: $1 (see --help)"; exit 1;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then echo "run as root (use: curl ... | sudo bash -s -- ...)"; exit 1; fi
if [ -z "$UNIT" ]; then
  if [ -t 0 ]; then read -r -p "Bus/unit name [bus-1]: " UNIT || true; fi
  UNIT="${UNIT:-bus-1}"
fi

if [ "$AUDIO" != "usb" ] && [ "$AUDIO" != "jack" ]; then echo "--audio must be usb|jack"; exit 1; fi

echo "==> NSL controller install: unit=$UNIT portal=$PORTAL dir=$DIR audio=$AUDIO"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-pip python3-pil python3-fastapi python3-uvicorn \
  sqlite3 flac alsa-utils network-manager evtest watchdog git curl python3-venv

if [ -d "$DIR/.git" ]; then
  echo "==> updating checkout $DIR"
  git -C "$DIR" pull --ff-only || true
else
  echo "==> cloning $REPO -> $DIR"
  git clone "$REPO" "$DIR"
fi

echo "==> base install (hostname, audio, bus-LAN, units)"
NSL_AUDIO="$AUDIO" bash "$DIR/pi/install.sh" front "$UNIT" "$AUDIO"

echo "==> LED matrix library"
if [ "$SKIP_MATRIX" -eq 1 ]; then
  echo "    skipped (--skip-matrix)"
elif python3 -c "import rgbmatrix" 2>/dev/null; then
  echo "    rgbmatrix already importable, skipping build"
else
  apt-get install -y build-essential python3-dev cython3
  rm -rf /root/rpi-rgb-led-matrix
  git clone https://github.com/hzeller/rpi-rgb-led-matrix.git /root/rpi-rgb-led-matrix
  (cd /root/rpi-rgb-led-matrix && pip install . --break-system-packages)
fi

echo "==> portal URL drop-in (token arrives via pairing)"
mkdir -p /etc/systemd/system/nsl-agent.service.d
cat > /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf <<EOF
[Service]
Environment="PORTAL_URL=$PORTAL"
Environment="NSL_ROLE=front"
Environment="NSL_UNIT=$UNIT"
EOF
mkdir -p /etc/systemd/system/nsl-pairing.service.d
cat > /etc/systemd/system/nsl-pairing.service.d/nsl-portal.conf <<EOF
[Service]
Environment="PORTAL_URL=$PORTAL"
Environment="NSL_BASE=/var/lib/nsl"
EOF

systemctl daemon-reload
systemctl enable nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing watchdog
systemctl restart nsl-core nsl-display nsl-local-ui nsl-pairing || true
systemctl restart nsl-agent || true

echo "==> registering with portal (needs internet: join hotspot first if offline)"
if PYTHONPATH="$DIR:$DIR/pi" NSL_BASE=/var/lib/nsl PORTAL_URL="$PORTAL" python3 -c "
import sys; sys.path.insert(0, '$DIR'); sys.path.insert(0, '$DIR/pi')
from nsl_pi import pairing as P
doc = P.ensure_registered('$PORTAL')
print('PAIRING_CODE=' + doc['pairing_code'])
print('CONTROLLER_ID=' + doc['controller_id'])
"; then
  echo "registered"
else
  echo "WARNING: registration failed (offline?). nsl-pairing keeps retrying — join the hotspot and watch journalctl -u nsl-pairing -f"
fi

CODE="$(PYTHONPATH="$DIR:$DIR/pi" python3 -c "from nsl_pi import pairing as P; d=P.load_pairing() or {}; print(d.get('pairing_code',''))" 2>/dev/null || true)"
if [ "$AUDIO" = "jack" ]; then
  echo "==> audio check (3.5mm onboard jack -> PA; keep software pulsing, reboot if you just switched)"
else
  echo "==> audio check (USB adapter -> PA; onboard jack is off by design)"
fi
if command -v aplay >/dev/null 2>&1; then
  aplay -l || true
  if PYTHONPATH="$DIR:$DIR/pi" NSL_BASE=/var/lib/nsl python3 -c "
from nsl_pi.player import play_test_tone
rep = play_test_tone('/var/lib/nsl')
print('test tone:', rep)
raise SystemExit(0 if rep.get('played') else 42)
"; then
    echo "    heard the chime? PA chain OK. If silent, check adapter/jack + amp volume."
  else
    echo "    WARNING: test tone did not play (no PA yet?). Boards/pairing still work;"
    echo "    re-run any time: curl -s -X POST http://192.168.4.1/api/audio-test"
  fi
else
  echo "    WARNING: aplay not found — audio packages may have failed to install"
fi
echo
echo "================================================================"
echo "  CONTROLLER READY — PAIR IT (one step):"
echo "  Code:  ${CODE:-<see journalctl -u nsl-pairing -f>}"
echo "  Portal: $PORTAL  -> Controllers -> Pair controller"
echo "  Enter the code + name \"$UNIT\", click Pair."
echo "  The board shows the same code until paired; the Pi fetches"
echo "  its token itself and starts syncing. Then publish + upload"
echo "  a program in the portal and select a destination by code."
echo "  Logs: journalctl -u nsl-pairing -u nsl-agent -u nsl-display -f"
echo "================================================================="
