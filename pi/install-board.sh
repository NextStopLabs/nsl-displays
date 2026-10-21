#!/bin/bash
# install-board - one-command setup for a SIDE or REAR receiver Pi (Pi 3B).
#
# Single copy-paste on a flashed Raspberry Pi OS Lite 64-bit:
#
#   curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
#     | sudo bash -s -- --role side --unit bus-1
#   curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
#     | sudo bash -s -- --role rear --unit bus-1
#
# Flags:
#   --role side|rear                 (required; prompted if a TTY and omitted)
#   --unit bus-1                     must match the front controller's unit
#   --front 10.9.0.1                 front Pi on the bus LAN
#   --repo ... --dir ... --skip-matrix (same as install-controller.sh)
#
# Receivers never touch the portal: they join the wired bus LAN, poll the
# front Pi at http://<front>:8090/state @10 Hz and render their own sign.
# No pairing code, no token. Pair the FRONT controller (§15) and the boards
# follow it automatically.
set -euo pipefail

ROLE=""
UNIT=""
FRONT="10.9.0.1"
REPO="https://github.com/NextStopLabs/nsl-displays.git"
DIR="/opt/nsl-displays"
SKIP_MATRIX=0

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLE="${2:?}"; shift 2;;
    --unit) UNIT="${2:?}"; shift 2;;
    --front) FRONT="${2:?}"; shift 2;;
    --repo) REPO="${2:?}"; shift 2;;
    --dir) DIR="${2:?}"; shift 2;;
    --skip-matrix) SKIP_MATRIX=1; shift;;
    -h|--help) sed -n '1,20p' "$0"; exit 0;;
    *) echo "unknown flag: $1 (see --help)"; exit 1;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then echo "run as root (use: curl ... | sudo bash -s -- ...)"; exit 1; fi
if [ "$ROLE" != "side" ] && [ "$ROLE" != "rear" ]; then
  if [ -t 0 ]; then read -r -p "Role (side/rear): " ROLE || true; fi
  if [ "$ROLE" != "side" ] && [ "$ROLE" != "rear" ]; then echo "need --role side|rear"; exit 1; fi
fi
if [ -z "$UNIT" ]; then
  if [ -t 0 ]; then read -r -p "Bus/unit name (must match front) [bus-1]: " UNIT || true; fi
  UNIT="${UNIT:-bus-1}"
fi

echo "==> NSL $ROLE board install: unit=$UNIT front=$FRONT dir=$DIR"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-pip python3-pil sqlite3 network-manager \
  watchdog git curl evtest

if [ -d "$DIR/.git" ]; then
  echo "==> updating checkout $DIR"
  git -C "$DIR" pull --ff-only || true
else
  echo "==> cloning $REPO -> $DIR"
  git clone "$REPO" "$DIR"
fi

echo "==> base install (hostname, bus-LAN, units)"
bash "$DIR/pi/install.sh" "$ROLE" "$UNIT"

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

echo "==> receiver-only services (no portal/audio/keypad/AP on boards)"
systemctl disable --now nsl-agent nsl-local-ui nsl-player nsl-input nsl-pairing 2>/dev/null || true
systemctl enable --now nsl-core nsl-display watchdog

echo "==> verifying link to front ($FRONT)"
if ping -c2 -W2 "$FRONT" >/dev/null 2>&1; then echo "    ping $FRONT OK"; else
  echo "    WARNING: ping $FRONT failed - check Ethernet + switch power"; fi
if curl -fs -m 5 "http://$FRONT:8090/state" >/dev/null 2>&1; then
  echo "    front state OK (board follows the controller now)"
else
  echo "    WARNING: no state from http://$FRONT:8090/state yet - pair + power the front first, this board holds its last frame meanwhile"
fi

WANT="160x40 (chain=2)"; [ "$ROLE" = "rear" ] && WANT="80x40 (chain=1)"
echo
echo "================================================================"
echo "  $ROLE BOARD READY - unit $UNIT, $WANT"
echo "  No portal step for boards. Once the FRONT controller is paired"
echo "  and a destination is selected, this sign follows automatically."
echo "  Logs: journalctl -u nsl-display -u nsl-core -f"
echo "================================================================="
