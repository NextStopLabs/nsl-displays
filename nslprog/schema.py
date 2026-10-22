"""NSL program file format (.nslprog) - version 1.

Single definition used by portal + Pi. See format.md.
A .nslprog file is a SQLite 3 database with application_id 0x4E534C50 ("NSLP")
and user_version 1.
"""
from __future__ import annotations

APPLICATION_ID = 0x4E534C50  # 1314081872 "NSLP"
FORMAT_VERSION = 1
FORMAT_NAME = "nslprog"
LAYOUT_SCHEMA = 1

NSLPROG_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE audio (
  hash        TEXT PRIMARY KEY CHECK (length(hash) = 64),
  label       TEXT NOT NULL,
  duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
  bytes       INTEGER NOT NULL CHECK (bytes > 0),
  codec       TEXT NOT NULL DEFAULT 'flac',
  sample_rate INTEGER NOT NULL
);

CREATE TABLE displays (
  id        TEXT PRIMARY KEY,
  name      TEXT NOT NULL UNIQUE,
  width_px  INTEGER NOT NULL CHECK (width_px  > 0),
  height_px INTEGER NOT NULL CHECK (height_px > 0),
  position  INTEGER NOT NULL
);

CREATE TABLE services (
  id                 TEXT PRIMARY KEY,
  number             TEXT NOT NULL UNIQUE,
  name               TEXT NOT NULL DEFAULT '',
  position           INTEGER NOT NULL,
  next_stop_audio    TEXT REFERENCES audio(hash),
  approaching_audio  TEXT REFERENCES audio(hash),
  last_stop_audio    TEXT REFERENCES audio(hash),
  terminus_clip_mode TEXT NOT NULL DEFAULT 'both'
                     CHECK (terminus_clip_mode IN ('next','approaching','both'))
);

CREATE TABLE destinations (
  id           TEXT PRIMARY KEY,
  service_id   TEXT NOT NULL REFERENCES services(id) ON DELETE CASCADE,
  code         TEXT NOT NULL UNIQUE
               CHECK (length(code) BETWEEN 1 AND 8 AND code NOT GLOB '*[^0-9]*'),
  name         TEXT NOT NULL,
  position     INTEGER NOT NULL,
  num          TEXT,
  via          TEXT NOT NULL DEFAULT '',
  dest         TEXT NOT NULL,
  extra_fields TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE destination_pages (
  id             TEXT PRIMARY KEY,
  destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  position       INTEGER NOT NULL CHECK (position >= 1),
  via            TEXT NOT NULL DEFAULT '',
  dest           TEXT NOT NULL DEFAULT '',
  UNIQUE (destination_id, position)
);

CREATE TABLE stops (
  id             TEXT PRIMARY KEY,
  destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  position       INTEGER NOT NULL CHECK (position >= 1),
  name           TEXT NOT NULL,
  audio          TEXT REFERENCES audio(hash),
  board_text     TEXT,
  board_override TEXT,
  is_terminus    INTEGER NOT NULL DEFAULT 0 CHECK (is_terminus IN (0,1)),
  UNIQUE (destination_id, position)
);

CREATE TABLE layouts (
  id             TEXT PRIMARY KEY,
  destination_id TEXT NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
  display_id     TEXT NOT NULL REFERENCES displays(id)     ON DELETE CASCADE,
  layout         TEXT NOT NULL,
  UNIQUE (destination_id, display_id)
);

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

CREATE TABLE override_presets (
  id       TEXT PRIMARY KEY,
  label    TEXT NOT NULL,
  text     TEXT NOT NULL,
  position INTEGER NOT NULL
);

-- Per-program GPIO input rules (plan.md 6.7): pin + trigger -> board/audio effect.
-- Triggers: press/long/double (momentary button), on_active (rising-edge
-- one-shot, e.g. door just opened), while_active (latched overlay while the
-- pin is held, e.g. reverse gear / door open / toggle switch on), on_release
-- (falling-edge one-shot, e.g. door just closed).
CREATE TABLE gpio_rules (
  id         TEXT PRIMARY KEY,
  label      TEXT NOT NULL DEFAULT '',
  pin        INTEGER NOT NULL,
  trigger    TEXT NOT NULL CHECK (trigger IN ('press','long','double','on_active','while_active','on_release','toggle')),
  effect     TEXT NOT NULL DEFAULT 'overlay' CHECK (effect IN ('overlay','override')),
  target     TEXT NOT NULL DEFAULT 'all',
  board_text TEXT NOT NULL DEFAULT '',
  board_source TEXT NOT NULL DEFAULT 'text' CHECK (board_source IN ('text','destination')),
  destination_id TEXT REFERENCES destinations(id),
  audio      TEXT REFERENCES audio(hash),
  duration_s INTEGER NOT NULL DEFAULT 0,
  pull       TEXT NOT NULL DEFAULT 'up' CHECK (pull IN ('up','down','none')),
  active_low INTEGER NOT NULL DEFAULT 1 CHECK (active_low IN (0,1)),
  debounce_ms INTEGER NOT NULL DEFAULT 50,
  position   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_destinations_service ON destinations (service_id, position);
CREATE INDEX idx_stops_destination    ON stops (destination_id, position);
CREATE INDEX idx_destination_pages_destination ON destination_pages (destination_id, position);
"""

META_REQUIRED_KEYS = (
    "format", "format_version", "program_id", "program_name", "version",
    "px_width", "default_display", "content_hash", "min_agent_version",
    "created_at", "created_by", "generator",
)

CONTROLLER_DB_DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS desired_programs (
  program_id      TEXT PRIMARY KEY,
  desired_version INTEGER NOT NULL,
  manifest        TEXT NOT NULL,
  received_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS installed_programs (
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

CREATE TABLE IF NOT EXISTS audio_cache (
  hash         TEXT PRIMARY KEY,
  bytes        INTEGER NOT NULL,
  added_at     TEXT NOT NULL,
  last_used_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
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

CREATE TABLE IF NOT EXISTS outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);

-- Controller configuration (plan.md 4.2)
CREATE TABLE IF NOT EXISTS display_outputs (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  width_px INTEGER NOT NULL,
  height_px INTEGER NOT NULL,
  parallel_chain INTEGER NOT NULL DEFAULT 1,
  x_offset INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  brightness INTEGER NOT NULL DEFAULT 70,
  logical_width_px INTEGER,
  transport TEXT NOT NULL DEFAULT 'local' CHECK (transport IN ('local','network')),
  receiver_addr TEXT,
  program_display TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS controller_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL DEFAULT 1,
  matrix_options TEXT NOT NULL DEFAULT '{}',
  audio_device TEXT NOT NULL DEFAULT 'default',
  ap_name TEXT NOT NULL DEFAULT 'NSL-unit',
  startup_program_id TEXT,
  startup_code TEXT,
  startup_mode TEXT NOT NULL DEFAULT 'restore',
  release_channel TEXT NOT NULL DEFAULT 'stable',
  portal_url TEXT NOT NULL DEFAULT 'https://nsldisplays.com/portal',
  controller_name TEXT NOT NULL DEFAULT 'bus-1',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keypad_config (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  device_match TEXT NOT NULL DEFAULT '',
  auto_select INTEGER NOT NULL DEFAULT 1 CHECK (auto_select IN (0,1)),
  entry_timeout_ms INTEGER NOT NULL DEFAULT 5000,
  entry_display TEXT NOT NULL DEFAULT 'front'
);

CREATE TABLE IF NOT EXISTS keypad_keys (
  id TEXT PRIMARY KEY,
  key_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('digit','enter','clear_one','clear_all','action')),
  digit TEXT,
  step_order INTEGER NOT NULL DEFAULT 0,
  action TEXT,
  action_param TEXT
);

CREATE TABLE IF NOT EXISTS gpio_bindings (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  pin INTEGER NOT NULL,
  pull TEXT NOT NULL DEFAULT 'up',
  active_low INTEGER NOT NULL DEFAULT 1,
  debounce_ms INTEGER NOT NULL DEFAULT 50
);

CREATE TABLE IF NOT EXISTS gpio_actions (
  id TEXT PRIMARY KEY,
  binding_id TEXT NOT NULL REFERENCES gpio_bindings(id) ON DELETE CASCADE,
  press_type TEXT NOT NULL CHECK (press_type IN ('press','long','double')),
  step_order INTEGER NOT NULL,
  action TEXT NOT NULL,
  action_param TEXT
);

-- Live GPIO overlay state, shared between the input/display/core processes
-- (in-memory state would stay invisible to the renderer). One row per target
-- display ('all' or a display name). expires_at is wall-clock seconds.
CREATE TABLE IF NOT EXISTS gpio_overlays (
  target TEXT PRIMARY KEY,
  text TEXT NOT NULL DEFAULT '',
  destination_id TEXT,
  rule_id TEXT,
  expires_at REAL,
  latched INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'oneshot',
  position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gpio_toggles (
  rule_id TEXT PRIMARY KEY,
  is_on INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wifi_networks (
  ssid TEXT PRIMARY KEY,
  secret TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'hotspot'
);

CREATE TABLE IF NOT EXISTS command_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  command TEXT NOT NULL,
  user_id TEXT,
  at TEXT NOT NULL
);
"""
