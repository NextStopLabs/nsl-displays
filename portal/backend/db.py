"""Server DB: one schema file, portable SQLite/Postgres (plan.md 3.1).

SQLite for dev (DATABASE_URL=sqlite:./dev.db), Postgres in prod.
No jsonb operators, no enums, UUIDs as TEXT, timestamps UTC TEXT.
Destination codes TEXT (leading zeros kept); per-program uniqueness.
"""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS organisations (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '', totp_secret TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  organisation_id TEXT NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
  PRIMARY KEY (user_id, organisation_id));
CREATE TABLE IF NOT EXISTS controllers (
  id TEXT PRIMARY KEY, organisation_id TEXT REFERENCES organisations(id),
  name TEXT NOT NULL, device_token_hash TEXT, status TEXT NOT NULL DEFAULT 'pending',
  last_seen_at TEXT, app_version TEXT DEFAULT '', channel TEXT DEFAULT 'stable',
  supported_formats TEXT DEFAULT '1', pairing_code TEXT, pairing_expires_at TEXT,
  config_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, sync_state TEXT DEFAULT NULL,
  pending_token TEXT DEFAULT NULL, live_state TEXT DEFAULT NULL);
CREATE TABLE IF NOT EXISTS controller_programs (
  controller_id TEXT NOT NULL, program_id TEXT NOT NULL,
  desired_version INTEGER, installed_version INTEGER, active_version INTEGER,
  status TEXT DEFAULT 'pending', last_error TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (controller_id, program_id));
CREATE TABLE IF NOT EXISTS voices (
  id TEXT PRIMARY KEY, organisation_id TEXT, name TEXT NOT NULL,
  model_key TEXT NOT NULL, config_key TEXT NOT NULL, language TEXT DEFAULT '',
  sample_rate INTEGER DEFAULT 22050, is_default INTEGER DEFAULT 0,
  licence_note TEXT DEFAULT '', uploaded_by TEXT DEFAULT '', size_bytes INTEGER DEFAULT 0,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audio_files (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, sha256 TEXT NOT NULL,
  filename TEXT NOT NULL, storage_key TEXT NOT NULL, duration_ms INTEGER DEFAULT 0,
  source TEXT DEFAULT 'upload', tts_text TEXT, tts_voice_id TEXT, tts_params TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (organisation_id, sha256));
-- program editing tables (mirror .nslprog + ownership/draft)
CREATE TABLE IF NOT EXISTS programs (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, name TEXT NOT NULL,
  px_width INTEGER DEFAULT 240, default_display TEXT DEFAULT '',
  draft_version INTEGER DEFAULT 0, published_version INTEGER DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS program_displays (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  name TEXT NOT NULL, width_px INTEGER NOT NULL, height_px INTEGER NOT NULL, position INTEGER NOT NULL,
  UNIQUE (program_id, name));
CREATE TABLE IF NOT EXISTS p_services (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  number TEXT NOT NULL, name TEXT DEFAULT '', position INTEGER NOT NULL,
  next_stop_audio TEXT, approaching_audio TEXT, last_stop_audio TEXT,
  terminus_clip_mode TEXT DEFAULT 'both',
  UNIQUE (program_id, number));
CREATE TABLE IF NOT EXISTS p_destinations (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  service_id TEXT NOT NULL, code TEXT NOT NULL, name TEXT NOT NULL, position INTEGER NOT NULL,
  num TEXT, via TEXT DEFAULT '', dest TEXT NOT NULL, extra_fields TEXT DEFAULT '{}',
  UNIQUE (program_id, code));
CREATE TABLE IF NOT EXISTS p_stops (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  destination_id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL,
  audio TEXT, board_text TEXT, board_override TEXT, is_terminus INTEGER DEFAULT 0,
  UNIQUE (destination_id, position));
CREATE TABLE IF NOT EXISTS p_destination_pages (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  destination_id TEXT NOT NULL, position INTEGER NOT NULL, via TEXT DEFAULT '', dest TEXT DEFAULT '',
  UNIQUE (destination_id, position));
CREATE TABLE IF NOT EXISTS p_layouts (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  destination_id TEXT NOT NULL, display_id TEXT NOT NULL, layout TEXT NOT NULL,
  UNIQUE (destination_id, display_id));
CREATE TABLE IF NOT EXISTS p_templates (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  display_id TEXT NOT NULL, name TEXT NOT NULL, layout TEXT NOT NULL, is_default INTEGER DEFAULT 0,
  UNIQUE (program_id, display_id, name));
CREATE TABLE IF NOT EXISTS p_presets (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  label TEXT NOT NULL, text TEXT NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS p_gpio_rules (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '', pin INTEGER NOT NULL,
  trigger TEXT NOT NULL DEFAULT 'press',
  effect TEXT NOT NULL DEFAULT 'overlay', target TEXT NOT NULL DEFAULT 'all',
  board_text TEXT NOT NULL DEFAULT '', board_source TEXT NOT NULL DEFAULT 'text',
  destination_id TEXT, audio TEXT,
  duration_s INTEGER NOT NULL DEFAULT 0,
  pull TEXT NOT NULL DEFAULT 'up', active_low INTEGER NOT NULL DEFAULT 1,
  debounce_ms INTEGER NOT NULL DEFAULT 50, position INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS program_versions (
  id TEXT PRIMARY KEY, program_id TEXT NOT NULL, version INTEGER NOT NULL,
  bundle_key TEXT NOT NULL, bundle_sha256 TEXT NOT NULL, content_hash TEXT NOT NULL,
  size_bytes INTEGER NOT NULL, audio_count INTEGER NOT NULL, audio_bytes INTEGER NOT NULL,
  notes TEXT DEFAULT '', created_by TEXT DEFAULT '', created_at TEXT NOT NULL,
  UNIQUE (program_id, version));
CREATE TABLE IF NOT EXISTS display_outputs (
  id TEXT PRIMARY KEY, controller_id TEXT NOT NULL, display_name TEXT NOT NULL,
  width_px INTEGER NOT NULL, height_px INTEGER NOT NULL, parallel_chain INTEGER DEFAULT 1,
  x_offset INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, brightness INTEGER DEFAULT 70,
  transport TEXT DEFAULT 'local', receiver_addr TEXT, program_display TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS keypad_config (
  controller_id TEXT PRIMARY KEY, device_match TEXT DEFAULT '',
  auto_select INTEGER DEFAULT 1, entry_timeout_ms INTEGER DEFAULT 5000, entry_display TEXT DEFAULT 'front');
CREATE TABLE IF NOT EXISTS wifi_networks (
  controller_id TEXT NOT NULL, ssid TEXT NOT NULL, secret TEXT DEFAULT '',
  priority INTEGER DEFAULT 0, kind TEXT DEFAULT 'hotspot', PRIMARY KEY (controller_id, ssid));
CREATE TABLE IF NOT EXISTS controller_config (
  controller_id TEXT PRIMARY KEY, version INTEGER DEFAULT 1, matrix_options TEXT DEFAULT '{}',
  audio_device TEXT DEFAULT 'default', ap_name TEXT DEFAULT 'NSL-unit',
  startup_program_id TEXT, startup_code TEXT, portal_url TEXT DEFAULT 'https://nsldisplays.com/portal',
  channel TEXT DEFAULT 'stable', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS command_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, controller_id TEXT NOT NULL, user_id TEXT,
  source TEXT NOT NULL, command TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS releases (
  id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, version TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'stable', bundle_key TEXT NOT NULL,
  bundle_sha256 TEXT NOT NULL DEFAULT '', signature TEXT NOT NULL, notes TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (organisation_id, channel, version));
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, organisation_id TEXT, user_id TEXT,
  action TEXT NOT NULL, detail TEXT DEFAULT '', at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_commands (
  id TEXT PRIMARY KEY, controller_id TEXT NOT NULL, command TEXT NOT NULL,
  from_user TEXT DEFAULT '', created_at TEXT NOT NULL, delivered_at TEXT);
"""


def connect(url: str | None = None) -> sqlite3.Connection:
    url = url or os.environ.get("DATABASE_URL", "sqlite:./dev.db")
    if url.startswith("sqlite:"):
        path = url.split("sqlite:", 1)[1] or "./dev.db"
        con = sqlite3.connect(path, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(SCHEMA)
        # lightweight migration for DBs created before active_version existed
        try:
            con.execute("ALTER TABLE controller_programs ADD COLUMN active_version INTEGER")
            con.commit()
        except Exception:
            pass
        for _ddl in ("ALTER TABLE controllers ADD COLUMN sync_state TEXT DEFAULT NULL",
                       "ALTER TABLE controllers ADD COLUMN pending_token TEXT DEFAULT NULL",
                       "ALTER TABLE controllers ADD COLUMN live_state TEXT DEFAULT NULL",
                       "ALTER TABLE voices ADD COLUMN num_speakers INTEGER DEFAULT 1",
                       "ALTER TABLE voices ADD COLUMN speakers TEXT DEFAULT '[]'",
                       "ALTER TABLE releases ADD COLUMN bundle_sha256 TEXT NOT NULL DEFAULT ''",
                       "ALTER TABLE releases ADD COLUMN organisation_id TEXT",
                       "ALTER TABLE p_gpio_rules ADD COLUMN board_source TEXT NOT NULL DEFAULT 'text'",
                       "ALTER TABLE p_gpio_rules ADD COLUMN destination_id TEXT"):
            try:
                con.execute(_ddl)
                con.commit()
            except Exception:
                pass
        try:
            con.execute("CREATE INDEX IF NOT EXISTS ix_controllers_token ON controllers(device_token_hash)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_controllers_code ON controllers(pairing_code)")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_releases_org_chan_ver "
                        "ON releases(organisation_id, channel, version)")
            con.commit()
        except Exception:
            pass
        try:
            con.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass
        return con
    # Postgres via psycopg if configured (same logical schema applied by migrations)
    raise RuntimeError("Postgres needs DATABASE_URL postgres://... and migrations; see deploy/")
