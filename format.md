# Program File Format (`.nslprog`) - Version 1

The program format is the one definition of a "program" used everywhere: the editing database on the server (PostgreSQL or SQLite), the published bundle that is uploaded to a controller, and the installed copy on the Raspberry Pi (SQLite).

A program holds **services → destinations → stops**, the **displays** it targets, per-destination **board layouts**, and references to **audio clips**. Audio is stored as separate content-addressed files, not inside the program file.

---

## 1. Design goals

1. **One schema, three places.** The same tables and columns on the server (PostgreSQL or SQLite) and on the Pi (SQLite). The server adds only ownership and draft columns.
2. **A published program is a single SQLite file** (`.nslprog`). Installing it on the Pi is a file move; rolling back is switching a pointer.
3. **Small and incremental over a phone hotspot.** The program file is tiny. Audio is downloaded separately and only if the Pi does not already have it (matched by SHA-256).
4. **Stable ids.** Every entity has a UUID that stays the same across versions, so the Pi's current selection survives an upgrade.
5. **Validated before it is ever uploaded.** Bad data (duplicate codes, broken layouts) is caught on the server, and re-checked on the Pi before activation.
6. **Forward compatible.** A format version number, a minimum agent version, and a rule that older readers ignore unknown optional data.

---

## 2. Where programs live

| Place | Storage | Role |
|---|---|---|
| Server (editing) | PostgreSQL in production, SQLite for local dev. Same tables as section 5 plus `organisation_id`, `program_id` and draft columns | Source of truth while a user edits. Never read by a Pi |
| Server (published) | `.nslprog` files in file storage, plus a `program_versions` row per file | Immutable, numbered versions |
| Pi | `programs/<program_id>/<version>.nslprog` (SQLite files, opened read-only) and `controller.db` (SQLite: registry, state, config) | What the controller actually runs |
| Offline transfer | `.nslpack` (zip containing a `.nslprog` and its audio) | Local import, backup, moving between servers |

---

## 3. File container

- A `.nslprog` file is a standard **SQLite 3 database**.
- `PRAGMA application_id = 0x4E534C50` (1314081872, the ASCII letters "NSLP") identifies it.
- `PRAGMA user_version = 1` is the **format version**. A reader that does not understand the version refuses the file.
- UTF-8, `page_size = 4096`, `journal_mode = DELETE`, finished with `VACUUM` so the file is compact and tidy.
- The Pi opens it read-only and immutable (`file:...?mode=ro&immutable=1`) with `foreign_keys = ON`.
- A published file is never modified. Changes produce a new version.

### 3.1 Identifiers and types

| Item | Type | Rule |
|---|---|---|
| Entity ids | `TEXT` | UUID (v7 recommended), lowercase, stable across versions |
| Destination code | `TEXT` | Exactly 8 digits `0`-`9`, leading zeros kept, unique within the program |
| Audio hash | `TEXT` | Lowercase hex SHA-256 of the audio file, 64 characters |
| Booleans | `INTEGER` | `0` or `1` with a `CHECK` |
| Timestamps | `TEXT` | ISO 8601 UTC, for example `2026-10-20T11:30:00Z` |
| JSON | `TEXT` | Plain text (no database-specific JSON operators, so it behaves the same on SQLite and PostgreSQL) |
| Positions | `INTEGER` | Order within the parent |

---

## 4. Audio

- Clips are stored **outside** the program file as `audio/<aa>/<hash>.flac`, where `<aa>` is the first two hex characters of the hash.
- Standard format: **mono, 16-bit FLAC, one fixed sample rate** (22.05 kHz or 44.1 kHz, chosen once for the system), silence trimmed, loudness normalised. This keeps clips small, lossless and sample-accurate when chained.
- The player decodes the clips of an announcement to PCM and chains them directly, so there is no reliance on a media player's gapless mode.
- The same clip used by many services, programs or versions is stored once, because its name is its hash.
- The program file lists the clips it needs in the `audio` table. The Pi downloads any hash it does not already have.

---

## 5. Schema (SQLite DDL for the `.nslprog` file)

```sql
PRAGMA application_id = 1314081872;   -- 0x4E534C50 "NSLP"
PRAGMA user_version   = 1;            -- format version
PRAGMA foreign_keys   = ON;

-- Key/value facts about this file (see 5.1)
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Audio clips used by this program (files live outside the database)
CREATE TABLE audio (
  hash        TEXT PRIMARY KEY CHECK (length(hash) = 64),
  label       TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
  bytes       INTEGER NOT NULL CHECK (bytes > 0),
  codec       TEXT NOT NULL DEFAULT 'flac',
  sample_rate INTEGER NOT NULL
);

-- The displays (boards) this program is designed for, e.g. front / side / rear
CREATE TABLE displays (
  id        TEXT PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  width_px  INTEGER NOT NULL CHECK (width_px  > 0),
  height_px INTEGER NOT NULL CHECK (height_px > 0),
  position  INTEGER NOT NULL
);

CREATE TABLE services (
  id                 TEXT PRIMARY KEY,
  number             TEXT NOT NULL UNIQUE,          -- e.g. '100'
  name               TEXT NOT NULL DEFAULT '',
  position           INTEGER NOT NULL,
  next_stop_audio    TEXT REFERENCES audio(hash),   -- "The next stop is"
  approaching_audio  TEXT REFERENCES audio(hash),   -- "Approaching"
  last_stop_audio    TEXT REFERENCES audio(hash),   -- "...where this service terminates"
  terminus_clip_mode TEXT NOT NULL DEFAULT 'both'
                     CHECK (terminus_clip_mode IN ('next','approaching','both'))
);

CREATE TABLE destinations (
  id           TEXT PRIMARY KEY,
  service_id   TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  code         TEXT NOT NULL UNIQUE
               CHECK (length(code) = 8 AND code NOT GLOB '*[^0-9]*'),
  name         TEXT NOT NULL,                      -- name shown in lists
  position     INTEGER NOT NULL,
  num          TEXT,                               -- route number override, NULL = service number
  via          TEXT NOT NULL DEFAULT '',           -- board field "via"
  dest         TEXT NOT NULL,                      -- board field "dest"
  extra_fields TEXT NOT NULL DEFAULT '{}'          -- JSON object, see 6.1
);

CREATE TABLE stops (
  id             TEXT PRIMARY KEY,
  destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  position       INTEGER NOT NULL CHECK (position >= 1),
  name           TEXT NOT NULL,
  audio          TEXT REFERENCES audio(hash),      -- clip of the stop name
  board_text     TEXT,                             -- NULL = use name
  board_override TEXT,                             -- NULL or '' = no override
  is_terminus    INTEGER NOT NULL DEFAULT 0 CHECK (is_terminus IN (0,1)),
  UNIQUE (destination_id, position)
);

-- Board layout for one destination on one display
CREATE TABLE layouts (
  id             TEXT PRIMARY KEY,
  destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  display_id     TEXT NOT NULL REFERENCES displays(id)     ON DELETE CASCADE,
  layout         TEXT NOT NULL,                    -- JSON, see 6.2
  UNIQUE (destination_id, display_id)
);

-- Reusable layouts; one may be the default for a display
CREATE TABLE layout_templates (
  id         TEXT PRIMARY KEY,
  display_id TEXT NOT NULL REFERENCES displays(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  layout     TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0,1)),
  UNIQUE (display_id, name)
);
CREATE UNIQUE INDEX one_default_template
  ON layout_templates (display_id) WHERE is_default = 1;

-- Quick override choices for buttons, keypad and remote control
CREATE TABLE override_presets (
  id       TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  text     TEXT NOT NULL,
  position INTEGER NOT NULL
);

CREATE INDEX idx_destinations_service ON destinations (service_id, position);
CREATE INDEX idx_stops_destination    ON stops (destination_id, position);
```

The `UNIQUE` constraint on `destinations.code` is the rule "only one destination in a program can have a given code". Because a file is exactly one program, a plain `UNIQUE` is enough here. On the server the same rule is `UNIQUE (program_id, code)`.

### 5.1 `meta` keys

| Key | Example | Meaning |
|---|---|---|
| `format` | `nslprog` | Must be this value |
| `format_version` | `1` | Same as `user_version` |
| `program_id` | UUID | Stable id of the program |
| `program_name` | `Burton routes` | Display name |
| `version` | `7` | Published version number (integer, increases by one) |
| `px_width` | `240` | The program's logical width (default 240) |
| `default_display` | display UUID | Display used when an output has no matching name |
| `content_hash` | 64 hex | Identity of the data, see 8 |
| `min_agent_version` | `1.0.0` | Oldest Pi software that can run this file |
| `created_at` | ISO time | When it was published |
| `created_by` | `A. User` | Who published it |
| `generator` | `portal 1.0.0` | Software that built the file |

---

## 6. JSON structures

### 6.1 `destinations.extra_fields`

A JSON object of extra named board fields, for example `{"note": "Via Tamworth"}`.
- Keys match `^[a-z][a-z0-9_]{0,31}$`.
- Values are strings (up to 200 characters).
- Keys must not be `num`, `via` or `dest` (reserved fields).

### 6.2 Layout JSON (used in `layouts.layout` and `layout_templates.layout`)

```json
{
  "schema": 1,
  "width": 240,
  "height": 40,
  "background": "#000000",
  "page_seconds": 3,
  "override": { "mode": "replace", "replaces": "dest", "hide": ["via"] },
  "elements": [
    { "type": "field", "field": "num",  "x": 0,  "y": 0,  "w": 48,  "h": 40,
      "font": "pixel-large", "colour": "#FFB000", "align": "centre", "valign": "middle",
      "scroll": "none", "flash_ms": 0, "page": 1, "show_if": "always" },
    { "type": "field", "field": "via",  "x": 52, "y": 0,  "w": 188, "h": 16,
      "font": "pixel-small", "colour": "#FFB000", "align": "left", "valign": "middle",
      "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "has_text" },
    { "type": "field", "field": "dest", "x": 52, "y": 16, "w": 188, "h": 24,
      "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
      "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "always" }
  ]
}
```

| Property | Values |
|---|---|
| `schema` | `1`. Unknown higher values are rejected by older readers |
| `width`, `height` | Must equal the target display's size |
| `background` | Hex colour, default `#000000` |
| `page_seconds` | How long each page shows when elements use more than one `page` |
| `override.mode` | `replace` (override text replaces one element), `hide_others` (override text only, other elements hidden), `full` (override text fills the display) |
| `override.replaces` | The `field` whose text is replaced (default `dest`) |
| `override.hide` | Fields hidden while an override is active |
| element `type` | `field` (shows a field) or `text` (fixed text in a `text` property) |
| element `field` | `num`, `via`, `dest`, or an extra field key |
| `x`, `y`, `w`, `h` | Pixels, element must lie inside the display |
| `font` | Pixel font id, or `han-<STEM>` for a Hanover bitmap font (1:1 pixels, see README §19.1). Unknown font falls back to a default and raises a warning |
| `scale` | Integer 1–8, default 1. Integer-upscales Hanover glyphs (ignored by pixel fonts) |
| `spacing` | Integer −8…16 pixels, default 0. Extra tracking between Hanover glyphs (negative tightens; ignored by pixel fonts) |
| `colour` | Hex RGB |
| `align` / `valign` | `left` `centre` `right` / `top` `middle` `bottom` |
| `scroll` | `none`, `always`, `if_too_long` |
| `flash_ms` | 0 = steady, otherwise on/off period in milliseconds |
| `page` | Integer from 1. Pages alternate every `page_seconds` |
| `show_if` | `always`, `has_text`, `override_active`, `override_inactive` |

**Layout resolution for a destination on a display:**
1. The destination's own row in `layouts` for that display, else
2. the `is_default` template for that display, else
3. a built-in **auto-fit** layout (route number on the left, destination text filling the rest).

**Field values at runtime:** `num` = `destinations.num`, or the service `number` when that is `NULL`. `via` and `dest` come from the destination. Extra fields come from `extra_fields`. During an active override the override text takes the place of the element named in `override.replaces`.

---

## 7. Announcement composition (how the player uses the data)

For the current stop of the current destination:

| Moment | Clips played, in order |
|---|---|
| Next stop | `service.next_stop_audio`, `stop.audio` |
| Approaching | `service.approaching_audio`, `stop.audio` |
| Next stop at the terminus | `next_stop_audio`, `stop.audio`, `last_stop_audio` (if `terminus_clip_mode` is `next` or `both`) |
| Approaching at the terminus | `approaching_audio`, `stop.audio`, `last_stop_audio` (if `terminus_clip_mode` is `approaching` or `both`) |

A `NULL` clip is skipped. The terminus is the stop with `is_terminus = 1`.

---

## 8. Validation rules

Run on the server when a user presses Publish, and again on the Pi before a downloaded file is activated.

**Errors (publishing or activation is refused):**
- `destinations.code` is not exactly 8 digits, or appears twice in the program
- `services.number` appears twice
- The program has no displays, or `default_display` does not exist
- Stop positions are not 1, 2, 3 … with no gaps
- A stop marked `is_terminus` is not the last stop of its destination, or a destination has more than one
- A layout's `width`/`height` differs from its display, an element lies outside the display, or the JSON is invalid
- An audio hash used by a service or stop is missing from the `audio` table
- `PRAGMA integrity_check` or `PRAGMA foreign_key_check` reports a problem (Pi side)
- `content_hash` does not match the recomputed value (Pi side)

**Warnings (shown, but allowed):**
- A destination has no stops (display-only: boards work, announcements have nothing to chain)
- A service is missing a next-stop, approaching or last-stop clip
- A stop has no audio (it will be silent)
- A destination has no terminus flagged
- An unknown font id, or text that cannot fit and is not set to scroll
- A display name in the program that no controller output uses

### 8.1 `content_hash` (identity of the data)

`content_hash` is the SHA-256 of a canonical text dump of every table except `meta`: tables in alphabetical order, rows sorted by primary key, each row written as one line of JSON with sorted keys and no extra whitespace. Two publishes with identical data therefore have the same `content_hash`, even if the file bytes differ. The portal refuses to create a new version when nothing has changed.

The **file's own SHA-256** (`bundle_sha256`) is recorded in the manifest and is only used to check the download.

---

## 9. Server side

### 9.1 Editing tables

The server tables use the same columns as section 5 with these differences:

| Difference | Reason |
|---|---|
| Extra columns `organisation_id`, `program_id` on every table | Ownership and tenant isolation. Every query is scoped by `organisation_id` |
| `destinations`: `UNIQUE (program_id, code)` and `services`: `UNIQUE (program_id, number)` | Per-program uniqueness in a shared table |
| `audio` becomes the library table `audio_files` keyed by `(organisation_id, hash)` | The library is shared across programs |
| Draft columns: `created_at`, `updated_at`, `updated_by` | Editing history |
| Table `programs` (name, px_width, default_display, draft_version, published_version) | Replaces the `meta` table while editing |
| Table `program_versions` (program_id, version, bundle_key, bundle_sha256, content_hash, size, audio_count, audio_bytes, notes, created_by, created_at) | Registry of published files |

### 9.2 Dialect differences

The schema is defined once (in the ORM/migration tool) and generated for both databases. The only meaningful differences:

| Item | SQLite | PostgreSQL |
|---|---|---|
| Code check | `length(code) = 8 AND code NOT GLOB '*[^0-9]*'` | `code ~ '^[0-9]{8}$'` |
| Booleans | `INTEGER` with `CHECK (x IN (0,1))` | `BOOLEAN` or the same `INTEGER` form |
| Partial unique index | Supported | Supported |
| JSON columns | `TEXT` | `TEXT` (no `jsonb` operators used) |
| Foreign keys | `PRAGMA foreign_keys = ON` per connection | Always on |

Contract tests run the same publish-and-read scenarios on both databases and require identical `content_hash` values.

### 9.3 Publish (building a `.nslprog`)

1. Open a read transaction on the program and run the validation in section 8. Errors stop the publish.
2. Compute `content_hash`. If it equals the latest published version, stop ("no changes").
3. Create a new SQLite file, apply the schema in section 5, set `application_id` and `user_version`.
4. Copy the rows in a fixed order, fill `audio` with only the clips actually referenced, fill `meta`, then `VACUUM`.
5. Compute `bundle_sha256`, upload the file to storage, and insert the `program_versions` row with the next version number.
6. Create the manifest (section 10) for the controllers the user chose.

---

## 10. Transfer to the Pi (built for a phone hotspot)

The Pi is online only now and then, so transfer is small, resumable and safe.

**Manifest** (JSON, one per controller per program):

```json
{
  "manifest_version": 1,
  "program_id": "0190f1c2-0000-7000-8000-000000000001",
  "program_name": "Burton routes",
  "version": 7,
  "format_version": 1,
  "content_hash": "3b6c…",
  "min_agent_version": "1.0.0",
  "bundle": { "url": "/portal/api/device/programs/0190f1c2-…/versions/7/bundle",
              "sha256": "9f2a…", "bytes": 48128 },
  "audio": [
    { "hash": "a41d…", "bytes": 31244, "url": "/portal/api/device/audio/a41d…" },
    { "hash": "c07e…", "bytes": 28810, "url": "/portal/api/device/audio/c07e…" }
  ]
}
```

**Rules:**
- The Pi downloads the program file first (small), then **only the audio hashes it does not already have**.
- Downloads use HTTP range requests so a dropped connection resumes where it stopped. Partial files stay in `incoming/` until complete.
- Every file is verified against its SHA-256 before it is moved into place.
- All requests use the device token over HTTPS.
- The portal shows the total download size of a version when you press Upload.

---

## 11. Pi side

### 11.1 Folder layout

```
/var/lib/nsl/
  controller.db                          SQLite: registry, state, config, outbox
  programs/<program_id>/<version>.nslprog
  audio/<aa>/<hash>.flac
  incoming/                              partial downloads
```

### 11.2 `controller.db` (program-related tables)

```sql
CREATE TABLE desired_programs (          -- what the portal wants installed
  program_id      TEXT PRIMARY KEY,
  desired_version INTEGER NOT NULL,
  manifest        TEXT NOT NULL,         -- manifest JSON as received
  received_at     TEXT NOT NULL
);

CREATE TABLE installed_programs (        -- what is on disk
  program_id    TEXT    NOT NULL,
  version       INTEGER NOT NULL,
  program_name  TEXT    NOT NULL,
  content_hash  TEXT    NOT NULL,
  bundle_sha256 TEXT    NOT NULL,
  path          TEXT    NOT NULL,
  bytes         INTEGER NOT NULL,
  status        TEXT    NOT NULL CHECK (status IN ('installed','active','superseded','failed')),
  source        TEXT    NOT NULL CHECK (source IN ('portal','local_import')),
  installed_at  TEXT    NOT NULL,
  last_error    TEXT,
  PRIMARY KEY (program_id, version)
);

CREATE TABLE audio_cache (
  hash         TEXT PRIMARY KEY,
  bytes        INTEGER NOT NULL,
  added_at     TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);

CREATE TABLE state (                     -- single row, survives restarts
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  program_id       TEXT,
  program_version  INTEGER,
  service_id       TEXT,
  destination_id   TEXT,
  stop_id          TEXT,
  override_text    TEXT,
  override_set_at  TEXT,
  volume           INTEGER NOT NULL DEFAULT 80,
  muted            INTEGER NOT NULL DEFAULT 0,
  updated_at       TEXT NOT NULL
);

CREATE TABLE outbox (                    -- events waiting for the next connection
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,              -- status, command_log, install_result …
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);
```

The controller's configuration tables (display outputs, matrix options, GPIO bindings, keypad, saved Wi-Fi networks) also live in `controller.db`; see section 4.2 of `plan.md`.

`controller.db` runs with `journal_mode = WAL` and `synchronous = NORMAL`, on the writable data partition.

### 11.3 Install and activate

1. **Download** the program file and any missing audio into `incoming/`, verify SHA-256 hashes.
2. **Check** the file: `application_id`, `user_version` supported, `meta.format` and `meta.version` match the manifest, `integrity_check`, `foreign_key_check`, section 8 rules, `content_hash` recomputed, and every audio hash exists in the cache.
3. **Install:** move the file to `programs/<program_id>/<version>.nslprog` and, in one transaction, insert `installed_programs` (status `installed`) and add the audio to `audio_cache`. The previous version stays untouched.
4. **Activate** at a safe moment, never while an announcement is playing or a service is being driven:
   - If it is the **active program**, activation happens when the driver next selects a destination or program, on an idle point, or on "apply now" from the portal or local controller.
   - Mark the new version `active` and the old one `superseded` in one transaction.
5. **Carry the state over by id:** `service_id`, `destination_id` and `stop_id` are looked up in the new file. If the destination or stop no longer exists, the controller falls back to the destination's first stop, or to idle if the destination is gone. An override stays only while the same stop is current.
6. **Report** the result to the portal through the outbox when online.

**Rollback:** if activation fails the checks, or the display driver cannot load the program, the previous version remains active and the failure is reported. The portal can also instruct "return to version N" (a pointer switch).

**Retention:** keep the active version plus the previous two versions of each program. Delete older files, then delete audio that no kept version references.

---

## 12. Offline package (`.nslpack`)

A zip file for moving a program without the portal:

```
manifest.json            same shape as section 10, with relative paths
program.nslprog
audio/<aa>/<hash>.flac
```

Uses:
- **Local controller import** at `192.168.4.1` when there is no internet.
- **Backup** and moving programs between two servers.
- **Export from the portal** (a published version) and **import into the portal** (creates a new draft program; the `.nslprog` rows are read back into the editing tables).

Trust rules: the Pi accepts a pack only after a local admin login, and still performs every check in section 8 and section 11.3. A locally imported version is marked `source = local_import` and reported to the portal at the next connection.

---

## 13. Versioning and compatibility

- `user_version` is the **format version**. Version 1 is described here.
- **Additive changes** (new optional `meta` keys, new optional JSON properties) do not change the version. Readers ignore unknown keys and unknown JSON properties.
- **Breaking changes** increase the format version. Each controller tells the portal which format versions it supports when it asks for its manifest, and the portal builds the newest format that controller can read. `min_agent_version` lets the portal hold back a program until the controller has updated itself first.
- Layout JSON has its own `schema` number for the same reason.
- A Pi supports the current and the previous format version.

---

## 14. Example (service 100)

```
displays:     front 240x40 (default), side 160x40, rear 80x40
service:      number 100, name "Burton to Lichfield"
destination:  code 10000001, name "Burton",   num NULL, via "Branston",  dest "BURTON"
destination:  code 10000002, name "Lichfield", num NULL, via "",         dest "LICHFIELD"
stops (Burton):
  1 "Lichfield Bus Station"
  2 "Branston Road"
  3 "Burton New Street"   is_terminus = 1, board_override "TERMINATING BUS"
layout (front, Burton):  num | via over dest  (three elements, as in 6.2)
layout (side,  Burton):  num | dest
layout (rear,  Burton):  num
```

Entering `10000001` on the keypad selects service 100, destination Burton, stop 1. Entering `10000002` selects Lichfield. The same code could exist in a different program without clashing.