# Project Plan: Bus Stop Announcement System

A multi-user online portal at **nslboards.com/portal** for building **programs** (services, destinations, stops, audio, board layouts) and a Raspberry Pi 4 controller that programs are uploaded to. The Pi plays announcements, drives the LED destination boards, and updates itself from the portal whenever it is connected (for example through a phone hotspot). The driver controls it with a **USB keypad**, the **portal's remote control page**, or the Pi's own **local controller** at **192.168.4.1** (works with no internet). GPIO buttons are optional.

The program data format is defined in **`program-format.md`**.

---

## 1. Goals

1. **Multi-user** portal where each account (organisation) has its own private data.
2. **Programs**: users create programs in the portal and upload them to controllers. A program holds services, destinations (with 8-digit codes), stops, audio references and board layouts, stored in the new **program file format** (`.nslprog`, a SQLite file; see `program-format.md`).
3. **Web portal** at `nslboards.com/portal` to pair controllers, build programs, manage audio and voices, design what each board shows, and remotely control a controller.
4. **Raspberry Pi 4 controller** that runs programs, plays announcements and drives the LED boards (one logical controller per bus: main Pi drives front directly, side/rear over network — see 6.6.1), controlled by a USB keypad, the portal and a local web controller (and optional GPIO buttons).
5. **Three displays from one logical controller** (front, side, rear) with a documented wiring method for a 10 m bus (see 6.6). Front is driven directly from the main Pi's GPIO; side and rear are too far for HUB75 ribbon and use networked receiver Pis.
6. **Local controller** on the Pi at `192.168.4.1` (its own Wi-Fi access point) that works fully offline.
7. **Works with an occasional connection**: the Pi joins your phone's hotspot when you want to update it; sync is small, resumable and safe.
8. **Auto-update**: the Pi updates itself from the portal, safely, with rollback.
9. **Fully user-definable**: board content for every destination, board hardware layout, keypad keys and what they do (and GPIO pins if used).
10. **Audio**: users upload their own clips, generate clips with Piper, and **upload their own Piper voice models**.
11. **Legacy-inspired UI**: light, modern, but in the spirit of classic transport software such as Hanover Helon (beveled panels, grey chrome, dialog-style forms).
12. **Self-hosted** on the owner's own hardware.

### Non-goals (for v1)
- Billing and payments. These are handled outside the system. The portal only needs an organisation `active / suspended` flag that an external process can set.
- Button feedback outputs (LED, buzzer) and spoken confirmations of button presses.
- Matrix keypads on GPIO or I2C. USB keypads only for now.
- Live GPS-triggered announcements (design leaves room for it, see Phase 6).
- Public passenger-facing features.
- Native mobile apps.

---

## 2. System Overview

```
┌──────────────────────────────┐   HTTPS / WebSocket (outbound   ┌────────────────────────────────┐
│  Web Portal                  │   from the Pi, over the phone   │  Raspberry Pi 4 Controller     │
│  nslboards.com/portal        │   hotspot when connected)       │  - Agent (sync + update)       │
│  - Accounts & orgs           │ ◄─────────────────────────────► │  - Controller core (state)     │
│  - Program editor            │   pairing, program upload,      │  - Player (audio engine)       │
│  - Board layout editor       │   audio download, remote        │  - Display driver (LED)        │
│  - Audio library + Piper TTS │   commands, live state,         │  - Keypad (+ optional GPIO)    │
│  - Voice uploads             │   updates, heartbeat            │  - Local controller (web UI)   │
│  - Controller config         │                                 │  - Local cache (offline)       │
│  - Remote control            │                                 └───────────────┬────────────────┘
│  - Release management        │                                                 │ own Wi-Fi access point
└────────────┬─────────────────┘                                  phone / laptop ─┘  http://192.168.4.1
             │
     ┌───────┴──────────────────┐
     │ SQLite (dev)             │   File storage: local disk or MinIO
     │ PostgreSQL (prod)        │   (audio, voices, .nslprog files, release bundles)
     └──────────────────────────┘
```

The Pi must keep working with **no internet**. It runs entirely from its local cache, the keypad always works, and the **local controller at 192.168.4.1** gives full control and configuration without the portal. Remote control from the portal needs the Pi to be online.

---

## 3. Proposed Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Portal frontend | TypeScript + React (Next.js) | Served under the base path `/portal`. Custom CSS for the legacy look, no heavy UI kit |
| Portal backend | Next.js API routes or a separate Node (Fastify) API | REST + WebSocket for live device state and remote control |
| Database (server) | **SQLite for local dev/testing, PostgreSQL for production** | One schema, one migration set, chosen by `DATABASE_URL` (see 3.1) |
| Database (Pi) | SQLite (`controller.db`) plus one SQLite `.nslprog` file per installed program version | See `program-format.md` |
| File storage | Local disk volume or MinIO (S3-compatible) on your own hardware | Audio, voices, `.nslprog` files, release bundles. Signed URLs for downloads |
| Text-to-speech | **Piper**, running on the portal server | Built-in default is your existing voice; users can upload their own voices (see 5.3b) |
| Auth | Email + password (Argon2), sessions, optional TOTP 2FA | Password reset by email |
| Pi agent | Python 3 running as systemd services | Sync, program install, update, heartbeat, remote commands |
| Pi local controller | Small web server on the Pi (for example FastAPI) serving the same UI components | Reachable at `192.168.4.1` on the Pi's access point |
| Pi network | NetworkManager (or `hostapd` + `dnsmasq`) with hotspot-client and access-point modes | See 6.5 |
| Audio playback | Player module decoding FLAC clips to PCM and chaining them to ALSA | Sample-accurate joins. USB audio device or I2S DAC (see 6.7) |
| Board output | Waveshare fork of `rpi-rgb-led-matrix` (Python bindings) driving HUB75 RGB-Matrix-Px-80x40 panels | Own privileged process; Pi 4B. Built on your existing display code (see 6.6) |
| Keypad | USB keyboard / numpad read through `evdev` | Number row and numpad keys (see 6.8) |
| GPIO (optional) | `gpiozero` with the `lgpio` backend | Only if pins are free (see 6.7) |
| Packaging/updates | Signed release bundles + A/B directories + systemd | Rollback on failed health check |

These are recommendations, and can be swapped without changing the rest of the plan.

### 3.1 Database: SQLite locally, PostgreSQL in production

- The portal reads `DATABASE_URL`:
  - Local: `sqlite:./dev.db` (zero setup, file-based, easy to wipe and reseed)
  - Production: `postgres://user:pass@host:5432/dbname`
- Use a query builder / ORM that supports both (for example Drizzle, Prisma or Knex) with **one migration set** that runs on both.
- The program tables are defined once and match `program-format.md`; the only dialect difference is the destination code check (SQLite `GLOB`, PostgreSQL regular expression).
- To stay portable, avoid Postgres-only features in the schema and queries: no `jsonb` operators, arrays, row-level security or Postgres enums. Store UUIDs as text, JSON as text, timestamps as UTC text.
- Destination codes are stored as **text**, so leading zeros are kept (`00000042`).
- Tenant isolation is enforced in the application data layer (every query scoped by `organisation_id`), so it behaves identically on both databases.
- **CI runs the full test suite against both SQLite and PostgreSQL**, including contract tests that publish the same program on both and require the same `content_hash`.
- A `seed` script creates a demo organisation, a sample program with a service (e.g. 100, Burton / Lichfield), destination codes, layouts and sample audio for local testing.

### 3.2 Self-hosting, domain and URLs

- Deployed with **Docker Compose** on your own hardware: `portal`, `db` (PostgreSQL), `storage` (local volume or MinIO), `tts` worker (Piper), `proxy` (Caddy or nginx with TLS).
- **Public URL:** `https://nslboards.com/portal`. The proxy routes `/portal` to the app, so the web app is built with base path `/portal` (API under `/portal/api`, device WebSocket under `/portal/api/device/stream`). The rest of `nslboards.com` can be a separate site.
- **Local URL on the Pi:** `http://192.168.4.1` (see 6.3). No internet needed.
- Pis only make **outbound** HTTPS/WebSocket connections to `nslboards.com/portal`, so no inbound ports are needed on the vehicles. The portal is exposed to the internet through the proxy, with a public TLS certificate (Let's Encrypt).
- **Backups**: nightly PostgreSQL dump plus a copy of the audio, voices, programs and releases folders, with a tested restore procedure.
- Portal upgrades with `docker compose pull && docker compose up -d`; migrations run automatically on start.
- Simple health endpoint and disk-space check so you know before storage fills.
- The release **signing private key** stays on your own machine (not on the web server), so a compromised portal cannot push malicious updates to the Pis.

---

## 4. Data Model

The program data model is specified in full in **`program-format.md`** (schema, JSON layouts, validation, transfer, install). This section summarises it and lists the other tables.

### 4.1 Entities

```
Organisation  (status: active | suspended, set externally)
  └─ User (role: owner | editor | viewer)
  └─ Voice (Piper models; server-wide built-in plus ones users upload)
  └─ Audio clips (library, shared across programs, identified by SHA-256)
  └─ Controller (a paired Raspberry Pi)
       ├─ installed programs (installed version, active program)
       ├─ display outputs   (front / side / rear panel regions, each bound to a program display name)
       ├─ keypad config     (USB keypad, key map)
       ├─ GPIO bindings     (optional: pin → press type → ordered actions)
       └─ saved networks    (phone hotspot, etc.)
  └─ Program            what you upload to a controller (published as a .nslprog file)
       ├─ px_width and Displays   e.g. front 240x40, side 160x40, rear 80x40
       ├─ Override presets        e.g. "Do not board", "Terminating bus"
       ├─ Service   e.g. "100"  (MANY per program)
       │    ├─ announcement clips
       │    │     • next_stop_clip      "The next stop is…"
       │    │     • approaching_clip    "Approaching…"
       │    │     • last_stop_clip      "…where this service terminates"
       │    └─ Destination (MANY per service)
       │         ├─ code               8 digits, unique within the PROGRAM
       │         ├─ board fields       num, via, dest (+ any extra fields)
       │         ├─ layouts            one per program display, fully user-defined
       │         └─ Stop (ordered)     e.g. "Burton New Street"
       │              ├─ stop_audio    (clip of the stop name)
       │              ├─ board text    (what the board shows)
       │              └─ board override (e.g. "Do not board", "Terminating bus")
```

### 4.2 Tables (summary)

**Program tables** (server editing copy; identical to the `.nslprog` schema plus `organisation_id`, `program_id` and draft columns, see `program-format.md` section 9): `programs`, `program_displays`, `services`, `destinations`, `stops`, `destination_layouts`, `layout_templates`, `override_presets`, `program_versions`.

**Other tables:**

| Table | Key fields |
|---|---|
| `organisations` | id, name, status (active/suspended), created_at |
| `users` | id, email, password_hash, name, totp_secret? |
| `memberships` | user_id, organisation_id, role |
| `controllers` | id, organisation_id, name, device_token_hash, status, last_seen_at, app_version, channel, supported_formats |
| `pairing_codes` | code, controller_id, expires_at, claimed_by_user_id? |
| `controller_programs` | controller_id, program_id, desired_version, installed_version, status (pending/downloading/installed/active/failed), last_error, updated_at |
| `voices` | id, organisation_id? (null = server-wide), name, model_key, config_key, language, sample_rate, is_default, licence_note, uploaded_by, size_bytes |
| `audio_files` | id, organisation_id, sha256, filename, storage_key, duration_ms, source (upload/tts), tts_text?, tts_voice_id?, tts_params?, created_at |
| `display_outputs` | id, controller_id, display_name, width_px, height_px, parallel_chain, x_offset, enabled, brightness, logical_width_px?, transport (local/network), receiver_addr? |
| `keypad_config` | controller_id, device_match (USB device name/id), auto_select, entry_timeout_ms, entry_display |
| `keypad_keys` | id, controller_id, key (evdev key name), role (digit/enter/clear/action), step_order, action, action_param |
| `gpio_bindings` | id, controller_id, label, pin (BCM number), pull (up/down/none), active_low, debounce_ms |
| `gpio_actions` | id, binding_id, press_type (press/long/double), step_order, action, action_param |
| `wifi_networks` | controller_id, ssid, secret (encrypted), priority, kind (hotspot/other) |
| `controller_config` | controller_id, version, matrix options (see 6.6), audio device, access point name, startup program/destination |
| `command_log` | id, controller_id, user_id?, source (portal/keypad/gpio/local), command, at |
| `releases` | id, version, channel, bundle_key, signature, notes, created_at |

Every row that belongs to a customer carries `organisation_id` (directly or via parent) and every query is scoped by it. On the Pi, `controller.db` holds the controller tables above (config, `installed_programs`, `state`, `audio_cache`, `outbox`).

### 4.3 Programs and destination codes

- A **program** is the unit users build and upload to a controller. It contains many services, each with many destinations. A controller can hold several programs and has one **active program** at a time.
- Every destination has a **code of exactly 8 digits** (`0`-`9`), **unique within its program**. Two destinations in the same program can never share a code, even across different services. The same code can be reused in a different program.
- Because the code is unique in the program, entering a code selects the service **and** destination in one go.
- The editor suggests the next free code and validates length, digits and uniqueness as you type (inline message under the field). The database enforces it with `UNIQUE (program_id, code)` on the server and `UNIQUE (code)` inside each `.nslprog` file.
- Destination ids are stable across versions, so editing a code never breaks saved selections. Codes are used to pick a destination by keypad, remote control and local controller.
- Destination board fields: `num` (route number, defaults to the service number unless overridden), `via`, `dest`, plus any number of extra user-named fields (for example `note`). These are what the board layouts display (see 5.3c, 6.6).

### 4.4 Announcement composition

Announcements are **strung together from clips** at play time:

| Moment | Clip sequence |
|---|---|
| Next stop (normal) | `next_stop_clip` → `stop.audio` |
| Approaching (normal) | `approaching_clip` → `stop.audio` |
| Next stop (terminus) | `next_stop_clip` → `stop.audio` → `last_stop_clip` |
| Approaching (terminus) | `approaching_clip` → `stop.audio` → `last_stop_clip` |

Examples:
- Normal stop: *"The next stop is"* + *"Burton New Street"*
- Last stop: *"The next stop is"* + *"Derby Bus Station"* + *"where this service terminates"*

Clips are decoded and joined sample-accurately so they sound like one sentence. The `last_stop_clip` is a **suffix** appended automatically when the stop is flagged as the terminus. A service setting (`terminus_clip_mode`) lets the user choose whether it is appended to "next stop" only, "approaching" only, or both (default: both).

### 4.5 Board display per stop

Each stop has:
- **Board text**: what the board shows for the stop (defaults to the stop name).
- **Board override**: a selectable or free-text message that replaces the destination text, e.g. `Do not board`, `Terminating bus`, `Not in service`, or custom. Empty means the normal destination is shown.

**Override lifetime (runtime rule):** once an override becomes active it **stays on the board until the current stop changes**, or until a **new destination, service or program is selected**. It is never cleared by a timer, by replaying an announcement, by a sync, by a program upgrade or by a portal reconnect. A stop's saved override activates when that stop becomes the current stop. A driver can also apply an override manually from the keypad, the portal or the local controller, and the same clearing rules apply. **Clear override** removes it immediately. The override is saved in the controller's state, so it survives a restart.

---

## 5. Web Portal (nslboards.com/portal)

### 5.1 Accounts and multi-user
- Sign up, sign in, sign out, password reset.
- A user belongs to one or more organisations; roles: **Owner** (everything, including users), **Editor** (programs, audio, voices, controllers, remote control), **Viewer** (read-only).
- Invite users to an organisation by email.
- Strict tenant isolation: no cross-organisation reads or writes, enforced in the data layer and covered by tests.
- A suspended organisation can sign in read-only and its controllers stop receiving updates (status is set outside the system, e.g. by your billing process or an admin).

### 5.2 Pair a controller (Pi) to your account

**Flow:**
1. A fresh Pi boots, has no token, and shows a **pairing code on its LED boards** (6 characters). The same code is also shown on the local controller at `192.168.4.1`.
2. Pairing needs the internet, so turn on your phone hotspot; the Pi joins it (see 6.5) and registers with the portal, receiving a pending `controller` record tied to that code (valid 15 minutes).
3. In the portal: **Controllers → Pair controller**, enter the code and give the controller a name.
4. Portal links the controller to the organisation and issues a long-lived **device token** (stored hashed server-side).
5. The Pi receives the token over its open connection, saves it, and begins syncing.

**Management:** rename, see online/offline, last seen, app version, release channel, installed programs and versions with status, unpair (revokes token), and trigger a manual sync.

### 5.2a Controller configuration (displays, keypad, network)

Per controller, editable in the portal and on the local controller at `192.168.4.1`. Config is versioned and synced both ways when the Pi is online; the most recent save wins.

- **Display outputs**: add, rename and remove outputs (default: front, side, rear). For each set the number of 80x40 panels, which parallel chain it is wired to, pixel offset, brightness, enabled state, and which **program display name** it plays (see 6.6). A live preview shows the resulting pixel layout. **Wiring profile presets**: "3 parallel chains (3 + 2 + 1 panels)" and "single chain of 6 panels".
- **Matrix options**: the same options as your current command line flags (rows, cols, chain, parallel, brightness, GPIO mapping, slowdown, row address type, multiplexing, PWM bits, refresh limit, hardware pulse, panel width), with your current values as defaults.
- **Keypad**: pick the USB keypad device, the key map and entry behaviour (see 6.8).
- **GPIO bindings** (optional): add bindings on free pins with an **ordered list of actions** per press type (see 6.7).
- **Network**: saved Wi-Fi networks (your phone hotspot), access point name and password, hotspot join window (see 6.5).
- **Startup behaviour**: which program and destination (by code) to select at power-on, restore the last state, or "idle" (blank board until chosen).
- **Test tools**: send a test pattern to a display, flash a test message, play a test announcement, and show live keypad and GPIO state while pressing keys.

### 5.3 Programs

**Programs list**: table of programs with name, services, destinations, published version, which controllers have it and at what version. Buttons: New, Edit, Duplicate, Delete, Upload to controllers, Export, Import.

**Program editor** (tabbed dialog style):
1. **General** tab: program name, `px_width`, default display, the **displays** the program defines (name, width, height; for example front 240x40, side 160x40, rear 80x40), and **override presets**.
2. **Services** tab: add many services; opens the service editor (5.3a).
3. **Codes** tab: table of every destination in the program with its 8-digit code, service and name. Sort, search, find duplicates (flagged in red), bulk re-number, export/print a code sheet for drivers.
4. **Uploads** tab: which controllers have this program, desired vs installed version, status and last error.

**Draft and publish:** edits are saved as a draft and do not touch any controller. **Upload to controllers** runs validation (see `program-format.md` section 8), builds an immutable numbered `.nslprog` version, and queues it for the chosen controllers. The upload dialog shows the **total download size** (program file plus new audio). Because the Pi connects only now and then, "queued" means the controller will take it the next time it is online. The Pi installs it atomically and reports success or failure; the previous version stays until the new one verifies, and the active program is never switched mid-run (see 6.2). If nothing changed since the last version, no new version is created.

**Export / import:** export a published version as a `.nslpack` (zip with the program file and its audio) for backup, moving between servers, or loading on the local controller. Import a `.nslpack` into the portal to create a new draft program.

### 5.3a Services, destinations and stops

**Service editor** (inside a program):

1. **General** tab
   - Service number/name
   - Announcement clips: Next stop, Approaching, Last stop (each: choose from library, upload, or generate with Piper; with play button)
   - Terminus clip mode (next stop, approaching, both)
2. **Destinations** tab
   - Add as many destinations as needed (e.g. Burton, Lichfield, Derby)
   - Per destination: **Code** (8 digits, unique in the program), **Name**, **Route number** (defaults to the service number), **Via**, **Destination text**, and extra fields
   - Reorder, rename, delete
   - Copy a destination reversed (to build the return direction quickly; a new code is suggested)
3. **Stops** tab (for the selected destination)
   - Ordered list; drag to reorder
   - Edit stop in a side panel: Stop name, Stop audio, Board text, Board override
   - Mark terminus (must be the last stop)
   - Preview button plays the full announcement string for that stop (next / approaching / last)
4. **Display** tab (for the selected destination): the board layout editor (see 5.3c)
5. **Preview** tab
   - Simulates the journey: steps through stops, plays audio and shows every display as it would appear

### 5.3b Audio library, Piper and voice uploads

**Ways to get a clip:**
1. **Upload** your own file (MP3/WAV/OGG/M4A/FLAC).
2. **Generate with Piper**: type the text, pick a voice, press Generate, listen, then Save. The clip is stored as audio, and the text and settings are kept so it can be edited and regenerated.

**Generate dialog** (labels above fields, no placeholder text): Text to speak, Display name, Voice, Speed, Pause between sentences, and an optional **Spoken form** field for pronunciation fixes (for example spell a place name phonetically while the label keeps the real name).

**Voices:**
- Your existing Piper model is installed on the server as the **built-in default voice**.
- **Users can upload their own voice models** (Editors and Owners): the model file (`.onnx`) and its config (`.onnx.json`). The voice belongs to that organisation, is only visible to it, and appears in the voice list for generating clips.
- Upload checks: the pair is complete and matches, JSON config is valid, sample rate and language are read from it, a size limit applies, and a short test phrase is synthesised before the voice is accepted.
- The uploader ticks a box confirming they have the rights to use the model, and can enter a licence note (voice models carry their own licences).
- Voices can be renamed, set as the organisation's default, or deleted. Clips already rendered stay valid after a voice is deleted, but cannot be regenerated.

**Processing (all clips, uploaded or generated):** convert to the standard clip format (mono, 16-bit FLAC at one fixed sample rate), trim leading and trailing silence, normalise loudness to a common target, store duration and SHA-256. The SHA-256 is the clip's identity, so identical clips are stored and downloaded once. This is what makes separate clips sound smooth when strung together.

**Bulk tools:**
- "Generate audio for all stops in this destination/service/program" from the stop names, skipping stops that already have audio.
- "Regenerate" a clip, or all clips using a voice, after changing the voice or text.

**Housekeeping:** rename, delete (blocked while in use), reuse clips across services and programs, and an in-use list per clip.

**Where it runs:** Piper runs on the portal server as a background job queue, in a sandboxed worker (no network, CPU/memory/time limits). The Pi never needs Piper; it downloads finished audio and works offline.

### 5.3c Board layout editor (Display tab)

Lets the user decide **exactly what each display shows for each destination**.

- Choose a program display (front, side, rear, or any custom one) to edit. The canvas is that display's real pixel size, for example 240 x 40, 160 x 40, 80 x 40.
- A layout is a set of **elements**. Each element is either a **field** (`num`, `via`, `dest`, or an extra field) or **fixed text**.
- Per element: position and size, font, colour, alignment, scrolling (none / scroll / scroll only if too long), flashing, and **page** (elements on different pages alternate on a timer, for example showing `via` and `dest` alternately).
- Per element **visibility**: always, only if the field has text, only while an override is active, or only while it is not.
- **Override behaviour** per layout: replace one element, hide others, or show the override as a full-display message.
- Example setups (all user-defined, nothing fixed):
  - **Front (240 x 40)**: `num`, `via` and `dest`
  - **Side (160 x 40)**: `num` and `dest` only
  - **Rear (80 x 40)**: `num` only
- **Layout templates** per display: save a layout as a template, set a default per display, apply a template to a destination, a whole service, or every destination in the program.
- A destination with no layout of its own for a display uses that display's default template, and then a built-in auto-fit layout.
- Live preview in an LED-style dot-matrix renderer, including the override and paging behaviour. The same renderer is used on the Pi.

### 5.4 Form rules (from requirements)
- **Every text box has a visible label above it. No placeholder text anywhere.**
- Inline validation messages below the field.
- Required fields marked in the label.

### 5.5 Admin (internal)
- Release management: upload a signed bundle, choose channel (stable/beta), roll out to all or selected controllers.
- Server-wide voice management: install voices, set the default, review and remove uploaded voices.
- Organisation status (active/suspended) for external billing control.
- Basic audit log of user actions.

### 5.6 Remote control

A per-controller page that acts as the driver's screen when they are not using the keypad, or lets a dispatcher control a vehicle. It shares its components with the local controller (6.3). It only works while the Pi is online (for example on your phone hotspot).

- **Live status**: online/offline, active program, selected service and destination (name and code), current stop, next stop, active override, volume, last announcement.
- **Live previews** of every display exactly as the LED panels show them.
- **Controls**: select program, select destination (list or type the 8-digit code), advance stop, previous stop, play next-stop / approaching / last-stop announcement, replay, apply a board override (choose a preset or type text), clear override, volume, mute, and "apply pending update now".
- Commands go over the controller's WebSocket and are acknowledged by the Pi; failures are shown on the page.
- Large buttons so it is usable on a phone.
- Editors can control; viewers can only watch. Every command is written to the command log with user and time.
- The Pi's controller core is the single source of truth for state. Keypad, portal, local controller (and GPIO) all send the same commands to it, and the most recent command wins.
- If the Pi is offline the page shows that and disables the controls; the keypad and the local controller keep working on the vehicle.

---

## 6. Raspberry Pi Software

### 6.1 Components

| Component | Responsibility |
|---|---|
| **Agent** | Pairing, auth, sync, program download and install, heartbeat, update check, remote command channel, network mode switching. Runs as a systemd service. |
| **Controller core** | Holds the current state (program, service, destination, stop, override, volume), saved in `controller.db`. Receives commands from keypad, portal, local controller (and GPIO), applies the override lifetime rule, and tells the player and display driver what to do. |
| **Player** | Decodes and chains clips into one announcement. |
| **Display driver** | Separate privileged process that owns the HUB75 matrix and renders each display's layout (see 6.6). Also has a virtual output for development. |
| **Input service** | Reads the USB keypad (and optional GPIO pins), debounces, and sends actions to the controller core (see 6.7, 6.8). |
| **Local controller** | Web interface at `http://192.168.4.1` for control, configuration and diagnostics, working offline (see 6.3). |
| **Local store** | `controller.db` (SQLite), installed `.nslprog` files and a cache of audio files (see `program-format.md` section 11). |

### 6.2 Sync and program install

The Pi connects only when you bring up the phone hotspot (or another saved network), so sync is designed for short, interrupted connections.

- **On connect:** register/authenticate, send heartbeat, send queued events from the outbox, then fetch the manifest: assigned programs with desired versions, config version and the latest release.
- **Small and incremental:** download the `.nslprog` file (tiny) first, then only the audio clips whose SHA-256 the Pi does not already have. Downloads are resumable (range requests) and every file is verified by hash.
- **Atomic install:** stage in `incoming/`, run the program file checks, move into place, record in `installed_programs`; the previous version stays until the new one is verified.
- **Safe activation:** a new version of the **active program** is installed in the background and applied only at a safe moment (when the driver selects a destination or program, on an idle point, or on "apply now" from the keypad, portal or local controller), so a running service is not interrupted. The current selection carries over by id; an override stays only while the same stop is current.
- **Order of work:** program data and config first, then audio, then software updates (see 6.4), so a short connection still delivers the most important changes.
- **Offline:** continue using the cache. A sync never changes the current state or clears an override.
- **Reporting:** installed versions, install results and the command log are queued in the outbox and sent at the next connection.

### 6.3 Local controller (192.168.4.1)

The Pi creates its own Wi-Fi access point. Join it from a phone or laptop and open `http://192.168.4.1`. It works with **no internet** and uses the same UI components as the portal's remote control page.

**Control** (same abilities as remote control, 5.6): choose program, destination (list or 8-digit code), advance/previous stop, announcements, overrides, volume, live display previews.

**Programs:** list installed programs and versions, choose the active program, apply a pending version, roll back to the previous version, and **import a `.nslpack` file** to install a program with no internet (see `program-format.md` section 12).

**Settings** (login required): display outputs and matrix options, keypad, GPIO bindings, audio device and volume, access point name and password, saved Wi-Fi networks, "join hotspot now", portal URL, release channel, controller name. Changes are versioned and sync to the portal when online.

**Diagnostics:** connection status, last sync, pairing code, test patterns, live keypad/GPIO state, logs, "Check for updates", restart, shut down.

**Access and safety:**
- The local web server is bound to the access point interface only (not exposed on any other network by default).
- The access point uses WPA2 with a per-unit generated password, and the local controller needs a login (PIN/password) for settings. Because it is plain HTTP on a private point-to-point network, no portal credentials are ever entered there; it uses the controller's local credentials only.
- Rate-limited login and a session timeout.

### 6.4 Auto-update

- On connect, the agent checks the portal for a newer release on the controller's channel.
- Releases are **signed bundles**; the Pi verifies the signature against a public key baked into the image before applying.
- **A/B layout**: install into an inactive directory, switch via symlink, restart the services.
- **Health check** after restart (agent, controller core, player, display driver, local controller respond within a timeout); if it fails, **roll back** automatically to the previous version and report the failure to the portal.
- Never switch software while a service is actively running unless forced; apply at the next idle moment, at the next restart, or on "apply now".
- A release is downloaded with resume and hash checks like programs, so a hotspot dropping mid-download is harmless.
- Report the current version and update status back to the portal.

### 6.5 Base image and networking
- Raspberry Pi OS Lite, headless, no desktop.
- Flashable image so a new unit boots straight to showing a pairing code on its displays.
- Read-only root with a writable data partition (protects the SD card against power loss).
- Onboard audio disabled (see 6.6, 6.7); USB audio or I2S DAC enabled.
- Watchdog enabled.

**Two network modes on the Pi's Wi-Fi radio:**

| Mode | When | What you get |
|---|---|---|
| **Hotspot client** | Your phone's hotspot is on and in range | Internet: sync, program upload, software updates, remote control from the portal. The Pi joins the saved hotspot (SSID and password set in config, more than one network allowed) |
| **Access point** | The saved hotspot is not available, or you ask for it | The Pi creates its own Wi-Fi network `NSL-<unit id>` at `192.168.4.1/24` with DHCP, serving the local controller. Works fully offline |

**Behaviour:**
- **On boot:** the Pi looks for a saved network for a short window (default 60 seconds). If it finds your hotspot it joins it; otherwise it starts the access point.
- **While in access point mode** with someone connected, the Pi does not switch away, because the single radio cannot do both reliably.
- **"Update mode":** a key on the keypad, a button on the local controller, or a command in the portal (if online) tells the Pi to drop the access point and try to join the hotspot for a set time (default 5 minutes), then return to the access point. Turn the phone hotspot on first, then start update mode, then watch progress in the portal.
- A phone cannot join the Pi's access point while it is hosting the hotspot, so while on the hotspot you control the Pi from the **portal's remote control page** (over mobile data), and use `192.168.4.1` only in access point mode.
- The first pairing has to be done while on the hotspot.
- **Optional hardware:** a second USB Wi-Fi adapter lets the Pi keep its access point up while also joining the hotspot (the single built-in radio can only do both on one shared channel, which is unreliable with a phone hotspot).
- Updates and uploads only happen when the Pi is online, and the portal shows each controller as "pending" until it next connects.

### 6.6 Display hardware, wiring and rendering (LED boards)

**Hardware (based on the Waveshare RGB-Matrix-Px-80x40 Raspberry Pi guide and your existing code):**
- Panels are 80x40 pixels, HUB75, driven from a **Raspberry Pi 4B** using the Waveshare fork of `rpi-rgb-led-matrix` and its Python bindings.
- The library drives up to **3 chains in parallel** from the Pi's 40-pin header; the Waveshare guide uses the same idea, wiring groups `[1]`, `[2]`, `[3]` for panels 1 to 3, where the control lines are shared and each chain has its own colour data lines.
- **Your existing display program becomes the base of the display driver.** The portal and Pi config use the **same option names and defaults as your current command line flags**, so your current settings carry over directly.

**Matrix options (your current defaults):**

| Option | Default | Notes |
|---|---|---|
| `led-rows` | 40 | Rows per panel (80x40 P4) |
| `led-cols` | 80 | Columns per panel |
| `led-chain` | 3 | Panels in one chain; width = cols x chain (3 gives 240 wide) |
| `led-parallel` | 1 | Set to 3 to drive front, side and rear on three chains (see 6.6.1) |
| `led-brightness` | 70 | Also adjustable per display output |
| `led-gpio-mapping` | `regular` | `regular`, `adafruit-hat`, `adafruit-hat-pwm` |
| `led-slowdown-gpio` | 4 | Needed on Pi 4; try 2 to 5 if flicker |
| `led-row-addr-type` | 0 | Advanced |
| `led-multiplexing` | 0 | Advanced |
| `led-pwm-bits` | 11 | The Waveshare guide notes 11 is heavy and 7 is common for dynamic content; lower it if refresh is poor with six panels |
| `led-limit-refresh` | 0 | 0 = no limit |
| `no-hardware-pulse` | on | Avoids the `snd_bcm2835` onboard sound clash, at the cost of slightly more flicker |
| `hardware-pulse` | off | Best quality. Use only once onboard sound is disabled (`dtparam=audio=off`), which the base image does |
| `panel-width` | 0 | Logical width override; 0 = use the program display's width (for example 240) |
| `display` | empty | Program display name this output plays (front/side/rear…); empty = the program's default display |

#### 6.6.1 Wiring three displays on a 10 m bus (confirmed constraints)

**Confirmed on this vehicle:**
- Front to rear distance: ~10 m (front at one end, rear at the other, side in the middle).
- Current wiring: front board wired directly into the main Pi's GPIO (works — keep for front only).
- Power: front 15 V feed already split down to 5 V for the 3 chained front panels at the sign. Side 10 V / 5 A and rear 5 V / 5 A feeds available, plus 5 V for the Pi(s) — each remote sign still needs its own local 5 V regulation (see Power below).
- Spares on hand: 1x Pi 3B, 1x Pi 4, plus Pico Ws. No existing programs to convert (no converter needed).

**Key rule: HUB75 ribbon cannot run 10 m.** It is single-ended 3.3/5 V parallel at MHz pixel clocks and is only reliable for ~30–50 cm (max ~1 m with good ribbon and buffering). Your current direct-GPIO wiring is fine for the front panels next to the Pi, but must **not** be extended down the bus. Do not run long HUB75 ribbons to side/rear.

**Chosen architecture: one logical controller, three physical drivers**

| Location | Panels | Size | Driver (confirmed stock) |
|---|---|---|---|
| front | 3 | 240 x 40 | Main Pi 4B, direct HUB75 (short ribbon <50 cm) — existing wiring |
| side | 2 | 160 x 40 | Spare Pi 4 at the sign, direct HUB75 (`chain=2, parallel=1`) |
| rear | 1 | 80 x 40 | Spare Pi 3B at the sign, direct HUB75 (`chain=1, parallel=1`) |

- Pico Ws are **not** used as display drivers in v1: they cannot run `rpi-rgb-led-matrix` + the shared Pillow renderer. Keep them for future use (e.g. door sensors, temperature, keypad satellite). Displays are all Pi-driven so portal preview and bus rendering stay identical.
- The main Pi 4B at the front holds controller core, player, agent, keypad, and drives the front chain directly (your existing wiring, ideally via a buffered HAT/bonnet — see below).
- Each remote sign gets its assigned spare Pi mounted within 50 cm of its panels, driving them locally (`side Pi 4: chain=2, parallel=1`; `rear Pi 3B: chain=1, parallel=1`).
- The main Pi publishes state (destination fields, layouts, override, brightness) to the receivers over the bus LAN (wired Ethernet preferred; dedicated Wi-Fi as fallback). Latency target <100 ms — fine for destination boards. Receivers render with the same renderer and hold last frame if the link drops.
- The portal still sees **one controller** per bus. `display_outputs` gains a `transport: local | network` field plus receiver address; wiring profiles become "10 m bus (front local + side/rear networked)" as default, keeping the old "3 parallel chains" and "single chain" profiles for bench testing only.

**Front wiring (keep, harden):**
- Keep your direct GPIO wiring for prototyping, but for vehicle use put a buffered HAT between Pi and panels (Adafruit RGB Matrix HAT/Bonnet, Electrodragon drive board, or Active-3 adapter). This adds 3.3 V → 5 V level shifting (74AHCT245) and is far more vibration-tolerant than hand-wired ribbons. Confirm E address line routing for your 80x40 panels.
- Keep Pi-to-first-panel ribbon <50 cm. Daisy-chain the 3 front panels output→input following the arrows.
- Mount the main Pi at the front sign with the 5 V Pi supply.

**Side / rear network link:**
- Preferred: wired Ethernet (point-to-point or via a small vehicle switch) from main Pi to each receiver Pi. Static IPs, no internet needed. Also carries SSH for diagnostics.
- Fallback: receiver Pis join the main Pi's access point (`NSL-<unit id>`). Works but less robust in a metal bus; prefer wire.
- Protocol (v1, simple): main Pi exposes state over TCP/WebSocket on the bus LAN; receivers poll/subscribe and render. Reuse the layout JSON from `program-format.md` section 6.2 so receivers need no portal access. Full spec in Phase 3.

**Why not differential HUB75 extenders over CAT6:** sender/receiver pairs exist but are a single point of failure, still need power and mounting at each end, and are fiddly to debug in a vehicle. Networked receiver Pis are cheaper, use standard parts, isolate faults per sign, and match the existing "same renderer everywhere" design.

**Power (confirmed front, same rule at side/rear):**
- Front is already done right: 15 V stepped down to 5 V at the sign for the 3 front panels. Keep per-panel fusing and short thick 5 V runs.
  - Side (10 V / 5 A = 50 W in): 10 V → 5 V buck at the sign, rated ≥10 A (2 panels), Pi 4 fed from its own clean 5 V / 3 A tap.
  - Rear (5 V / 5 A): usable directly **only if** regulated 5.0 V ±5% with low ripple. Otherwise add a buck/filter. Pi 3B needs 5 V / 2.5 A from the same point.
- Star-ground each sign locally: converter 0 V → panel power GND → receiver/adapter GND → Pi GND, short and thick. Do **not** daisy-chain a thin ground 10 m down the bus. Fuse every panel branch (5–7.5 A automotive fuse) and fuse each converter input.
- Wire gauge: 5 V panel feeds in 14–16 AWG (short runs only); converter inputs in 18–20 AWG minimum for the 10/15 V feeds. Keep 5 V runs under ~1 m; the 10/15 V feeds are the ones that travel distance.
- Measure under full-white test pattern before sign-off: no more than ~100 mV drop source-to-panel, panels stable at brightness 70.

**Configuration:** the controller config's wiring presets become: (1) "10 m bus (default): front local chain=3 + networked side/rear", (2) bench-only "3 parallel chains", (3) bench-only "single chain of 6". Each output records `transport`, panel count, chain/offset, brightness, and bound program display name, as before.

**Display names and program displays:** each output is bound to a **program display name**. When a program is active, the output renders that program display's layout for the current destination. If the program has no display with that name, the output shows the program's default display, and the portal warns about the mismatch. If an output's size differs from the program display size, the portal flags it; `panel-width` / `logical_width_px` lets you render at the program's logical width where that is intended, otherwise the Pi falls back to a simple auto-fit layout rather than distorting the design.

**User-defined content:** what each display shows is defined by the destination's **layout** for that display (see 5.3c and the layout JSON in `program-format.md` section 6.2), not by fixed templates.

**Rendering:** the controller core sends the display driver the current state (destination fields, override) plus the layouts. The driver renders each display into its region with Pillow (pixel fonts for a legacy look), handles scrolling, paging and flashing, then pushes the frame to the matrix with vsync swap. It runs as its own systemd service so a crash elsewhere never blanks the boards, and it keeps showing the last good frame if the agent restarts.

**Pi 4 tuning for six panels:** watch the refresh rate with `led-show-refresh`, lower `led-pwm-bits` or raise `led-pwm-lsb-nanoseconds` if flickering, enable hardware pulse once onboard audio is off, and reserve one CPU core for the display driver.

**Development without hardware:** the same renderer produces a virtual display shown in the portal (layout editor, preview, remote control) and the local controller, so everything can be built and tested on a laptop with SQLite and no panels.

### 6.7 Inputs: GPIO (optional) and audio constraints

**GPIO buttons are optional.** With three parallel chains the matrix takes almost every usable GPIO pin, so the plan does not rely on GPIO for control. The GPIO feature stays available for pins that are free (for example with the single-chain wiring, or any pins your adapter leaves free).

- Any pin the config UI shows as free (BCM numbering) can be bound. There is no fixed pin.
- Per binding: label, pull-up / pull-down / none, active-low, debounce time, and separate behaviour for **press**, **long press** and **double press**.
- Each press type has an **ordered list of actions**, so one button can do several things (for example "advance to next stop" then "play next-stop announcement").
- The same action list is used by keypad keys (see 6.8).
- The config UI computes the **reserved pins from the chosen matrix mapping** and parallel count, shows them greyed out, and rejects bindings on them. Check the computed list against the Waveshare pin table for your wiring.
- A **pin test view** shows live state of each pin.

**Actions (shared by keypad keys and GPIO buttons):**
- Announcements: next stop, approaching, last stop, replay last
- Navigation: advance stop, previous stop, jump to first stop
- Selection: next / previous program, select program, next / previous service, next / previous destination, **select destination by code**
- Displays: apply override (preset or custom text), clear override
- Audio: volume up / down, mute
- Network: update mode (join the hotspot for a set time), start access point
- System: apply pending update now

**Audio:** the matrix library clashes with the Pi 4's onboard sound. Use a **USB audio adapter or an I2S DAC** (I2S also uses pins, so the UI reserves those when enabled; with three chains a USB audio adapter is the practical choice) and set `dtparam=audio=off`. With onboard sound off, hardware pulse can be switched on for a better picture.

### 6.8 USB keypad

A standard **USB keyboard number row and/or USB numeric keypad** is the main on-vehicle control for now. It is read directly through Linux `evdev`, so no GPIO pins are used and no display or login session is needed. The controller grabs the device so keystrokes never reach the Pi's console.

**Keys understood by default:**
- Digits: `0`-`9` on the number row and on the numpad (`KP0`-`KP9`)
- Enter: `Enter` and numpad `Enter`
- Clear: `Backspace` (one digit) and `Delete`/`Escape` (clear all)
- Numpad `+`, `-`, `*`, `/` and `.` are unassigned and free to map to actions

**Key map (user definable):** each key has a role: **digit**, **enter**, **clear**, or **action** (an ordered list of actions as in 6.7, for example numpad `+` = advance stop, `-` = previous stop, `*` = next-stop announcement, `/` = clear override, `.` = update mode). Keys can also have long-press behaviour. NumLock state does not matter, because the controller reads the raw key codes.

**Entry behaviour:**
1. Typing digits builds the code; the digits typed so far are shown on a chosen display (default front, configurable) as a short entry message.
2. By default the destination is selected automatically on the **8th digit**. A setting can require **Enter** instead.
3. **Backspace** removes the last digit, clear removes all. Entry times out after a configurable idle period and returns to the normal display.
4. Valid code: selects the service and destination in the active program, resets the stop to the first stop, clears any override, and shows the destination.
5. Unknown code: the display briefly shows an "invalid code" message and nothing changes.
6. Codes are looked up in the **active program** only; selecting a program is a separate action (key action, portal or local controller).

The same code entry box is available on the portal remote control page and the local controller. If the keypad is unplugged the controller keeps running and reconnects it automatically when it returns.

---

## 7. UI / Design System

**Direction:** light, modern, clean, but recognisably in the family of legacy transport software such as Hanover Helon.

| Element | Style |
|---|---|
| Palette | Light silver/grey surfaces (`#ECE9E2`-ish), white input fields, a single deep blue accent for title bars and selection, amber/red for warnings |
| Window chrome | Panels with a subtle 1px bevel (light top-left, darker bottom-right edge), thin title bars with a solid blue fill (no gradients) |
| Typography | Clear sans-serif at readable sizes (system UI font); monospace or pixel font only for display previews |
| Forms | Dialog-style groups with a titled border (group box), **labels above inputs**, **no placeholders** |
| Buttons | Rectangular with slight bevel, clear pressed state, large on the remote control and local controller pages for phone use |
| Lists | Row-striped tables with a selected-row highlight, like a classic list view |
| Tabs | Classic tab strip on the program and service editors |
| Display preview | Dark panel with amber LED-style dot-matrix text, so it is clear what the board shows |
| Motion | Minimal. Instant feedback, no decorative animation |
| Accessibility | Keyboard navigable, visible focus rings, sufficient contrast, large touch targets on phone-facing pages |

Build as a shared CSS/component package used by the portal and the Pi's local controller.

---

## 8. API Outline

All portal routes are under the base path `/portal` (for example `https://nslboards.com/portal/api/...`).

**Portal (user, session auth)**
- `POST /auth/signup`, `POST /auth/login`, `POST /auth/logout`, `POST /auth/reset`
- `GET/POST /organisations/:id/users`, `POST /organisations/:id/invites`
- `GET/POST /controllers`, `POST /controllers/pair` (code), `PATCH/DELETE /controllers/:id`
- `GET/PUT /controllers/:id/config` (display outputs, matrix options, keypad, GPIO bindings, networks, startup)
- `GET/POST /programs`, `GET/PUT/DELETE /programs/:id`
- `POST /programs/:id/publish` (validate and build `.nslprog`), `POST /programs/:id/upload` (body: controller ids and version)
- `GET /programs/:id/versions/:v/export` (`.nslpack`), `POST /programs/import`
- `GET/POST /programs/:id/services`, `GET/PUT/DELETE /services/:id`
- `POST /services/:id/destinations`, `PUT /destinations/:id`, `GET /programs/:id/destinations/check-code?code=` (8 digits, unique in the program)
- `PUT /destinations/:id/stops` (ordered list)
- `GET/PUT /destinations/:id/layouts/:displayId`, `GET/POST /programs/:id/layout-templates`
- `POST /audio` (upload), `POST /audio/generate` (Piper), `GET /audio`, `DELETE /audio/:id`
- `GET /voices`, `POST /voices` (upload model + config), `PATCH/DELETE /voices/:id`
- `POST /controllers/:id/command` (remote control), WebSocket `/controllers/:id/live` (state and display previews)

**Controller (device token auth)**
- `POST /device/register` → pairing code
- `GET /device/manifest?formats=1` → assigned programs and manifests, config version, plus latest release
- `GET /device/programs/:id/versions/:v/bundle` → `.nslprog` (range requests supported)
- `GET /device/audio/:hash` → clip (range requests supported)
- `POST /device/heartbeat` → status, installed versions, errors, outbox events
- `GET /device/release` → signed bundle URL and signature
- WebSocket `/device/stream` for "changed" and "update available" pushes, remote commands (with acks) and state updates

**Local controller (on the Pi, local login)**
- `GET /api/state`, `POST /api/command`, `GET/PUT /api/config`, `GET /api/programs`, `POST /api/programs/import`, WebSocket `/api/live`

---

## 9. Security

- Passwords hashed with Argon2; rate-limited login; optional 2FA.
- Tenant isolation on every query, with automated tests for cross-tenant access.
- Device tokens are random, stored hashed, revocable; pairing codes are single-use and short-lived.
- Remote commands are only accepted over the authenticated device WebSocket, only from users with Editor or Owner role in that controller's organisation, and are logged.
- HTTPS everywhere on the portal (TLS at the reverse proxy); audio and bundle downloads via short-lived signed URLs.
- Update bundles are cryptographically signed with a key kept off the web server; the Pi rejects unsigned or tampered bundles. Program files carry a SHA-256 and a `content_hash` that the Pi re-checks, and are only accepted from the paired portal (or from a local admin import).
- Phone hotspot credentials are stored encrypted on the Pi and in the portal.
- Upload validation: file type, size limits, transcode rather than serve raw. Voice model uploads are size-limited, validated, test-synthesised and only ever run inside the sandboxed TTS worker (no network, resource limits, timeout).
- Local controller bound to the access point only, WPA2 with a per-unit password, login required for settings, rate limiting. Only the display driver needs elevated access on the Pi.
- Regular DB backups and an audio/voice storage lifecycle policy.

---

## 10. Phased Delivery

### Phase 0: Foundations (1 to 2 weeks)
- Repo structure (monorepo: `portal/`, `agent/`, `ui-kit/`, `docs/`, `deploy/`)
- Design system skeleton (colours, bevels, form group, tabs, list view)
- Database schema and migrations on SQLite and PostgreSQL; CI on both
- Program file format library (build, read, validate, `content_hash`) with tests, shared by portal and agent
- Docker Compose for self-hosting; app served under `/portal`

### Phase 1: Portal core (3 to 5 weeks)
- Auth, organisations, roles, invites
- Programs, services, many destinations with 8-digit codes unique per program, stops, clips, board text/override
- Audio library: upload, convert, loudness normalise, hash, preview
- Piper text-to-speech with your existing voice as default, generate/regenerate, bulk generate, **user voice model upload**
- Program draft, validation, publish to `.nslprog`, versions, export/import `.nslpack`

### Phase 2: Pairing, upload and sync (2 to 3 weeks)
- Pairing flow end to end (code shown on the boards)
- Device API, manifests, resumable program and audio download, atomic install, status reporting
- Pi agent with `controller.db`, local cache, outbox and heartbeat
- Network modes: join the phone hotspot, fall back to the access point at `192.168.4.1`, update mode

### Phase 3: Pi player, display and controls (4 to 5 weeks)
- Controller core with persisted state and the override lifetime rule
- Clip decoding and chaining, announcement composition
- Display driver based on your existing code, with your option names/defaults: front 3 panels local on main Pi 4 (`chain=3, parallel=1`), side/rear on networked receiver Pis (chain=2 / chain=1); bench-only profiles for 3 parallel chains and single chain of 6 retained
- Receiver-Pi display sync protocol over bus LAN (state + layout JSON, <100 ms, hold-last-frame on drop)
- Board layout editor in the portal and layout rendering on the Pi (same renderer on main and receivers)
- **USB keypad** input: 8-digit code entry, key map, actions
- Optional GPIO bindings with reserved-pin checks and a pin test view
- Controller configuration screens (displays, matrix options, keypad, networks, startup) in the portal
- Remote control page with live state and previews
- Local controller at `192.168.4.1`: control, programs (including `.nslpack` import), settings, diagnostics

### Phase 4: Auto-update (1 to 2 weeks)
- Release pipeline, signing, A/B install, health check, rollback
- Portal release management and rollout by channel

### Phase 5: Hardening and polish (2 weeks)
- Security review, tenant isolation tests, load test
- Flashable Pi image, read-only root, watchdog
- Pi 4 display tuning on the real six-panel rig; hardware pulse once onboard audio is off
- Backups and restore drill
- Documentation: user guide, installer guide, wiring guide

### Phase 6: Future
- GPS-based auto-advance
- Matrix keypads on GPIO/I2C, button feedback outputs, other USB input devices
- Bulk import of stops (CSV), shared stop libraries
- Per-controller schedules, fleet dashboard, alerts when a controller goes offline
- Upload of custom fonts for boards
- Second USB Wi-Fi adapter support for access point and hotspot at the same time

---

## 11. Testing

- **Program format**: build and read round-trip on SQLite and PostgreSQL with identical `content_hash`; validation errors and warnings; layout JSON parsing; unknown optional data ignored by an older reader; unsupported `format_version` refused.
- **Unit**: announcement composition, versioning, pairing code lifecycle, destination codes (exactly 8 digits, leading zeros kept, unique within a program, same code allowed in another program), layout resolution.
- **Integration**: portal ↔ device API, program publish/upload/install with interrupted and resumed downloads over a dropping connection, software update and rollback, remote command round trip with acknowledgement.
- **State carry-over**: upgrading the active program keeps the selected destination and stop by id; a removed destination or stop falls back safely; an override survives an upgrade only on the same stop.
- **Security**: cross-tenant access attempts, token revocation, role checks on remote commands, signature and hash tamper tests, malformed program files and voice uploads.
- **Hardware**: main Pi 4 with USB audio driving front 3 panels local; 2x receiver Pis driving side (2 panels) and rear (1 panel) over bus LAN; check mapping works with `regular`; flicker/refresh with all displays lit, with and without hardware pulse; bus-LAN drop/reconnect (receivers hold last frame); USB keypad on number row and numpad; power-loss tests; full-white current measurement per sign (front/side/rear bucks, ≤100 mV drop).
- **Network**: boot with and without the hotspot present; update mode on and off; behaviour when the hotspot drops mid-sync; access point stays up while someone is connected.
- **Local controller**: join the access point with no internet and run a full service from `192.168.4.1`; import a `.nslpack`; confirm settings changes sync to the portal when the uplink returns.
- **Keypad**: full-code auto-select, Enter-required mode, backspace/clear/timeout, invalid code, code lookups limited to the active program, device unplug and replug.
- **Rendering**: snapshot tests that a layout renders identically in the portal preview and on the Pi renderer, including paging, scrolling and override.
- **Audio**: generated and uploaded clips have matching loudness and no audible gaps when chained; the same clip is stored and downloaded once; user-uploaded voices work and are isolated per organisation.
- **Override rule**: automated tests that an override survives replays, syncs, program upgrades, restarts and portal reconnects and clears only on a stop change or new destination/service/program.
- **UI**: keyboard-only walkthroughs; phone walkthrough of the remote control and local controller pages; contrast checks; manual review against the legacy-style guide.

---

## 12. Open Questions

1. **Distance between displays**: ~~how far?~~ Resolved — ~10 m front-to-rear. Design updated to 6.6.1 (front local, side/rear networked receiver Pis). No long HUB75 runs.
2. **Wiring board**: front currently direct-GPIO (works for prototype). For vehicle use: do you want to stay hand-wired or fit a buffered HAT/bonnet (recommended)? Either way it must carry the E line for 80x40 panels.
3. **Power in the vehicle**: confirmed feeds are front 15 V/5 A, side 10 V/5 A, rear 5 V/5 A + 5 V for Pi(s). Still needed: are those feeds already regulated/clean, what wire gauge/fuse is on each, and where exactly the buck converters will mount? Panels must get 5 V only — 15 V/10 V direct will destroy them.
4. **Converting existing programs**: ~~do you need a converter?~~ Resolved — no existing programs, no converter needed.