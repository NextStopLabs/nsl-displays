# NSL Displays — Full Setup Guide (v2)

Canonical end-to-end setup. The old `README.md` sections are reference material;
**this file is the one to follow** for a fresh install.

Portal: **https://nsldisplays.com/portal**
Health: **https://nsldisplays.com/portal/health**

Local-only URLs (unchanged — these are device networks, not the internet):
`http://192.168.4.1` (Pi AP), `http://10.9.0.1:8090/state` (bus LAN).

---

## 0. What you're building

```
https://nsldisplays.com/portal  (Docker: portal + Caddy, your server)
  │  Pi-initiated HTTPS (hotspot / wired net with internet)
  ▼
FRONT Pi 4B (10.9.0.1) ──bus LAN──► SIDE Pi 3B (10.9.0.2) ──► 160x40 sign
  │  ├──► front 240x40 sign (direct HUB75)      └─► REAR Pi 3B (10.9.0.3) ──► 80x40 sign
  │  ├──► USB keypad, USB audio or 3.5mm jack → PA
  │  └──► Wi-Fi AP 192.168.4.1 (offline control)
```

One bus = one portal controller. Side/rear Pis never touch the portal.

---

## 1. Portal server

Requirements: Ubuntu 22.04+/Debian 12, Docker + Compose v2, DNS `A` record
`nsldisplays.com` → server IP, ports 80/443 open.

```bash
git clone https://github.com/NextStopLabs/nsl-displays.git && cd nsl-displays/deploy
cp .env.example .env && nano .env
# set: DOMAIN=nsldisplays.com
#      ACME_EMAIL=you@example.com
#      PORTAL_SECRET=$(openssl rand -hex 32)
docker compose up --build -d
docker compose ps
curl https://nsldisplays.com/portal/health   # {"ok": true, ...}
```

Notes:

- State lives in the `portal-data` volume (`portal.db` + `storage/`). Back up nightly:
  `docker compose --profile backup run --rm backup` (host cron `0 3 * * *`).
- Never commit `.env`. Rotating `PORTAL_SECRET` logs everyone out and forces
  every bus to re-pair (it signs sessions and device tokens).
- Update: `git pull && docker compose up --build -d`.
- Bare metal instead of Docker: see old `README.md` §12A option 2
  (`PORTAL_SECRET`, `DATABASE_URL`, `STORAGE_DIR`, `uvicorn ... --port 8000`).

---

## 2. Flash and pair — golden images (recommended)

No SSH, no typing, no file-copying. Flash, boot with wired net, pair.

```bash
# download once (per role — front/side/rear are different images):
curl -fSL -o nsl-front.img.xz https://nsldisplays.com/images/nsl-front.img.xz
curl -fSL -o nsl-front.img.xz.sha256 https://nsldisplays.com/images/nsl-front.img.xz.sha256
sha256sum -c nsl-front.img.xz.sha256
# same for nsl-side.img.xz / nsl-rear.img.xz
```

Flash with Raspberry Pi Imager → **Use custom** → pick the `.img.xz`,
flash one SD per Pi, boot with wired net (needs internet once for the
first registration):

- Front: boards show their **own pairing code** after ~1-2 min → portal
  Controllers → Pair → done (token fetched automatically, chime plays).
- Side/rear: signs follow the front automatically. No portal step.

Clones of one image serve many buses. After booting a front on a new bus,
set its name **without SSH**: join its AP (`NSL-<unit>`), open
`http://192.168.4.1`, Bus identity → enter `bus-2` → Apply + reboot. Then pair
it (each bus gets its own code). Same-name APs in one depot will clash, so
rename before deploying two fresh fronts side by side.

Publishing images (after building per §2A Option B):
`cp nsl-*.img.xz* deploy/images/` on the server, `docker compose up --build -d`
(proxy serves `/srv/images`), verify `https://nsldisplays.com/images/`.

---

## 2B. Fallback: 3 copy-pastes on stock Lite

Flash Raspberry Pi OS **Lite 64-bit** on each card, boot with SSH on.
Wired setup is assumed (keyboard/screen or SSH). Run one command per Pi.

```bash
# 1) FRONT controller (Pi 4B):
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-controller.sh \
  | sudo bash -s -- --unit bus-1

# 2) SIDE board (Pi 3B):
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role side --unit bus-1

# 3) REAR board (Pi 3B):
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role rear --unit bus-1
```

`--unit` must match on all three (AP becomes `NSL-<unit>`).
Add `--audio jack` to the front command for 3.5mm onboard audio
(default `--audio jack`; pass `--audio usb` for a USB adapter). Add `--portal <url>` only for a non-production portal —
use the exact base your browser uses: if the UI loads at
`https://nsldisplays.com` with no `/portal` path, pass
`--portal https://nsldisplays.com`.

What the scripts do (all idempotent — safe to re-run):

- Controller: hostname `nsl-front`, audio mode (`dtparam=audio=off` for USB,
  kept enabled for jack), `gpio=18=op,dh` so panels stay blank from power-on
  instead of full-bright, deps install, checkout to `/opt/nsl-displays`,
  `rpi-rgb-led-matrix` build (skipped if already importable or `--skip-matrix`),
  `controller.db` bootstrap, bus-LAN `10.9.0.1/24`, systemd units
  (`nsl-core/agent/display/local-ui/player/input/pairing`), portal registration.
- Boards: hostname `nsl-side`/`nsl-rear`, bus-LAN `10.9.0.2/24`/`10.9.0.3/24`,
  matrix lib, receiver-only services (`nsl-core` + `nsl-display`; agent, local
  UI, player, input, pairing disabled), link check against the front Pi.

Reboot each Pi after install if audio config or the GPIO blanking line changed.

---

## 2C. Zero-typing flash (no SSH, no typing on the Pi)

Fallback when you don't have a golden image yet. Both end at §3.

**Stock Lite + firstrun file (per SD, ~15-25 min unattended).**

1. Flash stock Raspberry Pi OS **Lite 64-bit** (no Imager customization needed).
2. Open `pi/autoinstall/firstrun.sh` in a text editor, set the 5 CONFIG lines
   (`NSL_ROLE` per Pi, `NSL_UNIT` per bus, `NSL_AUDIO`, portal default is fine).
3. Copy the file onto the SD boot drive as `firstrun.sh` (the small FAT
   partition your computer shows after flashing — drive root, next to
   `config.txt`). Optional: also drop your SSH key as `nsl-ssh.pub`.
4. Boot with wired net (needs internet). Walk away — it creates the `pi` user
   (password `nsl-setup`, forced change on first login), enables SSH, and runs
   the normal installer as a self-removing service. Progress: re-mount the SD
   and read `nsl-status.txt`, or SSH in and tail
   `/var/log/nsl-autoinstall.log`. It reboots itself when done.

**Golden images (per role, flash → code in ~2 min).**
The §2 images. To rebuild them after repo changes:

```bash
# on one verified UNPAIRED Pi of each role, from the repo checkout:
sudo bash pi/image/prepare-golden.sh
sudo poweroff   # pull the SD, capture it on your workstation:
sudo bash pi/image/capture.sh /dev/sdX nsl-front   # → nsl-front.img.xz + .sha256
cp nsl-*.img.xz* deploy/images/   # publish at https://nsldisplays.com/images/
```

Each clone sanitizes itself on first boot (fresh machine-id/SSH keys/hostname,
pairing state wiped) and shows its **own** pairing code. Personalize per bus
via the local UI (§2) or `sudo nsl-set-unit bus-2`. Boards need nothing further.

---

## 3. Pairing — one code, one click

1. Power all three Pis. The front boards show a 6-char **pairing code**
   (also `journalctl -u nsl-pairing -f`, and `http://192.168.4.1`).
2. Give the front Pi internet (phone hotspot; `nsl-pairing` re-registers
   automatically if the 15-min code lapses).
3. Portal → Controllers → Pair controller → enter code + name (e.g. `bus-1`).
   Click Pair.
4. The Pi polls `POST /api/device/claim`, fetches its device token itself,
   stores it (`/var/lib/nsl/device-token`, mode 0600, + agent drop-in),
   restarts `nsl-agent`, and **plays the test chime** (§4). Boards switch from
   the pairing frame to normal content. No token copy-paste.

---

## 4. Audio

| Setup | Installer flag | Notes |
|---|---|---|
| USB adapter → amp → PA | `--audio usb` | No HUB75 tradeoff, best picture (opt-in) |
| 3.5mm onboard jack → amp → PA | `--audio jack` | Keep software pulsing (default); never enable hardware pulse with the jack; reboot after switching |

Check chain without any program synced:

```bash
curl -X POST http://192.168.4.1/api/audio-test   # front Pi, AP or bus LAN
```

The installer plays this chime at setup, and pairing plays it once more on
success. If silent: `aplay -l`, adapter/jack seating, amp volume,
`audio_device` in `controller.db`. Portal clips can only work once the local
chime is heard.

Real announcements need portal clips: portal → Audio (upload WAV/MP3/OGG or
generate with a Piper voice) → attach to service moments (`next_stop`,
`approaching`, `last_stop`) and/or per stop → publish → upload (§5).
Announcements auto-play on stop change; keypad/portal/local-UI buttons play
immediately. No clips = silence (preview lists clips per stop first).

---

## 5. First program — portal to bus

1. Portal → Programs → New (`Burton routes`).
2. Open it → add Service `100`, add destinations with 8-digit codes
   (`10000001` Burton…), Suggest validates inline, codes stay unique per program.
3. Per destination → Stops (ordered, terminus last) → Display (per-sign
   layouts; auto-fit starter is fine) → Preview (screens + announcements).
4. Publish → Upload to the bus (sends latest version).
5. Next hotspot session the bus pulls it (program first, audio second,
   software last — short windows still land the important bits).
6. On the bus: type the 8-digit code (keypad, portal remote, or local UI).
   Check all three signs + announcement. `INVALID` = wrong active program.

---

## 6. Verify checklist

```bash
ping 10.9.0.2; ping 10.9.0.3
curl http://10.9.0.1:8090/state        # front snapshot JSON
aplay -l                               # USB card or Headphones jack present
vcgencmd get_throttled                 # expect 0x0 (no brown-out)
journalctl -u nsl-pairing -u nsl-agent -u nsl-display -f
```

Boards: pairing code while unpaired; destination content after select;
receivers hold last frame if Ethernet drops. Keypad: 8 digits auto-select.
Portal controller row: online badge, `want vX / bus vY / active vZ`,
pending command count.

---

## 7. Operating

- Logs: `journalctl -u nsl-core -u nsl-agent -u nsl-display -u nsl-player -u nsl-input -u nsl-pairing -f`.
- Portal remote page: stop nav, announcements, override, volume, program select.
  Commands to an offline bus are refused (409), never silently queued.
- Rotate token (portal row → Rotate token) if a token leaks; old one dies.
  Unpair revokes access (bus keeps showing last content, syncs stop).
- Portal upgrade: `git pull && docker compose up --build -d`.
- Pi software: auto-updates from portal releases (signed bundles, A/B +
  rollback). Re-run the install scripts any time for repair — they update the
  checkout and re-enable services without wiping state.

---

## 8. Migrating to the domain (IP or old domain → nsldisplays.com)

Tokens are domain-independent — **no re-pair needed**. Point the portal and
the buses at the new URL:

```bash
# server: deploy/.env → DOMAIN=nsldisplays.com, then:
docker compose up --build -d   # Caddy issues the Let's Encrypt cert
curl https://nsldisplays.com/portal/health

# each FRONT Pi (boards need nothing — they never call the portal):
NEW=https://nsldisplays.com/portal
sudo sed -i "s|^Environment=\"PORTAL_URL=.*|Environment=\"PORTAL_URL=$NEW\"|" \
  /etc/systemd/system/nsl-agent.service.d/nsl-portal.conf \
  /etc/systemd/system/nsl-pairing.service.d/nsl-portal.conf
if [ -f /var/lib/nsl/pairing.json ]; then
  sudo python3 -c "import json; p='/var/lib/nsl/pairing.json'; d=json.load(open(p)); d['portal_url']='$NEW'; json.dump(d, open(p, 'w'))"
fi
sqlite3 /var/lib/nsl/controller.db "UPDATE controller_config SET portal_url='$NEW' WHERE id=1"
sudo systemctl daemon-reload && sudo systemctl restart nsl-agent nsl-pairing
```

(Or just re-run `install-controller.sh` — same `--unit`, it re-registers and
keeps state. Either way the already-paired token keeps working.)

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Full-bright panels at boot | old image without the GPIO line | re-run installer (sets `gpio=18=op,dh`), reboot |
| `INVALID` on every code | wrong active program | select the published version first (`bus parked on vX` warning in portal) |
| No audio / distorted | wrong output, adapter unseated, amp down | `aplay -l`, `/api/audio-test`, `audio_device`, amp volume |
| Side/rear blank, front OK | bus LAN down, receiver down | `ping`, `curl http://10.9.0.1:8090/state`, `receiver_addr`, `journalctl -u nsl-display` |
| Flicker/tearing | `pwm-bits` high, weak 5 V | lower `pwm-bits`, check 5 V under load, `throttled` flags |
| Panels reset on bright text | thin 5 V runs | shorten/thicken runs, fuses, re-measure drop |
| Pairing code expired | 15-min window, no internet | join hotspot; `nsl-pairing` re-registers automatically |
| Portal unreachable from bus | hotspot off / DNS | join hotspot, `curl https://nsldisplays.com/portal/health` from a laptop on it |

---

## 10. URL reference

| What | URL |
|---|---|
| Portal | https://nsldisplays.com/portal |
| Health | https://nsldisplays.com/portal/health |
| Device API (Pi-initiated) | https://nsldisplays.com/portal/api/device/… |
| Local controller (offline OK) | http://192.168.4.1 |
| Front state (bus LAN) | http://10.9.0.1:8090/state |
| Audio test (front) | `POST http://192.168.4.1/api/audio-test` |
