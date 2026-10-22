"""Controller core - single source of truth (plan.md 6.1).

Holds program/service/destination/stop/override/volume in controller.db `state`.
Override lifetime rule: override stays until stop changes or new
destination/service/program selected. Never cleared by timers, replays,
syncs, upgrades, reconnects. Clear override removes immediately.
Persisted across restarts (WAL store).
"""
from __future__ import annotations

import datetime
import json
import sqlite3

ACTIONS = {
    "announce_next", "announce_approaching", "announce_last", "announce_replay",
    "stop_next", "stop_prev", "stop_first",
    "program_next", "program_prev", "program_select",
    "service_next", "service_prev", "destination_next", "destination_prev",
    "destination_code", "override_apply", "override_clear",
    "volume_up", "volume_down", "volume_mute",
    "net_update_mode", "net_ap", "system_apply_pending",
}


def _utcnow() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_state_row(con: sqlite3.Connection):
    con.execute("INSERT OR IGNORE INTO state (id, program_id, program_version, service_id, destination_id, stop_id, override_text, override_set_at, volume, muted, updated_at) VALUES (1,NULL,NULL,NULL,NULL,NULL,NULL,NULL,80,0,?)",
                (_utcnow(),))


def get_state(con: sqlite3.Connection) -> dict:
    ensure_state_row(con)
    cur = con.execute("SELECT * FROM state WHERE id=1")
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, cur.fetchone()))


def _prog_con(db_path: str, program_id: str, version: int) -> sqlite3.Connection | None:
    row_con = sqlite3.connect("file::memory:?cache=shared", uri=True)
    return None  # placeholder replaced below


class Core:
    """Operates on controller.db + installed .nslprog files under base_dir."""

    def __init__(self, controller_db: str, programs_dir: str):
        self.controller_db = controller_db
        self.programs_dir = programs_dir
        self._listeners: list = []
        import os
        os.makedirs(programs_dir, exist_ok=True)
        from .db import connect as _db_connect
        self._con = _db_connect(controller_db, row_factory=sqlite3.Row)
        self._con.executescript("""
        CREATE TABLE IF NOT EXISTS gpio_overlays (
          target TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '',
          destination_id TEXT, rule_id TEXT, expires_at REAL,
          latched INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'oneshot',
          position INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS gpio_toggles (
          rule_id TEXT PRIMARY KEY, is_on INTEGER NOT NULL DEFAULT 0);""")
        self._con.commit()

    def on_change(self, fn):
        self._listeners.append(fn)

    # ---- GPIO display rules (per-program gpio_rules table) ----
    def gpio_rules(self) -> list[dict]:
        """Rules of the active program (empty when nothing selected)."""
        st = self.state()
        if not st["program_id"] or not st["program_version"]:
            return []
        pc = self._open_prog(st["program_id"], st["program_version"])
        if pc is None:
            return []
        try:
            try:
                rows = pc.execute("SELECT * FROM gpio_rules ORDER BY position").fetchall()
            except Exception:
                return []  # bundles published before gpio_rules existed
            return [dict(r) for r in rows]
        finally:
            pc.close()

    # Overlay state lives in controller.db (gpio_overlays / gpio_toggles) so
    # every process - input, display, core state server - sees the same
    # boards. In-memory state would stay invisible to the renderer, which
    # runs in its own process. expires_at uses wall-clock seconds.
    _OVERLAY_COLS = ("target, text, destination_id, rule_id, expires_at,"
                     " latched, kind, position")

    def _prune_gpio_overlays(self, now: float | None = None) -> None:
        import time as _t
        now = now if now is not None else _t.time()
        rows = self._con.execute(
            "SELECT target, rule_id, kind FROM gpio_overlays"
            " WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)).fetchall()
        if not rows:
            return
        for r in rows:
            if r["kind"] == "toggle" and r["rule_id"]:
                self._con.execute("INSERT OR REPLACE INTO gpio_toggles VALUES (?,0)",
                                  (r["rule_id"],))
        self._con.execute("DELETE FROM gpio_overlays WHERE expires_at IS NOT NULL AND expires_at <= ?",
                          (now,))
        self._con.commit()

    def _clear_oneshot_gpio_overlays(self) -> None:
        self._con.execute("DELETE FROM gpio_overlays WHERE latched=0")
        self._con.commit()

    def _clear_all_gpio_overlays(self) -> None:
        self._con.execute("DELETE FROM gpio_overlays")
        self._con.execute("DELETE FROM gpio_toggles")
        self._con.commit()

    def _gpio_board_entry(self, r: dict) -> dict:
        """Overlay entry for one rule: destination board ref + text fallback."""
        return {"text": (r.get("board_text") or "").strip(),
                "destination_id": (r.get("destination_id")
                                   if (r.get("board_source") or "text") == "destination" else None),
                "rule_id": r.get("id")}

    def _write_gpio_overlay(self, target: str, entry: dict, expires_at: float | None,
                            latched: bool, kind: str, position: int) -> None:
        self._con.execute(
            f"INSERT OR REPLACE INTO gpio_overlays ({self._OVERLAY_COLS}) VALUES (?,?,?,?,?,?,?,?)",
            (target, entry["text"], entry["destination_id"], entry["rule_id"],
             expires_at, 1 if latched else 0, kind, position))
        self._con.commit()

    def sync_gpio_levels(self, active_pins: set[int], now: float | None = None) -> dict:
        """Reconcile latched ``while_active`` overlays with live pin levels.

        Called by the input service after (re)connects and program changes so
        a held switch (reverse gear, toggle on) re-asserts its board without
        needing an edge. Returns the current overlay map.
        """
        import time as _t
        now = now if now is not None else _t.time()
        self._prune_gpio_overlays(now)
        want: dict[str, dict] = {}
        for r in self.gpio_rules():
            if r.get("trigger") != "while_active":
                continue
            entry = self._gpio_board_entry(r)
            if not entry["text"] and not entry["destination_id"]:
                continue
            try:
                pin = int(r["pin"])
            except (TypeError, ValueError):
                continue
            if pin not in active_pins:
                continue
            target = r.get("target") or "all"
            prev = want.get(target)
            if prev is None or (r.get("position") or 0) < (prev.get("position") or 0):
                want[target] = dict(entry, expires_at=None, latched=True, kind="level",
                                    position=r.get("position") or 0)
        # only level-follow entries are owned by the live pin state; toggle
        # boards stay until their rule is tapped again (or the program changes)
        self._con.execute("DELETE FROM gpio_overlays WHERE latched=1 AND kind='level'")
        for target, o in want.items():
            self._write_gpio_overlay(target, o, None, True, "level", o["position"])
        # The delete above must commit even when want is empty (pin released),
        # otherwise other connections never see the overlay clear.
        self._con.commit()
        return self.gpio_overlay_map(now)

    def gpio_overlay_map(self, now: float | None = None) -> dict[str, str]:
        """Current overlay text per target (for display_snapshot)."""
        self._prune_gpio_overlays(now)
        return {r["target"]: r["text"] for r in self._con.execute(
            "SELECT target, text FROM gpio_overlays WHERE text != ''")}

    def _gpio_toggle_text(self, r: dict, entry: dict) -> str:
        """Driver text for override effects: destination's driver message, else custom text."""
        text = entry["text"]
        if entry["destination_id"]:
            board = self._gpio_board(entry["destination_id"])
            if board and board["destination"].get("name"):
                text = board["destination"]["name"]
        return text

    def _gpio_toggle_on(self, rule_id: str | None) -> bool:
        row = self._con.execute("SELECT is_on FROM gpio_toggles WHERE rule_id=?", (rule_id,)).fetchone()
        return bool(row and row["is_on"])

    def _flip_gpio_toggle(self, r: dict, entry: dict, now: float,
                          source: str = "gpio", user_id: str | None = None) -> bool:
        """Flip one toggle rule's latched board. Returns True when turned on.

        On: overlay boards latch on their target (stop changes don't clear
        them); override effects apply the driver text. Off: our overlay entry
        is removed (old board restores); overrides are cleared only when they
        still hold the text we set, so a driver's own message is never wiped.
        """
        rid = r.get("id")
        target = r.get("target") or "all"
        on = not self._gpio_toggle_on(rid)
        self._con.execute("INSERT OR REPLACE INTO gpio_toggles VALUES (?,?)", (rid, 1 if on else 0))
        self._con.commit()
        if on:
            if (r.get("effect") or "overlay") == "override":
                text = self._gpio_toggle_text(r, entry)
                if text:
                    self.apply_override(text, source=source, user_id=user_id)
            else:
                dur = int(r.get("duration_s") or 0)
                self._write_gpio_overlay(target, entry, (now + dur) if dur > 0 else None,
                                         True, "toggle", r.get("position") or 0)
        else:
            cur = self._con.execute("SELECT rule_id FROM gpio_overlays WHERE target=?", (target,)).fetchone()
            if cur and cur["rule_id"] == rid:
                self._con.execute("DELETE FROM gpio_overlays WHERE target=?", (target,))
                self._con.commit()
            if (r.get("effect") or "overlay") == "override":
                if self.state()["override_text"] == self._gpio_toggle_text(r, entry):
                    self.clear_override(source=source, user_id=user_id)
        return on

    def apply_gpio_fired(self, fired: list[dict], now: float | None = None,
                         source: str = "gpio", user_id: str | None = None) -> dict:
        """Apply fired rule effects (press/long/double/on_active/on_release/toggle).

        Overlay effects show a board (auto-cleared after ``duration_s`` when
        set); override effects reuse the driver's override text. ``toggle``
        flips a latched board on/off per tap (audio plays on switch-on).
        Audio-only rules (no board text or board) just play their clip with
        no board change. Returns ``{"announcement": [clip hashes],
        "overlay": {...}}`` for the caller to speak through the PA.
        """
        import time as _t
        now = now if now is not None else _t.time()
        clips: list[str] = []
        for r in sorted(fired or [], key=lambda x: (x.get("position") or 0)):
            entry = self._gpio_board_entry(r)
            has_board = bool(entry["text"] or entry["destination_id"])
            if (r.get("trigger") or "press") == "toggle":
                if not has_board:
                    # audio-only toggle: nothing to latch, play on every tap
                    if r.get("audio") and r["audio"] not in clips:
                        clips.append(r["audio"])
                    continue
                if self._flip_gpio_toggle(r, entry, now, source=source, user_id=user_id):
                    if r.get("audio") and r["audio"] not in clips:
                        clips.append(r["audio"])
                continue
            if r.get("audio") and r["audio"] not in clips:
                clips.append(r["audio"])
            if not has_board:
                continue
            if (r.get("effect") or "overlay") == "override":
                # override shows driver text: the destination's driver message,
                # else the rule's custom text
                text = self._gpio_toggle_text(r, entry)
                if text:
                    self.apply_override(text, source=source, user_id=user_id)
            else:
                dur = int(r.get("duration_s") or 0)
                self._write_gpio_overlay(r.get("target") or "all", entry,
                                         (now + dur) if dur > 0 else None,
                                         False, "oneshot", r.get("position") or 0)
        self._prune_gpio_overlays(now)
        out: dict = {"announcement": clips, "overlay": self.gpio_overlay_map(now)}
        if clips:
            self._log(source, f"gpio_announce {' '.join(clips)}", user_id)
            self._con.commit()
        return out

    def _emit(self, event: str):
        for fn in self._listeners:
            try:
                fn(event, self.state())
            except Exception:
                pass

    def state(self) -> dict:
        cur = self._con.execute("SELECT * FROM state WHERE id=1")
        row = cur.fetchone()
        if not row:
            ensure_state_row(self._con)
            self._con.commit()
            cur = self._con.execute("SELECT * FROM state WHERE id=1")
            row = cur.fetchone()
        # Close any implicit read transaction so the next read sees the latest
        # committed state (WAL mode can otherwise keep a stale snapshot open).
        self._con.commit()
        return dict(row)

    def _prog_path(self, program_id: str, version: int) -> str:
        import os
        return os.path.join(self.programs_dir, program_id, f"{version}.nslprog")

    def _open_prog(self, program_id: str, version: int):
        import os
        p = self._prog_path(program_id, version)
        if not os.path.exists(p):
            return None
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def _log(self, source: str, command: str, user_id: str | None = None):
        self._con.execute("INSERT INTO command_log (source, command, user_id, at) VALUES (?,?,?,?)",
                          (source, command, user_id, _utcnow()))

    # ---- selection ----
    def select_program(self, program_id: str, source: str = "local", user_id: str | None = None) -> dict:
        """Select program: picks active installed version, first destination/stop, clears override."""
        row = self._con.execute(
            "SELECT version FROM installed_programs WHERE program_id=? AND status IN ('installed','active') ORDER BY version DESC LIMIT 1",
            (program_id,)).fetchone()
        if not row:
            raise ValueError(f"program {program_id} not installed")
        version = row[0]
        pc = self._open_prog(program_id, version)
        if pc is None:
            raise ValueError("program file missing")
        try:
            dest = pc.execute("SELECT d.*, s.number AS service_number FROM destinations d JOIN services s ON s.id=d.service_id ORDER BY s.position, d.position LIMIT 1").fetchone()
            if not dest:
                raise ValueError("program has no destinations")
            stop = pc.execute("SELECT * FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                              (dest["id"],)).fetchone()
            self._con.execute(
                "UPDATE state SET program_id=?, program_version=?, service_id=?, destination_id=?, stop_id=?, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                (program_id, version, dest["service_id"], dest["id"], stop["id"] if stop else None, _utcnow()))
            self._log(source, f"program_select {program_id}@{version}", user_id)
            self._con.commit()
        finally:
            pc.close()
        self._clear_all_gpio_overlays()
        self._emit("state")
        return self.state()

    def select_code(self, code: str, source: str = "keypad", user_id: str | None = None) -> dict:
        """1-8 digit code -> service+destination in ACTIVE program. Clears override, resets to stop 1."""
        code = (code or "").strip()
        if not (1 <= len(code) <= 8) or not code.isdigit():
            raise ValueError("INVALID")
        st = self.state()
        if not st["program_id"]:
            raise ValueError("INVALID")
        pc = self._open_prog(st["program_id"], st["program_version"])
        if pc is None:
            raise ValueError("INVALID")
        try:
            dest = pc.execute("SELECT * FROM destinations WHERE code=?", (code,)).fetchone()
            if not dest:
                raise ValueError("INVALID")
            stop = pc.execute("SELECT * FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                              (dest["id"],)).fetchone()
            self._con.execute(
                "UPDATE state SET service_id=?, destination_id=?, stop_id=?, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                (dest["service_id"], dest["id"], stop["id"] if stop else None, _utcnow()))
            self._log(source, f"destination_code {code}", user_id)
            self._con.commit()
        finally:
            pc.close()
        self._clear_oneshot_gpio_overlays()
        self._emit("state")
        return self.state()

    def select_destination(self, destination_id: str, source="local", user_id=None) -> dict:
        st = self.state()
        pc = self._open_prog(st["program_id"], st["program_version"])
        try:
            dest = pc.execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
            if not dest:
                raise ValueError("unknown destination")
            stop = pc.execute("SELECT * FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                              (destination_id,)).fetchone()
            self._con.execute(
                "UPDATE state SET service_id=?, destination_id=?, stop_id=?, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                (dest["service_id"], destination_id, stop["id"] if stop else None, _utcnow()))
            self._log(source, f"destination_select {destination_id}", user_id)
            self._con.commit()
        finally:
            pc.close()
        self._clear_oneshot_gpio_overlays()
        self._emit("state")
        return self.state()

    def browse(self) -> dict:
        """Return services + destinations for the currently selected program."""
        st = self.state()
        pid = st.get("program_id")
        version = st.get("program_version")
        if not pid or not version:
            return {"services": []}
        pc = self._open_prog(pid, version)
        if pc is None:
            return {"services": []}
        try:
            services = []
            for row in pc.execute("SELECT * FROM services ORDER BY position"):
                svc = dict(row)
                dests = [dict(d) for d in pc.execute(
                    "SELECT id, name, code, position FROM destinations WHERE service_id=? ORDER BY position",
                    (svc["id"],))]
                services.append({
                    "id": svc["id"],
                    "number": svc.get("number") or "",
                    "name": svc.get("description") or svc.get("name") or "",
                    "destinations": dests,
                })
            return {"services": services}
        finally:
            pc.close()

    # ---- stops ----
    def _stops_of_current(self, pc) -> list:
        st = self.state()
        return list(pc.execute("SELECT * FROM stops WHERE destination_id=? ORDER BY position",
                               (st["destination_id"],)).fetchall())

    def goto_stop(self, stop_id: str, source="local", user_id=None) -> dict:
        """Changing stop applies that stop's saved override (lifetime rule)."""
        st = self.state()
        pc = self._open_prog(st["program_id"], st["program_version"])
        try:
            row = pc.execute("SELECT * FROM stops WHERE id=?", (stop_id,)).fetchone()
            if not row:
                raise ValueError("unknown stop")
            saved = row["board_override"] or None
            self._con.execute(
                "UPDATE state SET stop_id=?, override_text=?, override_set_at=CASE WHEN ? IS NOT NULL THEN ? ELSE NULL END, updated_at=? WHERE id=1",
                (stop_id, saved, saved, _utcnow() if saved else None, _utcnow()))
            self._log(source, f"stop_goto {stop_id}", user_id)
            self._con.commit()
        finally:
            pc.close()
        self._clear_oneshot_gpio_overlays()
        self._emit("state")
        return self.state()

    def stop_step(self, delta: int, source="local", user_id=None) -> dict:
        st = self.state()
        if not st["destination_id"]:
            raise ValueError("no destination")
        pc = self._open_prog(st["program_id"], st["program_version"])
        try:
            stops = list(pc.execute("SELECT * FROM stops WHERE destination_id=? ORDER BY position",
                                    (st["destination_id"],)).fetchall())
            if not stops:
                raise ValueError("no stops")
            ids = [r["id"] for r in stops]
            try:
                idx = ids.index(st["stop_id"])
            except ValueError:
                idx = 0
            idx = max(0, min(len(ids) - 1, idx + delta))
            target = stops[idx]
            saved = target["board_override"] or None
            self._con.execute(
                "UPDATE state SET stop_id=?, override_text=?, override_set_at=CASE WHEN ? IS NOT NULL THEN ? ELSE NULL END, updated_at=? WHERE id=1",
                (target["id"], saved, saved, _utcnow() if saved else None, _utcnow()))
            self._log(source, f"stop_step {delta}", user_id)
            self._con.commit()
        finally:
            pc.close()
        self._clear_oneshot_gpio_overlays()
        self._emit("state")
        return self.state()

    # ---- override ----
    def apply_override(self, text: str, source="local", user_id=None) -> dict:
        self._con.execute("UPDATE state SET override_text=?, override_set_at=?, updated_at=? WHERE id=1",
                          (text, _utcnow(), _utcnow()))
        self._log(source, f"override_apply {text!r}", user_id)
        self._con.commit()
        self._emit("state")
        return self.state()

    def clear_override(self, source="local", user_id=None) -> dict:
        self._con.execute("UPDATE state SET override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                          (_utcnow(),))
        self._log(source, "override_clear", user_id)
        self._con.commit()
        self._emit("state")
        return self.state()

    # ---- volume ----
    def set_volume(self, volume: int | None = None, muted: bool | None = None,
                   source="local", user_id=None) -> dict:
        st = self.state()
        v = st["volume"] if volume is None else max(0, min(100, int(volume)))
        m = st["muted"] if muted is None else (1 if muted else 0)
        self._con.execute("UPDATE state SET volume=?, muted=?, updated_at=? WHERE id=1", (v, m, _utcnow()))
        self._log(source, f"volume {v} muted={m}", user_id)
        self._con.commit()
        self._emit("state")
        return self.state()

    # ---- activation carry-over (format.md 11.3 step 5) ----
    def activate_version(self, program_id: str, version: int, source="system") -> dict:
        """Switch active pointer with state carry-over by id; override survives only on same stop."""
        st = self.state()
        old_stop = st["stop_id"]
        old_override = st["override_text"]
        pc = self._open_prog(program_id, version)
        if pc is None:
            raise ValueError("program file missing")
        try:
            # validate destination/stop still exist
            dest_ok = True
            if st["program_id"] == program_id and st["destination_id"]:
                dest_ok = pc.execute("SELECT 1 FROM destinations WHERE id=?", (st["destination_id"],)).fetchone() is not None
            if st["program_id"] == program_id and dest_ok and st["destination_id"]:
                stop_ok = pc.execute("SELECT 1 FROM stops WHERE id=? AND destination_id=?",
                                     (st["stop_id"], st["destination_id"])).fetchone() is not None
                if not stop_ok:
                    first = pc.execute("SELECT id FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                                       (st["destination_id"],)).fetchone()
                    new_stop = first["id"] if first else None
                    self._con.execute("UPDATE state SET program_version=?, stop_id=?, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                                      (version, new_stop, _utcnow()))
                else:
                    # same stop: keep override; (upgrade never clears it)
                    self._con.execute("UPDATE state SET program_version=?, updated_at=? WHERE id=1",
                                      (version, _utcnow()))
            else:
                # different program or destination gone -> first destination/stop, clear override
                dest = pc.execute("SELECT * FROM destinations ORDER BY position LIMIT 1").fetchone()
                # need service order; fallback simple
                if dest:
                    first = pc.execute("SELECT id FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                                       (dest["id"],)).fetchone()
                    self._con.execute(
                        "UPDATE state SET program_id=?, program_version=?, service_id=?, destination_id=?, stop_id=?, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                        (program_id, version, dest["service_id"], dest["id"],
                         first["id"] if first else None, _utcnow()))
                else:
                    self._con.execute(
                        "UPDATE state SET program_id=?, program_version=?, service_id=NULL, destination_id=NULL, stop_id=NULL, override_text=NULL, override_set_at=NULL, updated_at=? WHERE id=1",
                        (program_id, version, _utcnow()))
            self._con.execute("UPDATE installed_programs SET status='superseded' WHERE program_id=? AND status='active'",
                              (program_id,))
            self._con.execute("UPDATE installed_programs SET status='active' WHERE program_id=? AND version=?",
                              (program_id, version))
            self._log(source, f"activate {program_id}@{version}", None)
            self._con.commit()
        finally:
            pc.close()
        self._clear_all_gpio_overlays()
        self._emit("state")
        return self.state()

    # ---- unified command dispatch (local UI, keypad agent, portal agent) ----
    def apply_command(self, cmd: dict, source: str = "local", user_id: str | None = None) -> dict:
        """Apply one command dict. Raises ValueError on bad/unknown commands."""
        if isinstance(cmd, str):
            try:
                cmd = json.loads(cmd)
            except Exception:
                raise ValueError(f"bad command payload {cmd!r}")
        cmd = cmd or {}
        action = cmd.get("command")
        # aliases used by the portal remote page
        action = {"destination_code": "select_code", "program_select": "select_program"}.get(action, action)
        if action == "select_code":
            return self.select_code(cmd.get("code", ""), source=source, user_id=user_id)
        if action == "select_destination":
            return self.select_destination(cmd.get("destination_id", ""), source=source, user_id=user_id)
        if action == "select_program":
            return self.select_program(cmd.get("program_id", ""), source=source, user_id=user_id)
        if action in ("stop_next", "stop_prev", "stop_first"):
            if action == "stop_first":
                st = self.state()
                pc = self._open_prog(st["program_id"], st["program_version"])
                if pc is None:
                    raise ValueError("no program selected")
                try:
                    first = pc.execute("SELECT id FROM stops WHERE destination_id=? ORDER BY position LIMIT 1",
                                       (st["destination_id"],)).fetchone()
                finally:
                    pc.close()
                if not first:
                    raise ValueError("no stops")
                return self.goto_stop(first["id"], source=source, user_id=user_id)
            return self.stop_step({"stop_next": 1, "stop_prev": -1}[action], source=source, user_id=user_id)
        if action == "override_apply":
            return self.apply_override(cmd.get("text", ""), source=source, user_id=user_id)
        if action == "override_clear":
            return self.clear_override(source=source, user_id=user_id)
        if action == "volume":
            return self.set_volume(cmd.get("volume"), cmd.get("muted"), source=source, user_id=user_id)
        if action == "activate":
            return self.activate_version(cmd.get("program_id"), int(cmd.get("version")), source=source)
        if action in ("announce_next", "announce_approaching"):
            return {"announcement": self.announcement_clips("next" if action == "announce_next" else "approaching")}
        raise ValueError(f"unknown command {action}")

    # ---- announcement composition (format.md 7) ----
    def announcement_clips(self, moment: str) -> list[str]:
        """moment in next|approaching. Returns ordered clip hashes (NULL skipped)."""
        st = self.state()
        if not st["program_id"]:
            return []
        pc = self._open_prog(st["program_id"], st["program_version"])
        try:
            dest = pc.execute("SELECT * FROM destinations WHERE id=?", (st["destination_id"],)).fetchone()
            stop = pc.execute("SELECT * FROM stops WHERE id=?", (st["stop_id"],)).fetchone()
            if not dest or not stop:
                return []
            svc = pc.execute("SELECT * FROM services WHERE id=?", (dest["service_id"],)).fetchone()
            seq: list[str] = []
            head = svc["next_stop_audio"] if moment == "next" else svc["approaching_audio"]
            if head:
                seq.append(head)
            if stop["audio"]:
                seq.append(stop["audio"])
            if stop["is_terminus"]:
                mode = svc["terminus_clip_mode"] or "both"
                if (moment == "next" and mode in ("next", "both")) or \
                   (moment == "approaching" and mode in ("approaching", "both")):
                    if svc["last_stop_audio"]:
                        seq.append(svc["last_stop_audio"])
            return seq
        finally:
            pc.close()

    def _gpio_board(self, destination_id: str) -> dict | None:
        """Resolve a GPIO board reference to render bits from the active bundle.

        Returns ``{fields, pages, layouts, destination, service}`` or None when
        the program/bundle/destination is gone (caller falls back to text).
        """
        if not destination_id:
            return None
        st = self.state()
        if not st["program_id"] or not st["program_version"]:
            return None
        pc = self._open_prog(st["program_id"], st["program_version"])
        if pc is None:
            return None
        try:
            try:
                dest = pc.execute("SELECT * FROM destinations WHERE id=?", (destination_id,)).fetchone()
            except Exception:
                return None
            if not dest:
                return None
            dest = dict(dest)
            svc = pc.execute("SELECT * FROM services WHERE id=?", (dest["service_id"],)).fetchone()
            svc = dict(svc) if svc else None
            from .renderer import resolve_fields
            fields = resolve_fields(dest, svc["number"] if svc else "")
            try:
                pages = [dict(r) for r in pc.execute(
                    "SELECT position, via, dest FROM destination_pages WHERE destination_id=? ORDER BY position",
                    (dest["id"],))]
            except Exception:
                pages = []
            layouts = {}
            names = {r["id"]: r["name"] for r in pc.execute("SELECT id, name FROM displays")}
            for lr in pc.execute("SELECT display_id, layout FROM layouts WHERE destination_id=?", (dest["id"],)):
                layouts[names.get(lr["display_id"], lr["display_id"])] = lr["layout"]
            for tr in pc.execute("SELECT display_id, layout FROM layout_templates WHERE is_default=1"):
                layouts.setdefault("template:" + names.get(tr["display_id"], tr["display_id"]), tr["layout"])
            return {"fields": fields, "pages": pages, "layouts": layouts,
                    "destination": dest, "service": svc}
        finally:
            pc.close()

    def gpio_board_map(self) -> dict[str, dict]:
        """Current destination-board overlays per target (for display_snapshot)."""
        self._prune_gpio_overlays()
        out: dict[str, dict] = {}
        for r in self._con.execute(
                "SELECT target, text, destination_id FROM gpio_overlays WHERE destination_id IS NOT NULL"):
            board = self._gpio_board(r["destination_id"])
            if board is not None:
                out[r["target"]] = board
            elif r["text"]:
                pass  # text fallback stays in gpio_overlay
        return out

    def display_snapshot(self) -> dict:
        """Fields + override + layout ids for display driver / receivers."""
        # Always end the read transaction before returning. In WAL mode a
        # long-lived implicit read transaction can pin an old snapshot, so the
        # display process never sees newly-cleared GPIO overlays.
        try:
            st = self.state()
            gpio_overlay = self.gpio_overlay_map()
            gpio_boards = self.gpio_board_map()
            if not st["program_id"]:
                # no program selected: still show an active override (manual message
                # must reach the boards with or without program context)
                return {"state": st, "fields": {}, "layouts": {}, "override": st["override_text"],
                        "gpio_overlay": gpio_overlay, "gpio_boards": gpio_boards}
            pc = self._open_prog(st["program_id"], st["program_version"])
            if pc is None:
                # installed row without its bundle (deleted mid-sync?) - empty, not a crash
                return {"state": st, "fields": {}, "layouts": {}, "override": st["override_text"],
                        "gpio_overlay": gpio_overlay, "gpio_boards": gpio_boards}
            try:
                dest = pc.execute("SELECT * FROM destinations WHERE id=?", (st["destination_id"],)).fetchone()
                svc = pc.execute("SELECT * FROM services WHERE id=?", (st["service_id"],)).fetchone() if st["service_id"] else None
                stop = pc.execute("SELECT * FROM stops WHERE id=?", (st["stop_id"],)).fetchone()
                if not dest:
                    return {"state": st, "fields": {}, "layouts": {}, "override": st["override_text"],
                            "gpio_overlay": gpio_overlay, "gpio_boards": gpio_boards}
                from .renderer import resolve_fields
                fields = resolve_fields(dict(dest), svc["number"] if svc else "")
                try:
                    pages = [dict(r) for r in pc.execute(
                        "SELECT position, via, dest FROM destination_pages WHERE destination_id=? ORDER BY position",
                        (dest["id"],))]
                except Exception:
                    pages = []
                if stop:
                    fields["__stop_name"] = stop["name"]
                    fields["__board_text"] = stop["board_text"] or stop["name"]
                layouts = {}
                names = {r["id"]: r["name"] for r in pc.execute("SELECT id, name FROM displays")}
                for lr in pc.execute("SELECT display_id, layout FROM layouts WHERE destination_id=?", (dest["id"],)):
                    # key by display NAME (front/side/rear): controller outputs and
                    # receivers address layouts by name, UUIDs never leave the bundle
                    layouts[names.get(lr["display_id"], lr["display_id"])] = lr["layout"]
                for tr in pc.execute("SELECT display_id, layout FROM layout_templates WHERE is_default=1"):
                    layouts.setdefault("template:" + names.get(tr["display_id"], tr["display_id"]), tr["layout"])
                return {"state": st, "fields": fields, "pages": pages, "destination": dict(dest),
                        "service": dict(svc) if svc else None, "stop": dict(stop) if stop else None,
                        "layouts": layouts, "override": st["override_text"],
                        "gpio_overlay": gpio_overlay, "gpio_boards": gpio_boards}
            finally:
                pc.close()
        finally:
            self._con.commit()
