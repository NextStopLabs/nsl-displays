"""Portal backend — FastAPI under base path /portal (plan.md 8).

Run: uvicorn portal.backend.app:app --root-path /portal
Covers: auth, orgs, controllers+pairing, programs/services/destinations/stops,
layouts/templates, audio+Piper stub, voices, publish/upload/export/import,
device API (manifest/bundle/audio/heartbeat/release/stream), remote command + WS.
Storage: local disk (STORAGE_DIR) with hash layout audio/aa/hash.flac.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .db import connect

# The shared Pi library lives in repo-root/pi (``pi.nsl_pi``). Dev checkouts and
# Pi services have it on sys.path already; bare portal deployments (uvicorn from
# the repo root, Docker) only have the root — add both, idempotently.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "pi")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
del _p

APP_VERSION = "1.0.0"
JWT_SECRET = os.environ.get("PORTAL_SECRET", "dev-secret-change-me")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")
BASE_PATH = "/portal"  # URL prefix when served behind the proxy; pass --root-path /portal to the server

app = FastAPI(title="NSL Portal")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


import threading as _threading
_tls = _threading.local()

def _current_db_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:./dev.db")


def db() -> sqlite3.Connection:
    url = _current_db_url()
    con = getattr(_tls, "con", None)
    # Honour test reset (tests set app.state._db = None to get a fresh DB).
    if getattr(app.state, "_db", "unset") is None:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass
        con, _tls.url = None, None
        # mark consumed so we don't reconnect-loop; tests set it to None again for next reset
        app.state._db = False
    if con is None or getattr(_tls, "url", None) != url:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass
        con = connect(url)
        try:
            con.execute("PRAGMA busy_timeout = 30000")
        except Exception:
            pass
        _tls.con, _tls.url = con, url
        app.state._db = con
    return con


@app.middleware("http")
async def _rollback_orphan_txn(request, call_next):
    try:
        resp = await call_next(request)
    except Exception:
        try:
            c = getattr(_tls, "con", None)
            if c is not None and getattr(c, "in_transaction", False):
                c.rollback()
        except Exception:
            pass
        raise
    # A handler that raised HTTPException after execute() but before commit()
    # would otherwise leave the txn open on the shared thread-local connection,
    # leaking half-written rows into the next request. Roll back leftovers on errors.
    try:
        if resp.status_code >= 400:
            c = getattr(_tls, "con", None)
            if c is not None and getattr(c, "in_transaction", False):
                c.rollback()
    except Exception:
        pass
    return resp


# ---------- auth (session token = signed user id, demo-grade) ----------
def _sign(uid: str) -> str:
    sig = hmac.new(JWT_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{uid}.{sig}"


def _verify(token: str) -> str | None:
    try:
        uid, sig = token.split(".", 1)
    except ValueError:
        return None
    expect = hmac.new(JWT_SECRET.encode(), uid.encode(), hashlib.sha256).hexdigest()[:32]
    return uid if hmac.compare_digest(expect, sig) else None


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "login required")
    uid = _verify(authorization.split(" ", 1)[1])
    if not uid:
        raise HTTPException(401, "bad token")
    row = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        raise HTTPException(401, "unknown user")
    return dict(row)


def _hash_pw(pw: str) -> str:
    from argon2 import PasswordHasher
    return PasswordHasher().hash(pw)


def _check_pw(hash_: str, pw: str) -> bool:
    try:
        from argon2 import PasswordHasher
        return PasswordHasher().verify(hash_, pw)
    except Exception:
        return False


def _membership(uid: str, org: str) -> dict | None:
    r = db().execute("SELECT * FROM memberships WHERE user_id=? AND organisation_id=?", (uid, org)).fetchone()
    return dict(r) if r else None


def require_role(uid: str, org: str, *roles: str) -> dict:
    m = _membership(uid, org)
    if not m or (roles and m["role"] not in roles):
        raise HTTPException(403, "forbidden")
    # suspended orgs read-only
    o = db().execute("SELECT * FROM organisations WHERE id=?", (org,)).fetchone()
    if o and o["status"] == "suspended" and any(r != "viewer" for r in roles):
        raise HTTPException(403, "organisation suspended (read-only)")
    return m


# ---------- auth routes ----------
@app.post("/api/auth/signup")
def signup(email: str = Form(...), password: str = Form(...), name: str = Form(""), org: str = Form("Demo Org")):
    con = db()
    if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise HTTPException(400, "email in use")
    if len(password or "") < 8:
        raise HTTPException(400, "password must be at least 8 characters")
    uid, oid = str(uuid.uuid4()), str(uuid.uuid4())
    con.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (uid, email, _hash_pw(password), name, None, _utcnow()))
    con.execute("INSERT INTO organisations VALUES (?,?,?,?)", (oid, org, "active", _utcnow()))
    con.execute("INSERT INTO memberships VALUES (?,?,?)", (uid, oid, "owner"))
    con.commit()
    return {"token": _sign(uid), "user_id": uid, "organisation_id": oid}


@app.post("/api/auth/login")
def login(email: str = Form(...), password: str = Form(...)):
    row = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not _check_pw(row["password_hash"], password):
        raise HTTPException(401, "bad login")
    return {"token": _sign(row["id"]), "user_id": row["id"]}


@app.get("/api/me")
def me(u: dict = Depends(current_user)):
    rows = db().execute(
        "SELECT o.id, o.name, o.status, m.role FROM organisations o "
        "JOIN memberships m ON m.organisation_id=o.id WHERE m.user_id=?", (u["id"],)).fetchall()
    return {"user": {"id": u["id"], "email": u["email"], "name": u.get("name", "")},
            "organisations": [dict(r) for r in rows]}


# ---------- controllers & pairing ----------
ONLINE_AFTER_S = 150  # last heartbeat within this window counts as online


def _controller_online(c) -> bool:
    """A bus is online only if it checked in recently. Never trust a stale flag."""
    try:
        last = datetime.strptime((c["last_seen_at"] or ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < ONLINE_AFTER_S


@app.get("/api/controllers")
def list_controllers(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    con = db()
    rows = con.execute("SELECT * FROM controllers WHERE organisation_id=?", (organisation_id,)).fetchall()
    out = []
    for r in rows:
        c = dict(r)
        c["online"] = _controller_online(c)
        try:
            c["sync_state"] = json.loads(c.get("sync_state") or "null")
        except Exception:
            c["sync_state"] = None
        c["programs"] = [dict(x) for x in con.execute(
            "SELECT cp.program_id, p.name, cp.desired_version, cp.installed_version, cp.active_version, cp.status "
            "FROM controller_programs cp LEFT JOIN programs p ON p.id=cp.program_id "
            "WHERE cp.controller_id=?", (c["id"],))]
        c["pending_commands"] = con.execute(
            "SELECT COUNT(*) FROM pending_commands WHERE controller_id=? AND delivered_at IS NULL",
            (c["id"],)).fetchone()[0]
        out.append(c)
    return {"controllers": out}


@app.post("/api/controllers/pair")
def pair_controller(organisation_id: str = Form(...), code: str = Form(...), name: str = Form(...),
                    u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor")
    con = db()
    row = con.execute("SELECT * FROM controllers WHERE pairing_code=? AND organisation_id IS NULL",
                      (code.upper(),)).fetchone()
    # portal preregisters pending rows on device register; find by code regardless of org
    if not row:
        row = con.execute("SELECT * FROM controllers WHERE pairing_code=?", (code.upper(),)).fetchone()
    if not row or (row["pairing_expires_at"] or "") < _utcnow():
        raise HTTPException(400, "bad or expired code")
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    con.execute("UPDATE controllers SET organisation_id=?, name=?, device_token_hash=?, status='paired', "
                "pairing_code=NULL, pairing_expires_at=NULL, pending_token=? WHERE id=?",
                (organisation_id, name, th, token, row["id"]))
    con.execute("INSERT INTO audit_log (organisation_id, user_id, action, detail, at) VALUES (?,?,?,?,?)",
                (organisation_id, u["id"], "controller.pair", row["id"], _utcnow()))
    con.commit()
    return {"controller_id": row["id"], "device_token": token}


@app.patch("/api/controllers/{cid}")
def rename_controller(cid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    con.execute("UPDATE controllers SET name=? WHERE id=?", (body.get("name", c["name"]), cid))
    con.commit()
    return {"ok": True}


# ---------- programs CRUD (draft editing) ----------
def _org_of_program(pid: str) -> str:
    r = db().execute("SELECT organisation_id FROM programs WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "no program")
    return r[0]


@app.get("/api/programs")
def list_programs(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    rows = db().execute("SELECT * FROM programs WHERE organisation_id=?", (organisation_id,)).fetchall()
    return {"programs": [dict(r) for r in rows]}


@app.post("/api/programs")
def create_program(body: dict, u: dict = Depends(current_user)):
    org = body["organisation_id"]
    require_role(u["id"], org, "owner", "editor")
    pid = str(uuid.uuid4())
    con = db()
    now = _utcnow()
    con.execute("INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, org, body.get("name", "Untitled"), body.get("px_width", 240),
                 body.get("default_display", ""), 0, 0, now, now, u["id"]))
    # default displays front/side/rear
    for i, (nm, w, h) in enumerate([("front", 240, 40), ("side", 160, 40), ("rear", 80, 40)]):
        con.execute("INSERT INTO program_displays VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), org, pid, nm, w, h, i))
        if i == 0:
            con.execute("UPDATE programs SET default_display=(SELECT id FROM program_displays WHERE program_id=? AND name='front') WHERE id=?",
                        (pid, pid))
    con.commit()
    return {"program_id": pid}


@app.get("/api/programs/{pid}")
def get_program(pid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    con = db()
    p = dict(con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone())
    p["displays"] = [dict(r) for r in con.execute("SELECT * FROM program_displays WHERE program_id=? ORDER BY position", (pid,))]
    p["services"] = [dict(r) for r in con.execute("SELECT * FROM p_services WHERE program_id=? ORDER BY position", (pid,))]
    for s in p["services"]:
        s["destinations"] = [dict(r) for r in con.execute(
            "SELECT * FROM p_destinations WHERE service_id=? ORDER BY position", (s["id"],))]
        for d in s["destinations"]:
            d["stops"] = [dict(r) for r in con.execute(
                "SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (d["id"],))]
            d["layouts"] = [dict(r) for r in con.execute(
                "SELECT * FROM p_layouts WHERE destination_id=?", (d["id"],))]
    p["presets"] = [dict(r) for r in con.execute("SELECT * FROM p_presets WHERE program_id=? ORDER BY position", (pid,))]
    p["versions"] = [dict(r) for r in con.execute("SELECT * FROM program_versions WHERE program_id=? ORDER BY version", (pid,))]
    return p


@app.post("/api/programs/{pid}/services")
def add_service(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    if con.execute("SELECT 1 FROM p_services WHERE program_id=? AND number=?", (pid, body["number"])).fetchone():
        raise HTTPException(400, "duplicate service number")
    sid = str(uuid.uuid4())
    pos = con.execute("SELECT COUNT(*) FROM p_services WHERE program_id=?", (pid,)).fetchone()[0]
    con.execute("INSERT INTO p_services VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, org, pid, body["number"], body.get("name", ""), pos,
                 body.get("next_stop_audio"), body.get("approaching_audio"),
                 body.get("last_stop_audio"), body.get("terminus_clip_mode", "both")))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"service_id": sid}


@app.post("/api/services/{sid}/destinations")
def add_destination(sid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    s = con.execute("SELECT * FROM p_services WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "no service")
    require_role(u["id"], s["organisation_id"], "owner", "editor")
    code = (body.get("code") or "").strip()
    if len(code) != 8 or not code.isdigit():
        raise HTTPException(400, "code must be exactly 8 digits")
    if con.execute("SELECT 1 FROM p_destinations WHERE program_id=? AND code=?", (s["program_id"], code)).fetchone():
        raise HTTPException(400, "duplicate code in program")
    did = str(uuid.uuid4())
    pos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=?", (sid,)).fetchone()[0]
    con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, s["organisation_id"], s["program_id"], sid, code, body.get("name", code), pos,
                 body.get("num"), body.get("via", ""), body.get("dest", ""),
                 json.dumps(body.get("extra", {}), separators=(",", ":"))))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], s["program_id"]))
    con.commit()
    return {"destination_id": did}


@app.put("/api/destinations/{did}")
def update_destination(did: str, body: dict, u: dict = Depends(current_user)):
    """Edit code / service / board fields of one destination (HELEN Edit dialog)."""
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    code = (body.get("code") or d["code"]).strip()
    if len(code) != 8 or not code.isdigit():
        raise HTTPException(400, "code must be exactly 8 digits")
    if con.execute("SELECT 1 FROM p_destinations WHERE program_id=? AND code=? AND id<>?",
                   (d["program_id"], code, did)).fetchone():
        raise HTTPException(400, "duplicate code in program")
    sid = body.get("service_id", d["service_id"])
    s = con.execute("SELECT * FROM p_services WHERE id=?", (sid,)).fetchone()
    if not s or s["program_id"] != d["program_id"]:
        raise HTTPException(400, "service must be in the same program")
    pos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=? AND id<>?",
                      (sid, did)).fetchone()[0]
    if sid != d["service_id"]:
        pos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=?", (sid,)).fetchone()[0]
    else:
        pos = d["position"]
    con.execute("UPDATE p_destinations SET service_id=?, code=?, name=?, position=?, num=?, via=?, dest=? WHERE id=?",
                (sid, code, body.get("name", d["name"]), pos,
                 body.get("num", d["num"]), body.get("via", d["via"]), body.get("dest", d["dest"]), did))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"ok": True}


@app.delete("/api/destinations/{did}")
def delete_destination(did: str, u: dict = Depends(current_user)):
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    con.execute("DELETE FROM p_stops WHERE destination_id=?", (did,))
    con.execute("DELETE FROM p_layouts WHERE destination_id=?", (did,))
    con.execute("DELETE FROM p_destinations WHERE id=?", (did,))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"ok": True}


@app.delete("/api/services/{sid}")
def delete_service(sid: str, u: dict = Depends(current_user)):
    con = db()
    s = con.execute("SELECT * FROM p_services WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "no service")
    require_role(u["id"], s["organisation_id"], "owner", "editor")
    dids = [r[0] for r in con.execute("SELECT id FROM p_destinations WHERE service_id=?", (sid,))]
    for did in dids:
        con.execute("DELETE FROM p_stops WHERE destination_id=?", (did,))
        con.execute("DELETE FROM p_layouts WHERE destination_id=?", (did,))
    con.execute("DELETE FROM p_destinations WHERE service_id=?", (sid,))
    con.execute("DELETE FROM p_services WHERE id=?", (sid,))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], s["program_id"]))
    con.commit()
    return {"ok": True, "deleted_destinations": len(dids)}


@app.get("/api/programs/{pid}/destinations/check-code")
def check_code(pid: str, code: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    ok_shape = len(code) == 8 and code.isdigit()
    taken = db().execute("SELECT id FROM p_destinations WHERE program_id=? AND code=?", (pid, code)).fetchone()
    return {"code": code, "shape_ok": ok_shape, "unique": taken is None,
            "suggestion": None if (ok_shape and taken is None) else suggest_code(pid)}


def suggest_code(pid: str) -> str:
    con = db()
    used = {r[0] for r in con.execute("SELECT code FROM p_destinations WHERE program_id=?", (pid,))}
    svc = con.execute("SELECT number FROM p_services WHERE program_id=? ORDER BY position LIMIT 1", (pid,)).fetchone()
    prefix = (svc[0] if svc else "10")[:4].ljust(2, "0")
    for i in range(1, 1000000):
        cand = (prefix + f"{i:06d}")[:8]
        if cand not in used and len(cand) == 8 and cand.isdigit():
            return cand
    return "00000001"


@app.put("/api/destinations/{did}/stops")
def put_stops(did: str, body: dict, u: dict = Depends(current_user)):
    """Replace ordered stop list (positions 1..N validated)."""
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    stops = body.get("stops", [])
    for _s in stops:
        if not (_s.get("name") or "").strip():
            raise HTTPException(400, "every stop needs a name")
    # validate positions: must be 1..N after reorder
    terms = [s for s in stops if s.get("is_terminus")]
    if len(terms) > 1:
        raise HTTPException(400, "only one terminus allowed")
    if terms and terms[0] is not stops[-1]:
        raise HTTPException(400, "terminus must be last stop")
    con.execute("DELETE FROM p_stops WHERE destination_id=?", (did,))
    for i, s in enumerate(stops, 1):
        con.execute("INSERT INTO p_stops VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (s.get("id") or str(uuid.uuid4()), d["organisation_id"], d["program_id"], did, i,
                     s["name"], s.get("audio"), s.get("board_text"), s.get("board_override"),
                     1 if s.get("is_terminus") else 0))
    con.commit()
    return {"ok": True, "count": len(stops)}


@app.get("/api/destinations/{did}/layouts/{display}")
def get_layout(did: str, display: str, u: dict = Depends(current_user)):
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor", "viewer")
    disp = con.execute("SELECT * FROM program_displays WHERE program_id=? AND name=?",
                       (d["program_id"], display)).fetchone()
    if not disp:
        raise HTTPException(404, "no display")
    row = con.execute("SELECT layout FROM p_layouts WHERE destination_id=? AND display_id=?",
                      (did, disp["id"])).fetchone()
    if row:
        return {"layout": json.loads(row[0]), "source": "destination"}
    t = con.execute("SELECT layout FROM p_templates WHERE program_id=? AND display_id=? AND is_default=1",
                    (d["program_id"], disp["id"])).fetchone()
    if t:
        return {"layout": json.loads(t[0]), "source": "template"}
    return {"layout": None, "source": "auto-fit"}


@app.get("/api/destinations/{did}/preview")
def preview_destination(did: str, pos: int = 1, u: dict = Depends(current_user)):
    """Stop-by-stop preview snapshot (mirrors Pi Core logic on draft tables).

    Returns fields, per-display layouts, and ordered announcement clip
    hashes for next/approaching at stop `pos` (1-based).
    """
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor", "viewer")
    d = dict(d)
    s = con.execute("SELECT * FROM p_services WHERE id=?", (d["service_id"],)).fetchone()
    s = dict(s) if s else {}
    stops = [dict(r) for r in con.execute(
        "SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (did,))]
    displays = [dict(r) for r in con.execute(
        "SELECT * FROM program_displays WHERE program_id=? ORDER BY position", (d["program_id"],))]
    count = len(stops)
    if count == 0:
        return {"destination": d, "service": s, "stop": None, "pos": 0, "count": 0,
                "stops": [], "fields": {}, "layouts": {}, "displays": displays,
                "next": [], "approaching": []}
    pos = max(1, min(pos, count))
    stop = stops[pos - 1]
    fields = {"num": d.get("num") or s.get("number") or "",
              "via": d.get("via") or "", "dest": d.get("dest") or ""}
    try:
        extra = json.loads(d.get("extra_fields") or "{}")
        for k, v in (extra.items() if isinstance(extra, dict) else []):
            fields[k] = v
    except Exception:
        pass
    fields["__stop_name"] = stop.get("name") or ""
    fields["__board_text"] = stop.get("board_text") or stop.get("name") or ""
    board_override = stop.get("board_override") or ""
    layouts = {}
    for disp in displays:
        row = con.execute("SELECT layout FROM p_layouts WHERE destination_id=? AND display_id=?",
                          (did, disp["id"])).fetchone()
        if row:
            layouts[disp["name"]] = {"layout": json.loads(row[0]), "source": "destination"}
            continue
        t = con.execute("SELECT layout FROM p_templates WHERE program_id=? AND display_id=? AND is_default=1",
                        (d["program_id"], disp["id"])).fetchone()
        if t:
            layouts[disp["name"]] = {"layout": json.loads(t[0]), "source": "template"}
        else:
            layouts[disp["name"]] = {"layout": None, "source": "auto-fit"}
    clips = {r["sha256"]: dict(r) for r in con.execute(
        "SELECT sha256, filename, duration_ms FROM audio_files WHERE organisation_id=?",
        (d["organisation_id"],))}
    def _meta(h):
        c = clips.get(h or "")
        return {"sha": h, "label": (c["filename"] if c else h or "—"),
                "duration_ms": (c["duration_ms"] if c else None)} if h else None
    def _seq(moment):
        seq = []
        head = s.get("next_stop_audio") if moment == "next" else s.get("approaching_audio")
        if head:
            m = _meta(head)
            if m:
                m["kind"] = "intro"
                seq.append(m)
        if stop.get("audio"):
            m = _meta(stop["audio"])
            if m:
                m["kind"] = "stop"
                seq.append(m)
        if stop.get("is_terminus"):
            mode = s.get("terminus_clip_mode") or "both"
            if (moment == "next" and mode in ("next", "both")) or \
               (moment == "approaching" and mode in ("approaching", "both")):
                if s.get("last_stop_audio"):
                    m = _meta(s["last_stop_audio"])
                    if m:
                        m["kind"] = "terminus"
                        seq.append(m)
        return seq
    return {"destination": d, "service": s, "stop": stop, "pos": pos, "count": count,
            "stops": [{"position": x["position"], "name": x["name"],
                       "is_terminus": bool(x["is_terminus"])} for x in stops],
            "fields": fields, "board_override": board_override,
            "layouts": layouts, "displays": displays,
            "next": _seq("next"), "approaching": _seq("approaching")}


@app.put("/api/destinations/{did}/layouts/{display}")
def put_layout(did: str, display: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    disp = con.execute("SELECT * FROM program_displays WHERE program_id=? AND name=?",
                       (d["program_id"], display)).fetchone()
    if not disp:
        raise HTTPException(404, "no display")
    lay = body["layout"]
    if lay["width"] != disp["width_px"] or lay["height"] != disp["height_px"]:
        raise HTTPException(400, "layout size must equal display size")
    lid = str(uuid.uuid4())
    con.execute("INSERT OR REPLACE INTO p_layouts VALUES (?,?,?,?,?,?)",
                (lid, d["organisation_id"], d["program_id"], did, disp["id"], json.dumps(lay)))
    con.commit()
    return {"ok": True}


# ---------- publish / upload / export / import ----------
def _build_nslprog(pid: str, version: int, created_by: str) -> tuple[str, dict]:
    """Copy draft -> immutable .nslprog file. Returns (path, info)."""
    from nslprog import ProgramBuilder
    con = db()
    p = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    b = ProgramBuilder(program_id=pid, program_name=p["name"], px_width=p["px_width"] or 240,
                       created_by=created_by, generator=f"portal {APP_VERSION}")
    # displays
    disp_map = {}
    for d in con.execute("SELECT * FROM program_displays WHERE program_id=? ORDER BY position", (pid,)):
        nd = b.add_display(d["name"], d["width_px"], d["height_px"], d["position"], id=d["id"])
        disp_map[d["id"]] = nd
    default_disp = p["default_display"] or next(iter(disp_map), None)
    # audio referenced by this program only (services + stops). Bundling the
    # whole org library bloated every publish and slowed bus downloads.
    ref_hashes: set[str] = set()
    for s in con.execute("SELECT next_stop_audio, approaching_audio, last_stop_audio FROM p_services WHERE program_id=?", (pid,)):
        for v in (s["next_stop_audio"], s["approaching_audio"], s["last_stop_audio"]):
            if v:
                ref_hashes.add(v)
    for (h,) in con.execute("SELECT DISTINCT audio FROM p_stops WHERE program_id=? AND audio IS NOT NULL", (pid,)):
        if h:
            ref_hashes.add(h)
    audio_needed: dict[str, dict] = {}
    if ref_hashes:
        for a in con.execute(
                f"SELECT * FROM audio_files WHERE organisation_id=? AND sha256 IN ({','.join('?'*len(ref_hashes))})",
                (p["organisation_id"], *ref_hashes)):
            audio_needed[a["sha256"]] = dict(a)
    missing = ref_hashes - set(audio_needed)
    if missing:
        raise ValueError(f"{len(missing)} referenced clip(s) missing from the audio library — re-upload them")
    for a in audio_needed.values():
        fp = _resolve_clip_path(a) or os.path.join(STORAGE_DIR, str(a.get("storage_key") or ""))
        try:
            size = os.path.getsize(fp)
        except OSError:
            size = 0
        b.add_audio(a["sha256"], a["filename"], a["duration_ms"] or 1000, size)
    for s in con.execute("SELECT * FROM p_services WHERE program_id=? ORDER BY position", (pid,)):
        b.add_service(s["number"], s["name"] or "", s["position"], s["next_stop_audio"],
                      s["approaching_audio"], s["last_stop_audio"], s["terminus_clip_mode"] or "both",
                      id=s["id"])
        for d in con.execute("SELECT * FROM p_destinations WHERE service_id=? ORDER BY position", (s["id"],)):
            b.add_destination(s["id"], d["code"], d["name"], d["via"] or "", d["dest"] or "",
                              d["num"], d["position"], json.loads(d["extra_fields"] or "{}"), id=d["id"])
            for st in con.execute("SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (d["id"],)):
                b.add_stop(d["id"], st["position"], st["name"], st["audio"], st["board_text"],
                           st["board_override"], bool(st["is_terminus"]), id=st["id"])
            for l in con.execute("SELECT * FROM p_layouts WHERE destination_id=?", (d["id"],)):
                b.add_layout(d["id"], l["display_id"], json.loads(l["layout"]), id=l["id"])
    for t in con.execute("SELECT * FROM p_templates WHERE program_id=?", (pid,)):
        b.add_template(t["display_id"], t["name"], json.loads(t["layout"]), bool(t["is_default"]), id=t["id"])
    for pr in con.execute("SELECT * FROM p_presets WHERE program_id=? ORDER BY position", (pid,)):
        b.add_preset(pr["label"], pr["text"], pr["position"], id=pr["id"])
    os.makedirs(os.path.join(STORAGE_DIR, "programs", pid), exist_ok=True)
    out = os.path.join(STORAGE_DIR, "programs", pid, f"{version}.nslprog")
    info = b.build(out, version, default_display_id=default_disp)
    return out, info


@app.post("/api/programs/{pid}/publish")
def publish(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    try:
        return _do_publish(pid, org, body.get("notes", ""), u)
    except ValueError as e:
        raise HTTPException(400, f"cannot publish: {e}")


def _do_publish(pid: str, org: str, notes: str, u: dict) -> dict:
    """Build the next immutable version unless nothing changed. Shared by
    publish and publish-upload (one flow, one code path)."""
    con = db()
    p = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    nxt = (p["published_version"] or 0) + 1
    # build to temp version path first to compute content_hash
    out, info = _build_nslprog(pid, nxt, u["email"] if isinstance(u, dict) else "")
    latest = con.execute("SELECT content_hash FROM program_versions WHERE program_id=? ORDER BY version DESC LIMIT 1",
                         (pid,)).fetchone()
    if latest and latest[0] == info["content_hash"]:
        os.remove(out)
        return {"ok": True, "version": nxt - 1, "unchanged": True, "content_hash": info["content_hash"]}
    # audio totals
    import sqlite3 as _s
    c2 = _s.connect(f"file:{out}?mode=ro", uri=True)
    acount, abytes = c2.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM audio").fetchone()
    c2.close()
    con.execute("INSERT INTO program_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), pid, nxt, f"programs/{pid}/{nxt}.nslprog",
                 info["bundle_sha256"], info["content_hash"], info["bytes"], acount, abytes,
                 notes, u["id"], _utcnow()))
    con.execute("UPDATE programs SET published_version=?, updated_at=? WHERE id=?", (nxt, _utcnow(), pid))
    con.commit()
    return {"ok": True, "version": nxt, "content_hash": info["content_hash"],
            "bundle_sha256": info["bundle_sha256"], "bytes": info["bytes"],
            "audio_count": acount, "audio_bytes": abytes}


def _do_queue_upload(con, org: str, pid: str, ver: int, cids: list) -> list:
    queued = []
    for cid in cids:
        c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
        if not c or c["organisation_id"] != org:
            raise HTTPException(400, f"bad controller {cid}")
        con.execute("INSERT OR REPLACE INTO controller_programs "
                    "(controller_id, program_id, desired_version, installed_version, status, last_error, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (cid, pid, ver, None, "pending", None, _utcnow()))
        queued.append(cid)
    con.commit()
    return queued


@app.post("/api/programs/{pid}/upload")
def upload(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    p = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    ver = body.get("version") or p["published_version"]
    queued = _do_queue_upload(con, org, pid, ver, body.get("controllers", []))
    return {"ok": True, "version": ver, "queued": queued}


@app.post("/api/programs/{pid}/publish-upload")
def publish_upload(pid: str, body: dict, u: dict = Depends(current_user)):
    """One flow: publish (if changed) + upload latest to the chosen buses.
    This is the normal path — versions are an implementation detail."""
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    try:
        pub = _do_publish(pid, org, body.get("notes", ""), u)
    except ValueError as e:
        raise HTTPException(400, f"cannot publish: {e}")
    queued = _do_queue_upload(db(), org, pid, pub["version"], body.get("controllers", []))
    return {"ok": True, "version": pub["version"], "unchanged": pub.get("unchanged", False),
            "queued": queued}


@app.get("/api/programs/{pid}/versions/{v}/export")
def export_pack_route(pid: str, v: int, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    from nslprog import export_pack
    bundle = os.path.join(STORAGE_DIR, "programs", pid, f"{v}.nslprog")
    if not os.path.exists(bundle):
        raise HTTPException(404, "no such version")
    pack = os.path.join(STORAGE_DIR, "programs", pid, f"{v}.nslpack")
    export_pack(bundle, os.path.join(STORAGE_DIR, "audio"), pack, base_url=".")
    return FileResponse(pack, filename=f"{pid}-v{v}.nslpack")


# ---------- Hanover bitmap fonts (repo HANOVERFONTS/, served to the layout editor) ----------
@app.get("/api/fonts")
def list_fonts(u: dict = Depends(current_user)):
    from nsl_pi import hanover as _han
    return {"fonts": _han.list_fonts()}


@app.get("/api/fonts/{fid}")
def get_font(fid: str, u: dict = Depends(current_user)):
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", fid or ""):
        raise HTTPException(400, "bad font id")
    from nsl_pi import hanover as _han
    try:
        f = _han.get_font(fid if fid.startswith("han-") else "han-" + fid)
    except KeyError:
        raise HTTPException(404, "no such font")
    return {"id": f["id"], "height": f["height"], "maxwidth": f["maxwidth"],
            "glyphs": {str(k): [v[0], *v[1]] for k, v in f["glyphs"].items()}}

# ---------- audio + voices (upload / Piper stub) ----------
def _slugify_clip(name: str) -> str:
    import re as _re
    s = (name or "").strip().lower()
    # drop extension if user pasted a filename
    if "." in s:
        s = s.rsplit(".", 1)[0]
    s = _re.sub(r"[^a-z0-9]+", "-", s)
    s = _re.sub(r"-{2,}", "-", s).strip("-")
    return s[:64]


def _unique_clip_name(con, organisation_id: str, want: str, sha: str) -> str:
    """Keep friendly names unique per org; derby-bus-station, derby-bus-station-2, ..."""
    rows = con.execute("SELECT sha256, filename FROM audio_files WHERE organisation_id=?",
                       (organisation_id,)).fetchall()
    taken = {r[1]: r[0] for r in rows}
    if want not in taken or taken[want] == sha:
        return want
    i = 2
    while f"{want}-{i}" in taken and taken[f"{want}-{i}"] != sha:
        i += 1
    return f"{want}-{i}"


# ---------- audio normalisation (fixes silent/unplayable .flac previews) ----------
# Root causes of "play is silent even on my system":
#  1. generate_audio wrote 1s of digital silence (zeros) as a stub.
#  2. upload_audio stored raw bytes (MP3/WAV/...) under a `.flac` name, so
#     players expecting FLAC got undecodable data.
# Canonical form is now: mono 16-bit WAV @ 22050 Hz. Browsers, `wave`,
# `aplay` and the Pi player all handle it with no extra codec.
_CANON_RATE = 22050


def _wav_bytes_from_pcm(pcm: bytes, rate: int = _CANON_RATE) -> bytes:
    import io as _io
    import wave as _w
    buf = _io.BytesIO()
    with _w.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _decode_to_mono22050(raw: bytes, hint_name: str = "upload") -> tuple[bytes, int]:
    """Decode arbitrary audio bytes -> (pcm mono s16 @22050, duration_ms).

    Raises HTTPException(400) with a user-actionable message when undecodable.
    """
    import io as _io
    import shutil as _sh
    import struct as _st
    import subprocess as _sp
    import tempfile as _tf
    import wave as _wv
    if not raw or len(raw) < 100:
        raise HTTPException(400, "that file is empty or too small to be audio")
    # Fast path: real WAV parses with stdlib (no ffmpeg needed).
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        try:
            with _wv.open(_io.BytesIO(raw), "rb") as w:
                n, ch, sw, sr = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
                frames = w.readframes(n)
            if n == 0:
                raise HTTPException(400, "that WAV contains no audio frames")
            # bytes -> samples, downmix + resample handling
            if sw == 1:  # 8-bit unsigned
                samples = [(b - 128) * 256 for b in frames]
            elif sw == 2:
                samples = list(_st.unpack(f"<{len(frames)//2}h", frames))
            else:
                raise ValueError(f"unsupported WAV depth {sw*8}-bit")
            if ch == 2:
                samples = [int((samples[i] + samples[i + 1]) / 2) for i in range(0, len(samples), 2)]
            elif ch > 2:
                raise HTTPException(400, f"that WAV has {ch} channels — export mono or stereo")
            if sr != _CANON_RATE:
                ff = _sh.which("ffmpeg")
                if not ff:
                    raise HTTPException(400, f"that WAV is {sr} Hz — export 22050 Hz mono WAV (or install ffmpeg)")
                with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as t:
                    t.write(raw)
                    tmp = t.name
                try:
                    p = _sp.run([ff, "-v", "error", "-i", tmp, "-ac", "1", "-ar", str(_CANON_RATE),
                                 "-f", "s16le", "-"], capture_output=True)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                if not p.stdout:
                    raise HTTPException(400, "could not resample that WAV to 22050 Hz")
                pcm = p.stdout
            else:
                pcm = _st.pack(f"<{len(samples)}h", *[max(-32768, min(32767, s)) for s in samples])
            peak = max((abs(s) for s in _st.unpack(f"<{len(pcm)//2}h", pcm)), default=0) if pcm else 0
            if peak < 100:
                raise HTTPException(400, "that file is digital silence — nothing would play")
            return pcm, int(len(pcm) / 2 / _CANON_RATE * 1000)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"could not read that WAV ({e})")
    # General path: needs ffmpeg (MP3/OGG/M4A/FLAC/etc).
    ff = _sh.which("ffmpeg")
    if not ff:
        raise HTTPException(400, "that format needs ffmpeg to decode — upload a 22050 Hz mono WAV instead")
    suffix = os.path.splitext(hint_name or "upload")[1] or ".bin"
    with _tf.NamedTemporaryFile(suffix=suffix, delete=False) as t:
        t.write(raw)
        tmp = t.name
    try:
        p = _sp.run([ff, "-v", "error", "-i", tmp, "-ac", "1", "-ar", str(_CANON_RATE),
                     "-f", "s16le", "-"], capture_output=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not p.stdout or len(p.stdout) < 200:
        msg = (p.stderr or b"").decode(errors="ignore")[-300:]
        raise HTTPException(400, "could not decode that file as audio. Upload MP3/WAV/OGG/M4A/FLAC. " + msg)
    import struct as _st2
    peak = max((abs(s) for s in _st2.unpack(f"<{len(p.stdout)//2}h", p.stdout)), default=0)
    if peak < 100:
        raise HTTPException(400, "that file decodes to silence — nothing would play")
    return p.stdout, int(len(p.stdout) / 2 / _CANON_RATE * 1000)


def _placeholder_tts_wav(text: str) -> tuple[bytes, int]:
    """Audible stand-in until a real Piper voice is configured.

    Old stub wrote 1s of zeros (pure silence). This plays a two-tone chime
    whose length scales with the text, so the play chain is verifiable.
    """
    import math as _m
    import struct as _st
    n_chars = max(1, min(len(text), 60))
    secs = min(4.0, 0.6 + 0.06 * n_chars)
    n = int(_CANON_RATE * secs)
    out = []
    for i in range(n):
        t = i / _CANON_RATE
        # 660 Hz then 880 Hz halves + gentle decay so it sounds like a chime
        f = 660.0 if t < secs / 2 else 880.0
        env = 0.6 + 0.4 * _m.sin(_m.pi * t / secs)
        out.append(int(12000 * env * _m.sin(2 * _m.pi * f * t)))
    pcm = _st.pack(f"<{n}h", *out)
    return _wav_bytes_from_pcm(pcm), int(secs * 1000)


def _try_piper_wav(text: str, voice_id: str | None, organisation_id: str) -> tuple[tuple[bytes, int] | None, str]:
    """Run the `piper` binary with the uploaded ONNX voice.

    Returns (result, reason). result is None when falling back to the
    preview tone; reason explains why (surfaced to the UI).
    """
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    if not voice_id:
        return None, "no voice selected — pick a voice in the Voice dropdown"
    if not _sh.which("piper"):
        return None, "piper binary not found on server PATH — install piper-tts (pip install piper-tts) + espeak-ng"
    con = db()
    v = con.execute("SELECT * FROM voices WHERE id=? AND (organisation_id=? OR organisation_id IS NULL)",
                    (voice_id, organisation_id)).fetchone()
    if not v:
        return None, "selected voice not found for this organisation — re-select it"
    model = os.path.join(STORAGE_DIR, str(v["model_key"]))
    config = os.path.join(STORAGE_DIR, str(v["config_key"]))
    # piper1-gpl __main__.py ignores --config and always loads
    # f"{model}.json" (PiperVoice.load(model_path) with no config_path).
    # Heal both old and new layouts: ensure model.onnx.json exists.
    expected_cfg = model + ".json"
    try:
        if not os.path.exists(expected_cfg) and os.path.exists(config):
            import shutil as _shutil
            _shutil.copyfile(config, expected_cfg)
        if os.path.exists(expected_cfg):
            config = expected_cfg
    except OSError:
        pass
    if not (os.path.exists(model) and os.path.exists(config)):
        return None, "voice files missing on server disk (STORAGE_DIR mismatch?) — re-upload the voice"
    with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        tmp = t.name
    try:
        p = _sp.run(["piper", "--model", model, "--config", config, "--output_file", tmp],
                    input=text.encode(), capture_output=True, timeout=120)
        if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 200:
            err = (p.stderr or b"").decode(errors="ignore")[-300:] if p else ""
            return None, f"piper failed (exit {p.returncode if p else '?'}) {err}".strip()
        with open(tmp, "rb") as f:
            raw = f.read()
        pcm, _ = _decode_to_mono22050(raw, "piper.wav")
        wav = _wav_bytes_from_pcm(pcm)
        return (wav, int(len(pcm) / 2 / _CANON_RATE * 1000)), ""
    except Exception as e:
        return None, f"piper error: {e}"
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _resolve_clip_path(row) -> str | None:
    """Find the actual file for a clip row (new .wav + legacy .flac names)."""
    candidates = [os.path.join(STORAGE_DIR, str(row["storage_key"]))]
    sk = str(row["storage_key"])
    if sk.endswith(".flac"):
        candidates.append(os.path.join(STORAGE_DIR, sk[:-5] + ".wav"))
    elif sk.endswith(".wav"):
        candidates.append(os.path.join(STORAGE_DIR, sk[:-4] + ".flac"))
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


@app.post("/api/audio")
def upload_audio(organisation_id: str = Form(...), file: UploadFile = File(...),
                 name: str = Form(default=""),
                 u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor")
    raw = file.file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "file too large")
    # Friendly clip name, e.g. derby-bus-station (never show the hash to users).
    want = _slugify_clip(name) or _slugify_clip(os.path.splitext(file.filename or "")[0])
    if not want or len(want) < 2:
        raise HTTPException(400, "give this clip a name, e.g. derby-bus-station")
    # Normalise to canonical playable WAV; rejects undecodable/silent files
    # (old code stored raw bytes under a .flac name -> silent/unplayable).
    pcm, duration_ms = _decode_to_mono22050(raw, file.filename or "upload")
    wav = _wav_bytes_from_pcm(pcm)
    h = hashlib.sha256(wav).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".wav")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(wav)
    con = db()
    friendly = _unique_clip_name(con, organisation_id, want, h)
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), organisation_id, h, friendly, f"audio/{h[:2]}/{h}.wav",
                 duration_ms, "upload", None, None, None, _utcnow()))
    con.commit()
    row = con.execute("SELECT filename, duration_ms FROM audio_files WHERE organisation_id=? AND sha256=?",
                      (organisation_id, h)).fetchone()
    final = row[0] if row else friendly
    dur = row[1] if row else duration_ms
    return {"sha256": h, "bytes": len(wav), "name": final, "filename": final,
            "duration_ms": dur}


@app.post("/api/audio/generate")
def generate_audio(body: dict, u: dict = Depends(current_user)):
    """Piper TTS. Fails loudly — no preview-tone fallback."""
    require_role(u["id"], body["organisation_id"], "owner", "editor")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "type the text to speak first")
    if not body.get("voice_id"):
        raise HTTPException(400, "select a voice — no preview tones, generation needs a real voice")
    want = _slugify_clip(body.get("name") or "")
    if not want:
        # suggest from the text: first few words -> derby-bus-station style
        want = _slugify_clip("-".join(text.split()[:5]))
    if not want or len(want) < 2:
        raise HTTPException(400, "give this clip a name, e.g. derby-bus-station")
    real, tts_error = _try_piper_wav(text, body.get("voice_id"), body["organisation_id"])
    if real is None:
        raise HTTPException(500, tts_error or "voice generation failed")
    wav, duration_ms = real
    h = hashlib.sha256(wav).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".wav")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(wav)
    con = db()
    friendly = _unique_clip_name(con, body["organisation_id"], want, h)
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), body["organisation_id"], h, friendly,
                 f"audio/{h[:2]}/{h}.wav", duration_ms, "tts", text, body.get("voice_id"),
                 json.dumps({"speed": body.get("speed", 1.0), "placeholder": False}), _utcnow()))
    con.commit()
    row = con.execute("SELECT filename, duration_ms FROM audio_files WHERE organisation_id=? AND sha256=?",
                      (body["organisation_id"], h)).fetchone()
    final = row[0] if row else friendly
    dur = row[1] if row else duration_ms
    return {"sha256": h, "name": final, "filename": final, "duration_ms": dur,
            "placeholder": False}


@app.post("/api/voices")
def upload_voice(organisation_id: str = Form(...), name: str = Form(...),
                 model: UploadFile = File(...), config: UploadFile = File(...),
                 u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor")
    mraw, craw = model.file.read(), config.file.read()
    if len(mraw) > 200 * 1024 * 1024:
        raise HTTPException(400, "voice too large")
    try:
        cfg = json.loads(craw.decode())
    except Exception:
        raise HTTPException(400, "config must be JSON (.onnx.json)")
    vid = str(uuid.uuid4())
    os.makedirs(os.path.join(STORAGE_DIR, "voices", vid), exist_ok=True)
    open(os.path.join(STORAGE_DIR, "voices", vid, "model.onnx"), "wb").write(mraw)
    # NOTE: piper1-gpl ignores --config and loads f"{model}.json",
    # so the config MUST live at model.onnx.json. Keep config.json as a
    # copy for backwards compat with older rows.
    open(os.path.join(STORAGE_DIR, "voices", vid, "model.onnx.json"), "wb").write(craw)
    open(os.path.join(STORAGE_DIR, "voices", vid, "config.json"), "wb").write(craw)
    # Piper .onnx.json uses nested shapes: language is an object like
    # {"code": "en_US", ...} and sample_rate lives under audio.sample_rate.
    # Flatten both to TEXT / INTEGER for the voices table.
    lang = cfg.get("language", "")
    if isinstance(lang, dict):
        lang = lang.get("code") or lang.get("name") or ""
    if not isinstance(lang, str):
        lang = str(lang or "")
    sr = cfg.get("sample_rate")
    if not isinstance(sr, int) or isinstance(sr, bool):
        audio = cfg.get("audio")
        sr = audio.get("sample_rate", 22050) if isinstance(audio, dict) else 22050
    try:
        sr = int(sr)
    except Exception:
        sr = 22050
    con = db()
    con.execute("INSERT INTO voices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, organisation_id, name, f"voices/{vid}/model.onnx",
                 f"voices/{vid}/model.onnx.json", lang, sr,
                 0, "", u["id"], len(mraw), _utcnow()))
    con.commit()
    return {"voice_id": vid, "sample_rate": sr}


@app.get("/api/voices")
def list_voices(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    import shutil as _sh
    rows = db().execute(
        "SELECT * FROM voices WHERE organisation_id IS NULL OR organisation_id=?", (organisation_id,)).fetchall()
    return {"voices": [dict(r) for r in rows], "piper_available": bool(_sh.which("piper"))}


# ---------- device API (Pi, token auth) ----------
def device_controller(token: str | None) -> dict:
    if not token or not token.startswith("Bearer "):
        raise HTTPException(401, "device token required")
    th = hashlib.sha256(token.split(" ", 1)[1].encode()).hexdigest()
    row = db().execute("SELECT * FROM controllers WHERE device_token_hash=?", (th,)).fetchone()
    if not row:
        raise HTTPException(401, "bad device token")
    if row["organisation_id"]:
        o = db().execute("SELECT status FROM organisations WHERE id=?", (row["organisation_id"],)).fetchone()
        if o and o[0] == "suspended":
            raise HTTPException(403, "suspended")
    return dict(row)


@app.post("/api/device/register")
def device_register(body: dict, authorization: str | None = Header(default=None)):
    """Fresh Pi: no token -> pairing code (6 chars) + pending record (15 min)."""
    con = db()
    try:
        con.execute("DELETE FROM controllers WHERE organisation_id IS NULL AND pairing_expires_at IS NOT NULL "
                    "AND pairing_expires_at < ?", (_utcnow(),))
        con.commit()
    except Exception:
        pass
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
    cid = str(uuid.uuid4())
    exp = datetime.now(timezone.utc).timestamp() + 15 * 60
    exp_s = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("INSERT INTO controllers (id, organisation_id, name, device_token_hash, status,"
                " last_seen_at, app_version, channel, supported_formats, pairing_code, pairing_expires_at,"
                " config_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, None, "unpaired", None, "pending", None, "", "stable", "1",
                 code, exp_s, 1, _utcnow()))
    con.commit()
    return {"controller_id": cid, "pairing_code": code, "expires_at": exp_s}


@app.post("/api/device/claim")
def device_claim(body: dict):
    """Pi polls with its controller_id after showing the pairing code.

    Returns paired=False until the portal pairs it, then the one-time
    device_token (cleared on read so it can't be fetched twice)."""
    cid = (body or {}).get("controller_id", "")
    row = db().execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown controller")
    tok = row["pending_token"] if "pending_token" in row.keys() else None
    if row["organisation_id"] and row["device_token_hash"] and tok:
        db().execute("UPDATE controllers SET pending_token=NULL WHERE id=?", (cid,))
        db().commit()
        return {"paired": True, "device_token": tok, "controller_id": cid}
    return {"paired": False, "controller_id": cid}


@app.get("/api/device/manifest")
def device_manifest(authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    con = db()
    out = []
    for cp in con.execute("SELECT * FROM controller_programs WHERE controller_id=?", (c["id"],)):
        v = con.execute("SELECT * FROM program_versions WHERE program_id=? AND version=?",
                        (cp["program_id"], cp["desired_version"])).fetchone()
        if not v:
            continue
        p = con.execute("SELECT name FROM programs WHERE id=?", (cp["program_id"],)).fetchone()
        # audio list: hashes referenced by this version's bundle
        import sqlite3 as _s
        bundle = os.path.join(STORAGE_DIR, str(v["bundle_key"]))
        ahashes = []
        if os.path.exists(bundle):
            cc = _s.connect(f"file:{bundle}?mode=ro", uri=True)
            try:
                ahashes = [{"hash": r[0], "bytes": r[1],
                            "url": f"/api/device/audio/{r[0]}"} for r in cc.execute("SELECT hash, bytes FROM audio")]
            finally:
                cc.close()
        out.append({"manifest_version": 1, "program_id": cp["program_id"],
                    "program_name": p["name"] if p else cp["program_id"],
                    "version": v["version"], "format_version": 1,
                    "content_hash": v["content_hash"], "min_agent_version": "1.0.0",
                    "bundle": {"url": f"/api/device/programs/{cp['program_id']}/versions/{v['version']}/bundle",
                               "sha256": v["bundle_sha256"], "bytes": v["size_bytes"]},
                    "audio": ahashes})
    return {"programs": out}


@app.get("/api/device/programs/{pid}/versions/{v}/bundle")
def device_bundle(pid: str, v: int, authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    con = db()
    prog = con.execute("SELECT organisation_id FROM programs WHERE id=?", (pid,)).fetchone()
    if not prog or prog[0] != c["organisation_id"]:
        raise HTTPException(404, "no bundle")
    if not con.execute("SELECT 1 FROM controller_programs WHERE controller_id=? AND program_id=?",
                       (c["id"], pid)).fetchone():
        raise HTTPException(404, "no bundle")
    path = os.path.join(STORAGE_DIR, "programs", pid, f"{v}.nslprog")
    if not os.path.exists(path):
        raise HTTPException(404, "no bundle")
    return FileResponse(path, filename=f"{v}.nslprog")


@app.get("/api/device/audio/{h}")
def device_audio(h: str, authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    if not db().execute("SELECT 1 FROM audio_files WHERE sha256=? AND organisation_id=?",
                        (h, c["organisation_id"])).fetchone():
        raise HTTPException(404, "no clip")
    # canonical store is .wav (legacy .flac accepted); serve whichever exists.
    base = os.path.join(STORAGE_DIR, "audio", h[:2], h)
    for ext, media in ((".wav", "audio/wav"), (".flac", "audio/x-flac")):
        if os.path.exists(base + ext):
            return FileResponse(base + ext, filename=h + ext, media_type=media)
    raise HTTPException(404, "no clip")


@app.post("/api/device/heartbeat")
def device_heartbeat(body: dict, authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    con = db()
    con.execute("UPDATE controllers SET last_seen_at=?, status='online', app_version=? WHERE id=?",
                (_utcnow(), body.get("app_version", ""), c["id"]))
    con.execute("UPDATE controllers SET sync_state=? WHERE id=?",
                (json.dumps(body.get("sync")) if body.get("sync") else None, c["id"]))
    for inst in body.get("installed", []):
        con.execute("UPDATE controller_programs SET installed_version=?, status=?, updated_at=? WHERE controller_id=? AND program_id=?",
                    (inst.get("version"), inst.get("status", "installed"), _utcnow(), c["id"], inst.get("program_id")))
    st8 = body.get("state") or {}
    if st8.get("program_id"):
        con.execute("UPDATE controller_programs SET active_version=NULL WHERE controller_id=?", (c["id"],))
        con.execute("UPDATE controller_programs SET active_version=? WHERE controller_id=? AND program_id=?",
                    (st8.get("program_version"), c["id"], st8.get("program_id")))
    for ev in body.get("events", []):
        con.execute("INSERT INTO command_log (controller_id, user_id, source, command, at) VALUES (?,?,?,?,?)",
                    (c["id"], None, "device", json.dumps(ev)[:500], _utcnow()))
    pend = [dict(r) for r in con.execute(
        "SELECT id, command, created_at FROM pending_commands "
        "WHERE controller_id=? AND delivered_at IS NULL ORDER BY created_at", (c["id"],))]
    if pend:
        con.execute("UPDATE pending_commands SET delivered_at=? WHERE controller_id=? AND delivered_at IS NULL",
                    (_utcnow(), c["id"]))
    con.commit()
    return {"accepted": True, "now": _utcnow(), "pending_commands": pend}


@app.get("/api/device/release")
def device_release(authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    row = db().execute("SELECT * FROM releases WHERE channel=? ORDER BY created_at DESC LIMIT 1",
                       (c["channel"] or "stable",)).fetchone()
    if not row:
        return {"version": None}
    return {"version": row["version"], "bundle_key": row["bundle_key"], "signature": row["signature"]}


# remote command fan-out: portal persists, Pi picks up on next heartbeat (<=60 s).
# Offline buses are refused up front — a command that can't reach a bus is an
# error here, not a silent "queued".
@app.post("/api/controllers/{cid}/command")
def remote_command(cid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c or not c["organisation_id"]:
        raise HTTPException(404, "no paired controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    if not _controller_online(c):
        raise HTTPException(409, f"controller offline (last seen {c['last_seen_at'] or 'never'}) — command not queued")
    con.execute("INSERT INTO pending_commands VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), cid, json.dumps(body), u["id"], _utcnow(), None))
    con.execute("INSERT INTO command_log (controller_id, user_id, source, command, at) VALUES (?,?,?,?,?)",
                (cid, u["id"], "portal", json.dumps(body)[:500], _utcnow()))
    con.commit()
    pending = con.execute("SELECT COUNT(*) FROM pending_commands WHERE controller_id=? AND delivered_at IS NULL",
                          (cid,)).fetchone()[0]
    return {"queued": True, "online": True, "pending": pending}


@app.websocket("/api/device/stream")
async def device_stream(ws: WebSocket):
    await ws.accept()
    try:
        hello = await ws.receive_json()
        token = hello.get("token", "")
        th = hashlib.sha256(token.encode()).hexdigest()
        row = db().execute("SELECT * FROM controllers WHERE device_token_hash=?", (th,)).fetchone()
        if not row:
            await ws.send_json({"error": "bad token"})
            await ws.close()
            return
        cid = row["id"]
        pend = [dict(r) for r in db().execute(
            "SELECT id, command, created_at FROM pending_commands "
            "WHERE controller_id=? AND delivered_at IS NULL ORDER BY created_at", (cid,))]
        await ws.send_json({"hello": "ok", "pending": pend})
        if pend:
            db().execute("UPDATE pending_commands SET delivered_at=? WHERE controller_id=? AND delivered_at IS NULL",
                         (_utcnow(), cid))
            db().commit()
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "state":
                await ws.send_json({"ack": msg.get("seq", 0)})
            pend = [dict(r) for r in db().execute(
                "SELECT id, command, created_at FROM pending_commands "
                "WHERE controller_id=? AND delivered_at IS NULL ORDER BY created_at", (cid,))]
            if pend:
                await ws.send_json({"commands": pend})
                db().execute("UPDATE pending_commands SET delivered_at=? WHERE controller_id=? AND delivered_at IS NULL",
                             (_utcnow(), cid))
                db().commit()
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/api/controllers/{cid}/live")
async def portal_live(ws: WebSocket, cid: str):
    await ws.accept()
    await ws.send_json({"status": "watch-only; Pi pushes state on /api/device/stream when online"})
    await ws.close()


# serve static frontend (portal UI) if built
FRONT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.isdir(FRONT):
    app.mount("/static", StaticFiles(directory=FRONT, html=True), name="static")

    @app.get("/", include_in_schema=False)
    def _index():
        # relative target so it works with or without the /portal prefix
        return RedirectResponse(url="static/", status_code=307)


@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


# ---------- controller config / keypad / networks / startup (plan.md 5.2a) ----------
@app.get("/api/controllers/{cid}/config")
def get_controller_config(cid: str, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor", "viewer")
    cfg = con.execute("SELECT * FROM controller_config WHERE controller_id=?", (cid,)).fetchone()
    outs = [dict(r) for r in con.execute("SELECT * FROM display_outputs WHERE controller_id=?", (cid,))]
    kp = con.execute("SELECT * FROM keypad_config WHERE controller_id=?", (cid,)).fetchone()
    nets = [dict(r) for r in con.execute("SELECT ssid, priority, kind FROM wifi_networks WHERE controller_id=?", (cid,))]
    return {"controller": dict(c), "config": dict(cfg) if cfg else None,
            "outputs": outs, "keypad": dict(kp) if kp else None, "networks": nets}


@app.put("/api/controllers/{cid}/config")
def put_controller_config(cid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    import json as _j
    con.execute("INSERT INTO controller_config VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(controller_id) DO UPDATE SET version=version+1, matrix_options=excluded.matrix_options, audio_device=excluded.audio_device, ap_name=excluded.ap_name, startup_program_id=excluded.startup_program_id, startup_code=excluded.startup_code, portal_url=excluded.portal_url, channel=excluded.channel, updated_at=excluded.updated_at",
                (cid, 1, _j.dumps(body.get("matrix_options", {})), body.get("audio_device", "default"),
                 body.get("ap_name", "NSL-unit"), body.get("startup_program_id"), body.get("startup_code"),
                 body.get("portal_url", "https://nslboards.com/portal"), body.get("channel", "stable"), _utcnow()))
    con.execute("UPDATE controllers SET config_version=config_version+1 WHERE id=?", (cid,))
    for o in body.get("outputs", []):
        if "id" in o and con.execute("SELECT 1 FROM display_outputs WHERE id=?", (o["id"],)).fetchone():
            con.execute("UPDATE display_outputs SET display_name=?, width_px=?, height_px=?, parallel_chain=?, x_offset=?, enabled=?, brightness=?, transport=?, receiver_addr=?, program_display=? WHERE id=?",
                        (o.get("display_name"), o.get("width_px"), o.get("height_px"), o.get("parallel_chain", 1),
                         o.get("x_offset", 0), o.get("enabled", 1), o.get("brightness", 70),
                         o.get("transport", "local"), o.get("receiver_addr"), o.get("program_display", ""), o["id"]))
        else:
            con.execute("INSERT INTO display_outputs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), cid, o.get("display_name"), o.get("width_px"), o.get("height_px"),
                         o.get("parallel_chain", 1), o.get("x_offset", 0), o.get("enabled", 1),
                         o.get("brightness", 70), o.get("transport", "local"), o.get("receiver_addr"),
                         o.get("program_display", "")))
    con.commit()
    return {"ok": True}


@app.delete("/api/controllers/{cid}")
def unpair(cid: str, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    con.execute("UPDATE controllers SET device_token_hash=NULL, pending_token=NULL, pairing_code=NULL, pairing_expires_at=NULL, status='unpaired' WHERE id=?", (cid,))
    con.execute("INSERT INTO audit_log (organisation_id, user_id, action, detail, at) VALUES (?,?,?,?,?)",
                (c["organisation_id"], u["id"], "controller.unpair", cid, _utcnow()))
    con.commit()
    return {"ok": True}


@app.post("/api/controllers/{cid}/token")
def rotate_device_token(cid: str, u: dict = Depends(current_user)):
    """Issue a fresh device token for a paired controller (old one stops working).

    The token is returned once and never stored — the UI must show it immediately.
    """
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c or not c["organisation_id"]:
        raise HTTPException(404, "no paired controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    con.execute("UPDATE controllers SET device_token_hash=?, pending_token=NULL, status='paired' WHERE id=?", (th, cid))
    con.execute("INSERT INTO audit_log (organisation_id, user_id, action, detail, at) VALUES (?,?,?,?,?)",
                (c["organisation_id"], u["id"], "controller.token.rotate", cid, _utcnow()))
    con.commit()
    return {"controller_id": cid, "device_token": token}


# ---------- programs: duplicate / delete / templates / presets / import ----------
@app.post("/api/programs/{pid}/duplicate")
def duplicate_program(pid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    import copy
    con = db()
    src = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    nid = str(uuid.uuid4())
    now = _utcnow()
    con.execute("INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, org, src["name"] + " (copy)", src["px_width"], src["default_display"], 0, 0, now, now, u["id"]))
    idmap = {}
    def _cp(table, cols, where, new_prog=True):
        for r in con.execute(f"SELECT * FROM {table} WHERE {where[0]}=?", (where[1],)):
            d = dict(r)
            old = d["id"]
            d["id"] = str(uuid.uuid4())
            idmap[old] = d["id"]
            if new_prog:
                d["program_id"] = nid
            for k in ("service_id", "destination_id", "display_id"):
                if k in d and d[k] in idmap:
                    d[k] = idmap[d[k]]
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?'*len(d))})", list(d.values()))
    # order matters: displays, services, destinations, stops, layouts, templates, presets
    for r in con.execute("SELECT * FROM program_displays WHERE program_id=?", (pid,)):
        d = dict(r); o = d["id"]; d["id"] = str(uuid.uuid4()); idmap[o] = d["id"]; d["program_id"] = nid
        con.execute("INSERT INTO program_displays VALUES (?,?,?,?,?,?,?)", list(d.values()))
    for r in con.execute("SELECT * FROM p_services WHERE program_id=?", (pid,)):
        d = dict(r); o = d["id"]; d["id"] = str(uuid.uuid4()); idmap[o] = d["id"]; d["program_id"] = nid
        con.execute("INSERT INTO p_services VALUES (?,?,?,?,?,?,?,?,?,?)", list(d.values()))
    for r in con.execute("SELECT * FROM p_destinations WHERE program_id=?", (pid,)):
        d = dict(r); o = d["id"]; d["id"] = str(uuid.uuid4()); idmap[o] = d["id"]; d["program_id"] = nid
        d["service_id"] = idmap.get(d["service_id"], d["service_id"])
        con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)", list(d.values()))
    for table, n in (("p_stops", 10), ("p_layouts", 6), ("p_templates", 6), ("p_presets", 5)):
        for r in con.execute(f"SELECT * FROM {table} WHERE program_id=?", (pid,)):
            d = dict(r); o = d["id"]; d["id"] = str(uuid.uuid4()); d["program_id"] = nid
            for k in ("service_id", "destination_id", "display_id"):
                if k in d and d[k] in idmap:
                    d[k] = idmap[d[k]]
            con.execute(f"INSERT INTO {table} VALUES ({','.join('?'*n)})", list(d.values()))
    con.commit()
    return {"program_id": nid}


@app.delete("/api/programs/{pid}")
def delete_program(pid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner")
    con = db()
    for t in ("p_stops", "p_layouts", "p_templates", "p_presets", "p_destinations", "p_services",
              "program_displays", "program_versions", "controller_programs", "programs"):
        col = "id" if t == "programs" else "program_id"
        try:
            con.execute(f"DELETE FROM {t} WHERE {col}=?", (pid,))
        except Exception:
            pass
    con.commit()
    import shutil as _shutil
    try:
        _shutil.rmtree(os.path.join(STORAGE_DIR, "programs", pid), ignore_errors=True)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/audio")
def list_audio(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    rows = db().execute("SELECT sha256, filename, duration_ms, source, tts_params FROM audio_files WHERE organisation_id=? ORDER BY filename",
                        (organisation_id,)).fetchall()
    clips = []
    for r in rows:
        d = dict(r)
        d["name"] = d.get("filename", "")
        raw_params = d.pop("tts_params", None)
        try:
            params = json.loads(raw_params or "{}")
        except Exception:
            params = {}
        d["placeholder"] = bool(params.get("placeholder"))
        # Old stub wrote 1s of zeros with params like {"speed": 1.0} and no
        # placeholder marker — flag so the UI can say "silent, regenerate".
        d["silent_legacy"] = (d.get("source") == "tts" and "placeholder" not in params
                              and (d.get("duration_ms") or 0) == 1000)
        clips.append(d)
    return {"clips": clips}


@app.get("/api/audio/{sha}/file")
def preview_audio(sha: str, organisation_id: str, u: dict = Depends(current_user)):
    """Stream a clip for the portal preview player (user auth, not device token)."""
    import subprocess as _sp
    import tempfile as _tf
    row = db().execute("SELECT * FROM audio_files WHERE sha256=? AND organisation_id=?",
                       (sha, organisation_id)).fetchone()
    if not row:
        raise HTTPException(404, "no clip")
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    path = _resolve_clip_path(row)
    if not path:
        raise HTTPException(404, "file missing — re-upload or regenerate this clip")
    with open(path, "rb") as f:
        magic = f.read(12)
    # Canonical WAV (and legacy WAV-data-with-.flac-name) -> audio/wav.
    if magic[:4] == b"RIFF":
        return FileResponse(path, media_type="audio/wav", filename=f"{row['filename']}.wav")
    if magic[:4] == b"fLaC":
        return FileResponse(path, media_type="audio/flac", filename=f"{row['filename']}.flac")
    # Legacy raw uploads stored under a .flac name (e.g. MP3 bytes) — transcode
    # on the fly so old clips play instead of staying silent.
    import shutil as _sh
    from fastapi.responses import Response as _Response
    ff = _sh.which("ffmpeg")
    if not ff:
        raise HTTPException(404, "stored file is not directly playable and ffmpeg is missing — re-upload as WAV")
    with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        tmp = t.name
    try:
        p = _sp.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", str(_CANON_RATE),
                     "-f", "wav", "-y", tmp], capture_output=True)
        if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 100:
            raise HTTPException(404, "stored file is corrupt — re-upload or regenerate this clip")
        with open(tmp, "rb") as f:
            data = f.read()
        return _Response(content=data, media_type="audio/wav")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "stored file is corrupt — re-upload or regenerate this clip")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@app.patch("/api/audio/{sha}")
def rename_audio(sha: str, body: dict, u: dict = Depends(current_user)):
    org = body.get("organisation_id", "")
    row = db().execute("SELECT * FROM audio_files WHERE sha256=? AND organisation_id=?",
                       (sha, org)).fetchone()
    if not row:
        raise HTTPException(404, "no clip")
    require_role(u["id"], org, "owner", "editor")
    want = _slugify_clip(body.get("name") or "")
    if not want or len(want) < 2:
        raise HTTPException(400, "use lowercase letters, numbers and dashes, e.g. derby-bus-station")
    con = db()
    friendly = _unique_clip_name(con, org, want, sha)
    con.execute("UPDATE audio_files SET filename=? WHERE sha256=? AND organisation_id=?",
                (friendly, sha, org))
    con.commit()
    return {"sha256": sha, "name": friendly, "filename": friendly}


@app.delete("/api/audio/{sha}")
def delete_audio(sha: str, organisation_id: str, u: dict = Depends(current_user)):
    row = db().execute("SELECT * FROM audio_files WHERE sha256=? AND organisation_id=?",
                       (sha, organisation_id)).fetchone()
    if not row:
        raise HTTPException(404, "no clip")
    require_role(u["id"], organisation_id, "owner", "editor")
    con = db()
    used = []
    if con.execute("SELECT 1 FROM p_stops WHERE audio=? AND organisation_id=? LIMIT 1",
                   (sha, organisation_id)).fetchone():
        used.append("stops")
    for col in ("next_stop_audio", "approaching_audio", "last_stop_audio"):
        if con.execute(f"SELECT 1 FROM p_services WHERE {col}=? AND organisation_id=? LIMIT 1",
                       (sha, organisation_id)).fetchone():
            used.append(col)
            break
    if used:
        raise HTTPException(400, f"clip is used by {', '.join(used)} — change those first")
    con.execute("DELETE FROM audio_files WHERE sha256=? AND organisation_id=?", (sha, organisation_id))
    con.commit()
    # remove the stored file only when no org references it any more
    if not con.execute("SELECT 1 FROM audio_files WHERE sha256=? LIMIT 1", (sha,)).fetchone():
        for cand in {os.path.join(STORAGE_DIR, str(row["storage_key"])),
                     os.path.join(STORAGE_DIR, str(row["storage_key"])[:-5] + ".wav")
                     if str(row["storage_key"]).endswith(".flac") else ""}:
            if cand:
                try:
                    os.remove(cand)
                except OSError:
                    pass
    return {"ok": True}


@app.put("/api/services/{sid}")
def update_service(sid: str, body: dict, u: dict = Depends(current_user)):
    """Rename a service and pick its announcement clips from the library."""
    con = db()
    s = con.execute("SELECT * FROM p_services WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "no service")
    require_role(u["id"], s["organisation_id"], "owner", "editor")
    org = s["organisation_id"]
    # validate clip hashes belong to this org (or are cleared)
    for key in ("next_stop_audio", "approaching_audio", "last_stop_audio"):
        v = body.get(key, s[key])
        if v in (None, ""):
            continue
        if not con.execute("SELECT 1 FROM audio_files WHERE sha256=? AND organisation_id=?",
                           (v, org)).fetchone():
            raise HTTPException(400, f"unknown clip for {key}: pick one from your audio library")
    con.execute("UPDATE p_services SET number=?, name=?, next_stop_audio=?, approaching_audio=?, "
                "last_stop_audio=?, terminus_clip_mode=? WHERE id=?",
                (body.get("number", s["number"]), body.get("name", s["name"]),
                 body.get("next_stop_audio", s["next_stop_audio"]),
                 body.get("approaching_audio", s["approaching_audio"]),
                 body.get("last_stop_audio", s["last_stop_audio"]),
                 body.get("terminus_clip_mode", s["terminus_clip_mode"] or "both"), sid))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?",
                (_utcnow(), u["id"], s["program_id"]))
    con.commit()
    return {"ok": True}


@app.post("/api/programs/import")
def import_pack_route(u: dict = Depends(current_user), file: UploadFile = File(...),
                      organisation_id: str = Form(...)):
    require_role(u["id"], organisation_id, "owner", "editor")
    import tempfile, zipfile as _z
    from nslprog import check_file as _cf
    tmp = tempfile.mkdtemp()
    zp = os.path.join(tmp, "up.nslpack")
    open(zp, "wb").write(file.file.read())
    with _z.ZipFile(zp) as z:
        for _m in z.namelist():
            _dest = os.path.realpath(os.path.join(tmp, _m))
            if not _dest.startswith(os.path.realpath(tmp) + os.sep) and _dest != os.path.realpath(tmp):
                raise HTTPException(400, "bad archive: path escapes package")
        z.extractall(tmp)
    res = _cf(os.path.join(tmp, "program.nslprog"))
    if not res.ok:
        raise HTTPException(400, "; ".join(e.message for e in res.errors))
    # creates a new draft program from pack meta (rows re-read)
    import sqlite3 as _s
    src = _s.connect(f"file:{os.path.join(tmp,'program.nslprog')}?mode=ro", uri=True)
    src.row_factory = _s.Row
    meta = {r[0]: r[1] for r in src.execute("SELECT key, value FROM meta")}
    con = db()
    nid = str(uuid.uuid4())
    now = _utcnow()
    con.execute("INSERT INTO programs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (nid, organisation_id, meta.get("program_name", "imported"),
                 int(meta.get("px_width", 240)), "", 0, 0, now, now, u["id"]))
    idmap = {}
    for t, cols in (("displays", None), ("services", None), ("destinations", None), ("stops", None),
                    ("layouts", None), ("layout_templates", None), ("override_presets", None)):
        for r in src.execute(f"SELECT * FROM {t}"):
            d = dict(r)
            o = d.get("id")
            if o:
                d2 = str(uuid.uuid4()); idmap[o] = d2; d["id"] = d2
            for k in ("service_id", "destination_id", "display_id"):
                if k in d and d[k] in idmap:
                    d[k] = idmap[d[k]]
            dst = {"displays": "program_displays", "services": "p_services", "destinations": "p_destinations",
                   "stops": "p_stops", "layouts": "p_layouts", "layout_templates": "p_templates",
                   "override_presets": "p_presets"}[t]
            if t == "displays":
                con.execute("INSERT INTO program_displays VALUES (?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["name"], d["width_px"], d["height_px"], d["position"]))
            elif t == "services":
                con.execute("INSERT INTO p_services VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["number"], d["name"], d["position"],
                             d["next_stop_audio"], d["approaching_audio"], d["last_stop_audio"], d["terminus_clip_mode"]))
            elif t == "destinations":
                con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["service_id"], d["code"], d["name"], d["position"],
                             d["num"], d["via"], d["dest"], d["extra_fields"]))
            elif t == "stops":
                con.execute("INSERT INTO p_stops VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["destination_id"], d["position"], d["name"],
                             d["audio"], d["board_text"], d["board_override"], d["is_terminus"]))
            elif t == "layouts":
                con.execute("INSERT INTO p_layouts VALUES (?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["destination_id"], d["display_id"], d["layout"]))
            elif t == "layout_templates":
                con.execute("INSERT INTO p_templates VALUES (?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["display_id"], d["name"], d["layout"], d["is_default"]))
            else:
                con.execute("INSERT INTO p_presets VALUES (?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["label"], d["text"], d["position"]))
    con.commit()
    src.close()
    return {"program_id": nid}
