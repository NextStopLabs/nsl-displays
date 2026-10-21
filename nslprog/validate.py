"""Validation per format.md section 8. Errors block, warnings allow."""
from __future__ import annotations

import json
import re
import sqlite3

EXTRA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_FIELDS = {"num", "via", "dest"}
KNOWN_FONTS = {"pixel-small", "pixel-medium", "pixel-large", "pixel-huge", "mono-5x7", "mono-6x8"}
ALIGNS = {"left", "centre", "center", "right"}
VALIGNS = {"top", "middle", "bottom"}
SCROLLS = {"none", "always", "if_too_long"}
SHOW_IFS = {"always", "has_text", "override_active", "override_inactive"}
OVERRIDE_MODES = {"replace", "hide_others", "full"}


class Issue:
    def __init__(self, code: str, message: str, table: str = "", ref: str = ""):
        self.code = code
        self.message = message
        self.table = table
        self.ref = ref

    def as_dict(self):
        return {"code": self.code, "message": self.message, "table": self.table, "ref": self.ref}

    def __repr__(self):
        return f"Issue({self.code}: {self.message})"


class ValidationResult:
    def __init__(self):
        self.errors: list[Issue] = []
        self.warnings: list[Issue] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self):
        return {"ok": self.ok,
                "errors": [e.as_dict() for e in self.errors],
                "warnings": [w.as_dict() for w in self.warnings]}


def _rows(con: sqlite3.Connection, sql: str, params=()):
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None
    except sqlite3.Error:
        return False


def validate_connection(con: sqlite3.Connection, check_hash: bool = True) -> ValidationResult:
    """Full section-8 validation on an open .nslprog connection."""
    res = ValidationResult()
    con.execute("PRAGMA foreign_keys = ON")

    # integrity + fk (Pi side checks, run always - cheap on small files)
    try:
        integ = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integ != "ok":
            res.errors.append(Issue("integrity", f"integrity_check failed: {integ}"))
    except sqlite3.Error as e:
        res.errors.append(Issue("integrity", str(e)))
    try:
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            res.errors.append(Issue("foreign_key", f"foreign_key_check: {len(fk)} violation(s)", ref=str(fk[:3])))
    except sqlite3.Error as e:
        res.errors.append(Issue("foreign_key", str(e)))

    displays = _rows(con, "SELECT * FROM displays ORDER BY position")
    services = _rows(con, "SELECT * FROM services ORDER BY position")
    destinations = _rows(con, "SELECT * FROM destinations ORDER BY position")
    dest_pages = _rows(con, "SELECT * FROM destination_pages ORDER BY destination_id, position") \
        if _has_table(con, "destination_pages") else []
    stops = _rows(con, "SELECT * FROM stops ORDER BY destination_id, position")
    layouts = _rows(con, "SELECT * FROM layouts")
    templates = _rows(con, "SELECT * FROM layout_templates")
    audio_rows = _rows(con, "SELECT * FROM audio")
    audio_hashes = {a["hash"] for a in audio_rows}
    meta = {r["key"]: r["value"] for r in _rows(con, "SELECT key, value FROM meta")}

    # --- displays / default_display ---
    if not displays:
        res.errors.append(Issue("no_displays", "program has no displays"))
    disp_ids = {d["id"] for d in displays}
    disp_by_name = {d["name"]: d for d in displays}
    default_disp = meta.get("default_display", "")
    if default_disp and default_disp not in disp_ids:
        res.errors.append(Issue("bad_default_display", f"default_display {default_disp!r} does not exist"))
    if not default_disp:
        res.errors.append(Issue("bad_default_display", "default_display is missing"))

    # --- services: duplicate number ---
    seen_numbers: dict[str, int] = {}
    for s in services:
        seen_numbers.setdefault(s["number"], 0)
        seen_numbers[s["number"]] += 1
    for num, n in seen_numbers.items():
        if n > 1:
            res.errors.append(Issue("dup_service_number", f"service number {num!r} appears {n} times", "services", num))

    # --- destinations: code shape + uniqueness ---
    seen_codes: dict[str, list] = {}
    for d in destinations:
        code = d["code"] or ""
        if not (1 <= len(code) <= 8) or not code.isdigit():
            res.errors.append(Issue("bad_code", f"destination {d['id']} code {code!r} must be 1-8 digits",
                                    "destinations", d["id"]))
        seen_codes.setdefault(code, []).append(d["id"])
    for code, ids in seen_codes.items():
        if len(ids) > 1:
            res.errors.append(Issue("dup_code", f"code {code!r} used by {len(ids)} destinations", "destinations", code))
    # service ref
    svc_ids = {s["id"] for s in services}
    for d in destinations:
        if d["service_id"] not in svc_ids:
            res.errors.append(Issue("bad_service_ref", f"destination {d['id']} unknown service", "destinations", d["id"]))
    # extra_fields
    for d in destinations:
        try:
            ef = json.loads(d["extra_fields"] or "{}")
            if not isinstance(ef, dict):
                raise ValueError("must be object")
            for k, v in ef.items():
                if k in RESERVED_FIELDS:
                    res.errors.append(Issue("extra_reserved", f"extra field {k!r} is reserved", "destinations", d["id"]))
                if not EXTRA_KEY_RE.match(k):
                    res.errors.append(Issue("extra_key", f"bad extra field key {k!r}", "destinations", d["id"]))
                if not isinstance(v, str) or len(v) > 200:
                    res.errors.append(Issue("extra_value", f"extra field {k!r} must be string<=200", "destinations", d["id"]))
        except (ValueError, json.JSONDecodeError) as e:
            res.errors.append(Issue("extra_json", f"extra_fields invalid: {e}", "destinations", d["id"]))

    # --- destination pages: per destination 1..N no gaps ---
    dest_ids = {d["id"] for d in destinations}
    from collections import defaultdict
    pages_by_dest: dict[str, list] = defaultdict(list)
    for pg in dest_pages:
        if pg["destination_id"] not in dest_ids:
            res.errors.append(Issue("orphan_pages", f"pages for unknown destination {pg['destination_id']}",
                                    "destination_pages", pg["id"]))
        else:
            pages_by_dest[pg["destination_id"]].append(pg)
    for dest_id, lst in pages_by_dest.items():
        positions = sorted(r["position"] for r in lst)
        if positions != list(range(1, len(lst) + 1)):
            res.errors.append(Issue("page_gap", f"destination {dest_id} page positions must be 1..N no gaps, got {positions}",
                                    "destination_pages", dest_id))
        for pg in lst:
            if len(pg["via"] or "") > 200 or len(pg["dest"] or "") > 200:
                res.errors.append(Issue("page_value", "page via/dest must be <=200 chars",
                                        "destination_pages", pg["id"]))

    # --- stops: per destination 1..N no gaps, terminus last & single ---
    from collections import defaultdict
    by_dest: dict[str, list] = defaultdict(list)
    for s in stops:
        by_dest[s["destination_id"]].append(s)
    dest_ids = {d["id"] for d in destinations}
    for d in destinations:
        lst = sorted(by_dest.get(d["id"], []), key=lambda r: r["position"])
        if not lst:
            # display-only destinations need no stops (boards work without them;
            # announcements simply have nothing to chain). Warn, don't block.
            res.warnings.append(Issue("no_stops", f"destination {d['name']!r} ({d['code']}) has no stops",
                                      "stops", d["id"]))
            continue
        positions = [r["position"] for r in lst]
        if positions != list(range(1, len(lst) + 1)):
            res.errors.append(Issue("stop_gap", f"destination {d['name']!r} stop positions must be 1..N no gaps, got {positions}",
                                    "stops", d["id"]))
        termini = [r for r in lst if r["is_terminus"] == 1]
        if len(termini) > 1:
            res.errors.append(Issue("multi_terminus", f"destination {d['name']!r} has {len(termini)} termini",
                                    "stops", d["id"]))
        if termini and termini[0]["position"] != len(lst):
            res.errors.append(Issue("terminus_not_last", f"terminus must be last stop in {d['name']!r}",
                                    "stops", termini[0]["id"]))
        # audio refs
        for srow in lst:
            if srow["audio"] and srow["audio"] not in audio_hashes:
                res.errors.append(Issue("missing_audio", f"stop {srow['name']!r} audio {srow['audio'][:12]}… missing",
                                        "stops", srow["id"]))
            if not srow["audio"]:
                res.warnings.append(Issue("stop_no_audio", f"stop {srow['name']!r} has no audio (silent)",
                                          "stops", srow["id"]))
        if not termini:
            res.warnings.append(Issue("no_terminus", f"destination {d['name']!r} has no terminus flagged",
                                      "destinations", d["id"]))
    # orphan stops
    for dest_id in by_dest:
        if dest_id not in dest_ids:
            res.errors.append(Issue("orphan_stops", f"stops for unknown destination {dest_id}", "stops", dest_id))

    # --- service audio refs + warnings ---
    for s in services:
        for col in ("next_stop_audio", "approaching_audio", "last_stop_audio"):
            if s[col] and s[col] not in audio_hashes:
                res.errors.append(Issue("missing_audio", f"service {s['number']} {col} {s[col][:12]}… missing",
                                        "services", s["id"]))
        if not s["next_stop_audio"]:
            res.warnings.append(Issue("svc_no_next", f"service {s['number']} missing next-stop clip", "services", s["id"]))
        if not s["approaching_audio"]:
            res.warnings.append(Issue("svc_no_approach", f"service {s['number']} missing approaching clip", "services", s["id"]))
        if not s["last_stop_audio"]:
            res.warnings.append(Issue("svc_no_last", f"service {s['number']} missing last-stop clip", "services", s["id"]))

    # --- layouts ---
    page_counts: dict[str, int] = {}
    for _pg in dest_pages:
        page_counts[_pg["destination_id"]] = page_counts.get(_pg["destination_id"], 0) + 1
    for lay in layouts + [{"id": t["id"], "display_id": t["display_id"], "layout": t["layout"],
                           "destination_id": f"template:{t['name']}", "_template": True} for t in templates]:
        disp = next((d for d in displays if d["id"] == lay["display_id"]), None)
        try:
            lj = json.loads(lay["layout"])
        except (json.JSONDecodeError, TypeError) as e:
            ref = lay.get("destination_id", lay["id"])
            res.errors.append(Issue("layout_json", f"layout {lay['id']} invalid JSON: {e}", "layouts", ref))
            continue
        ref = lay.get("destination_id", lay["id"])
        npc = None if lay.get("_template") else page_counts.get(lay.get("destination_id"), 1)
        _check_layout_json(lj, disp, res, ref, page_count=npc)

    # unknown display names warning (program displays no output uses - informational;
    # portal knows outputs, Pi always accepts; emit only if a layout references nothing - skip here)

    # --- content_hash ---
    if check_hash and "content_hash" in meta:
        from .hashing import content_hash_of_connection
        recomputed = content_hash_of_connection(con)
        if recomputed != meta["content_hash"]:
            res.errors.append(Issue("content_hash", "content_hash mismatch (file modified after publish)",
                                    "meta", meta["content_hash"][:16]))

    return res


def _check_layout_json(lj: dict, disp: dict | None, res: ValidationResult, ref: str,
                       page_count: int | None = None):
    if not isinstance(lj, dict):
        res.errors.append(Issue("layout_json", "layout must be object", "layouts", ref))
        return
    if lj.get("schema", 1) != 1:
        # higher schema rejected by older readers per spec; treat unknown as error here
        if isinstance(lj.get("schema"), int) and lj["schema"] > 1:
            res.warnings.append(Issue("layout_schema", f"layout schema {lj['schema']} newer than reader", "layouts", ref))
    w, h = lj.get("width"), lj.get("height")
    if disp is not None and (w != disp["width_px"] or h != disp["height_px"]):
        res.errors.append(Issue("layout_size", f"layout {w}x{h} != display {disp['width_px']}x{disp['height_px']}",
                                "layouts", ref))
    ov = lj.get("override", {})
    if isinstance(ov, dict) and ov.get("mode", "replace") not in OVERRIDE_MODES:
        res.errors.append(Issue("layout_override", f"bad override mode {ov.get('mode')!r}", "layouts", ref))
    els = lj.get("elements", [])
    if not isinstance(els, list):
        res.errors.append(Issue("layout_elements", "elements must be a list", "layouts", ref))
        return
    for i, el in enumerate(els):
        tag = f"{ref} el#{i}"
        if not isinstance(el, dict):
            res.errors.append(Issue("layout_element", "element must be object", "layouts", tag))
            continue
        et = el.get("type")
        if et == "field":
            if not el.get("field"):
                res.errors.append(Issue("layout_field", "field element needs 'field'", "layouts", tag))
        elif et == "text":
            if "text" not in el:
                res.errors.append(Issue("layout_text", "text element needs 'text'", "layouts", tag))
        else:
            res.errors.append(Issue("layout_type", f"bad element type {et!r}", "layouts", tag))
        if "pages" in el and el.get("pages") not in (None, []):
            want = el.get("pages")
            if not isinstance(want, list) or not want or any(not isinstance(v, int) for v in want):
                res.errors.append(Issue("layout_pages", "'pages' must be a non-empty list of board-page numbers", "layouts", tag))
            elif page_count is not None and any(v < 1 or v > page_count for v in want):
                res.errors.append(Issue("layout_pages", f"element shows on board page(s) {want} but destination has {page_count} page(s) - fix the layout or the board pages", "layouts", tag))
        try:
            x, y, ww, hh = int(el.get("x", 0)), int(el.get("y", 0)), int(el.get("w", 0)), int(el.get("h", 0))
        except (TypeError, ValueError):
            res.errors.append(Issue("layout_geom", "x/y/w/h must be integers", "layouts", tag))
            continue
        if disp is not None and (x < 0 or y < 0 or ww <= 0 or hh <= 0 or x + ww > disp["width_px"] or y + hh > disp["height_px"]):
            res.errors.append(Issue("layout_overflow", f"element outside display: {x},{y} {ww}x{hh}", "layouts", tag))
        font = el.get("font", "pixel-medium")
        if font not in KNOWN_FONTS and not (isinstance(font, str) and font.startswith("han-")):
            res.warnings.append(Issue("unknown_font", f"unknown font {font!r}, will fall back", "layouts", tag))
        if "scale" in el:
            try:
                sc = int(el["scale"])
            except (TypeError, ValueError):
                sc = 0
            if sc < 1 or sc > 8:
                res.errors.append(Issue("layout_scale", "scale must be an integer 1..8", "layouts", tag))
        if "spacing" in el:
            try:
                sp = int(el["spacing"])
            except (TypeError, ValueError):
                sp = 999
            if sp < -8 or sp > 16:
                res.errors.append(Issue("layout_spacing", "spacing must be an integer -8..16", "layouts", tag))
        if el.get("align", "left") not in ALIGNS and el.get("align") not in (None,):
            res.errors.append(Issue("layout_align", f"bad align {el.get('align')!r}", "layouts", tag))
        if el.get("valign", "top") not in VALIGNS and el.get("valign") not in (None,):
            res.errors.append(Issue("layout_valign", f"bad valign {el.get('valign')!r}", "layouts", tag))
        if el.get("scroll", "none") not in SCROLLS:
            res.errors.append(Issue("layout_scroll", f"bad scroll {el.get('scroll')!r}", "layouts", tag))
        if el.get("show_if", "always") not in SHOW_IFS:
            res.errors.append(Issue("layout_show_if", f"bad show_if {el.get('show_if')!r}", "layouts", tag))
        # unscrollable overflow warning: fixed text wider than element without scroll
        txt = el.get("text", "") if et == "text" else ""
        if txt and el.get("scroll", "none") == "none" and len(txt) * 6 > ww:
            res.warnings.append(Issue("unscrollable_overflow", f"text may not fit ({len(txt)} chars in {ww}px)",
                                      "layouts", tag))


def validate_file(path: str, check_hash: bool = True) -> ValidationResult:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return validate_connection(con, check_hash=check_hash)
    finally:
        con.close()
