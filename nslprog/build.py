"""Build + read .nslprog files (format.md 9.3)."""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import tempfile
import uuid

from .hashing import content_hash_of_connection, file_sha256
from .schema import APPLICATION_ID, FORMAT_VERSION, NSLPROG_DDL
from .validate import ValidationResult, validate_connection


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_uuid() -> str:
    return str(uuid.uuid4())


def create_empty(path: str):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.executescript(NSLPROG_DDL)
        con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
        con.commit()
    finally:
        con.close()


class ProgramBuilder:
    """Assemble a program in memory then write an immutable .nslprog file."""

    def __init__(self, program_id: str | None = None, program_name: str = "Untitled",
                 px_width: int = 240, min_agent_version: str = "1.0.0",
                 created_by: str = "", generator: str = "nslprog 1.0.0"):
        self.program_id = program_id or new_uuid()
        self.program_name = program_name
        self.px_width = px_width
        self.min_agent_version = min_agent_version
        self.created_by = created_by
        self.generator = generator
        self.displays: list[dict] = []
        self.services: list[dict] = []
        self.destinations: list[dict] = []
        self.destination_pages: list[dict] = []
        self.stops: list[dict] = []
        self.layouts: list[dict] = []
        self.templates: list[dict] = []
        self.presets: list[dict] = []
        self.audio: dict[str, dict] = {}
        self.default_display_id: str | None = None

    # -- helpers to add rows --
    def add_display(self, name: str, w: int, h: int, position: int = 0, id: str | None = None) -> str:
        did = id or new_uuid()
        self.displays.append({"id": did, "name": name, "width_px": w, "height_px": h, "position": position})
        if self.default_display_id is None:
            self.default_display_id = did
        return did

    def add_audio(self, sha256: str, label: str, duration_ms: int, size: int,
                  sample_rate: int = 22050, codec: str = "flac") -> str:
        self.audio[sha256.lower()] = {"hash": sha256.lower(), "label": label,
                                      "duration_ms": duration_ms, "bytes": size,
                                      "codec": codec, "sample_rate": sample_rate}
        return sha256.lower()

    def add_service(self, number: str, name: str = "", position: int = 0,
                    next_stop_audio: str | None = None, approaching_audio: str | None = None,
                    last_stop_audio: str | None = None, terminus_clip_mode: str = "both",
                    id: str | None = None) -> str:
        sid = id or new_uuid()
        self.services.append({"id": sid, "number": number, "name": name, "position": position,
                              "next_stop_audio": next_stop_audio, "approaching_audio": approaching_audio,
                              "last_stop_audio": last_stop_audio, "terminus_clip_mode": terminus_clip_mode})
        return sid

    def add_destination(self, service_id: str, code: str, name: str, via: str = "", dest: str = "",
                        num: str | None = None, position: int = 0, extra: dict | None = None,
                        id: str | None = None) -> str:
        did = id or new_uuid()
        self.destinations.append({"id": did, "service_id": service_id, "code": code, "name": name,
                                  "position": position, "num": num, "via": via, "dest": dest,
                                  "extra_fields": json.dumps(extra or {}, separators=(",", ":"))})
        return did

    def add_destination_page(self, destination_id: str, position: int, via: str = "",
                             dest: str = "", id: str | None = None) -> str:
        pid = id or new_uuid()
        self.destination_pages.append({"id": pid, "destination_id": destination_id,
                                       "position": position, "via": via or "", "dest": dest or ""})
        return pid

    def add_stop(self, destination_id: str, position: int, name: str,
                 audio: str | None = None, board_text: str | None = None,
                 board_override: str | None = None, is_terminus: bool = False,
                 id: str | None = None) -> str:
        sid = id or new_uuid()
        self.stops.append({"id": sid, "destination_id": destination_id, "position": position,
                           "name": name, "audio": audio, "board_text": board_text,
                           "board_override": board_override, "is_terminus": 1 if is_terminus else 0})
        return sid

    def add_layout(self, destination_id: str, display_id: str, layout: dict, id: str | None = None) -> str:
        lid = id or new_uuid()
        self.layouts.append({"id": lid, "destination_id": destination_id, "display_id": display_id,
                             "layout": json.dumps(layout, separators=(",", ":"))})
        return lid

    def add_template(self, display_id: str, name: str, layout: dict,
                     is_default: bool = False, id: str | None = None) -> str:
        tid = id or new_uuid()
        self.templates.append({"id": tid, "display_id": display_id, "name": name,
                               "layout": json.dumps(layout, separators=(",", ":")),
                               "is_default": 1 if is_default else 0})
        return tid

    def add_preset(self, label: str, text: str, position: int = 0, id: str | None = None) -> str:
        pid = id or new_uuid()
        self.presets.append({"id": pid, "label": label, "text": text, "position": position})
        return pid

    def _referenced_audio(self) -> set[str]:
        refs: set[str] = set()
        for s in self.services:
            for k in ("next_stop_audio", "approaching_audio", "last_stop_audio"):
                if s[k]:
                    refs.add(s[k].lower())
        for st in self.stops:
            if st["audio"]:
                refs.add(st["audio"].lower())
        return refs

    def build(self, out_path: str, version: int, default_display_id: str | None = None) -> dict:
        """Validate in-memory, write file, return manifest-ish info."""
        # in-memory pre-checks so errors are ValueError with clear messages (not bare IntegrityError)
        codes: dict[str, str] = {}
        for d in self.destinations:
            c = d["code"] or ""
            if not (1 <= len(c) <= 8) or not c.isdigit():
                raise ValueError(f"build validation failed: destination {d['id']} code {c!r} must be 1-8 digits")
            if c in codes:
                raise ValueError(f"build validation failed: code {c!r} used twice in program")
            codes[c] = d["id"]
        nums: dict[str, str] = {}
        for s in self.services:
            if s["number"] in nums:
                raise ValueError(f"build validation failed: service number {s['number']!r} appears twice")
            nums[s["number"]] = s["id"]
        default_display_id = default_display_id or self.default_display_id
        tmp = out_path + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        con = sqlite3.connect(tmp)
        try:
            con.executescript(NSLPROG_DDL)
            con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            con.execute(f"PRAGMA user_version = {FORMAT_VERSION}")
            for d in self.displays:
                con.execute("INSERT INTO displays VALUES (?,?,?,?,?)",
                            (d["id"], d["name"], d["width_px"], d["height_px"], d["position"]))
            refs = self._referenced_audio()
            for h, a in self.audio.items():
                if h in refs:
                    con.execute("INSERT INTO audio VALUES (?,?,?,?,?,?)",
                                (a["hash"], a["label"], a["duration_ms"], a["bytes"], a["codec"], a["sample_rate"]))
            for s in self.services:
                con.execute("INSERT INTO services VALUES (?,?,?,?,?,?,?,?)",
                            (s["id"], s["number"], s["name"], s["position"], s["next_stop_audio"],
                             s["approaching_audio"], s["last_stop_audio"], s["terminus_clip_mode"]))
            for d in self.destinations:
                con.execute("INSERT INTO destinations VALUES (?,?,?,?,?,?,?,?,?)",
                            (d["id"], d["service_id"], d["code"], d["name"], d["position"],
                             d["num"], d["via"], d["dest"], d["extra_fields"]))
            for pg in self.destination_pages:
                con.execute("INSERT INTO destination_pages VALUES (?,?,?,?,?)",
                            (pg["id"], pg["destination_id"], pg["position"], pg["via"], pg["dest"]))
            for st in self.stops:
                con.execute("INSERT INTO stops VALUES (?,?,?,?,?,?,?,?)",
                            (st["id"], st["destination_id"], st["position"], st["name"], st["audio"],
                             st["board_text"], st["board_override"], st["is_terminus"]))
            for lay in self.layouts:
                con.execute("INSERT INTO layouts VALUES (?,?,?,?)",
                            (lay["id"], lay["destination_id"], lay["display_id"], lay["layout"]))
            for t in self.templates:
                con.execute("INSERT INTO layout_templates VALUES (?,?,?,?,?)",
                            (t["id"], t["display_id"], t["name"], t["layout"], t["is_default"]))
            for p in self.presets:
                con.execute("INSERT INTO override_presets VALUES (?,?,?,?)",
                            (p["id"], p["label"], p["text"], p["position"]))
            # content hash before meta insert (meta excluded)
            chash = content_hash_of_connection(con)
            meta = {"format": "nslprog", "format_version": str(FORMAT_VERSION),
                    "program_id": self.program_id, "program_name": self.program_name,
                    "version": str(version), "px_width": str(self.px_width),
                    "default_display": default_display_id or "",
                    "content_hash": chash, "min_agent_version": self.min_agent_version,
                    "created_at": _utcnow(), "created_by": self.created_by, "generator": self.generator}
            con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
            con.commit()
            vr = validate_connection(con, check_hash=True)
            if not vr.ok:
                raise ValueError("build validation failed: " + "; ".join(
                    e.message for e in vr.errors))
            con.commit()
        except Exception:
            con.close()
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        con.close()
        # vacuum for compactness (needs separate connection)
        vc = sqlite3.connect(tmp)
        vc.execute("VACUUM")
        vc.close()
        os.replace(tmp, out_path)
        sha = file_sha256(out_path)
        return {"program_id": self.program_id, "version": version, "content_hash": chash,
                "bundle_sha256": sha, "bytes": os.path.getsize(out_path)}


def read_meta(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
    finally:
        con.close()


def check_file(path: str) -> ValidationResult:
    """application_id + user_version + full validation."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        appid = con.execute("PRAGMA application_id").fetchone()[0]
        uver = con.execute("PRAGMA user_version").fetchone()[0]
        res = ValidationResult()
        if appid != APPLICATION_ID:
            res.errors.append(__import__("nslprog.validate", fromlist=["Issue"]).Issue(
                "app_id", f"bad application_id {appid:#x}"))
            return res
        if uver != FORMAT_VERSION:
            res.errors.append(__import__("nslprog.validate", fromlist=["Issue"]).Issue(
                "format_version", f"unsupported format version {uver}"))
            return res
        fmt = con.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        if not fmt or fmt[0] != "nslprog":
            from nslprog.validate import Issue
            res.errors.append(Issue("meta_format", "meta.format must be 'nslprog'"))
            return res
        inner = validate_connection(con, check_hash=True)
        res.errors.extend(inner.errors)
        res.warnings.extend(inner.warnings)
        return res
    finally:
        con.close()
