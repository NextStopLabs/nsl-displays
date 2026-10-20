"""Controller core — single source of truth (plan.md 6.1).

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
        self._con = sqlite3.connect(controller_db, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode = WAL")
        self._con.execute("PRAGMA foreign_keys = ON")

    def on_change(self, fn):
        self._listeners.append(fn)

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
        self._emit("state")
        return self.state()

    def select_code(self, code: str, source: str = "keypad", user_id: str | None = None) -> dict:
        """8-digit code -> service+destination in ACTIVE program. Clears override, resets to stop 1."""
        code = (code or "").strip()
        if len(code) != 8 or not code.isdigit():
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
        self._emit("state")
        return self.state()

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
        self._emit("state")
        return self.state()

    # ---- unified command dispatch (local UI, keypad agent, portal agent) ----
    def apply_command(self, cmd: dict, source: str = "local", user_id: str | None = None) -> dict:
        """Apply one command dict. Raises ValueError on bad/unknown commands."""
        cmd = cmd or {}
        action = cmd.get("command")
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

    def display_snapshot(self) -> dict:
        """Fields + override + layout ids for display driver / receivers."""
        st = self.state()
        if not st["program_id"]:
            return {"state": st, "fields": {}, "layouts": {}, "override": None}
        pc = self._open_prog(st["program_id"], st["program_version"])
        try:
            dest = pc.execute("SELECT * FROM destinations WHERE id=?", (st["destination_id"],)).fetchone()
            svc = pc.execute("SELECT * FROM services WHERE id=?", (st["service_id"],)).fetchone() if st["service_id"] else None
            stop = pc.execute("SELECT * FROM stops WHERE id=?", (st["stop_id"],)).fetchone()
            if not dest:
                return {"state": st, "fields": {}, "layouts": {}, "override": st["override_text"]}
            from .renderer import resolve_fields
            fields = resolve_fields(dict(dest), svc["number"] if svc else "")
            if stop:
                fields["__stop_name"] = stop["name"]
                fields["__board_text"] = stop["board_text"] or stop["name"]
            layouts = {}
            for lr in pc.execute("SELECT display_id, layout FROM layouts WHERE destination_id=?", (dest["id"],)):
                layouts[lr["display_id"]] = lr["layout"]
            for tr in pc.execute("SELECT display_id, layout FROM layout_templates WHERE is_default=1"):
                layouts.setdefault("template:" + tr["display_id"], tr["layout"])
            return {"state": st, "fields": fields, "destination": dict(dest),
                    "service": dict(svc) if svc else None, "stop": dict(stop) if stop else None,
                    "layouts": layouts, "override": st["override_text"]}
        finally:
            pc.close()
