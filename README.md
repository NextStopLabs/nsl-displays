# NSL Displays - Bus Stop Announcement & LED Destination System

> **Start here for installs: [`README2.md`](README2.md) - full setup guide
> (portal + flash-and-pair images + pairing + audio + first program).
> This file remains the detailed reference.

> One logical controller per bus. Three physical signs. Portal-built programs. Offline-first Pi control.

This README is the wiki for the project. `plan.md` is the build plan, `format.md` is the `.nslprog` file spec. This file is how to wire it, power it, run it, and operate it.

---

## Contents

1. [What this is](#1-what-this-is)
2. [Hardware you have](#2-hardware-you-have)
3. [How it fits together](#3-how-it-fits-together)
4. [Wiring - the 10 m bus rule](#4-wiring--the-10-m-bus-rule)
5. [Power](#5-power)
6. [Networking](#6-networking)
7. [Programs, codes, stops, audio, layouts](#7-programs-codes-stops-audio-layouts)
8. [Driver operation cheat sheet](#8-driver-operation-cheat-sheet)
9. [Keypad](#9-keypad)
10. [Local controller 192.168.4.1](#10-local-controller-19216841)
11. [Portal remote control](#11-portal-remote-control)
12. [Sync, offline, updates](#12-sync-offline-updates)
12A. [Host server setup - portal](#12a-host-server-setup--portal)
12B. [Front controller Pi setup - Pi 4B](#12b-front-controller-pi-setup--pi-4b)
12C. [Receiver Pi setup - side/rear Pi 3B](#12c-receiver-pi-setup--siderear-pi-3b)
13. [First build - bench test](#13-first-build--bench-test)
14. [Bus install checklist](#14-bus-install-checklist)
15. [Power-on, pairing, first program](#15-power-on-pairing-first-program)
16. [File layout on the Pi](#16-file-layout-on-the-pi)
17. [Configuration reference](#17-configuration-reference)
18. [Audio standard](#18-audio-standard)
19. [Board layout standard](#19-board-layout-standard)
20. [Troubleshooting](#20-troubleshooting)
21. [Safety](#21-safety)
22. [Repo files](#22-repo-files)
23. [Glossary](#23-glossary)

---

## 1. What this is

- **Portal** at `nsldisplays.com/portal`: multi-user web app. Build programs (services → destinations → stops), upload/generate audio with Piper, design what each LED board shows, pair controllers, push programs, remote-control a bus.
- **Bus controller**: a main Raspberry Pi 4 at the front of the bus. Runs the service: plays announcements, drives the front LED sign, tells the side/rear signs what to show, reads the USB keypad, hosts the local controller page.
- **Two receiver Pis**: one at the side sign, one at the rear sign. They drive their local panels and mirror the main Pi's state. The portal still sees **one controller per bus**.
- **Program file** (`.nslprog`): a single SQLite file per published program version. Holds services, 1–8 digit destination codes, stops, displays, board layouts, audio references. Audio travels as separate hash-named FLAC files. Full spec in `format.md`.
- **Offline-first**: the bus runs entirely from local cache. Internet (phone hotspot) is only needed to sync new programs, audio, or software updates.

```
Portal (nsldisplays.com/portal)
  │  HTTPS/WSS, Pi-initiated only (hotspot)
  ▼
Front Pi 4B ──bus LAN (Ethernet switch)──► Side Pi 3B ──► 160x40 sign
  │                                         └─► Rear Pi 3B ──► 80x40 sign
  ├──► Front 240x40 sign (direct HUB75)
  ├──► USB keypad (driver input)
  ├──► USB audio out (announcements)
  └──► Wi-Fi AP 192.168.4.1 (local control, no internet needed)
```

---

## 2. Reference hardware (generic setup)

| Location | Panels | Resolution | Driver | Chain setting |
|---|---|---|---|---|
| Front | 3x 80x40 P4 HUB75 | 240x40 | Raspberry Pi 4B | `chain=3, parallel=1` |
| Side | 2x 80x40 P4 HUB75 | 160x40 | Raspberry Pi 3B | `chain=2, parallel=1` |
| Rear | 1x 80x40 P4 HUB75 | 80x40 | Raspberry Pi 3B | `chain=1, parallel=1` |
| Network | - | - | 5-port unmanaged Ethernet switch (5 V) + 3x CAT5e | `10.9.0.0/24` bus LAN |

Panels: 80x40 P4 HUB75, requiring the **E address line**. Any vendor-compatible 80x40 HUB75 panel works if E is routed.

Pis:
- Front Pi 4B - controller. Runs core, player, agent, keypad, front display driver, AP + hotspot client, local web UI. Pi 4 is used here because 3 chained panels is the heaviest render load.
- Side Pi 3B - receiver. Display renderer only.
- Rear Pi 3B - receiver. Display renderer only. A Pi 3B is sufficient for 1–2 panels.
- Microcontrollers (e.g. Pico) are **not** display drivers in this design. Receivers must run Linux + `rpi-rgb-led-matrix` + the shared Pillow layout renderer so portal preview and bus output stay identical.

Power (generic): vehicle 12/24 V stepped down to 5 V **at each sign** with local buck converters. See §5 for sizing. Each Pi needs its own clean 5 V tap (Pi 4: 5 V / 3 A, Pi 3B: 5 V / 2.5 A).

Programs are built fresh in the portal - no legacy converter in v1.

---

## 3. How it fits together

**One bus = one controller record in the portal**, with three display outputs:

| Output | Program display | Physical driver | Transport |
|---|---|---|---|
| front | `front` 240x40 | Front Pi 4B local HUB75 | `local` |
| side | `side` 160x40 | Side Pi 3B local HUB75 | `network` |
| rear | `rear` 80x40 | Rear Pi 3B local HUB75 | `network` |

Software on the main Pi (all systemd services):

| Service | Job |
|---|---|
| `agent` | Pairing, auth, sync, program/audio download, heartbeat, update check, remote command channel, network mode switching |
| `controller-core` | Single source of truth: program/service/destination/stop/override/volume. Persists to `controller.db`. Tells player + displays what to do |
| `player` | Decodes FLAC clips to PCM, chains announcement sequences gaplessly to ALSA (USB audio) |
| `display-driver` | Owns front HUB75 matrix, renders front layout with Pillow, pushes frames with vsync. Holds last frame on crash |
| `input` | Reads USB keypad via `evdev`, maps keys to actions, sends to core |
| `local-ui` | Web UI at `192.168.4.1`, same components as portal remote control |
| `receiver-sync` | Publishes state to side/rear Pis over bus LAN |
| `player` | Auto-announces on stop change through USB audio (front only) |
| `input` | Reads the cab USB keypad, dispatches to Core (front only) |

Software on each receiver Pi: minimal `receiver` service (render layout JSON + hold last frame) + `display-driver`. No portal contact, no audio, no keypad.

---

## 4. Wiring - the long-bus rule

**HUB75 ribbon does not travel the length of a bus.** It is single-ended 3.3/5 V parallel running at MHz pixel clocks. Reliable length is ~30–50 cm, ~1 m max with good cable and buffering. The correct topology on any 8–12 m vehicle is: **short HUB75 at each sign, long Ethernet between signs.**

```
FRONT cabinet (all within 50 cm)
┌──────────┐  HUB75 ribbon  ┌────────┐
│ Front    ├───────────────►│Panel 1 │─►Panel 2─►Panel 3 (240x40)
│ Pi 4B    │  (<50 cm)      └────────┘
└────┬─────┘
     │ USB keypad, USB audio
     │ Ethernet ──► switch (see §6.1)

SIDE (all within 50 cm)
┌──────────┐  HUB75 ribbon  ┌────────┐
│ Side     ├───────────────►│Panel 1 │─►Panel 2 (160x40)
│ Pi 3B    │                └────────┘
└──────────┘  Ethernet ──► switch

REAR (all within 50 cm)
┌──────────┐  HUB75 ribbon  ┌────────┐
│ Rear     ├───────────────►│Panel 1 │  (80x40)
│ Pi 3B    │  (<50 cm)      └────────┘
└──────────┘  Ethernet ──► switch
```

Within each chain: daisy-chain **output → input** following the arrows printed on the panel PCBs. Only the first panel's input connects to the Pi side.

**Sign driver (use a buffered HAT in vehicles):**
Direct GPIO wiring works for bench/prototype. For vehicle installs, put a buffered HAT between each Pi and its panels: Adafruit RGB Matrix HAT/Bonnet, Electrodragon drive board v2, or Active-3 adapter. What it buys: 3.3 V → 5 V level shifting (74AHCT245), solid IDC sockets, vibration tolerance. Whatever is used must route the **E address line** (required by 80x40 panels). Keep `led-gpio-mapping=regular` unless the board docs say otherwise.

**Receiver Pis:** each Pi mounts within 50 cm of its panels, same short-ribbon rule. A single-chain HAT or direct wiring is fine since `parallel=1` uses few pins.

**Cables:** Use proper 2x8 2.54 mm IDC ribbon for HUB75. Keep Pi-to-adapter and adapter-to-first-panel short. Route HUB75 away from 5 V high-current feeds and audio cable.

### 4.1 Full HUB75 wiring diagram (pin-level, `regular` mapping, `parallel=1`)

Every Pi runs `parallel=1`, so each Pi only needs **chain [1]** plus the shared signals. Same wiring at all three signs - only `led-chain` differs (front 3, side 2, rear 1).

**Pi 40-pin header → first panel INPUT (IDC 2x8).** Pin numbers are physical header pins. Based on `hzeller/rpi-rgb-led-matrix` `wiring.md`, `regular` mapping. Wire once, copy to all three Pis.

| Pi header pin | Signal | To panel INPUT IDC | Notes |
|---|---|---|---|
| 6 (GND) | GND | IDC GND | Plus one more GND (e.g. header 9/14/20) to IDC GND - common ground |
| 7 | STB/LAT (strobe) | LAT | Shared to all chains (only chain 1 used here) |
| 11 | CLK (clock) | CLK | Shared |
| 12 (GPIO18) | OE- | OE | Shared |
| 15 | A | A | Shared address line |
| 16 | B | B | Shared |
| 18 | C | C | Shared |
| 22 | D | D | Shared (needed for 32+ row panels) |
| 10 | E | E | Shared - **required for 80x40 panels** |
| 13 | [1] G1 | G1 (upper) | Chain-1 colour data |
| 19 | [1] B2 | B2 (lower) | Chain-1 colour data |
| 21 | [1] G2 | G2 (lower) | Chain-1 colour data |
| 23 | [1] R1 | R1 (upper) | Chain-1 colour data |
| 24 | [1] R2 | R2 (lower) | Chain-1 colour data |
| 26 | [1] B1 | B1 (upper) | Chain-1 colour data |

Unused for this build: header pins 3, 5, 8, 29, 31–38, 40 (chains [2]/[3] data) - leave unconnected. With `parallel=1` most GPIOs stay free for optional buttons.

**Panel chaining (at each sign):**

```
Pi ──IDC──► Panel 1 INPUT │ Panel 1 OUTPUT ──IDC──► Panel 2 INPUT │ Panel 2 OUTPUT ──► Panel 3 INPUT (front only)
                  (arrow on PCB points INPUT → OUTPUT)
5 V + fused branch per panel ──► each panel's power socket (centre connector, check +/− silkscreen!)
```

- Only Panel 1 INPUT touches the Pi. Panels 2/3 connect output→input down the chain.
- HUB75 IDC is 2x8 (R1 G1 B1 GND R2 G2 B2 E / A B C D CLK LAT OE GND - verify against the panel silkscreen; some vendors label R1/R2 as R0/R1, same function).
- If using a HAT/bonnet: Pi plugs into HAT, HAT's J1/output goes to Panel 1 INPUT. Same signal map, just buffered to 5 V via 74AHCT245.

**Combined sign wiring (one of these per sign):**

```
                    ┌── Fuse 5–7.5A ──► Panel 1 5V (14–16 AWG, <1 m)
Buck 5V out ──┬─────┼── Fuse 5–7.5A ──► Panel 2 5V (front/side only)
at sign       │     └── Fuse 5–7.5A ──► Panel 3 5V (front only)
              │     └── Fuse 1–3A ────► Pi 5V (USB-C / GPIO 5V pins 2+4)
              └───── GND star point ──► Panel GNDs + HAT GND + Pi GND (header 6 etc.)
Pi header (table above) ── short IDC (<50 cm) ──► Panel 1 INPUT ──► Panel 2 ──► Panel 3
Pi eth0 ── CAT5e ──► switch (see §6.1)
```

**GPIO note:** With `parallel=1` per Pi, most GPIO pins stay free, so optional GPIO buttons remain possible. The reserved-pin list is computed from the matrix mapping - the config UI greys those out.

---

## 5. Power

Step the vehicle supply down to 5 V **at each sign**: **the long run carries the higher voltage (12/24 V), the 5 V run stays under ~1 m.**

### 5.1 Per-sign requirements

| Sign | Step-down at sign | 5 V load (typical text @ brightness 70) | 5 V load (worst full-white) |
|---|---|---|---|
| Front (3 panels + Pi 4B) | 12/24 V → 5 V buck ≥15 A | ~3–5 A + Pi ~1 A | ~12 A + Pi |
| Side (2 panels + Pi 3B) | 12/24 V → 5 V buck ≥10 A | ~2–4 A + Pi ~0.8 A | ~8 A + Pi |
| Rear (1 panel + Pi 3B) | 12/24 V → 5 V buck ≥5 A | ~1–2 A + Pi ~0.8 A | ~4 A + Pi |

Pis need clean 5 V: Pi 4 = 5 V / 3 A, Pi 3B = 5 V / 2.5 A. Give each Pi its own tap/fuse - do not power a Pi from a panel's power pigtail.

### 5.2 Rules

1. **Panels take 5 V only.**
2. **One fused branch per panel.** 5–7.5 A automotive fuse per panel power lead. Fuse each converter input too.
3. **Star ground at each sign:** converter 0 V → panel GNDs → HAT/adapter GND → Pi GND, short and thick. Do not daisy-chain a thin ground down the bus.
4. **Wire gauge:** 5 V panel feeds 14–16 AWG, runs <1 m. Converter inputs 18–20 AWG minimum. Thin wire on 5 V causes brown-outs that look like software flicker.
5. **Acceptance test:** display full-white test pattern, measure at the panel terminals. Drop source-to-panel ≤100 mV, no panel reset, Pi `vcgencmd get_throttled` shows `0x0`.
6. **Brightness 70** is the operating default - it cuts real current substantially vs full-white worst case, which is what the bucks are sized for.

---

## 6. Networking

Two networks, different jobs. Don't confuse them.

### 6.1 Bus LAN (signs talking to each other - always on)

Front Pi 4B ↔ side Pi 3B ↔ rear Pi 3B, all via one Ethernet switch. Carries display state (destination fields + layout + override + brightness). Requirements: <100 ms latency, hold-last-frame on drop.

**Physical connection - all three Pis into a switch:**

```
                    ┌──────────────────┐
                    │ 5-port unmanaged │
                    │ switch (front    │
                    │ cabinet, 5 V)    │
                    └───┬──────┬───┬───┘
              CAT5e     │      │     │      CAT5e
          (~1 m)        │      │     │      (~5–10 m)
                 ┌──────┘      │     └──────┐
                 ▼             ▼            ▼
           ┌──────────┐  ┌──────────┐ ┌──────────┐
           │ Front    │  │ Side     │ │ Rear     │
           │ Pi 4B    │  │ Pi 3B    │ │ Pi 3B    │
           │ 10.9.0.1 │  │ 10.9.0.2 │ │ 10.9.0.3 │
           └──────────┘  └──────────┘ └──────────┘
All: 10.9.0.0/24, netmask 255.255.255.0, no gateway, no DNS.
Switch mounted at the front cabinet, powered from the front 5 V rail (fused 1 A).
```

- Cable: standard CAT5e/CAT6 patch, RJ45. Short patch front Pi → switch, long runs switch → side (~5 m) and switch → rear (~10 m). No crossover needed (Pi auto-MDIX).
- Switch: any tiny 5-port unmanaged 5 V industrial/desktop switch. One port per Pi, two ports spare. This is the star topology - no daisy-chaining, one unplug never takes down two signs.
- IPs (static, on the wired interface only - do not touch the Wi-Fi interface):
  - Front: `10.9.0.1/24`, Side: `10.9.0.2/24`, Rear: `10.9.0.3/24`.
  - Example (NetworkManager): `nmcli con add type ethernet ifname eth0 con-name bus-lan ip4 10.9.0.1/24`
  - Test: `ping 10.9.0.2`, `ping 10.9.0.3`, `ssh pi@10.9.0.2`.
- Why wired, not Wi-Fi: a bus body is a metal box with engine/door RF noise, and a single-radio Pi cannot be AP + client at once. Wire is fit-and-forget. Wi-Fi join to the front AP is fallback only if a cable is cut - signs hold last frame meanwhile.
- Keep bus LAN and hotspot/AP networks separate: bus LAN is `10.9.0.0/24` wired only; AP is `192.168.4.0/24` Wi-Fi only. Internet (hotspot) touches the front Pi only.

Receiver protocol v1 (kept deliberately simple): front Pi exposes state over TCP/WebSocket on the bus LAN (`http://10.9.0.1:8090/state`); receivers poll/subscribe at 10 Hz, render with the same Pillow renderer, hold last frame if the link drops. Receivers never talk to the portal. The portal still sees one controller.

### 6.2 Phone hotspot + Pi access point (updates and local control)

| Mode | When | What you get |
|---|---|---|
| Hotspot client | Phone hotspot on, in range | Internet: sync programs/audio/software, portal remote control |
| Access point | Hotspot absent, or requested | Pi creates `NSL-<unit id>` at `192.168.4.1`, DHCP, local controller. Fully offline |

Behaviour:
- Boot: look for saved hotspot ~60 s → join if found, else start AP.
- While AP has a client connected, don't switch away (single radio can't do both reliably).
- **Update mode** (keypad key / local UI button / portal command): drop AP, join hotspot for N minutes (default 5), then return to AP. Turn hotspot on first, then trigger update mode.
- Phone can't join the Pi AP while hosting the hotspot - so on hotspot use the **portal** remote page (over mobile data); use `192.168.4.1` only in AP mode.
- First pairing must be done on hotspot (needs internet).

---

## 7. Programs, codes, stops, audio, layouts

A **program** is what you build in the portal and upload to a bus. One program → many services → many destinations → ordered stops.

```
Program "Burton routes"  (displays: front 240x40, side 160x40, rear 80x40)
 └─ Service "100"
 │    ├─ clips: next_stop / approaching / last_stop
 │    ├─ Destination code 10000001 "Burton" (via Branston, dest BURTON)
 │    │    ├─ layout front / side / rear
 │    │    └─ Stops: Lichfield Bus Station → Branston Road → Burton New Street (terminus)
 │    └─ Destination code 10000002 "Lichfield" ...
 └─ Service "101" ...
```

**Destination codes:** 1–8 digits, unique within the program, stored as text (leading zeros kept). Entering a code selects service + destination in one go. Short codes are confirmed with Enter; 8-digit codes also auto-select on the 8th digit. Same code may exist in a different program without clash. Editor suggests the next free code, validates inline, DB enforces `UNIQUE(program_id, code)`.

**Stops:** ordered list per destination, positions 1..N no gaps. Each stop: name, stop audio clip, board text (default = name), board override (e.g. `TERMINATING BUS`, empty = none), terminus flag (must be last stop only).

**Announcements** are chained gaplessly at play time:

| Moment | Clips in order |
|---|---|
| Next stop | `next_stop_clip` → `stop.audio` |
| Approaching | `approaching_clip` → `stop.audio` |
| Next stop at terminus | `next_stop_clip` → `stop.audio` → `last_stop_audio` |
| Approaching at terminus | `approaching_clip` → `stop.audio` → `last_stop_audio` |

`terminus_clip_mode` per service: `next` / `approaching` / `both` (default `both`). NULL clips are skipped.

**Override lifetime rule:** once an override is active it stays until the stop changes or a new destination/service/program is selected. Never cleared by timers, replays, syncs, upgrades, or reconnects. A stop's saved override activates when that stop becomes current. `Clear override` removes it immediately. Persisted across restarts.

**Board layouts:** fully user-defined per destination per display (see §19). Templates per display + auto-fit fallback.

**Draft → publish → upload:** edits are drafts, never touch a bus. Upload validates (§7.1), builds an immutable numbered `.nslprog`, queues per controller. Queued = installs next time the bus is online. Previous version stays until the new one verifies; active program never switches mid-run (activates on next destination/program select, idle point, or Apply-now).

### 7.1 Publish validation (errors block, warnings allow)

Errors: bad/duplicate code, duplicate service number, no displays, bad `default_display`, stop gaps, terminus not last / multiple termini, layout size/JSON/overflow, missing audio hash, integrity/foreign-key/content-hash failure.
Warnings: destination with no stops (display-only), missing service clip, stop with no audio (silent), no terminus flagged, unknown font, unscrollable overflow, unused display name.

---

## 8. Driver operation cheat sheet

Print this for the cab.

```
SELECT DESTINATION:  type 1–8 digit code ─► Enter confirms (8-digit codes auto-select on the 8th digit)
                     (or type code + ENTER if Enter-required mode)
CORRECT A DIGIT:     BACKSPACE (one digit) / DELETE or ESC (clear all)
BAD CODE:            board flashes INVALID - nothing changes, retype
NEXT STOP:           [+]  (default map, user-definable)
PREV STOP:           [-]
ANNOUNCE NEXT STOP:  [*]
CLEAR OVERRIDE:      [/]
UPDATE MODE:         [.]  (join hotspot 5 min - turn hotspot on first)
VOLUME:              keypad map or local UI / portal
```

State always resumes after power loss (program/service/destination/stop/override/volume persisted).

---

## 9. Keypad

Standard USB keyboard number row and/or USB numeric keypad, read via `evdev` (raw key codes, NumLock-independent). Grabbed exclusively so keystrokes never reach the console. Unplug/replug reconnects automatically.

Default map:

| Key | Role |
|---|---|
| `0`–`9` / `KP0`–`KP9` | Digit |
| `Enter` / numpad Enter | Enter |
| `Backspace` | Clear one digit |
| `Delete` / `Esc` | Clear all |
| Numpad `+ - * / .` | Free action keys (defaults in §8) |

Every action key (and long-press) holds an **ordered action list**: announcements, stop nav, program/service/destination select, code entry, override apply/clear, volume/mute, network update-mode, apply-pending-update. Fully remappable per controller in portal + local UI.

Entry behaviour: digits echo on the configured display (default front) → auto-select on 8th digit (or require Enter) → valid code resets to first stop + clears override; invalid shows `INVALID` briefly. Timeout returns to normal display. Lookup is in the **active program only**.

---

## 10. Local controller 192.168.4.1

Join the Pi AP (`NSL-<unit id>`, WPA2 per-unit password), open `http://192.168.4.1`. No internet needed. Same UI components as the portal remote page.

- **Control:** program select, destination list or 1–8 digit code, prev/next stop, next/approaching/last-stop announce, replay, override preset/custom + clear, volume/mute, apply-pending-update, live display previews.
- **Programs:** installed versions, active program, apply pending, roll back, **import `.nslpack`** (offline program install).
- **Settings (login):** display outputs + matrix options, keypad map, GPIO bindings, audio device/volume, AP name/password, saved hotspot networks, join-hotspot-now, portal URL, release channel, startup program/destination.
- **Diagnostics:** connection, last sync, pairing code, test pattern/message/announcement, live keypad/GPIO state, logs, check-for-updates, restart/shutdown.
- Listens on all interfaces (`NSL_UI_HOST`/`NSL_UI_PORT`, default `0.0.0.0:80`) but only serves loopback, the AP subnet (`192.168.4.0/24`) and the bus LAN (`10.9.0.0/24`) - anything else gets 403. Extra subnets via `NSL_UI_ALLOW` (e.g. `NSL_UI_ALLOW=0.0.0.0/0` opens it fully - only on networks you trust: commands are unauthenticated). There is no login; the network boundary is the access control, so use `192.168.4.1` in AP mode and the portal page on hotspot.

---

## 11. Portal remote control

Per-controller page. Pick the bus, type the 1–8 digit code, done - stop nav, announcements, override, volume and program switching live under "More controls". Same abilities as §10 plus online/offline badge, programs wanted-vs-on-bus-vs-active, pending command count, last announcement, volume state. Editors control, viewers watch-only, everything logged with user + time.

Truth rules: a command to an **offline** bus is refused immediately (`controller offline, last seen … - command not queued`) - it is never silently queued. Commands to an **online** bus persist in `pending_commands` (survive portal restarts) and the bus picks them up on its next heartbeat (every `NSL_POLL_S`, default 10 s); the agent logs each receipt (`portal command received: …`) and reports it back, so arrival is visible in the log. The controllers table shows per program `want vX / bus vY / active vZ` - installed ≠ selected: codes only work in the **active** version, and a `bus parked on vX` warning tells you exactly when the bus sits on an old one. Offline ⇒ keypad + local UI keep working.

---

## 12. Sync, offline, updates

**On hotspot connect:** auth → heartbeat → flush outbox (events, logs, install results) → fetch manifest (desired program versions, config version, latest release) → download `.nslprog` first (tiny), then only missing audio by SHA-256 (resumable range requests, hash-verified) → stage in `incoming/` → validate → atomic move → report.

Order: program data + config first, audio second, software last - so a short hotspot window still lands the important changes. Sync never changes current state or clears an override.

**Software auto-update:** on hotspot connect the agent checks `/api/device/release` for its release channel, downloads the signed bundle (resume + hash), verifies the Ed25519 signature against the public key installed at `/etc/nsl/release.pub`, unpacks it into the inactive A/B slot at `/opt/nsl/vN`, and schedules activation. A background helper switches the `/opt/nsl/current` symlink, restarts the NSL services, and runs a health check (systemd services + local UI `/api/health`); on failure it rolls back to the previous slot. Result is reported via the outbox on the next heartbeat. The bus never switches mid-service unless forced via a portal/local `update_now` command. To prepare release bundles, see `tools/generate-release-key.py` and `POST /api/admin/releases`.

---

## 12A. Host server setup - portal

Runs `portal/backend` (FastAPI under `/portal`) + Postgres + file storage + TTS worker, behind Caddy. See `deploy/docker-compose.yml`, `portal/Dockerfile`, `deploy/config/Caddyfile`, `portal/backend/db.py`.

**Requirements:** Ubuntu 22.04+ / Debian 12, Docker + Compose v2 plugin (or Python 3.11 + `ffmpeg` + `espeak-ng` + `piper` for bare metal). Ports `80/443` open, DNS A record for your domain pointing at the host. Only the proxy publishes ports; the portal is reachable solely through it at `https://<DOMAIN>/portal`.

**Requirements:** Ubuntu 22.04+ / Debian 12, Docker + Compose v2 plugin (or Python 3.11 + `ffmpeg` + `espeak-ng` + `piper` for bare metal). Ports `80/443` open, DNS A record for your domain pointing at the host.

**Option 1 - Docker (production):**

```bash
git clone <repo> nsl-displays && cd nsl-displays/deploy
cp .env.example .env && ${EDITOR:-nano} .env
# set DOMAIN, ACME_EMAIL, and PORTAL_SECRET (generate: openssl rand -hex 32)
docker compose up --build -d
docker compose ps            # portal (healthy) + proxy up
docker compose logs portal   # expect Uvicorn on 0.0.0.0:8000 with --root-path /portal
curl https://<DOMAIN>/portal/health   # {"ok": true, ...}
```

What this starts (`deploy/docker-compose.yml`):
- `portal`: built from repo root (`portal/Dockerfile`: `python:3.11-slim` + `ffmpeg` + `espeak-ng` + `requirements.txt` incl. `piper-tts`), runs `uvicorn portal.backend.app:app --host 0.0.0.0 --port 8000 --root-path /portal`. State is SQLite at `/data/portal.db` + blobs under `/data/storage` (`audio/<aa>/<hash>.wav`, `programs/<id>/<v>.nslprog`, `voices/<id>/model.onnx`), all on the `portal-data` volume - schema auto-creates on first request. Postgres is planned (plan.md) but not wired yet (`db.py` only speaks SQLite), so there is no `db` service.
- `proxy`: `caddy:2-alpine` with `deploy/config/Caddyfile` (`{$DOMAIN}`, automatic Let's Encrypt via `{$ACME_EMAIL}`), strips the `/portal` prefix and reverse-proxies to `portal:8000`. Certs persist in `caddy-data`.
- `backup` (profile, not always-on): `docker compose --profile backup run --rm backup` copies the db + storage tree to `deploy/backups/<date>/` via `tools/backup.py`. Run it from host cron nightly (`0 3 * * * ...`), and copy off-host.

Update with `git pull && docker compose up --build -d`. Never commit `.env` (gitignored) - `PORTAL_SECRET` signs user sessions and all Pi device tokens; rotating it logs everyone out and forces re-pairing.

**Option 2 - bare metal / dev (SQLite, no Docker):**

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# audio/TTS decode needs system binaries:
sudo apt-get install -y ffmpeg espeak-ng   # plus: pip install piper-tts (provides `piper` binary)
export PORTAL_SECRET="dev-secret-change-me"
export DATABASE_URL="sqlite:./dev.db"      # default; schema auto-created on first connect
export STORAGE_DIR="./storage"
mkdir -p "$STORAGE_DIR"
uvicorn portal.backend.app:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000/  (no /portal prefix locally; proxy strips it in prod)
```

**Run it as a service** (bare metal, survives SSH drop + reboot):

```bash
sudo mkdir -p /etc/nsl
sudo cp deploy/portal.env.example /etc/nsl/portal.env
sudo nano /etc/nsl/portal.env   # set PORTAL_SECRET + data paths
# adjust WorkingDirectory/ExecStart in deploy/nsl-portal.service to your checkout + venv, then:
sudo cp deploy/nsl-portal.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now nsl-portal
journalctl -u nsl-portal -f
```

**First use:** sign up (`POST /api/auth/signup` or the UI) → creates user + org. Then pair a controller (§15), publish a program, upload audio/voices (`.onnx` + `.onnx.json` pair per voice). Back up nightly via the `backup` profile. Never commit `PORTAL_SECRET`.

---

## 12B. Front controller Pi setup - Pi 4B

The bus brain: core + player + agent + keypad + front HUB75 + AP + bus-LAN server. See `pi/install.sh`, `pi/nsl_pi/service_main.py`, `pi/nsl_pi/agent_main.py`, `pi/nsl_pi/netmode.py`.

**Full bus - 3 copy-pastes (one per flashed Pi, Raspberry Pi OS Lite 64-bit):**

```bash
# 1) FRONT (Pi 4B) - copy-paste on the flashed front Pi:
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-controller.sh \
  | sudo bash -s -- --unit bus-1
# 2) SIDE (Pi 3B) - copy-paste on the flashed side Pi:
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role side --unit bus-1
# 3) REAR (Pi 3B) - copy-paste on the flashed rear Pi:
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role rear --unit bus-1
# Add --audio jack to the front command if you use the 3.5mm onboard jack
# 3.5mm onboard jack is the default audio output; pass --audio usb for a USB adapter instead.
```

Then: read the 6-char code off the front board → portal Controllers → Pair controller → enter code + name → Pair. The Pi fetches its token, plays the test chime, and starts syncing. Boards are wired setup (bus LAN `10.9.0.x`), no hotspot credentials needed for install.

**Hardware:** Pi 4B (2 GB+), 32 GB+ SD, buffered HAT (74AHCT245 level shifter, E-line routed), 5 V/3 A Pi tap, USB keypad at cab, USB audio to PA/amp, CAT5e to switch. Panels wired per §4.1, ribbon <50 cm.

**1. Flash OS:** Raspberry Pi OS **Lite 64-bit**, hostname `nsl-front`, user `pi`, SSH on. Boot, `sudo apt-get update && sudo apt-get upgrade -y`.

**2. Install (one command - copy-paste on the flashed Pi):**

```bash
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-controller.sh \
  | sudo bash -s -- --unit bus-1
# --portal defaults to https://nsldisplays.com/portal; --unit sets the AP name NSL-<unit>
# 3.5mm onboard jack is the default; pass --audio usb for a USB adapter.
# For OTA updates, generate a key pair with tools/generate-release-key.py and pass
# --release-key /path/to/release.pub so the Pi can verify signed bundles.
# Jack mode needs software pulsing (the default) and a reboot after install.
```

That script clones/updates `/opt/nsl-displays`, runs the base install below,
builds the LED matrix library, enables `nsl-core/agent/display/local-ui/player/input/pairing`,
registers with the portal and prints the 6-char pairing code. Manual equivalent
(`git clone` + `sudo bash pi/install.sh front bus-1`) still works; the script wraps it.

What `pi/install.sh` does:
- `hostnamectl set-hostname nsl-front`.
- Audio: `--audio usb` (default) appends `dtparam=audio=off` to `/boot/firmware/config.txt` (or `/boot/config.txt`) - onboard audio clashes with HUB75, USB adapter is front-only. `--audio jack` keeps the 3.5mm onboard jack (comments out `audio=off`, needs software pulsing + reboot).
- Sets `gpio=18=op,dh` in `/boot/firmware/config.txt` so panels stay blank (not full-bright) from power-on until the display driver takes over.
- `apt-get install python3-pip python3-pil python3-fastapi python3-uvicorn sqlite3 flac alsa-utils network-manager evtest watchdog`.
- `mkdir -p /var/lib/nsl /opt/nsl`, bootstraps `/var/lib/nsl/controller.db` via `pi/nsl_pi/store.py::init_controller_db` (state row, `controller_config`, keypad map, 3 `display_outputs` from `WIRING_PRESETS["bus-10m"]`).
- Adds wired `bus-lan` connection `10.9.0.1/24` (`ipv4.never-default yes`, no gateway/DNS).
- Copies `deploy/nsl-*.service` → `/etc/systemd/system`, writes per-service `nsl-path.conf` drop-ins (`NSL_ROLE`/`NSL_UNIT`, `PYTHONPATH=<repo>:<repo>/pi` so `python3 -m nsl_pi.*` resolves), enables `nsl-core nsl-agent nsl-display nsl-local-ui nsl-player nsl-input nsl-pairing watchdog`.

**3. Install the LED matrix library (once):**

```bash
git clone https://github.com/hzeller/rpi-rgb-led-matrix.git ~/rpi-rgb-led-matrix
sudo apt-get install -y build-essential python3-dev cython3
cd ~/rpi-rgb-led-matrix   # build from the repo root (pyproject.toml lives here, not in bindings/)
sudo pip install . --break-system-packages   # system-wide: samples + services run as root, which cannot see a ~/.local user install
# NOTE: samplebase.py appends the source tree to sys.path, so a user-only
# (non-sudo) install is shadowed and fails with "No module named 'rgbmatrix.core'".
# verify one 80x40 panel, E-line working:
cd bindings/python/samples
sudo ./runtext.py --led-rows=40 --led-cols=80 \
  --led-chain=3 --led-parallel=1 --led-gpio-mapping=regular --led-slowdown-gpio=4 \
  --led-brightness=70 -t "NSL"
```

Match `led-*` flags to §17 / `deploy/config/matrix.example.json` (`chain 3, parallel 1, rows 40, cols 80, brightness 70, slowdown-gpio 4, pwm-bits 11`).

**4. Environment + services:**

- `nsl-core` runs `python3 -m nsl_pi.service_main`: owns state, serves `http://10.9.0.1:8090/state` for receivers (front only; idle on side/rear). Never touches GPIO.
- `nsl-display` runs `python3 -m nsl_pi.display_main`: sole owner of the HUB75 matrix - front re-renders from Core at 10 Hz, side/rear poll the front at 10 Hz and hold last frame on drop.
- `nsl-agent` runs `python3 -m nsl_pi.agent_main`: needs `PORTAL_URL` (default `https://nsldisplays.com/portal`) + `NSL_TOKEN` (device token from pairing, §15). Set via systemd override until paired, then heartbeat/sync polls every `NSL_POLL_S` (default 10 s).
- `nsl-player` runs `python3 -m nsl_pi.player_main`: watches stop changes, plays the next-stop chain on the PA (front only, USB audio).
- `nsl-input` runs `python3 -m nsl_pi.input_main`: cab USB keypad → Core commands (front only).
- `nsl-local-ui` serves the local controller on port 80 (all interfaces, but only AP-subnet/bus-LAN/loopback clients get answers, 403 otherwise).

```bash
sudo systemctl edit nsl-agent   # add: [Service] Environment="PORTAL_URL=https://nsldisplays.com/portal" Environment="NSL_TOKEN=<token>"
sudo systemctl restart nsl-agent nsl-core nsl-display nsl-local-ui
systemctl is-active nsl-core nsl-agent nsl-display nsl-local-ui
```

**5. Network (NetworkManager):** keep bus LAN and Wi-Fi apart - `10.9.0.0/24` wired only, `192.168.4.0/24` AP only (§6). Save the phone hotspot with `nmcli`, then boot logic (`netmode.py`) joins it ~60 s if visible else starts AP `NSL-<unit>` at `192.168.4.1`.

**6. Verify:** `ping 10.9.0.2; ping 10.9.0.3`, `curl http://10.9.0.1:8090/state`, `aplay -l` shows USB audio, keypad types the code → front display selects, `vcgencmd get_throttled` → `0x0`. Unpaired boards show the 6-char pairing code.

---

## 12C. Receiver Pi setup - side/rear Pi 3B

Render-only: poll front state @10 Hz, render with the same Pillow renderer, hold last frame on drop. No portal, audio, or keypad. See `pi/nsl_pi/receiver.py`, `service_main.py` (`ROLE != front` → `Receiver(...).run_forever()`).

**Hardware:** Pi 3B per sign, 16 GB+ SD, single-chain HAT or direct wiring (`parallel=1`), 5 V/2.5 A Pi tap. Mount within 50 cm of its panels: side = 2 panels (160x40, `chain=2`), rear = 1 panel (80x40, `chain=1`).

**Setup (repeat per sign, only the role/IP differs):**

```bash
# side (copy-paste on the flashed side Pi):
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role side --unit bus-1   # → hostname nsl-side, bus-lan 10.9.0.2/24
# rear (copy-paste on the flashed rear Pi):
curl -fsSL https://raw.githubusercontent.com/NextStopLabs/nsl-displays/main/pi/install-board.sh \
  | sudo bash -s -- --role rear --unit bus-1   # → hostname nsl-rear, bus-lan 10.9.0.3/24
# same matrix library build as front (bindings/python → pip install .), then smoke-test the local chain:
sudo ~/rpi-rgb-led-matrix/bindings/python/samples/runtext.py --led-rows=40 --led-cols=80 \
  --led-chain=2 --led-parallel=1 --led-gpio-mapping=regular --led-slowdown-gpio=4 \
  --led-brightness=70 -t "SIDE"   # use --led-chain=1 on rear
```

**Receiver-only services:** `install.sh` enables the full set; on receivers disable what they never use (no portal contact, no AP hosting, no audio/keypad):

```bash
sudo systemctl disable --now nsl-agent nsl-local-ui
sudo systemctl enable --now nsl-core nsl-display watchdog
```

Bus LAN is wired only (`10.9.0.2/24` side, `10.9.0.3/24` rear, `ipv4.never-default yes`). Receivers never need `PORTAL_URL`/`NSL_TOKEN`, hotspot credentials, or AP config.

**Verify per receiver:** `ping 10.9.0.1`, `curl http://10.9.0.1:8090/state` (front snapshot JSON), then check its own sign follows destination changes and freezes (holds last frame) if you unplug Ethernet. If blank: check `receiver_addr` (`10.9.0.2`/`10.9.0.3` in `display_outputs`), `ping`, and `journalctl -u nsl-core -u nsl-display`.

---

## 13. First build - bench test

Do this on a bench before touching the bus.

1. Flash Raspberry Pi OS Lite to all three Pis. Hostnames: `nsl-front`, `nsl-side`, `nsl-rear`.
2. On each: disable onboard audio (`dtparam=audio=off` - required, HUB75 clashes with it), enable USB audio on front Pi only, install `rpi-rgb-led-matrix` Python bindings, verify E-line works on one 80x40 panel.
3. Wire front chain on bench: front Pi → panel 1 → panel 2 → panel 3, ribbon <50 cm. Run the display program with `led-chain=3, parallel=1, rows=40, cols=80, brightness=70, slowdown-gpio=4`. Confirm 240x40 canvas lights.
4. Wire side (`chain=2`) and rear (`chain=1`) the same way. Confirm each lights its own canvas.
5. Link the three over Ethernet with static IPs, run the receiver sync once, confirm all three show the same destination.
6. Plug in USB keypad, confirm code entry selects on the front display.
7. Full-white pattern 60 s per sign, check `vcgencmd get_throttled` = `0x0` and no panel brown-out. Then set brightness 70 for service.

---

## 14. Bus install checklist

- [ ] Front Pi 4B at front sign, ribbon <50 cm, buffered HAT seated, panels daisy-chained with arrows.
- [ ] Side Pi 3B at side sign, ribbon <50 cm, `chain=2`.
- [ ] Rear Pi 3B at rear sign, ribbon <50 cm, `chain=1`.
- [ ] Ethernet from each Pi to the front switch, tested with `ping` + state sync.
- [ ] Bucks mounted at each sign, per-panel fuses, star grounds, Pi taps fused separately.
- [ ] USB keypad at cab, USB audio to PA/amp, `dtparam=audio=off` set.
- [ ] Full-white load test per sign, voltage drop ≤100 mV, throttled flags clear.
- [ ] AP password changed from default, local UI login set, hotspot SSID/saved.
- [ ] Pairing done (§15), one program uploaded, code sheet printed for driver.

---

## 15. Power-on, pairing, first program

1. Power all three Pis. The installer already registered the front Pi: its boards show the 6-char **pairing code** (also in `journalctl -u nsl-pairing -f` and on `192.168.4.1`).
2. Join the phone hotspot so the front Pi reaches the portal (pairing record holds 15 min; `nsl-pairing` re-registers automatically if it lapses).
3. Portal → Controllers → Pair controller → enter code + name, click Pair. The Pi polls `POST /api/device/claim`, fetches its device token itself, saves it (`/var/lib/nsl/device-token` 0600 + agent drop-in), restarts `nsl-agent` and starts syncing - no token copy-paste.
4. Build a program in the portal (General → Services/Destinations/Stops → Display layouts → Preview). Publish → Upload to the bus. Note the total download size.
5. Next hotspot session the bus pulls it. Select a destination by code from keypad, portal, or local UI. Check all three signs + announcement chain.
6. Management: rename, online/last-seen/version, installed vs desired per program, manual sync trigger, unpair (revokes token).

---

## 16. File layout on the Pi

```
Front Pi (/var/lib/nsl/):
  controller.db                      registry, state, config, outbox (WAL mode)
  programs/<program_id>/<version>.nslprog   installed program files (read-only)
  audio/<aa>/<hash>.flac             content-addressed clips
  incoming/                          partial downloads
Receivers:
  cached state + layouts only (announcements play from front Pi)
```

`controller.db` holds `desired_programs`, `installed_programs`, `audio_cache`, single-row `state`, `outbox`, plus config tables (display outputs, matrix options, keypad, GPIO, Wi-Fi, release channel). State keys are stable UUIDs so upgrades carry selection by id; missing destination/stop falls back to first stop or idle; override survives only on the same stop.

Offline package `.nslpack` (zip): `manifest.json` + `program.nslprog` + `audio/…`. Used for local import at `192.168.4.1`, backup, server-to-server moves. Still fully validated on import; marked `source=local_import` and reported at next sync.

---

## 17. Configuration reference

Matrix options (same names as the current command-line flags):

| Option | Default | Notes |
|---|---|---|
| `led-rows` / `led-cols` | 40 / 80 | Per 80x40 panel |
| `led-chain` | front 3, side 2, rear 1 | Panels per chain |
| `led-parallel` | 1 | 1 per Pi in bus topology (bench profiles use 3) |
| `led-brightness` | 70 | Plus per-output trim |
| `led-gpio-mapping` | `regular` | Match to HAT |
| `led-slowdown-gpio` | 4 | Try 2–5 on flicker |
| `led-pwm-bits` | 11 → 7 if refresh poor | Lower = faster, coarser |
| `no-hardware-pulse` | on (default) | Avoids audio clash |
| `hardware-pulse` | off → on once `dtparam=audio=off` | Best picture |

Display output record: name, panel count, `parallel_chain`, `x_offset`, brightness, enabled, bound program display, `transport`, `receiver_addr`. Presets: (1) 10 m bus default, (2)/(3) bench-only 3-chain / single-chain-6.

Pi 4 tuning: watch `led-show-refresh`, lower `pwm-bits` / raise `pwm-lsb-nanoseconds` on flicker, enable hardware pulse, reserve one core for the display driver, USB audio (I2S also uses pins - avoid with HUB75).

---

## 18. Audio standard

- Mono 16-bit FLAC at one fixed system rate (22.05 or 44.1 kHz), silence trimmed, loudness-normalised, SHA-256 addressed. Identical clips stored/downloaded once.
- Sources: file upload (MP3/WAV/OGG/M4A/FLAC, transcoded) or Piper TTS (type text → pick voice → generate → save; text+settings kept for regen; bulk generate per stop/service/program skipping existing).
- Voices: built-in default + per-organisation uploads (`.onnx` + `.onnx.json` pair, validated, test-synthesised, size-limited, licence note + rights tick). Runs sandboxed on the server; Pi never runs Piper.
- Player decodes to PCM and chains sample-accurately - no gapless-mode dependency.
- Bus output is USB audio by default: `dtparam=audio=off` kills the onboard jack because hardware pulsing shares PWM hardware with HUB75. The 3.5 mm jack works too, with a tradeoff: keep software pulsing (`no-hardware-pulse`, the default) and the jack stays usable at the cost of slightly more flicker. To use it: make sure `dtparam=audio=off` is **absent** from `/boot/firmware/config.txt` (and `/boot/config.txt`), reboot, confirm `aplay -l` lists the Headphones card, then:
  ```bash
  speaker-test -c1 -t wav -D plughw:0,0   # short test tone, Ctrl+C to stop
  sqlite3 /var/lib/nsl/controller.db "UPDATE controller_config SET audio_device='plughw:0,0' WHERE id=1"
  sudo systemctl restart nsl-player
  ```
  Never enable hardware pulsing while using the jack (best picture and onboard audio are mutually exclusive). USB audio has no such tradeoff, which is why it stays the default recommendation:
  ```bash
  aplay -l                       # note the card, e.g. card 1
  speaker-test -c1 -t wav -D plughw:1,0   # short test tone, Ctrl+C to stop
  sqlite3 /var/lib/nsl/controller.db "UPDATE controller_config SET audio_device='plughw:1,0' WHERE id=1"
  sudo systemctl restart nsl-player
  ```
  Announcements auto-play on stop change; keypad/portal/local-UI announce buttons play immediately. No clips uploaded = silence (check the portal preview lists clips first).

  **Test audio (no program needed):** the controller installer plays a local two-tone chime at setup so you can verify the USB adapter → amp → PA chain before anything has synced. The same chime plays automatically once when pairing completes (`nsl-pairing`, after the token is saved). Re-play any time with `curl -X POST http://192.168.4.1/api/audio-test` (front Pi, AP or bus LAN). If the chime is silent, check `aplay -l`, the USB adapter seating, amp volume, and `audio_device` in `controller.db` - portal clips can only work once this local tone is heard.

---

## 19. Board layout standard

Canvas = real pixels (front 240x40, side 160x40, rear 80x40). A layout = elements of type `field` (`num`/`via`/`dest`/extra) or fixed `text`. Per element: x/y/w/h, font, colour, align/valign, scroll (`none`/`always`/`if_too_long`), flash, visibility (`always`/`has_text`/`override_active`/`override_inactive`), `scale` (integer 1–8, default 1; Hanover bitmap fonts only - integer-upscales the glyphs for tall displays, e.g. a 19 px font at ×2 fills 40 px), `spacing` (integer −8…16 px, default 0; extra tracking between Hanover glyphs, negative tightens). All elements always draw - there is only one pages system: the destination's board pages (each with its own `via`/`dest`), which rotate every `page_seconds`. Static text is never clipped to its box - ink may overflow and still shows (wide glyphs, tall fonts); only scrolling crops to the box. Per-layout override behaviour: `replace` / `hide_others` / `full`.

Example (front, `num | via over dest`):

```json
{
  "schema": 1, "width": 240, "height": 40, "page_seconds": 3,
  "override": { "mode": "replace", "replaces": "dest", "hide": ["via"] },
  "elements": [
    { "type": "field", "field": "num", "x": 0, "y": 0, "w": 48, "h": 40,
      "font": "pixel-large", "colour": "#FFB000",
      "align": "centre", "valign": "middle", "scroll": "none", "page": 1, "show_if": "always" },
    { "type": "field", "field": "via", "x": 52, "y": 0, "w": 188, "h": 16,
      "font": "pixel-small", "colour": "#FFB000",
      "align": "left", "valign": "middle", "scroll": "if_too_long", "page": 1, "show_if": "has_text" },
    { "type": "field", "field": "dest", "x": 52, "y": 16, "w": 188, "h": 24,
      "font": "pixel-medium", "colour": "#FFB000",
      "align": "left", "valign": "middle", "scroll": "if_too_long", "page": 1, "show_if": "always" }
  ]
}
```

Resolution order per destination per display: own layout → display default template → built-in auto-fit (`num` left + dest fill). Same renderer in portal preview, main Pi, and receivers - snapshot-tested identical.

### 19.1 Hanover bitmap fonts

`HANOVERFONTS/*.Fnt` (shipped in the repo, installed to `/var/lib/nsl/fonts`): real Hanover HELEN glyphs, 1:1 pixels, LSB = top row. Use font id `han-<STEM>` (e.g. `han-0705C1E1`) on any layout element, in the editor dropdown or JSON. Names carry approximate pixel height - most full-coverage workhorses are the 19 px set (`19ArialB`, `movia18`, `1713B3E1`…). ASCII 32–126 renders true; other code points fall back to `?`. The `/api/fonts` list reports per-font `coverage` (of 95 printable ASCII); avoid near-empty files (`bigbus*` carry 1–2 glyphs only - test remnants, not fonts). `scale` (1–8, default 1) integer-upscales Hanover glyphs - ×2 of 19 px fills a 40 px board. The editor preview, stop-by-stop preview, and bus renderer all blit the identical glyphs, so WYSIWYG holds. Unknown `han-` ids fall back with a warning, never a blank board.

---

## 20. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Rear/side blank, front OK | Bus LAN down, receiver down, or wrong `receiver_addr` | `ping` receivers, check receiver service, re-check config |
| Flicker/tearing all panels | `pwm-bits` too high, no hardware pulse, weak 5 V | Lower `pwm-bits` to 7, enable hardware pulse (audio off), measure 5 V under load |
| Panels reset on bright text | Thin wire / brown-out | Thicken 5 V runs, shorten, check fuses/holders, re-measure drop |
| `INVALID` on every code | Wrong active program | Select correct program first; codes are per-program |
| No audio / distorted | Onboard audio still on, wrong ALSA device | `dtparam=audio=off`, select USB device, test clip from diagnostics |
| Keypad dead | Grabbed by console, wrong device match | Check `evdev` device match string, replug, check input service log |
| Pi throttled flag set | Undervoltage | Dedicated Pi tap, thicker wire, check buck rating |
| Sync never completes | Hotspot drops, large audio backlog | Stay on hotspot longer; order is program→audio→software so short windows still help |
| Update failed / rolled back | Bad bundle, health check failed | Check release channel, logs; previous version auto-restored, reported to portal |
| Override won't clear | Working as designed | Changes only on stop change / new destination/service/program, or explicit Clear |

---

## 21. Safety

- Fuse everything: converter inputs + every panel branch + Pi taps. Automotive fuses, accessible holders.
- 5 V high-current wiring: no breadboard jumpers in the bus. Crimped/ferruled, strain-relieved, vibration-proof.
- Common the grounds at each sign, but never use the bus chassis as the sole 5 V return over 10 m.
- Read-only root + watchdog on all Pis (SD-card power-loss protection). Back up portal DB nightly + audio/voices/programs/releases.
- Signed updates only; local controller on AP only with changed password + login.

---

## 22. Repo files

| File | Role |
|---|---|
| `README.md` | This wiki (how to build/run/operate) |
| `plan.md` | Build plan (phases, API, schema summary, decisions) |
| `format.md` | `.nslprog` spec (schema, layout JSON, validation, transfer, install) |
| `example images/` | Reference photos |

---

## 23. Glossary

- **Program** - a set of services/destinations/stops/layouts published as one `.nslprog` version.
- **Service** - e.g. route 100, with its three announcement clips.
- **Destination** - a code + board fields (`num`/`via`/`dest`) + stops + layouts.
- **Code** - 1–8 digit selector, unique per program.
- **Stop** - ordered point with name audio + board text/override.
- **Display / output** - program display (front/side/rear sizes) vs physical output (panel chain + driver).
- **Override** - replacement board message with the lifetime rule in §7.
- **`.nslpack`** - offline zip (program + audio) for local import/backup.
- **Update mode** - temporary hotspot-join for syncing.
