"""controller.db bootstrap (format.md 11.2 + plan.md 4.2 config tables)."""
from __future__ import annotations

import datetime
import os
import uuid

from nslprog.schema import CONTROLLER_DB_DDL

DEFAULT_KEYMAP = [
    ("KEY_1", "digit", "1"), ("KEY_2", "digit", "2"), ("KEY_3", "digit", "3"),
    ("KEY_4", "digit", "4"), ("KEY_5", "digit", "5"), ("KEY_6", "digit", "6"),
    ("KEY_7", "digit", "7"), ("KEY_8", "digit", "8"), ("KEY_9", "digit", "9"),
    ("KEY_0", "digit", "0"),
    ("KEY_KP1", "digit", "1"), ("KEY_KP2", "digit", "2"), ("KEY_KP3", "digit", "3"),
    ("KEY_KP4", "digit", "4"), ("KEY_KP5", "digit", "5"), ("KEY_KP6", "digit", "6"),
    ("KEY_KP7", "digit", "7"), ("KEY_KP8", "digit", "8"), ("KEY_KP9", "digit", "9"),
    ("KEY_KP0", "digit", "0"),
    ("KEY_ENTER", "enter", None), ("KEY_KPENTER", "enter", None),
    ("KEY_BACKSPACE", "clear_one", None), ("KEY_DELETE", "clear_all", None), ("KEY_ESC", "clear_all", None),
    ("KEY_KPPLUS", "action", "stop_next"), ("KEY_KPMINUS", "action", "stop_prev"),
    ("KEY_KPASTERISK", "action", "announce_next"), ("KEY_KPSLASH", "action", "override_clear"),
    ("KEY_KPDOT", "action", "net_update_mode"),
]


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_controller_db(path: str, controller_name: str = "", role: str = "front"):
    """Bootstrap a fresh controller.db. controller_name stays EMPTY unless the
    caller passes one (installer --unit, nsl-set-unit, local UI): nothing in
    code presumes a bus name, so golden-image clones never share one."""
    from .db import connect as _db_connect
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fresh = not os.path.exists(path)
    con = _db_connect(path)
    con.executescript(CONTROLLER_DB_DDL)
    # Migration for pre-insert databases: state gains insert_destination_id.
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(state)").fetchall()]
        if "insert_destination_id" not in cols:
            con.execute("ALTER TABLE state ADD COLUMN insert_destination_id TEXT DEFAULT NULL")
        if "depart_mins" not in cols:
            con.execute("ALTER TABLE state ADD COLUMN depart_mins INTEGER DEFAULT NULL")
        if "depart_expires_at" not in cols:
            con.execute("ALTER TABLE state ADD COLUMN depart_expires_at REAL DEFAULT NULL")
    except Exception:
        pass
    if fresh:
        import json
        ap = f"NSL-{controller_name}" if controller_name else "NSL-setup"
        con.execute("INSERT OR IGNORE INTO state (id, program_id, program_version, service_id, destination_id, stop_id, override_text, override_set_at, volume, muted, updated_at, insert_destination_id) VALUES (1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,80,0,?,NULL)", (_utcnow(),))
        con.execute(
            "INSERT OR IGNORE INTO controller_config (id, version, matrix_options, audio_device, ap_name, startup_mode, release_channel, portal_url, controller_name, updated_at) VALUES (1,1,?,?,?,?,?,?,?,?)",
            (json.dumps({}), "default", ap, "restore", "stable",
             "https://nsldisplays.com/portal", controller_name, _utcnow()))
        con.execute("INSERT OR IGNORE INTO keypad_config VALUES (1,'',1,5000,'front')")
        for key_name, role_, param in DEFAULT_KEYMAP:
            if role_ == "digit":
                con.execute("INSERT OR IGNORE INTO keypad_keys (id, key_name, role, digit, step_order, action, action_param) VALUES (?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), key_name, role_, param, 0, None, None))
            elif role_ == "action":
                con.execute("INSERT OR IGNORE INTO keypad_keys (id, key_name, role, digit, step_order, action, action_param) VALUES (?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), key_name, role_, None, 0, param, None))
            else:
                con.execute("INSERT OR IGNORE INTO keypad_keys (id, key_name, role, digit, step_order, action, action_param) VALUES (?,?,?,?,?,?,?)",
                            (str(uuid.uuid4()), key_name, role_, None, 0, None, None))
        # default 10 m bus outputs
        from .config import WIRING_PRESETS
        for o in WIRING_PRESETS["bus-10m"]:
            con.execute("INSERT OR IGNORE INTO display_outputs (id, display_name, width_px, height_px, parallel_chain, x_offset, enabled, brightness, logical_width_px, transport, receiver_addr, program_display) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), o["display_name"], o["width_px"], o["height_px"], o["parallel_chain"],
                         o["x_offset"], 1, 70, None, o["transport"], o["receiver_addr"], o["program_display"]))
        con.commit()
    con.close()
    return path
