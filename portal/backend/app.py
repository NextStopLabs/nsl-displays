"""Portal backend - FastAPI under base path /portal (plan.md 8).

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
import re
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
# the repo root, Docker) only have the root - add both, idempotently.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_REPO_ROOT, os.path.join(_REPO_ROOT, "pi")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
del _p

APP_VERSION = "1.0.0"
JWT_SECRET = os.environ.get("PORTAL_SECRET", "dev-secret-change-me")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")
BASE_PATH = "/portal"  # URL prefix when served behind the proxy; pass --root-path /portal to the server

# Release signing: private key in PEM or raw 64-byte hex. If set, uploaded
# bundles are signed automatically; otherwise the uploader must supply a
# signature hex string.
RELEASE_PRIVATE_KEY = os.environ.get("NSL_RELEASE_PRIVATE_KEY", "")
RELEASE_PUBLIC_KEY = os.environ.get("NSL_RELEASE_PUBLIC_KEY", "")


def _load_signing_key():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key, Encoding, PublicFormat)
    except Exception:
        return None
    if not RELEASE_PRIVATE_KEY:
        return None
    raw = RELEASE_PRIVATE_KEY.strip()
    try:
        if raw.startswith("-"):
            key = load_pem_private_key(raw.encode(), password=None)
        else:
            key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw))
        if not isinstance(key, Ed25519PrivateKey):
            return None
        return key
    except Exception:
        return None


def _signing_public_key_pem() -> str | None:
    key = _load_signing_key()
    if not key:
        return None
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def _sign_bundle(path: str) -> str:
    key = _load_signing_key()
    if not key:
        raise RuntimeError("NSL_RELEASE_PRIVATE_KEY not configured")
    with open(path, "rb") as f:
        sig = key.sign(f.read())
    return sig.hex()


def _require_admin(u: dict):
    # In this app the only global admin distinction is the first user of an org;
    # release management is restricted to org owners. Each org manages its own
    # release channel independently.
    pass

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
        # handshake state: portal paired it, but the Pi hasn't collected its
        # one-time token yet (pending_token clears on first successful claim).
        c["awaiting_pi"] = bool(c.get("device_token_hash") and c.get("pending_token"))
        try:
            c["sync_state"] = json.loads(c.get("sync_state") or "null")
        except Exception:
            c["sync_state"] = None
        try:
            c["live_state"] = json.loads(c.get("live_state") or "null")
        except Exception:
            c["live_state"] = None
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
    p["templates"] = [dict(r) for r in con.execute("SELECT * FROM p_templates WHERE program_id=? ORDER BY name", (pid,))]
    for t in p["templates"]:
        try:
            t["layout"] = json.loads(t["layout"])
        except Exception:
            t["layout"] = {}
    p["services"] = [dict(r) for r in con.execute("SELECT * FROM p_services WHERE program_id=? ORDER BY position", (pid,))]
    for s in p["services"]:
        s["destinations"] = [dict(r) for r in con.execute(
            "SELECT * FROM p_destinations WHERE service_id=? ORDER BY position", (s["id"],))]
        for d in s["destinations"]:
            d["stops"] = [dict(r) for r in con.execute(
                "SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (d["id"],))]
            d["layouts"] = [dict(r) for r in con.execute(
                "SELECT * FROM p_layouts WHERE destination_id=?", (d["id"],))]
            try:
                d["pages"] = [dict(r) for r in con.execute(
                    "SELECT * FROM p_destination_pages WHERE destination_id=? ORDER BY position", (d["id"],))]
            except Exception:
                d["pages"] = []
    p["presets"] = [dict(r) for r in con.execute("SELECT * FROM p_presets WHERE program_id=? ORDER BY position", (pid,))]
    try:
        p["gpio_rules"] = [dict(r) for r in con.execute(
            "SELECT * FROM p_gpio_rules WHERE program_id=? ORDER BY position", (pid,))]
    except Exception:
        p["gpio_rules"] = []
    p["versions"] = [dict(r) for r in con.execute("SELECT * FROM program_versions WHERE program_id=? ORDER BY version", (pid,))]
    return p


@app.post("/api/programs/{pid}/displays")
def add_program_display(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "display name is required")
    con = db()
    if con.execute("SELECT 1 FROM program_displays WHERE program_id=? AND name=?", (pid, name)).fetchone():
        raise HTTPException(400, "a display with that name already exists")
    pos = con.execute("SELECT COUNT(*) FROM program_displays WHERE program_id=?", (pid,)).fetchone()[0]
    did = str(uuid.uuid4())
    con.execute("INSERT INTO program_displays VALUES (?,?,?,?,?,?,?)",
                (did, org, pid, name, int(body.get("width_px", 240)), int(body.get("height_px", 40)), pos))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"display_id": did}


@app.put("/api/programs/{pid}/displays/{did}")
def update_program_display(pid: str, did: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    d = con.execute("SELECT * FROM program_displays WHERE id=? AND program_id=?", (did, pid)).fetchone()
    if not d:
        raise HTTPException(404, "no display")
    name = (body.get("name") or d["name"]).strip()
    if name != d["name"] and con.execute("SELECT 1 FROM program_displays WHERE program_id=? AND name=? AND id<>?", (pid, name, did)).fetchone():
        raise HTTPException(400, "a display with that name already exists")

    con.execute("UPDATE program_displays SET name=?, width_px=?, height_px=? WHERE id=?",
                (name, int(body.get("width_px", d["width_px"])), int(body.get("height_px", d["height_px"])), did))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


@app.delete("/api/programs/{pid}/displays/{did}")
def delete_program_display(pid: str, did: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    d = con.execute("SELECT * FROM program_displays WHERE id=? AND program_id=?", (did, pid)).fetchone()
    if not d:
        raise HTTPException(404, "no display")
    con.execute("DELETE FROM p_layouts WHERE program_id=? AND display_id=?", (pid, did))
    con.execute("DELETE FROM program_displays WHERE id=?", (did,))
    # if this was the default display, clear default or pick first remaining
    prog = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    if prog and prog["default_display"] == did:
        first = con.execute("SELECT id FROM program_displays WHERE program_id=? ORDER BY position LIMIT 1", (pid,)).fetchone()
        con.execute("UPDATE programs SET default_display=? WHERE id=?", ((first or {})["id"] or "", pid))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


# ---------- program layout templates (preset/default layouts per display) ----------
@app.get("/api/programs/{pid}/templates")
def list_program_templates(pid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT t.*, d.name AS display_name FROM p_templates t "
        "JOIN program_displays d ON t.display_id=d.id "
        "WHERE t.program_id=? ORDER BY d.position, t.name", (pid,))]
    for r in rows:
        try:
            r["layout"] = json.loads(r["layout"])
        except Exception:
            r["layout"] = {}
    return {"templates": rows}


@app.post("/api/programs/{pid}/templates")
def add_program_template(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    did = body.get("display_id", "")
    name = (body.get("name") or "").strip()
    if not did or not name:
        raise HTTPException(400, "display_id and name are required")
    con = db()
    if not con.execute("SELECT 1 FROM program_displays WHERE id=? AND program_id=?", (did, pid)).fetchone():
        raise HTTPException(404, "no display")
    if con.execute("SELECT 1 FROM p_templates WHERE program_id=? AND display_id=? AND name=?", (pid, did, name)).fetchone():
        raise HTTPException(400, "a template with that name already exists for this display")
    layout = body.get("layout") or {
        "schema": 1, "width": int(body.get("width_px", 240)), "height": int(body.get("height_px", 40)),
        "background": "#000000", "page_seconds": 3,
        "override": {"mode": "replace", "replaces": "dest", "hide": []}, "elements": []}
    tid = str(uuid.uuid4())
    is_default = 1 if body.get("is_default") else 0
    if is_default:
        con.execute("UPDATE p_templates SET is_default=0 WHERE program_id=? AND display_id=?", (pid, did))
    con.execute("INSERT INTO p_templates VALUES (?,?,?,?,?,?,?)",
                (tid, org, pid, did, name, json.dumps(layout, separators=(",", ":")), is_default))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"template_id": tid}


@app.put("/api/programs/{pid}/templates/{tid}")
def update_program_template(pid: str, tid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    t = con.execute("SELECT * FROM p_templates WHERE id=? AND program_id=?", (tid, pid)).fetchone()
    if not t:
        raise HTTPException(404, "no template")
    name = (body.get("name") or t["name"]).strip()
    if name != t["name"] and con.execute(
            "SELECT 1 FROM p_templates WHERE program_id=? AND display_id=? AND name=? AND id<>?",
            (pid, t["display_id"], name, tid)).fetchone():
        raise HTTPException(400, "a template with that name already exists for this display")
    layout = body.get("layout")
    is_default = body.get("is_default")
    if is_default:
        con.execute("UPDATE p_templates SET is_default=0 WHERE program_id=? AND display_id=?", (pid, t["display_id"]))
    updates = []
    vals = []
    if name:
        updates.append("name=?"); vals.append(name)
    if layout is not None:
        updates.append("layout=?"); vals.append(json.dumps(layout, separators=(",", ":")))
    if is_default is not None:
        updates.append("is_default=?"); vals.append(1 if is_default else 0)
    if updates:
        vals.append(tid)
        con.execute(f"UPDATE p_templates SET {', '.join(updates)} WHERE id=?", tuple(vals))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


@app.delete("/api/programs/{pid}/templates/{tid}")
def delete_program_template(pid: str, tid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    t = con.execute("SELECT * FROM p_templates WHERE id=? AND program_id=?", (tid, pid)).fetchone()
    if not t:
        raise HTTPException(404, "no template")
    con.execute("DELETE FROM p_templates WHERE id=?", (tid,))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


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


def _check_code_shape(code: str) -> bool:
    return 1 <= len(code) <= 8 and code.isdigit()


def _insert_destination_page(con, org: str, pid: str, did: str, position: int,
                             via: str = "", dest: str = "") -> str:
    """Insert one board page row; table may be missing on old DBs (created lazily)."""
    try:
        con.execute("SELECT 1 FROM p_destination_pages LIMIT 1")
    except Exception:
        con.execute(
            "CREATE TABLE IF NOT EXISTS p_destination_pages ("
            "id TEXT PRIMARY KEY, organisation_id TEXT NOT NULL, program_id TEXT NOT NULL,"
            "destination_id TEXT NOT NULL, position INTEGER NOT NULL, via TEXT DEFAULT '',"
            "dest TEXT DEFAULT '', UNIQUE (destination_id, position))")
    pgid = str(uuid.uuid4())
    con.execute("INSERT INTO p_destination_pages VALUES (?,?,?,?,?,?,?)",
                (pgid, org, pid, did, position, via or "", dest or ""))
    return pgid


def _destination_pages(con, did: str) -> list[dict]:
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM p_destination_pages WHERE destination_id=? ORDER BY position", (did,))]
    except Exception:
        return []


def _sync_base_from_pages(con, did: str):
    """Keep base via/dest columns equal to page 1 so older readers/layouts still work."""
    try:
        row = con.execute(
            "SELECT via, dest FROM p_destination_pages WHERE destination_id=? ORDER BY position LIMIT 1",
            (did,)).fetchone()
    except Exception:
        return
    if row:
        con.execute("UPDATE p_destinations SET via=?, dest=? WHERE id=?", (row["via"], row["dest"], did))


@app.post("/api/services/{sid}/destinations")
def add_destination(sid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    s = con.execute("SELECT * FROM p_services WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "no service")
    require_role(u["id"], s["organisation_id"], "owner", "editor")
    code = (body.get("code") or "").strip()
    if not _check_code_shape(code):
        raise HTTPException(400, "code must be 1-8 digits 0-9")
    if con.execute("SELECT 1 FROM p_destinations WHERE program_id=? AND code=?", (s["program_id"], code)).fetchone():
        raise HTTPException(400, "duplicate code in program")
    did = str(uuid.uuid4())
    pos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=?", (sid,)).fetchone()[0]
    via, dest_text = body.get("via", ""), body.get("dest", "")
    # Optional extra pages: [{via, dest}, ...] — page 1 is via/dest above.
    extra_pages = body.get("pages") or []
    con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (did, s["organisation_id"], s["program_id"], sid, code, body.get("name", code), pos,
                 body.get("num"), via, dest_text,
                 json.dumps(body.get("extra", {}), separators=(",", ":"))))
    _insert_destination_page(con, s["organisation_id"], s["program_id"], did, 1, via, dest_text)
    for i, pg in enumerate(extra_pages, 2):
        _insert_destination_page(con, s["organisation_id"], s["program_id"], did, i,
                                 (pg or {}).get("via", ""), (pg or {}).get("dest", ""))
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
    if not _check_code_shape(code):
        raise HTTPException(400, "code must be 1-8 digits 0-9")
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
    # Keep page 1 in step when the base board text is edited directly.
    try:
        first = con.execute(
            "SELECT id FROM p_destination_pages WHERE destination_id=? ORDER BY position LIMIT 1",
            (did,)).fetchone()
        if first and ("via" in body or "dest" in body):
            con.execute("UPDATE p_destination_pages SET via=?, dest=? WHERE id=?",
                        (body.get("via", d["via"]), body.get("dest", d["dest"]), first["id"]))
    except Exception:
        pass
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"ok": True}


@app.post("/api/destinations/{did}/pages")
def add_destination_page(did: str, body: dict, u: dict = Depends(current_user)):
    """Append a board page (custom via/dest) to a destination. Position = N+1."""
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    pages = _destination_pages(con, did)
    if not pages:
        # Backfill page 1 from the base columns so older destinations join the model.
        _insert_destination_page(con, d["organisation_id"], d["program_id"], did, 1,
                                 d["via"] or "", d["dest"] or "")
        pages = _destination_pages(con, did)
    pgid = _insert_destination_page(con, d["organisation_id"], d["program_id"], did, len(pages) + 1,
                                    body.get("via", ""), body.get("dest", ""))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"page_id": pgid, "position": len(pages) + 1}


@app.put("/api/destination-pages/{pgid}")
def update_destination_page(pgid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    pg = con.execute("SELECT * FROM p_destination_pages WHERE id=?", (pgid,)).fetchone()
    if not pg:
        raise HTTPException(404, "no page")
    require_role(u["id"], pg["organisation_id"], "owner", "editor")
    via = body.get("via", pg["via"])
    dest_text = body.get("dest", pg["dest"])
    if len(via or "") > 200 or len(dest_text or "") > 200:
        raise HTTPException(400, "via/dest must be 200 characters or fewer")
    con.execute("UPDATE p_destination_pages SET via=?, dest=? WHERE id=?", (via or "", dest_text or "", pgid))
    if pg["position"] == 1:
        con.execute("UPDATE p_destinations SET via=?, dest=? WHERE id=?",
                    (via or "", dest_text or "", pg["destination_id"]))
    d = con.execute("SELECT program_id FROM p_destinations WHERE id=?", (pg["destination_id"],)).fetchone()
    if d:
        con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"ok": True}


@app.delete("/api/destination-pages/{pgid}")
def delete_destination_page(pgid: str, u: dict = Depends(current_user)):
    con = db()
    pg = con.execute("SELECT * FROM p_destination_pages WHERE id=?", (pgid,)).fetchone()
    if not pg:
        raise HTTPException(404, "no page")
    require_role(u["id"], pg["organisation_id"], "owner", "editor")
    remaining = con.execute("SELECT COUNT(*) FROM p_destination_pages WHERE destination_id=?",
                            (pg["destination_id"],)).fetchone()[0]
    if remaining <= 1:
        raise HTTPException(400, "a destination needs at least one page - edit it instead of deleting")
    con.execute("DELETE FROM p_destination_pages WHERE id=?", (pgid,))
    # Renumber higher pages down so positions stay 1..N.
    rows = con.execute("SELECT id FROM p_destination_pages WHERE destination_id=? ORDER BY position",
                       (pg["destination_id"],)).fetchall()
    for i, r in enumerate(rows, 1):
        con.execute("UPDATE p_destination_pages SET position=? WHERE id=?", (i, r["id"]))
    _sync_base_from_pages(con, pg["destination_id"])
    d = con.execute("SELECT program_id FROM p_destinations WHERE id=?", (pg["destination_id"],)).fetchone()
    if d:
        con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"ok": True}


@app.post("/api/destinations/{did}/duplicate")
def duplicate_destination(did: str, body: dict, u: dict = Depends(current_user)):
    """Copy a destination with its stops, board pages and layouts.

    Body: {service_id?, code?} - target service defaults to the source
    service (must be in the same program); code defaults to a free
    suggestion. The copy keeps name/num/via/dest/extra, stops, pages
    and per-display layouts.
    """
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    d = dict(d)
    target_sid = (body.get("service_id") if isinstance(body, dict) else None) or d["service_id"]
    s = con.execute("SELECT * FROM p_services WHERE id=?", (target_sid,)).fetchone()
    if not s or s["program_id"] != d["program_id"]:
        raise HTTPException(400, "service must be in the same program")
    code = ((body.get("code") if isinstance(body, dict) else None) or "").strip() or suggest_code(d["program_id"])
    if not _check_code_shape(code):
        raise HTTPException(400, "code must be 1-8 digits 0-9")
    if con.execute("SELECT 1 FROM p_destinations WHERE program_id=? AND code=?", (d["program_id"], code)).fetchone():
        raise HTTPException(400, "duplicate code in program")
    nid = str(uuid.uuid4())
    pos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=?", (target_sid,)).fetchone()[0]
    con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (nid, d["organisation_id"], d["program_id"], target_sid, code, d["name"], pos,
                 d["num"], d["via"], d["dest"], d["extra_fields"] or "{}"))
    for pg in _destination_pages(con, did):
        _insert_destination_page(con, d["organisation_id"], d["program_id"], nid,
                                 pg["position"], pg["via"] or "", pg["dest"] or "")
    for st in con.execute("SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (did,)):
        st = dict(st)
        con.execute("INSERT INTO p_stops VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), st["organisation_id"], st["program_id"], nid,
                     st["position"], st["name"], st["audio"], st["board_text"],
                     st["board_override"], st["is_terminus"]))
    for lo in con.execute("SELECT * FROM p_layouts WHERE destination_id=?", (did,)):
        lo = dict(lo)
        con.execute("INSERT INTO p_layouts VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), lo["organisation_id"], lo["program_id"], nid,
                     lo["display_id"], lo["layout"]))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], d["program_id"]))
    con.commit()
    return {"destination_id": nid, "code": code}


@app.delete("/api/destinations/{did}")
def delete_destination(did: str, u: dict = Depends(current_user)):
    con = db()
    d = con.execute("SELECT * FROM p_destinations WHERE id=?", (did,)).fetchone()
    if not d:
        raise HTTPException(404, "no destination")
    require_role(u["id"], d["organisation_id"], "owner", "editor")
    con.execute("DELETE FROM p_stops WHERE destination_id=?", (did,))
    con.execute("DELETE FROM p_layouts WHERE destination_id=?", (did,))
    try:
        con.execute("DELETE FROM p_destination_pages WHERE destination_id=?", (did,))
    except Exception:
        pass
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
        try:
            con.execute("DELETE FROM p_destination_pages WHERE destination_id=?", (did,))
        except Exception:
            pass
    con.execute("DELETE FROM p_destinations WHERE service_id=?", (sid,))
    con.execute("DELETE FROM p_services WHERE id=?", (sid,))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], s["program_id"]))
    con.commit()
    return {"ok": True, "deleted_destinations": len(dids)}


@app.get("/api/programs/{pid}/destinations/check-code")
def check_code(pid: str, code: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    ok_shape = _check_code_shape(code)
    taken = db().execute("SELECT id FROM p_destinations WHERE program_id=? AND code=?", (pid, code)).fetchone()
    return {"code": code, "shape_ok": ok_shape, "unique": taken is None,
            "suggestion": None if (ok_shape and taken is None) else suggest_code(pid)}


def suggest_code(pid: str, _used_extra: set[str] | None = None) -> str:
    con = db()
    used = {r[0] for r in con.execute("SELECT code FROM p_destinations WHERE program_id=?", (pid,))}
    if _used_extra:
        used |= set(_used_extra)
    svc = con.execute("SELECT number FROM p_services WHERE program_id=? ORDER BY position LIMIT 1", (pid,)).fetchone()
    digits = "".join(ch for ch in (svc[0] if svc else "") if ch.isdigit())[:4]
    prefix = (digits if len(digits) >= 2 else "10")[:4].ljust(2, "0")
    for i in range(1, 1000000):
        cand = (prefix + f"{i:06d}")[:8]
        if cand not in used and len(cand) == 8 and cand.isdigit():
            used.add(cand)
            return cand
    for i in range(1, 100000000):
        cand = f"{i:08d}"
        if cand not in used:
            return cand
    raise HTTPException(400, "program is full - no free destination codes")


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
    fields = {"num": d.get("num") or s.get("number") or "",
              "via": d.get("via") or "", "dest": d.get("dest") or ""}
    try:
        extra = json.loads(d.get("extra_fields") or "{}")
        for k, v in (extra.items() if isinstance(extra, dict) else []):
            fields[k] = v
    except Exception:
        pass
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
    count = len(stops)
    if count == 0:
        return {"destination": d, "service": s, "stop": None, "pos": 0, "count": 0,
                "stops": [], "fields": fields, "pages": _destination_pages(con, did),
                "layouts": layouts, "displays": displays,
                "next": [], "approaching": []}
    pos = max(1, min(pos, count))
    stop = stops[pos - 1]
    fields["__stop_name"] = stop.get("name") or ""
    fields["__board_text"] = stop.get("board_text") or stop.get("name") or ""
    board_override = stop.get("board_override") or ""
    clips = {r["sha256"]: dict(r) for r in con.execute(
        "SELECT sha256, filename, duration_ms FROM audio_files WHERE organisation_id=?",
        (d["organisation_id"],))}
    def _meta(h):
        c = clips.get(h or "")
        return {"sha": h, "label": (c["filename"] if c else h or "-"),
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
            "fields": fields, "pages": _destination_pages(con, did),
            "board_override": board_override,
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
    try:
        for (h,) in con.execute(
                "SELECT DISTINCT audio FROM p_gpio_rules WHERE program_id=? AND audio IS NOT NULL", (pid,)):
            if h:
                ref_hashes.add(h)
    except Exception:
        pass
    audio_needed: dict[str, dict] = {}
    if ref_hashes:
        for a in con.execute(
                f"SELECT * FROM audio_files WHERE organisation_id=? AND sha256 IN ({','.join('?'*len(ref_hashes))})",
                (p["organisation_id"], *ref_hashes)):
            audio_needed[a["sha256"]] = dict(a)
    missing = ref_hashes - set(audio_needed)
    if missing:
        raise ValueError(f"{len(missing)} referenced clip(s) missing from the audio library - re-upload them")
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
            try:
                page_rows = con.execute(
                    "SELECT * FROM p_destination_pages WHERE destination_id=? ORDER BY position", (d["id"],))
            except Exception:
                page_rows = []
            for pg in page_rows:
                b.add_destination_page(d["id"], pg["position"], pg["via"] or "", pg["dest"] or "",
                                       id=pg["id"])
            for st in con.execute("SELECT * FROM p_stops WHERE destination_id=? ORDER BY position", (d["id"],)):
                b.add_stop(d["id"], st["position"], st["name"], st["audio"], st["board_text"],
                           st["board_override"], bool(st["is_terminus"]), id=st["id"])
            for l in con.execute("SELECT * FROM p_layouts WHERE destination_id=?", (d["id"],)):
                b.add_layout(d["id"], l["display_id"], json.loads(l["layout"]), id=l["id"])
    for t in con.execute("SELECT * FROM p_templates WHERE program_id=?", (pid,)):
        b.add_template(t["display_id"], t["name"], json.loads(t["layout"]), bool(t["is_default"]), id=t["id"])
    for pr in con.execute("SELECT * FROM p_presets WHERE program_id=? ORDER BY position", (pid,)):
        b.add_preset(pr["label"], pr["text"], pr["position"], id=pr["id"])
    try:
        for gr in con.execute("SELECT * FROM p_gpio_rules WHERE program_id=? ORDER BY position", (pid,)):
            gd = dict(gr)
            b.add_gpio_rule(label=gd.get("label") or "", pin=int(gd["pin"]), trigger=gd["trigger"],
                            effect=gd["effect"], target=gd["target"], board_text=gd.get("board_text") or "",
                            board_source=gd.get("board_source") or "text",
                            destination_id=gd.get("destination_id"),
                            audio=gd.get("audio"), duration_s=int(gd.get("duration_s") or 0),
                            pull=gd.get("pull") or "up", active_low=bool(gd.get("active_low")),
                            debounce_ms=int(gd.get("debounce_ms") or 50),
                            position=int(gd.get("position") or 0), id=gd["id"])
    except Exception:
        pass
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
    This is the normal path - versions are an implementation detail."""
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


@app.post("/api/fonts")
def upload_font(file: UploadFile = File(...), u: dict = Depends(current_user)):
    """Save a Hanover .Fnt file so layouts can use it immediately.

    Validates with the same parser the Pi uses, then writes to the fonts
    directory and drops the cached copy. Owner/editor only.
    """
    import re as _re
    import tempfile as _tf
    # role from any org the user can edit
    rows = db().execute(
        "SELECT 1 FROM memberships WHERE user_id=? AND role IN ('owner','editor')", (u["id"],)).fetchone()
    if not rows:
        raise HTTPException(403, "editor role required")
    name = (file.filename or "").strip()
    if not name.lower().endswith(".fnt"):
        raise HTTPException(400, "only .Fnt files accepted")
    stem = os.path.splitext(os.path.basename(name))[0]
    if not _re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", stem):
        raise HTTPException(400, "bad file name (letters, digits, _ . - only)")
    raw = file.file.read()
    if len(raw) > 1_000_000:
        raise HTTPException(400, "file too large (max 1 MB)")
    from nsl_pi import hanover as _han
    with _tf.NamedTemporaryFile(suffix=".Fnt", delete=False) as t:
        t.write(raw)
        tmp = t.name
    try:
        parsed = _han.parse_font(tmp)
    except Exception as e:
        raise HTTPException(400, f"not a valid .Fnt file: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    d = _han.fonts_dir()
    os.makedirs(d, exist_ok=True)
    dest = os.path.join(d, stem + ".Fnt")
    overwritten = os.path.exists(dest)
    with open(dest, "wb") as f:
        f.write(raw)
    _han.invalidate(stem, d)
    return {"id": "han-" + stem, "height": parsed["height"], "maxwidth": parsed["maxwidth"],
            "overwritten": overwritten, "saved_to": dest}

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


def _resample_linear(samples: list[int], src_rate: int, dst_rate: int = _CANON_RATE) -> list[int]:
    """Pure-Python resample (linear interpolation) - no ffmpeg needed.

    Piper voices ship at 16/22.05/24 kHz and XTTS at 24 kHz; resampling the
    already-parsed samples also handles files whose headers ffmpeg chokes on.
    """
    if src_rate == dst_rate or not samples:
        return samples
    ratio = src_rate / dst_rate
    n_out = int(len(samples) / ratio)
    last = len(samples) - 1
    out = [0] * n_out
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        if j >= last:
            out[i] = samples[last]
            continue
        frac = pos - j
        out[i] = int(samples[j] + (samples[j + 1] - samples[j]) * frac)
    return out


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
            # bytes -> samples, downmix + resample handling (stdlib only, no ffmpeg)
            if sw == 1:  # 8-bit unsigned
                samples = [(b - 128) * 256 for b in frames]
            elif sw == 2:  # 16-bit signed
                samples = list(_st.unpack(f"<{len(frames)//2}h", frames))
            elif sw == 3:  # 24-bit signed (DAW exports)
                samples = [int.from_bytes(frames[i:i + 3], "little", signed=True) >> 8
                           for i in range(0, len(frames) - len(frames) % 3, 3)]
            elif sw == 4:  # 32-bit: float (-1..1) or int - sniff it
                n4 = len(frames) // 4
                as_float = list(_st.unpack(f"<{n4}f", frames[:n4 * 4]))
                if as_float and max(abs(v) for v in as_float) <= 4.0:
                    samples = [max(-32768, min(32767, int(v * 32768))) for v in as_float]
                else:
                    samples = [v >> 16 for v in _st.unpack(f"<{n4}i", frames[:n4 * 4])]
            else:
                raise ValueError(f"unsupported WAV depth {sw*8}-bit")
            if ch == 2:
                samples = [int((samples[i] + samples[i + 1]) / 2) for i in range(0, len(samples), 2)]
            elif ch > 2:
                raise HTTPException(400, f"that WAV has {ch} channels - export mono or stereo")
            if sr != _CANON_RATE:
                samples = _resample_linear(samples, sr, _CANON_RATE)
            pcm = _st.pack(f"<{len(samples)}h", *[max(-32768, min(32767, s)) for s in samples])
            peak = max((abs(s) for s in _st.unpack(f"<{len(pcm)//2}h", pcm)), default=0) if pcm else 0
            if peak < 100:
                raise HTTPException(400, "that file is digital silence - nothing would play")
            return pcm, int(len(pcm) / 2 / _CANON_RATE * 1000)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"could not read that WAV ({e})")
    # General path: needs ffmpeg (MP3/OGG/M4A/FLAC/etc).
    ff = _sh.which("ffmpeg")
    if not ff:
        raise HTTPException(400, "that format needs ffmpeg to decode - upload a 22050 Hz mono WAV instead")
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
        raise HTTPException(400, "that file decodes to silence - nothing would play")
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


def _resolve_piper_voice(voice_id: str | None,
                         organisation_id: str) -> tuple[str | None, str | None, str]:
    """Look up an uploaded voice once and return (model_path, config_path, error).

    Also heals the piper1-gpl layout issue (config must exist at
    ``model.onnx.json``). Empty error means usable.
    """
    if not voice_id:
        return None, None, "no voice selected - pick a voice in the Voice dropdown"
    con = db()
    v = con.execute("SELECT * FROM voices WHERE id=? AND (organisation_id=? OR organisation_id IS NULL)",
                    (voice_id, organisation_id)).fetchone()
    if not v:
        return None, None, "selected voice not found for this organisation - re-select it"
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
        return None, None, "voice files missing on server disk (STORAGE_DIR mismatch?) - re-upload the voice"
    return model, config, ""


def _piper_cli_wav(text: str, model: str, config: str, speaker_id: int | None = None) -> tuple[tuple[bytes, int] | None, str]:
    """Synthesise one text with the `piper` binary (no DB access; thread-safe)."""
    import shutil as _sh
    import subprocess as _sp
    import tempfile as _tf
    if not _sh.which("piper"):
        return None, "piper binary not found on server PATH — install piper-tts (pip install piper-tts) + espeak-ng"
    with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        tmp = t.name
    try:
        args = ["piper", "--model", model, "--config", config]
        if speaker_id is not None:
            args += ["--speaker", str(speaker_id)]
        args += ["--output_file", tmp]
        p = _sp.run(args, input=text.encode(), capture_output=True, timeout=120)
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


def _check_speaker(con, voice_id: str | None, organisation_id: str, speaker_id) -> int | None:
    """Normalise an optional speaker choice against the voice's speaker map.

    Returns the int id (or None for the default speaker). Raises 400 when the
    choice doesn't exist on that voice.
    """
    if speaker_id in (None, ""):
        return None
    try:
        sid = int(speaker_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "unknown speaker — pick one from the voice's speaker list")
    if not voice_id:
        raise HTTPException(400, "pick a voice to use a speaker")
    row = con.execute("SELECT speakers FROM voices WHERE id=? AND (organisation_id=? OR organisation_id IS NULL)",
                      (voice_id, organisation_id)).fetchone()
    ids: list[int] = []
    if row and row["speakers"]:
        try:
            ids = [s.get("id") for s in json.loads(row["speakers"]) if isinstance(s, dict)]
        except Exception:
            ids = []
    if ids:
        if sid not in ids:
            raise HTTPException(400, "unknown speaker — pick one from the voice's speaker list")
    elif sid != 0:
        raise HTTPException(400, "that voice has a single speaker")
    return sid


def _synth_many_piper(texts: list[str], voice_id: str | None,
                      organisation_id: str, speaker_id: int | None = None) -> dict[str, tuple[tuple[bytes, int] | None, str]]:
    """Synthesise many short texts with a single voice model load.

    The old per-stop path spawned one `piper` process per stop, reloading the
    ONNX model every time - minutes of silence for a full service. This loads
    the model once: in-process via the `piper` Python package when available,
    else one CLI process per text run across a small thread pool.
    Returns {text: ((wav, duration_ms) | None, error)}.
    """
    out: dict[str, tuple[tuple[bytes, int] | None, str]] = {}
    want = [t for t in dict.fromkeys(texts) if (t or "").strip()]
    if not want:
        return out
    model, config, err = _resolve_piper_voice(voice_id, organisation_id)
    if err:
        return {t: (None, err) for t in want}
    # Fast path: in-process synthesis, model loaded exactly once.
    try:
        from piper import PiperVoice as _PiperVoice
    except ImportError:
        _PiperVoice = None
    if _PiperVoice is not None:
        try:
            try:
                _voice = _PiperVoice.load(model, config)
            except TypeError:
                _voice = _PiperVoice.load(model)
            _syn_cfg = None
            if speaker_id is not None:
                try:
                    from piper.config import SynthesisConfig as _SynCfg
                    _syn_cfg = _SynCfg(speaker_id=speaker_id)
                except Exception:
                    return {t: (None, "installed piper package is too old to pick a speaker") for t in want}
            for t in want:
                try:
                    pcm_parts, rate = bytearray(), None
                    try:
                        chunks = _voice.synthesize(t, syn_config=_syn_cfg)
                    except TypeError:
                        if speaker_id is not None:
                            out[t] = (None, "installed piper package is too old to pick a speaker")
                            continue
                        chunks = _voice.synthesize(t)
                    for chunk in chunks:
                        pcm_parts += getattr(chunk, "audio_int16_bytes", b"")
                        rate = rate or getattr(chunk, "sample_rate", None)
                    if not pcm_parts:
                        out[t] = (None, "piper returned no audio")
                        continue
                    wav = _wav_bytes_at_rate(bytes(pcm_parts), rate or _CANON_RATE)
                    pcm, _ = _decode_to_mono22050(wav, "piper.wav")
                    canon = _wav_bytes_from_pcm(pcm)
                    out[t] = ((canon, int(len(pcm) / 2 / _CANON_RATE * 1000)), "")
                except Exception as e:
                    out[t] = (None, f"piper error: {e}")
            return out
        except Exception:
            pass  # model wouldn't load in-process either; try the CLI below
    # Fallback: one CLI process per text, but in parallel (model load ~4x overlapped).
    import concurrent.futures as _fu
    workers = max(1, min(4, len(want)))
    with _fu.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_piper_cli_wav, t, model, config, speaker_id): t for t in want}
        for fut in _fu.as_completed(futs):
            try:
                out[futs[fut]] = fut.result()
            except Exception as e:
                out[futs[fut]] = (None, f"piper error: {e}")
    return out


def _wav_bytes_at_rate(pcm: bytes, rate: int) -> bytes:
    """Wrap raw mono s16 PCM in a WAV header at the given sample rate."""
    import io as _io
    import wave as _w
    buf = _io.BytesIO()
    with _w.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _try_piper_wav(text: str, voice_id: str | None, organisation_id: str,
                   speaker_id: int | None = None) -> tuple[tuple[bytes, int] | None, str]:
    """Run the `piper` binary with the uploaded ONNX voice (single text).

    Returns (result, reason). result is None when falling back to the
    preview tone; reason explains why (surfaced to the UI).
    """
    model, config, err = _resolve_piper_voice(voice_id, organisation_id)
    if err:
        return None, err
    return _piper_cli_wav(text, model, config, speaker_id)


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
    """Piper TTS. Fails loudly - no preview-tone fallback."""
    require_role(u["id"], body["organisation_id"], "owner", "editor")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "type the text to speak first")
    if not body.get("voice_id"):
        raise HTTPException(400, "select a voice — no preview tones, generation needs a real voice")
    speaker_id = _check_speaker(db(), body.get("voice_id"), body["organisation_id"], body.get("speaker_id"))
    want = _slugify_clip(body.get("name") or "")
    if not want:
        # suggest from the text: first few words -> derby-bus-station style
        want = _slugify_clip("-".join(text.split()[:5]))
    if not want or len(want) < 2:
        raise HTTPException(400, "give this clip a name, e.g. derby-bus-station")
    real, tts_error = _try_piper_wav(text, body.get("voice_id"), body["organisation_id"], speaker_id)
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
                 json.dumps({"speed": body.get("speed", 1.0), "placeholder": False,
                             "speaker_id": speaker_id}), _utcnow()))
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
    # Multi-speaker models carry {"speaker_id_map": {"Name": 0, ...}}.
    spk_map = cfg.get("speaker_id_map") or {}
    speakers: list[dict] = []
    if isinstance(spk_map, dict):
        try:
            speakers = sorted(({"id": int(v), "name": str(k)} for k, v in spk_map.items()),
                              key=lambda s: s["id"])
        except (TypeError, ValueError):
            speakers = []
    num_speakers = len(speakers) if speakers else 1
    con = db()
    con.execute("INSERT INTO voices (id, organisation_id, name, model_key, config_key, language,"
                " sample_rate, is_default, licence_note, uploaded_by, size_bytes, created_at,"
                " num_speakers, speakers) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, organisation_id, name, f"voices/{vid}/model.onnx",
                 f"voices/{vid}/model.onnx.json", lang, sr,
                 0, "", u["id"], len(mraw), _utcnow(), num_speakers, json.dumps(speakers)))
    con.commit()
    return {"voice_id": vid, "sample_rate": sr, "num_speakers": num_speakers, "speakers": speakers}


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
    if "state" in body and isinstance(body["state"], dict):
        keep = {k: body["state"].get(k) for k in
                ("program_id", "program_version", "service_id", "destination_id",
                 "stop_id", "override_text", "volume", "muted", "insert_destination_id")}
        con.execute("UPDATE controllers SET live_state=? WHERE id=?", (json.dumps(keep), c["id"]))
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
    row = db().execute(
        "SELECT * FROM releases WHERE organisation_id=? AND channel=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (c["organisation_id"], c["channel"] or "stable")).fetchone()
    if not row:
        return {"version": None}
    path = os.path.join(STORAGE_DIR, str(row["bundle_key"]))
    return {
        "version": row["version"],
        "bundle_url": f"/api/device/release/bundle?v={row['version']}",
        "bundle_key": row["bundle_key"],
        "signature": row["signature"],
        "sha256": row["bundle_sha256"] if "bundle_sha256" in row.keys() else "",
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "notes": row["notes"] if "notes" in row.keys() else "",
        "min_agent_version": "1.0.0",
    }


@app.get("/api/device/release/bundle")
def device_release_bundle(v: str, authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    row = db().execute(
        "SELECT * FROM releases WHERE organisation_id=? AND channel=? AND version=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (c["organisation_id"], c["channel"] or "stable", v)).fetchone()
    if not row:
        raise HTTPException(404, "no release")
    path = os.path.join(STORAGE_DIR, str(row["bundle_key"]))
    if not os.path.exists(path):
        raise HTTPException(404, "bundle missing")
    return FileResponse(path, filename=f"nsl-release-{v}.tar.gz",
                        media_type="application/gzip")


@app.get("/api/admin/releases")
def admin_list_releases(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    rows = db().execute(
        "SELECT id, version, channel, bundle_sha256, notes, created_at FROM releases "
        "WHERE organisation_id=? ORDER BY created_at DESC, rowid DESC",
        (organisation_id,)).fetchall()
    return {"releases": [dict(r) for r in rows]}


@app.post("/api/admin/releases")
def admin_upload_release(
    organisation_id: str = Form(...),
    version: str = Form(...),
    channel: str = Form(default="stable"),
    notes: str = Form(default=""),
    signature: str = Form(default=""),
    file: UploadFile = File(...),
    u: dict = Depends(current_user)
):
    """Upload a new software release bundle. Organisation owners/editors may
    publish releases for their own buses. The bundle must be a .tar.gz archive
    of the application code that installs into /opt/nsl/vN. If
    NSL_RELEASE_PRIVATE_KEY is set, the portal signs the bundle automatically;
    otherwise the caller must supply a hex Ed25519 signature.
    """
    require_role(u["id"], organisation_id, "owner", "editor")
    if not version or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", version):
        raise HTTPException(400, "bad version string")
    if channel not in ("stable", "beta", "dev"):
        raise HTTPException(400, "channel must be stable, beta or dev")
    raw = file.file.read()
    if len(raw) < 100:
        raise HTTPException(400, "bundle too small")
    if raw[:2] != b"\x1f\x8b":
        raise HTTPException(400, "bundle must be a gzip tar archive")
    h = hashlib.sha256(raw).hexdigest()
    con = db()
    if con.execute("SELECT 1 FROM releases WHERE organisation_id=? AND channel=? AND version=?",
                   (organisation_id, channel, version)).fetchone():
        raise HTTPException(400, f"release {version} already exists for channel {channel}")
    # Reject a re-upload of identical content under a new version string:
    # date-based versions (push-release.sh) can otherwise produce a "new"
    # release whose bundle is byte-identical to the current latest, so the
    # Pi downloads, restarts, and reports up-to-date without any code change.
    dup = con.execute(
        "SELECT version FROM releases WHERE organisation_id=? AND channel=? "
        "AND bundle_sha256=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (organisation_id, channel, h)).fetchone()
    if dup:
        raise HTTPException(
            400, f"bundle content identical to release {dup['version']} on channel {channel} - "
                 "nothing changed (git HEAD is the same as that release?)")
    rid = str(uuid.uuid4())
    # Unique filename per release: the old releases/{version}.tar.gz scheme
    # let two orgs/channels sharing a version string overwrite each other's
    # bundle on disk (and a re-upload could clobber the live file).
    os.makedirs(os.path.join(STORAGE_DIR, "releases"), exist_ok=True)
    bundle_key = f"releases/{rid}.tar.gz"
    path = os.path.join(STORAGE_DIR, bundle_key)
    with open(path, "wb") as f:
        f.write(raw)
    if not signature:
        try:
            signature = _sign_bundle(path)
        except Exception as e:
            try:
                os.remove(path)
            except OSError:
                pass
            raise HTTPException(400, f"could not sign bundle: {e}")
    try:
        bytes.fromhex(signature)
    except ValueError:
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, "signature must be a hex string")
    con.execute(
        "INSERT INTO releases (id, organisation_id, version, channel, bundle_key, bundle_sha256, signature, notes, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, organisation_id, version, channel, bundle_key, h, signature, notes, _utcnow()))
    con.execute("INSERT INTO audit_log (organisation_id, user_id, action, detail, at) VALUES (?,?,?,?,?)",
                (organisation_id, u["id"], "release.upload", f"{version} ({channel})", _utcnow()))
    con.commit()
    return {"release_id": rid, "version": version, "channel": channel,
            "sha256": h, "bytes": len(raw), "signature": signature}


# remote command fan-out: portal persists, Pi picks up on next heartbeat (<=60 s).
# Offline buses are refused up front - a command that can't reach a bus is an
# error here, not a silent "queued".
@app.get("/api/controllers/{cid}/update")
def controller_update_status(cid: str, u: dict = Depends(current_user)):
    """Latest software update status for a controller: current app_version vs
    newest release on its channel."""
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor", "viewer")
    channel = c["channel"] or "stable"
    rel = con.execute(
        "SELECT * FROM releases WHERE organisation_id=? AND channel=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (c["organisation_id"], channel)).fetchone()
    current = c["app_version"] if "app_version" in c.keys() else ""
    latest = rel["version"] if rel else None
    return {
        "controller_id": cid,
        "channel": channel,
        "current_version": current,
        "latest_version": latest,
        "up_to_date": bool(current and latest and current == latest),
        "can_update": bool(_controller_online(c) and latest and current != latest),
    }
@app.get("/api/controllers/{cid}/live")
def controller_live(cid: str, u: dict = Depends(current_user)):
    """What the bus is doing right now: program, service, destination,
    current stop (n/N), next stop, override and volume - resolved to names
    from the published bundle the bus reports running.

    Shows last-known state when the bus is offline (`stale: true`); the
    keypad and local controller keep working on the vehicle meanwhile.
    """
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor", "viewer")
    try:
        live = json.loads(c["live_state"] or "null") or {}
    except Exception:
        live = {}
    online = _controller_online(c)
    out: dict = {"controller_id": cid, "online": online, "last_seen_at": c["last_seen_at"],
                 "stale": not online,
                 "program": None, "service": None, "destination": None,
                 "stop": None, "next_stop": None, "insert": None,
                 "override": live.get("override_text"),
                 "volume": live.get("volume", 80), "muted": bool(live.get("muted", 0))}
    pid, ver = live.get("program_id"), live.get("program_version")
    if not pid:
        return out
    prog = con.execute("SELECT name FROM programs WHERE id=?", (pid,)).fetchone()
    out["program"] = {"id": pid, "name": prog["name"] if prog else pid, "version": ver}
    bundle = os.path.join(STORAGE_DIR, "programs", pid, f"{ver}.nslprog") if ver is not None else None
    if not bundle or not os.path.exists(bundle):
        out["program"]["unresolved"] = True
        return out
    import sqlite3 as _s
    try:
        bc = _s.connect(f"file:{bundle}?mode=ro", uri=True)
    except Exception:
        out["program"]["unresolved"] = True
        return out
    try:
        bc.row_factory = _s.Row
        if live.get("service_id"):
            svc = bc.execute("SELECT id, number, name FROM services WHERE id=?",
                             (live["service_id"],)).fetchone()
            if svc:
                out["service"] = {"id": svc["id"], "number": svc["number"], "name": svc["name"]}
        if live.get("destination_id"):
            dest = bc.execute("SELECT id, code, name, via, dest FROM destinations WHERE id=?",
                              (live["destination_id"],)).fetchone()
            if dest:
                out["destination"] = {"id": dest["id"], "code": dest["code"], "name": dest["name"],
                                      "via": dest["via"], "dest": dest["dest"]}
                stops = [dict(r) for r in bc.execute(
                    "SELECT id, position, name, board_text, board_override, is_terminus FROM stops "
                    "WHERE destination_id=? ORDER BY position", (dest["id"],))]
                if stops:
                    ids = [s["id"] for s in stops]
                    try:
                        idx = ids.index(live.get("stop_id"))
                    except ValueError:
                        idx = 0
                    cur = stops[idx]
                    out["stop"] = {"id": cur["id"], "position": cur["position"], "count": len(stops),
                                   "name": cur["name"], "board_text": cur["board_text"] or cur["name"],
                                   "is_terminus": bool(cur["is_terminus"])}
                    if idx + 1 < len(stops):
                        nxt = stops[idx + 1]
                        out["next_stop"] = {"id": nxt["id"], "position": nxt["position"],
                                            "name": nxt["board_text"] or nxt["name"]}
                if live.get("insert_destination_id"):
                    ins = bc.execute("SELECT id, code, name, via, dest FROM destinations WHERE id=?",
                                     (live["insert_destination_id"],)).fetchone()
                    if ins:
                        out["insert"] = {"id": ins["id"], "code": ins["code"], "name": ins["name"],
                                         "via": ins["via"], "dest": ins["dest"]}
    finally:
        bc.close()
    return out


@app.get("/api/controllers/{cid}/stops")
def controller_stops(cid: str, u: dict = Depends(current_user)):
    """Ordered stop list for the bus's live destination, from the published
    bundle the bus reports running. No times - just the service's stops."""
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor", "viewer")
    try:
        live = json.loads(c["live_state"] or "null") or {}
    except Exception:
        live = {}
    pid, ver, did = live.get("program_id"), live.get("program_version"), live.get("destination_id")
    if not pid or ver is None or not did:
        return {"destination_id": did, "stops": []}
    bundle = os.path.join(STORAGE_DIR, "programs", pid, f"{ver}.nslprog")
    if not os.path.exists(bundle):
        return {"destination_id": did, "stops": []}
    import sqlite3 as _s
    bc = _s.connect(f"file:{bundle}?mode=ro", uri=True)
    try:
        bc.row_factory = _s.Row
        stops = [{"id": r["id"], "position": r["position"], "name": r["name"],
                  "board_text": r["board_text"] or r["name"],
                  "is_terminus": bool(r["is_terminus"])}
                 for r in bc.execute("SELECT id, position, name, board_text, is_terminus FROM stops "
                                      "WHERE destination_id=? ORDER BY position", (did,))]
        return {"destination_id": did, "stops": stops}
    finally:
        bc.close()


@app.get("/api/controllers/{cid}/browse")
def controller_browse(cid: str, u: dict = Depends(current_user)):
    """Services + destinations tree for the controller's current program."""
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor", "viewer")
    try:
        live = json.loads(c["live_state"] or "null") or {}
    except Exception:
        live = {}
    pid = live.get("program_id")
    ver = live.get("program_version")
    if not pid:
        # no live state yet: fall back to the program queued for this controller
        cp = con.execute(
            "SELECT program_id, desired_version FROM controller_programs WHERE controller_id=? ORDER BY updated_at DESC LIMIT 1",
            (cid,)).fetchone()
        if cp:
            pid, ver = cp["program_id"], cp["desired_version"]
    if not pid or ver is None:
        return {"services": []}
    bundle = os.path.join(STORAGE_DIR, "programs", pid, f"{ver}.nslprog")
    if not os.path.exists(bundle):
        return {"services": []}
    import sqlite3 as _s
    bc = _s.connect(f"file:{bundle}?mode=ro", uri=True)
    def _num_key(v: str) -> tuple:
        # numeric-aware: "9" < "80" < "100", then plain string fallback
        s = str(v or "")
        return (0, int(s), "") if s.isdigit() else (1, 0, s)
    try:
        bc.row_factory = _s.Row
        services = []
        for svc in bc.execute("SELECT * FROM services ORDER BY position"):
            dests = [dict(d) for d in bc.execute(
                "SELECT id, name, code, position FROM destinations WHERE service_id=? ORDER BY position",
                (svc["id"],))]
            dests.sort(key=lambda d: _num_key(d.get("code")))
            services.append({
                "id": svc["id"],
                "number": svc.get("number") or "",
                "name": svc.get("description") or svc.get("name") or "",
                "destinations": dests,
            })
        services.sort(key=lambda s: _num_key(s["number"]))
        return {"services": services}
    finally:
        bc.close()


@app.post("/api/controllers/{cid}/command")
def remote_command(cid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c or not c["organisation_id"]:
        raise HTTPException(404, "no paired controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    if not _controller_online(c):
        raise HTTPException(409, f"controller offline (last seen {c['last_seen_at'] or 'never'}) - command not queued")
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
                 body.get("portal_url", "https://nsldisplays.com/portal"), body.get("channel", "stable"), _utcnow()))
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
    for t, col in (("controller_programs", "controller_id"), ("pending_commands", "controller_id"),
                   ("display_outputs", "controller_id"), ("keypad_config", "controller_id"),
                   ("wifi_networks", "controller_id"), ("controller_config", "controller_id")):
        try:
            con.execute(f"DELETE FROM {t} WHERE {col}=?", (cid,))
        except Exception:
            pass
    con.execute("DELETE FROM controllers WHERE id=?", (cid,))
    con.execute("INSERT INTO audit_log (organisation_id, user_id, action, detail, at) VALUES (?,?,?,?,?)",
                (c["organisation_id"], u["id"], "controller.remove", cid, _utcnow()))
    con.commit()
    return {"ok": True}


@app.post("/api/controllers/{cid}/token")
def rotate_device_token(cid: str, u: dict = Depends(current_user)):
    """Issue a fresh device token for a paired controller (old one stops working).

    The token is returned once and never stored - the UI must show it immediately.
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


# ---------- GPIO pins: which header pins are free vs reserved by the matrix ----------
# Physical 40-pin header of the Pi 4B (BOARD numbering). BCM None = not a GPIO.
_PINOUT: list[tuple[int, int | None, str, str]] = [
    # (physical, bcm, label, kind)  kind: power | ground | gpio | eeprom
    (1, None, "3V3", "power"), (2, None, "5V", "power"),
    (3, 2, "GPIO2 · SDA (I2C)", "gpio"), (4, None, "5V", "power"),
    (5, 3, "GPIO3 · SCL (I2C)", "gpio"), (6, None, "GND", "ground"),
    (7, 4, "GPIO4", "gpio"), (8, 14, "GPIO14 · TXD", "gpio"),
    (9, None, "GND", "ground"), (10, 15, "GPIO15 · RXD", "gpio"),
    (11, 17, "GPIO17", "gpio"), (12, 18, "GPIO18 · OE", "gpio"),
    (13, 27, "GPIO27", "gpio"), (14, None, "GND", "ground"),
    (15, 22, "GPIO22", "gpio"), (16, 23, "GPIO23", "gpio"),
    (17, None, "3V3", "power"), (18, 24, "GPIO24", "gpio"),
    (19, 10, "GPIO10 · MOSI (SPI)", "gpio"), (20, None, "GND", "ground"),
    (21, 9, "GPIO9 · MISO (SPI)", "gpio"), (22, 25, "GPIO25", "gpio"),
    (23, 11, "GPIO11 · SCLK (SPI)", "gpio"), (24, 8, "GPIO8 · CE0 (SPI)", "gpio"),
    (25, None, "GND", "ground"), (26, 7, "GPIO7 · CE1 (SPI)", "gpio"),
    (27, 0, "ID_SD (EEPROM)", "eeprom"), (28, 1, "ID_SC (EEPROM)", "eeprom"),
    (29, 5, "GPIO5", "gpio"), (30, None, "GND", "ground"),
    (31, 6, "GPIO6", "gpio"), (32, 12, "GPIO12", "gpio"),
    (33, 13, "GPIO13", "gpio"), (34, None, "GND", "ground"),
    (35, 19, "GPIO19", "gpio"), (36, 16, "GPIO16", "gpio"),
    (37, 26, "GPIO26", "gpio"), (38, 20, "GPIO20", "gpio"),
    (39, None, "GND", "ground"), (40, 21, "GPIO21", "gpio"),
]


@app.get("/api/gpio/pins")
def gpio_pins(organisation_id: str, controller_id: str | None = None,
               u: dict = Depends(current_user)):
    """Visual pin map: which header pins are free vs reserved by the LED matrix.

    Reserved set comes from the controller's matrix options (mapping + parallel)
    so it stays correct if the HAT/mapping changes; without a controller it
    falls back to the default (regular, parallel=1).
    """
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    con = db()
    mapping, parallel = "regular", 1
    if controller_id:
        c = con.execute("SELECT * FROM controllers WHERE id=?", (controller_id,)).fetchone()
        if not c or c["organisation_id"] != organisation_id:
            raise HTTPException(404, "no controller")
        cfg = con.execute("SELECT matrix_options FROM controller_config WHERE controller_id=?",
                          (controller_id,)).fetchone()
        if cfg and cfg["matrix_options"]:
            try:
                opts = json.loads(cfg["matrix_options"])
                mapping = str(opts.get("led-gpio-mapping", mapping))
                parallel = int(opts.get("led-parallel", parallel))
            except Exception:
                pass
    try:
        from pi.nsl_pi.config import reserved_bcm_pins as _reserved
        reserved = set(_reserved(mapping, parallel))
    except Exception:
        reserved = {4, 17, 18, 22, 23, 24, 25, 15, 27, 10, 9, 11, 8, 7}
    pins = []
    for phys, bcm, label, kind in _PINOUT:
        is_reserved = kind == "gpio" and bcm in reserved
        status = "reserved" if is_reserved else ("free" if kind == "gpio" else kind)
        pins.append({"physical": phys, "bcm": bcm, "label": label, "kind": kind,
                     "reserved": bool(is_reserved), "status": status,
                     "reason": "LED matrix (HUB75)" if is_reserved else ""})
    free = [p for p in pins if p["status"] == "free"]
    taken = [p for p in pins if p["status"] == "reserved"]
    return {"mapping": mapping, "parallel": parallel, "reserved_bcm": sorted(reserved),
            "pins": pins,
            "summary": {"free": len(free), "reserved": len(taken),
                        "free_bcm": sorted(p["bcm"] for p in free if p["bcm"] is not None)}}


def _default_reserved_bcm() -> set[int]:
    try:
        from pi.nsl_pi.config import reserved_bcm_pins as _reserved
        return set(_reserved("regular", 1))
    except Exception:
        return {4, 17, 18, 22, 23, 24, 25, 15, 27, 10, 9, 11, 8, 7}


_GPIO_TRIGGERS = {"press", "long", "double", "on_active", "while_active", "on_release", "toggle"}
_GPIO_BCM_KIND = {bcm: kind for _, bcm, _, kind in _PINOUT if bcm is not None}


def _check_gpio_pin(pin: int) -> None:
    """Reject pins the matrix owns or that aren't usable GPIOs (power/GND/EEPROM)."""
    try:
        pin = int(pin)
    except (TypeError, ValueError):
        raise HTTPException(400, "pin must be a BCM number")
    kind = _GPIO_BCM_KIND.get(pin)
    if kind != "gpio":
        raise HTTPException(400, f"BCM {pin} is not a usable GPIO (power/ground/EEPROM)")
    if pin in _default_reserved_bcm():
        raise HTTPException(400, f"BCM {pin} is reserved by the LED matrix - pick a green (free) pin")


def _check_gpio_board(pid: str, board_source: str, destination_id: str | None) -> str | None:
    """Validate the rule's board content. Returns normalised destination_id."""
    if board_source not in ("text", "destination"):
        raise HTTPException(400, "board_source must be text or destination")
    if board_source == "destination":
        if not destination_id:
            raise HTTPException(400, "pick a board (destination) for this rule, or use custom text")
        con = db()
        if not con.execute("SELECT 1 FROM p_destinations WHERE program_id=? AND id=?",
                           (pid, destination_id)).fetchone():
            raise HTTPException(400, "board destination is not in this program")
        return destination_id
    return None


# ---------- per-program GPIO rules (pin + trigger -> board/audio effect) ----------
@app.get("/api/programs/{pid}/gpio-rules")
def list_gpio_rules(pid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor", "viewer")
    con = db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM p_gpio_rules WHERE program_id=? ORDER BY position", (pid,))]
    except Exception:
        rows = []
    return {"rules": rows}


@app.post("/api/programs/{pid}/gpio-rules")
def add_gpio_rule(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    trigger = (body.get("trigger") or "press").strip()
    if trigger not in _GPIO_TRIGGERS:
        raise HTTPException(400, f"trigger must be one of {sorted(_GPIO_TRIGGERS)}")
    _check_gpio_pin(body.get("pin"))
    effect = (body.get("effect") or "overlay").strip()
    if effect not in ("overlay", "override"):
        raise HTTPException(400, "effect must be overlay or override")
    target = (body.get("target") or "all").strip() or "all"
    con = db()
    if target != "all" and not con.execute(
            "SELECT 1 FROM program_displays WHERE program_id=? AND name=?", (pid, target)).fetchone():
        raise HTTPException(400, f"target {target!r} is not a display in this program (or use 'all')")
    audio = (body.get("audio") or None)
    if audio and not con.execute(
            "SELECT 1 FROM audio_files WHERE organisation_id=? AND sha256=?", (org, audio)).fetchone():
        raise HTTPException(400, "audio clip not found in this organisation's library")
    board_source = (body.get("board_source") or "text").strip()
    destination_id = _check_gpio_board(pid, board_source, (body.get("destination_id") or None))
    board_text = (body.get("board_text") or "").strip()[:200]
    if board_source == "text" and not board_text and not audio:
        raise HTTPException(400, "custom text needs board text or an audio clip (or pick a board instead)")
    pos = con.execute("SELECT COUNT(*) FROM p_gpio_rules WHERE program_id=?", (pid,)).fetchone()[0]
    rid = str(uuid.uuid4())
    try:
        duration = max(0, int(body.get("duration_s") or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "duration_s must be a whole number of seconds")
    pull = (body.get("pull") or "up").strip()
    if pull not in ("up", "down", "none"):
        raise HTTPException(400, "pull must be up, down or none")
    con.execute("INSERT INTO p_gpio_rules (id, organisation_id, program_id, label, pin,"
                " trigger, effect, target, board_text, board_source, destination_id, audio,"
                " duration_s, pull, active_low, debounce_ms, position)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, org, pid, (body.get("label") or "").strip()[:64], int(body.get("pin")),
                 trigger, effect, target, board_text, board_source, destination_id, audio,
                 duration, pull, 1 if body.get("active_low", True) else 0,
                 int(body.get("debounce_ms") or 50), pos))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"rule_id": rid}


@app.put("/api/programs/{pid}/gpio-rules/{rid}")
def update_gpio_rule(pid: str, rid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    r = con.execute("SELECT * FROM p_gpio_rules WHERE id=? AND program_id=?", (rid, pid)).fetchone()
    if not r:
        raise HTTPException(404, "no rule")
    r = dict(r)
    pin = int(body.get("pin", r["pin"]))
    _check_gpio_pin(pin)
    trigger = (body.get("trigger", r["trigger"]) or "press").strip()
    if trigger not in _GPIO_TRIGGERS:
        raise HTTPException(400, f"trigger must be one of {sorted(_GPIO_TRIGGERS)}")
    effect = (body.get("effect", r["effect"]) or "overlay").strip()
    if effect not in ("overlay", "override"):
        raise HTTPException(400, "effect must be overlay or override")
    target = (body.get("target", r["target"]) or "all").strip() or "all"
    if target != "all" and not con.execute(
            "SELECT 1 FROM program_displays WHERE program_id=? AND name=?", (pid, target)).fetchone():
        raise HTTPException(400, f"target {target!r} is not a display in this program (or use 'all')")
    audio = body.get("audio", r["audio"]) or None
    if audio and not con.execute(
            "SELECT 1 FROM audio_files WHERE organisation_id=? AND sha256=?", (org, audio)).fetchone():
        raise HTTPException(400, "audio clip not found in this organisation's library")
    board_source = (body.get("board_source", r.get("board_source") or "text") or "text").strip()
    destination_id = _check_gpio_board(
        pid, board_source, body.get("destination_id", r.get("destination_id")) or None)
    board_text = (body.get("board_text", r["board_text"]) or "").strip()[:200]
    if board_source == "text" and not board_text and not audio:
        raise HTTPException(400, "custom text needs board text or an audio clip (or pick a board instead)")
    try:
        duration = max(0, int(body.get("duration_s", r["duration_s"]) or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "duration_s must be a whole number of seconds")
    pull = (body.get("pull", r["pull"]) or "up").strip()
    if pull not in ("up", "down", "none"):
        raise HTTPException(400, "pull must be up, down or none")
    con.execute("UPDATE p_gpio_rules SET label=?, pin=?, trigger=?, effect=?, target=?, board_text=?, "
                "board_source=?, destination_id=?, audio=?, duration_s=?, pull=?, active_low=?, "
                "debounce_ms=? WHERE id=?",
                ((body.get("label", r["label"]) or "").strip()[:64], pin, trigger, effect, target,
                 board_text, board_source, destination_id, audio, duration,
                 pull, 1 if body.get("active_low", r["active_low"]) else 0,
                 int(body.get("debounce_ms", r["debounce_ms"]) or 50), rid))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


@app.delete("/api/programs/{pid}/gpio-rules/{rid}")
def delete_gpio_rule(pid: str, rid: str, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    if not con.execute("SELECT 1 FROM p_gpio_rules WHERE id=? AND program_id=?", (rid, pid)).fetchone():
        raise HTTPException(404, "no rule")
    con.execute("DELETE FROM p_gpio_rules WHERE id=?", (rid,))
    rows = con.execute("SELECT id FROM p_gpio_rules WHERE program_id=? ORDER BY position", (pid,)).fetchall()
    for i, row in enumerate(rows):
        con.execute("UPDATE p_gpio_rules SET position=? WHERE id=?", (i, row["id"]))
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), u["id"], pid))
    con.commit()
    return {"ok": True}


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
    try:
        for r in con.execute("SELECT * FROM p_destination_pages WHERE program_id=?", (pid,)):
            d = dict(r); o = d["id"]; d["id"] = str(uuid.uuid4()); d["program_id"] = nid
            d["destination_id"] = idmap.get(d["destination_id"], d["destination_id"])
            con.execute("INSERT INTO p_destination_pages VALUES (?,?,?,?,?,?,?)", list(d.values()))
    except Exception:
        pass
    for table, n in (("p_stops", 10), ("p_layouts", 6), ("p_templates", 6), ("p_presets", 5), ("p_gpio_rules", 17)):
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
    for t in ("p_stops", "p_destination_pages", "p_layouts", "p_templates", "p_presets", "p_gpio_rules", "p_destinations", "p_services",
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
        # placeholder marker - flag so the UI can say "silent, regenerate".
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
        raise HTTPException(404, "file missing - re-upload or regenerate this clip")
    with open(path, "rb") as f:
        magic = f.read(12)
    # Canonical WAV (and legacy WAV-data-with-.flac-name) -> audio/wav.
    if magic[:4] == b"RIFF":
        return FileResponse(path, media_type="audio/wav", filename=f"{row['filename']}.wav")
    if magic[:4] == b"fLaC":
        return FileResponse(path, media_type="audio/flac", filename=f"{row['filename']}.flac")
    # Legacy raw uploads stored under a .flac name (e.g. MP3 bytes) - transcode
    # on the fly so old clips play instead of staying silent.
    import shutil as _sh
    from fastapi.responses import Response as _Response
    ff = _sh.which("ffmpeg")
    if not ff:
        raise HTTPException(404, "stored file is not directly playable and ffmpeg is missing - re-upload as WAV")
    with _tf.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        tmp = t.name
    try:
        p = _sp.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", str(_CANON_RATE),
                     "-f", "wav", "-y", tmp], capture_output=True)
        if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 100:
            raise HTTPException(404, "stored file is corrupt - re-upload or regenerate this clip")
        with open(tmp, "rb") as f:
            data = f.read()
        return _Response(content=data, media_type="audio/wav")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, "stored file is corrupt - re-upload or regenerate this clip")
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


@app.get("/api/audio/{sha}/usages")
def audio_usages(sha: str, organisation_id: str, u: dict = Depends(current_user)):
    """Where a clip is used: stops + service announcement slots.

    Powers the delete-and-replace dialog so users pick a substitute
    instead of hitting a dead-end error.
    """
    require_role(u["id"], organisation_id, "owner", "editor")
    con = db()
    if not con.execute("SELECT 1 FROM audio_files WHERE sha256=? AND organisation_id=?",
                       (sha, organisation_id)).fetchone():
        raise HTTPException(404, "no clip")
    stops = [dict(r) for r in con.execute(
        """SELECT s.name AS stop_name, d.code AS dest_code, d.name AS dest_name,
                  sv.number AS service_number, p.name AS program_name
           FROM p_stops s
           JOIN p_destinations d ON d.id=s.destination_id
           JOIN p_services sv ON sv.id=d.service_id
           JOIN programs p ON p.id=s.program_id
           WHERE s.audio=? AND s.organisation_id=? ORDER BY p.name, sv.number, d.code LIMIT 21""",
        (sha, organisation_id))]
    stop_count = con.execute("SELECT COUNT(*) FROM p_stops WHERE audio=? AND organisation_id=?",
                             (sha, organisation_id)).fetchone()[0]
    services = []
    for col, label in (("next_stop_audio", "Next-stop announcement"),
                       ("approaching_audio", "Approaching announcement"),
                       ("last_stop_audio", "Terminus suffix")):
        for r in con.execute(
                f"""SELECT sv.number AS service_number, sv.name AS service_name,
                            p.name AS program_name FROM p_services sv
                     JOIN programs p ON p.id=sv.program_id
                     WHERE sv.{col}=? AND sv.organisation_id=?
                     ORDER BY p.name, sv.number LIMIT 21""",
                (sha, organisation_id)):
            d = dict(r)
            d["field"] = label
            services.append(d)
    return {"sha256": sha, "stop_count": stop_count, "stops": stops, "services": services}


@app.delete("/api/audio/{sha}")
def delete_audio(sha: str, organisation_id: str, replacement: str | None = None,
                 u: dict = Depends(current_user)):
    row = db().execute("SELECT * FROM audio_files WHERE sha256=? AND organisation_id=?",
                       (sha, organisation_id)).fetchone()
    if not row:
        raise HTTPException(404, "no clip")
    require_role(u["id"], organisation_id, "owner", "editor")
    con = db()
    stop_n = con.execute("SELECT COUNT(*) FROM p_stops WHERE audio=? AND organisation_id=?",
                         (sha, organisation_id)).fetchone()[0]
    svc_cols = [col for col in ("next_stop_audio", "approaching_audio", "last_stop_audio")
                if con.execute(f"SELECT 1 FROM p_services WHERE {col}=? AND organisation_id=? LIMIT 1",
                               (sha, organisation_id)).fetchone()]
    if (stop_n or svc_cols) and replacement is None:
        used = (["stops"] if stop_n else []) + svc_cols
        raise HTTPException(400, f"clip is used by {', '.join(used)} - pick a replacement")
    replaced_stops, replaced_services = 0, 0
    if replacement is not None:
        if replacement == sha:
            raise HTTPException(400, "replacement must be a different clip")
        new = None
        if replacement:
            if not con.execute("SELECT 1 FROM audio_files WHERE sha256=? AND organisation_id=?",
                               (replacement, organisation_id)).fetchone():
                raise HTTPException(400, "replacement clip not found in this organisation")
            new = replacement
        replaced_stops = con.execute("UPDATE p_stops SET audio=? WHERE audio=? AND organisation_id=?",
                                     (new, sha, organisation_id)).rowcount
        for col in ("next_stop_audio", "approaching_audio", "last_stop_audio"):
            replaced_services += con.execute(
                f"UPDATE p_services SET {col}=? WHERE {col}=? AND organisation_id=?",
                (new, sha, organisation_id)).rowcount
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
    return {"ok": True, "replaced_stops": replaced_stops, "replaced_services": replaced_services}


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


# ---------- TfL iBus audio creator (search + trim local announcement library) ----------
# Source files live in the repo at tfl-ibus-announcements/Renamed/{Stops,Destinations,Routes}/*.mp3
# (Stops = "X" stop names, Destinations = terminus boards, Routes = route numbers)
# plus tfl-ibus-announcements/Original/*.mp3 (raw S_<NAME>_001 recordings + messages).
# Library root override: TFL_LIBRARY_DIR (the Renamed folder). The older
# TFL_STOPS_DIR override (the Stops folder itself) is still honoured for stops.
# Identifiers are folder-qualified ("Stops/x.mp3"); a bare "x.mp3" still
# resolves to Stops for backwards compatibility. Trimmed/merged clips land in
# the org audio library (selectable in the stops portal).
_TFL_FOLDERS = ("Stops", "Destinations", "Routes", "Original")


def _tfl_root() -> str | None:
    root = os.environ.get("TFL_LIBRARY_DIR") or os.path.join(
        _REPO_ROOT, "tfl-ibus-announcements", "Renamed")
    return root if os.path.isdir(root) else None


def _tfl_stops_dir() -> str | None:
    override = os.environ.get("TFL_STOPS_DIR")
    if override and os.path.isdir(override):
        return override
    root = _tfl_root()
    if root:
        d = os.path.join(root, "Stops")
        if os.path.isdir(d):
            return d
    return None


def _tfl_folder_dir(folder: str) -> str | None:
    if folder not in _TFL_FOLDERS:
        return None
    if folder == "Stops":
        return _tfl_stops_dir()
    if folder == "Original":
        o = os.environ.get("TFL_ORIGINAL_DIR") or os.path.join(
            _REPO_ROOT, "tfl-ibus-announcements", "Original")
        return o if os.path.isdir(o) else None
    root = _tfl_root()
    if root:
        d = os.path.join(root, folder)
        if os.path.isdir(d):
            return d
    return None


def _tfl_safe_path(source: str) -> str:
    folder, rest = "Stops", (source or "")
    if "/" in rest:
        first, tail = rest.split("/", 1)
        if first in _TFL_FOLDERS:
            folder, rest = first, tail
    base = os.path.basename(rest)
    if not base or base in (".", "..") or not base.lower().endswith(".mp3"):
        raise HTTPException(400, "bad file")
    d = _tfl_folder_dir(folder)
    if not d:
        raise HTTPException(404, "TfL announcement library not found on this server")
    p = os.path.realpath(os.path.join(d, base))
    if not p.startswith(os.path.realpath(d) + os.sep) or not os.path.isfile(p):
        raise HTTPException(404, "no such announcement")
    return p


@app.get("/api/tfl/search")
def tfl_search(q: str = "", limit: int = 100, organisation_id: str = "",
               folder: str = "", u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    folders = [folder] if folder in _TFL_FOLDERS else list(_TFL_FOLDERS)
    needle = (q or "").strip().lower()
    hits = []
    for f in folders:
        d = _tfl_folder_dir(f)
        if not d:
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith(".mp3"):
                continue
            stem = fn[:-4]
            if needle and needle not in stem.lower():
                continue
            hits.append({"file": f"{f}/{fn}", "label": stem, "folder": f})
    hits.sort(key=lambda r: (r["label"].lower(), r["folder"]))
    total = len(hits)
    hits = hits[: max(1, min(limit or 100, 200))]
    return {"count": total, "results": hits}


@app.get("/api/tfl/file")
def tfl_file(file: str, organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    path = _tfl_safe_path(file)
    return FileResponse(path, media_type="audio/mpeg", filename=os.path.basename(path))


@app.post("/api/tfl/clip")
def tfl_clip(body: dict, u: dict = Depends(current_user)):
    org = body.get("organisation_id", "")
    require_role(u["id"], org, "owner", "editor")
    src = _tfl_safe_path(body.get("source") or "")
    try:
        start_ms = int(body.get("start_ms", 0) or 0)
        end_ms = int(body.get("end_ms", 0) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "start/end must be milliseconds")
    with open(src, "rb") as f:
        raw = f.read()
    pcm, full_ms = _decode_to_mono22050(raw, os.path.basename(src))
    total = len(pcm) // 2
    if end_ms <= 0:
        end_ms = full_ms
    start_ms, end_ms = max(0, start_ms), min(full_ms, end_ms)
    if start_ms >= end_ms:
        raise HTTPException(400, "selection is empty - drag a region on the waveform first")
    if end_ms - start_ms < 50:
        raise HTTPException(400, "selection too short (min 50 ms)")
    import struct as _st
    s0 = int(start_ms / 1000 * _CANON_RATE)
    s1 = int(end_ms / 1000 * _CANON_RATE)
    s0, s1 = max(0, min(s0, total)), max(0, min(s1, total))
    samples = list(_st.unpack(f"<{s1 - s0}h", pcm[s0 * 2: s1 * 2]))
    # 8 ms linear fades at both edges so cuts don't click on the bus.
    fade = min(len(samples) // 4, int(_CANON_RATE * 0.008))
    for i in range(fade):
        g = i / fade
        samples[i] = int(samples[i] * g)
        samples[-(i + 1)] = int(samples[-(i + 1)] * g)
    peak = max((abs(s) for s in samples), default=0)
    if peak < 100:
        raise HTTPException(400, "that selection is silence - widen the region")
    cut = _st.pack(f"<{len(samples)}h", *samples)
    wav = _wav_bytes_from_pcm(cut)
    duration_ms = int(len(cut) / 2 / _CANON_RATE * 1000)
    want = _slugify_clip(body.get("name") or "")
    if not want:
        want = _slugify_clip(os.path.splitext(os.path.basename(src))[0])
    if not want or len(want) < 2:
        raise HTTPException(400, "give this clip a name, e.g. morrisons-wood-lane")
    h = hashlib.sha256(wav).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".wav")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(wav)
    con = db()
    friendly = _unique_clip_name(con, org, want, h)
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), org, h, friendly, f"audio/{h[:2]}/{h}.wav",
                 duration_ms, "tfl-cut", os.path.splitext(os.path.basename(src))[0],
                 None, json.dumps({"start_ms": start_ms, "end_ms": end_ms}), _utcnow()))
    con.commit()
    row = con.execute("SELECT filename, duration_ms FROM audio_files WHERE organisation_id=? AND sha256=?",
                      (org, h)).fetchone()
    final = row[0] if row else friendly
    dur = row[1] if row else duration_ms
    return {"sha256": h, "name": final, "filename": final, "duration_ms": dur,
            "start_ms": start_ms, "end_ms": end_ms, "source_duration_ms": full_ms}


@app.post("/api/tfl/merge")
def tfl_merge(body: dict, u: dict = Depends(current_user)):
    """Join 2–20 trimmed announcement parts into one clip.

    Body: {organisation_id, parts: [{source, start_ms, end_ms}], gap_ms, name}.
    Each part is trimmed exactly like /api/tfl/clip, joined with `gap_ms` of
    silence between words, then saved to the org audio library.
    """
    org = body.get("organisation_id", "")
    require_role(u["id"], org, "owner", "editor")
    parts = body.get("parts") or []
    if not isinstance(parts, list) or not (2 <= len(parts) <= 20):
        raise HTTPException(400, "add 2–20 parts to merge (use “Add selection to merge” on each word)")
    try:
        gap_ms = int(body.get("gap_ms", 120) or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "gap must be milliseconds")
    gap_ms = max(0, min(gap_ms, 2000))
    # Per-boundary transitions: [{gap_ms, xfade_ms}]. xfade_ms > 0 overlaps the
    # two words with an equal-power crossfade (gap ignored on that join).
    raw_joins = body.get("joins")
    joins: list[dict] = []
    if raw_joins is not None:
        if not isinstance(raw_joins, list) or len(raw_joins) != len(parts) - 1:
            raise HTTPException(400, "joins must have one entry per boundary between parts")
        for j in raw_joins:
            if not isinstance(j, dict):
                raise HTTPException(400, "bad join")
            try:
                jgap = int(j.get("gap_ms", gap_ms) or 0)
                jx = int(j.get("xfade_ms", 0) or 0)
            except (TypeError, ValueError):
                raise HTTPException(400, "join gap/crossfade must be milliseconds")
            joins.append({"gap_ms": max(0, min(jgap, 2000)), "xfade_ms": max(0, min(jx, 2000))})
    else:
        joins = [{"gap_ms": gap_ms, "xfade_ms": 0} for _ in range(len(parts) - 1)]
    import struct as _st
    segs: list[list[int]] = []
    stems: list[str] = []
    meta_parts: list[dict] = []
    for p in parts:
        if not isinstance(p, dict):
            raise HTTPException(400, "bad part")
        src = _tfl_safe_path(p.get("source") or "")
        try:
            start_ms = int(p.get("start_ms", 0) or 0)
            end_ms = int(p.get("end_ms", 0) or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "part start/end must be milliseconds")
        with open(src, "rb") as f:
            raw = f.read()
        pcm, full_ms = _decode_to_mono22050(raw, os.path.basename(src))
        total = len(pcm) // 2
        if end_ms <= 0:
            end_ms = full_ms
        start_ms, end_ms = max(0, start_ms), min(full_ms, end_ms)
        if start_ms >= end_ms or end_ms - start_ms < 50:
            raise HTTPException(400, f"part {os.path.basename(src)} is empty/too short - check its selection")
        s0 = int(start_ms / 1000 * _CANON_RATE)
        s1 = int(end_ms / 1000 * _CANON_RATE)
        s0, s1 = max(0, min(s0, total)), max(0, min(s1, total))
        seg = list(_st.unpack(f"<{s1 - s0}h", pcm[s0 * 2: s1 * 2]))
        segs.append(seg)
        stems.append(os.path.splitext(os.path.basename(src))[0])
        meta_parts.append({"source": os.path.basename(src), "start_ms": start_ms, "end_ms": end_ms})
    import math as _m
    MICRO = int(_CANON_RATE * 0.005)  # anti-click micro-fade for hard (gap) joins
    out: list[float] = [float(v) for v in segs[0]]
    applied: list[dict] = []
    for i in range(1, len(segs)):
        j = joins[i - 1]
        seg = [float(v) for v in segs[i]]
        if j["xfade_ms"] > 0:
            ov = min(int(j["xfade_ms"] / 1000 * _CANON_RATE), len(out), len(seg))
            if ov < 32:
                raise HTTPException(400,
                    f"crossfade on join {i} is longer than the words either side - shorten it")
            # equal-power crossfade: constant loudness through the overlap.
            denom = max(1, ov - 1)
            mixed = [out[len(out) - ov + k] * _m.cos(k / denom * _m.pi / 2)
                     + seg[k] * _m.sin(k / denom * _m.pi / 2) for k in range(ov)]
            out = out[: len(out) - ov] + mixed + seg[ov:]
            applied.append({"gap_ms": 0, "xfade_ms": int(ov / _CANON_RATE * 1000)})
        else:
            # hard join: micro-fade both sides so there's no click, then silence gap.
            f = min(MICRO, len(out) // 2, len(seg) // 2)
            for k in range(f):
                out[len(out) - f + k] *= (f - k) / f
                seg[k] *= (k + 1) / f
            out.extend([0.0] * int(j["gap_ms"] / 1000 * _CANON_RATE))
            out.extend(seg)
            applied.append({"gap_ms": j["gap_ms"], "xfade_ms": 0})
    if len(out) / _CANON_RATE > 60:
        raise HTTPException(400, "merged clip would be over 60 s - shorten the parts")
    peak = max((abs(s) for s in out), default=0)
    if peak < 100:
        raise HTTPException(400, "merged audio is silence - widen the selections")
    # outer-edge fade so the clip starts/ends cleanly on the bus.
    edge = min(len(out) // 4, int(_CANON_RATE * 0.008))
    for i in range(edge):
        g = i / edge
        out[i] = int(out[i] * g)
        out[-(i + 1)] = int(out[-(i + 1)] * g)
    merged = _st.pack(f"<{len(out)}h", *[max(-32768, min(32767, int(round(v)))) for v in out])
    wav = _wav_bytes_from_pcm(merged)
    duration_ms = int(len(merged) / 2 / _CANON_RATE * 1000)
    want = _slugify_clip(body.get("name") or "")
    if not want:
        want = _slugify_clip(" ".join(stems))
    if not want or len(want) < 2:
        raise HTTPException(400, "give this clip a name, e.g. bloomfield-crescent")
    h = hashlib.sha256(wav).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".wav")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(wav)
    con = db()
    friendly = _unique_clip_name(con, org, want, h)
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), org, h, friendly, f"audio/{h[:2]}/{h}.wav",
                 duration_ms, "tfl-merge", " + ".join(stems),
                 None, json.dumps({"parts": meta_parts, "joins": applied}), _utcnow()))
    con.commit()
    row = con.execute("SELECT filename, duration_ms FROM audio_files WHERE organisation_id=? AND sha256=?",
                      (org, h)).fetchone()
    final = row[0] if row else friendly
    dur = row[1] if row else duration_ms
    return {"sha256": h, "name": final, "filename": final, "duration_ms": dur,
            "parts": len(segs), "joins": applied}


# ---------- bustimes.org import (services -> destinations + stops + stop audio) ----------
@app.get("/api/bustimes/services")
def bustimes_services(operator: str = "", search: str = "", page: int = 1,
                      u: dict = Depends(current_user)):
    """Search bustimes.org services. Any signed-in user on any org may browse;
    importing still needs editor on the target program's org."""
    from . import bustimes as _bt
    try:
        data = _bt.fetch_services(operator=operator, search=search, page=page)
    except Exception as e:
        raise HTTPException(502, f"bustimes.org lookup failed: {e}")
    out = []
    for r in (data.get("results") or []):
        desc = r.get("description") or ""
        out.append({"id": r.get("id"), "slug": r.get("slug", ""),
                    "line_name": r.get("line_name", ""), "description": desc,
                    "destinations": _bt.split_description(desc)})
    return {"count": data.get("count", len(out)), "next": data.get("next"),
            "previous": data.get("previous"), "results": out}


@app.get("/api/bustimes/services/{sid}")
def bustimes_service_detail(sid: int, u: dict = Depends(current_user)):
    """One bustimes service: typical destinations (description split on ' - ')
    plus the stop list of every timetable direction."""
    from . import bustimes as _bt
    try:
        detail = _bt.fetch_service_detail(sid)
    except Exception as e:
        raise HTTPException(502, f"bustimes.org lookup failed: {e}")
    desc = detail.get("description") or ""
    parts = _bt.split_description(desc)
    try:
        directions = _bt.fetch_timetable(sid)
    except Exception as e:
        raise HTTPException(502, f"timetable download failed: {e}")
    dirs = []
    for i, d in enumerate(directions):
        stops = d.get("stops") or []
        dirs.append({"index": i, "from": stops[0] if stops else "",
                     "to": stops[-1] if stops else "", "count": len(stops),
                     "trips": d.get("trips", 0), "stops": stops})
    return {"id": detail.get("id", sid), "line_name": detail.get("line_name", ""),
            "slug": detail.get("slug", ""), "description": desc,
            "destinations": parts,
            "suggested": [{"name": p, "direction": _bt.auto_direction_for_part(p, directions)}
                          for p in parts],
            "directions": dirs}


# ---------- bustimes.org import jobs ----------
# A full service import synthesises dozens of stop clips - minutes of Piper
# work. That must never block the request with no feedback, so the audio
# phase runs in a background thread and the UI polls this registry for
# live progress, then shows a confirmation summary when it finishes.
_jobs_lock = _threading.Lock()
_jobs: dict[str, dict] = {}


def _job_public(j: dict) -> dict:
    return {k: j.get(k) for k in ("job_id", "organisation_id", "program_id", "service_id",
                                 "service_number", "state", "stage", "done", "total",
                                 "created_at", "updated_at", "result", "error")}


def _job_create(org: str, program_id: str, service_id: str, service_number: str, total: int) -> dict:
    import time as _time
    jid = str(uuid.uuid4())
    now = _utcnow()
    job = {"job_id": jid, "organisation_id": org, "program_id": program_id,
           "service_id": service_id, "service_number": service_number,
           "state": "working", "stage": "starting", "done": 0, "total": total,
           "created_at": now, "updated_at": now, "result": None, "error": None,
           "_t": _time.monotonic()}
    with _jobs_lock:
        _jobs[jid] = job
        # bounded registry: drop terminal jobs older than an hour, cap at 100
        if len(_jobs) > 100:
            old = [k for k, v in _jobs.items()
                   if v.get("state") in ("done", "error") and now > v.get("updated_at", now)]
            for k in old[: len(_jobs) - 100]:
                del _jobs[k]
    return job


def _job_progress(jid: str, stage: str, done: int, total: int) -> None:
    with _jobs_lock:
        j = _jobs.get(jid)
        if j:
            j["stage"], j["done"], j["total"], j["updated_at"] = stage, done, total, _utcnow()


def _job_finish(jid: str, state: str, result: dict | None = None, error: str | None = None) -> None:
    with _jobs_lock:
        j = _jobs.get(jid)
        if j:
            j["state"], j["result"], j["error"], j["updated_at"] = state, result, error, _utcnow()


def _find_clip_for_stop(con, org: str, spoken_name: str, stop_name: str | None = None) -> str | None:
    """Reuse lookup for a stop: library clip by spoken text (short form with
    trailing period) or friendly filename, else the audio of an
    already-imported stop with the same full name.
    (The last leg covers byte-identical clips shared under one hash.)"""
    row = con.execute("SELECT sha256 FROM audio_files WHERE organisation_id=? AND tts_text=?",
                      (org, spoken_name)).fetchone()
    if not row and _slugify_clip(spoken_name):
        row = con.execute("SELECT sha256 FROM audio_files WHERE organisation_id=? AND filename=?",
                          (org, _slugify_clip(spoken_name))).fetchone()
    if not row:
        for cand in ([stop_name] if stop_name else []) + [spoken_name]:
            row = con.execute("SELECT audio FROM p_stops WHERE organisation_id=? AND name=? "
                              "AND audio IS NOT NULL AND audio<>'' LIMIT 1", (org, cand)).fetchone()
            if row:
                return row[0]
        return None
    return row[0]


def _store_clip(con, org: str, name: str, voice_id: str, wav: bytes, duration_ms: int,
                speaker_id: int | None = None) -> str:
    """Save synthesised WAV bytes to the audio library. Returns the sha256."""
    h = hashlib.sha256(wav).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".wav")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(wav)
    friendly = _unique_clip_name(con, org, _slugify_clip(name) or h[:12], h)
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), org, h, friendly, f"audio/{h[:2]}/{h}.wav",
                 duration_ms, "tts", name, voice_id,
                 json.dumps({"speed": 1.0, "placeholder": False, "speaker_id": speaker_id}), _utcnow()))
    return h


def _bustimes_audio_worker(jid: str, org: str, program_id: str, voice_id: str, names: list[str],
                           speaker_id: int | None = None) -> None:
    """Background thread: synthesise stop clips (one model load) and link them.

    Progress is visible on GET /api/bustimes/jobs/<id> after every stop, so a
    150-stop service shows movement instead of a hung page.
    """
    con = connect(_current_db_url())
    try:
        con.execute("PRAGMA busy_timeout = 30000")
    except Exception:
        pass
    try:
        from . import bustimes as _bt
        total = len(names)
        _job_progress(jid, "synthesising stop audio", 0, total)
        # Spoken form: "Tutbury, adj St Christopher Church" is announced as
        # "St Christopher Church." - the stop record keeps the full name,
        # only the clip name and spoken audio use the short form.
        spoken = {n: _bt.spoken_stop_name(n) for n in names}
        # Single model load for the whole batch (was: one `piper` process
        # per stop, each reloading the ONNX voice - minutes of hang).
        synth = _synth_many_piper([spoken[n] for n in names], voice_id, org, speaker_id)
        generated, reused, errors, silent = 0, 0, [], []
        for i, name in enumerate(names, 1):
            try:
                h = _find_clip_for_stop(con, org, spoken[name], name)
                if h:
                    reused += 1
                else:
                    real, tts_error = synth.get(spoken[name], (None, "voice generation failed"))
                    if real is None:
                        errors.append(f"{name}: {tts_error or 'voice generation failed'}")
                        silent.append(name)
                        _job_progress(jid, f"synthesising stop audio ({i}/{total})", i, total)
                        continue
                    wav, duration_ms = real
                    h = _store_clip(con, org, spoken[name], voice_id, wav, duration_ms, speaker_id)
                    generated += 1
                con.execute("UPDATE p_stops SET audio=? WHERE program_id=? AND name=? "
                            "AND (audio IS NULL OR audio='')", (h, program_id, name))
                con.commit()
            except Exception as e:
                errors.append(f"{name}: {e}")
            _job_progress(jid, f"synthesising stop audio ({min(i, total)}/{total})",
                          min(i, total), total)
        _job_finish(jid, "done", result={"unique_stops": total, "generated": generated,
                                         "reused": reused, "silent": silent, "errors": errors})
    except Exception as e:
        _job_finish(jid, "error", error=str(e))
    finally:
        try:
            con.close()
        except Exception:
            pass


@app.get("/api/bustimes/jobs/{jid}")
def bustimes_job(jid: str, u: dict = Depends(current_user)):
    """Poll a bustimes import job for live progress / final confirmation."""
    with _jobs_lock:
        job = _jobs.get(jid)
        job = dict(job) if job else None
    if not job:
        raise HTTPException(404, "no such import job")
    require_role(u["id"], job["organisation_id"], "owner", "editor", "viewer")
    return _job_public(job)


def _do_bustimes_import_one(con, org: str, pid: str, user_id: str, bustimes_id: int,
                            voice_id: str | None, speaker_id: int | None,
                            service_number: str | None = None,
                            service_name: str | None = None,
                            req_dests: list[dict] | None = None,
                            gen_audio: bool = True) -> tuple[dict, list[str]]:
    """Import a single bustimes.org service into a program.

    Returns (summary, pending_stop_names). The caller is responsible for
    spawning an audio worker if pending stops need synthesis.
    """
    from . import bustimes as _bt
    detail = _bt.fetch_service_detail(bustimes_id)
    line_name = (service_number or detail.get("line_name") or "").strip()
    if not line_name:
        raise ValueError("service has no line name - pass service_number explicitly")
    description = (service_name or detail.get("description") or line_name).strip()
    parts = _bt.split_description(detail.get("description") or description)
    directions = _bt.fetch_timetable(bustimes_id)
    if not directions or not any(d.get("stops") for d in directions):
        raise ValueError("no timetable stops found for that service")
    if req_dests is None:
        req_dests = [{"name": p, "via": "", "dest": p,
                      "direction": _bt.auto_direction_for_part(p, directions)} for p in parts]
        if not req_dests:
            req_dests = [{"name": description, "via": "", "dest": description, "direction": 0}]
    row = con.execute("SELECT * FROM p_services WHERE program_id=? AND number=?",
                      (pid, line_name)).fetchone()
    if row:
        sid = row["id"]
        if description and not row["name"]:
            con.execute("UPDATE p_services SET name=? WHERE id=?", (description, sid))
    else:
        sid = str(uuid.uuid4())
        pos = con.execute("SELECT COUNT(*) FROM p_services WHERE program_id=?", (pid,)).fetchone()[0]
        con.execute("INSERT INTO p_services VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, org, pid, line_name, description, pos, None, None, None, "both"))
    made, warnings = [], []
    used_codes = {r[0] for r in con.execute("SELECT code FROM p_destinations WHERE program_id=?", (pid,))}
    all_stops: list[str] = []
    for entry in req_dests:
        name = ((entry or {}).get("name") or "").strip()
        if not name:
            continue
        direction = (entry or {}).get("direction")
        stops: list[str] = []
        if direction is not None:
            try:
                stops = list((directions[int(direction)] or {}).get("stops") or [])
            except (ValueError, IndexError, TypeError):
                warnings.append(f"destination {name}: bad direction {direction!r} - no stops added")
                stops = []
        code = ((entry or {}).get("code") or "").strip()
        if not _check_code_shape(code) or code in used_codes or con.execute(
                "SELECT 1 FROM p_destinations WHERE program_id=? AND code=?", (pid, code)).fetchone():
            code = suggest_code(pid, used_codes)
        used_codes.add(code)
        via = ((entry or {}).get("via") or "")
        dest_text = ((entry or {}).get("dest") or name)
        did = str(uuid.uuid4())
        dpos = con.execute("SELECT COUNT(*) FROM p_destinations WHERE service_id=?", (sid,)).fetchone()[0]
        con.execute("INSERT INTO p_destinations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (did, org, pid, sid, code, name, dpos, None, via, dest_text,
                     json.dumps({}, separators=(",", ":"))))
        _insert_destination_page(con, org, pid, did, 1, via, dest_text)
        n_stops = 0
        for i, sname in enumerate(stops, 1):
            sname = (sname or "").strip()
            if not sname:
                continue
            all_stops.append(sname)
            con.execute("INSERT INTO p_stops VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), org, pid, did, i, sname, None,
                         None, None, 1 if i == len(stops) else 0))
            n_stops += 1
        if not stops:
            warnings.append(f"destination {name} ({code}): no stops - pick a timetable direction")
        made.append({"destination_id": did, "code": code, "name": name, "stops": n_stops,
                     "direction": direction})
    unique_stops = list(dict.fromkeys(all_stops))
    linked, pending = 0, []
    for sname in unique_stops:
        h = _find_clip_for_stop(con, org, _bt.spoken_stop_name(sname), sname)
        if h:
            con.execute("UPDATE p_stops SET audio=? WHERE program_id=? AND name=? "
                        "AND (audio IS NULL OR audio='')", (h, pid, sname))
            linked += 1
        else:
            pending.append(sname)
    con.execute("UPDATE programs SET updated_at=?, updated_by=? WHERE id=?", (_utcnow(), user_id, pid))
    con.commit()
    summary = {"ok": True, "service_id": sid, "service_number": line_name,
               "destinations": made, "warnings": warnings,
               "audio": {"unique_stops": len(unique_stops), "with_audio": linked,
                         "pending": len(pending), "voice_id": voice_id,
                         "speaker_id": speaker_id}}
    return summary, pending


@app.post("/api/bustimes/import")
def bustimes_import(body: dict, u: dict = Depends(current_user)):
    """Import a bustimes.org service into a program.

    Body: {program_id, bustimes_id, service_number?, service_name?,
           voice_id?, generate_audio=true,
           destinations?: [{name, via?, dest?, code?, direction?}]}
    Omits -> destinations default to the description parts, each mapped to
    its auto-detected timetable direction. Stops come from that direction's
    timetable block; stop audio is reused by name or generated with Piper.
    """
    pid = body.get("program_id", "")
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    try:
        bustimes_id = int(body.get("bustimes_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "bustimes_id (service ID) is required")
    voice_id = body.get("voice_id")
    gen_audio = body.get("generate_audio", True)
    con = db()
    speaker_id = _check_speaker(con, voice_id, org, body.get("speaker_id"))
    try:
        summary, pending = _do_bustimes_import_one(
            con, org, pid, u["id"], bustimes_id, voice_id, speaker_id,
            service_number=body.get("service_number"),
            service_name=body.get("service_name"),
            req_dests=body.get("destinations"), gen_audio=gen_audio)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"bustimes.org lookup failed: {e}")
    base = {k: summary[k] for k in ("ok", "service_id", "service_number", "destinations", "warnings")}
    if gen_audio and voice_id and pending:
        job = _job_create(org, pid, summary["service_id"], summary["service_number"], len(pending))
        _threading.Thread(target=_bustimes_audio_worker,
                          args=(job["job_id"], org, pid, voice_id, pending, speaker_id),
                          daemon=True).start()
        base.update(state="working", job_id=job["job_id"],
                    audio={"unique_stops": summary["audio"]["unique_stops"],
                           "with_audio": summary["audio"]["with_audio"],
                           "pending": len(pending), "voice_id": voice_id, "speaker_id": speaker_id,
                           "message": f"service imported; generating audio for {len(pending)} stop(s) in the background"})
        return base
    silent = pending
    warnings = list(summary["warnings"])
    if gen_audio and not voice_id and pending:
        warnings.append("no voice selected - stops stay silent until you pick a voice and re-run audio generation")
    base.update(state="done", job_id=None,
                audio={"unique_stops": summary["audio"]["unique_stops"],
                       "with_audio": summary["audio"]["with_audio"],
                       "silent": silent, "voice_id": voice_id, "speaker_id": speaker_id, "errors": []},
                warnings=warnings)
    return base


@app.post("/api/bustimes/import-all")
def bustimes_import_all(body: dict, u: dict = Depends(current_user)):
    """Import every bustimes.org service returned by an operator/search query.

    Body: {program_id, operator?, search?, voice_id?, speaker_id?, generate_audio=true}
    Services are imported sequentially; one background audio job is queued for
    all unique missing stop clips. Pagination is capped at 10 pages (~250 svcs).
    """
    from . import bustimes as _bt
    pid = body.get("program_id", "")
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    operator = (body.get("operator") or "").strip()
    search = (body.get("search") or "").strip()
    if not operator and not search:
        raise HTTPException(400, "operator or search is required")
    voice_id = body.get("voice_id")
    gen_audio = body.get("generate_audio", True)
    con = db()
    speaker_id = _check_speaker(con, voice_id, org, body.get("speaker_id"))

    services: list[dict] = []
    page = 1
    while page <= 10:
        try:
            data = _bt.fetch_services(operator=operator, search=search, page=page)
        except Exception as e:
            raise HTTPException(502, f"bustimes.org lookup failed: {e}")
        results = data.get("results") or []
        services.extend(results)
        if not data.get("next") or not results:
            break
        page += 1
    if not services:
        raise HTTPException(400, "no services found for that operator/search")

    imported: list[dict] = []
    skipped: list[dict] = []
    all_pending: list[str] = []
    for svc in services:
        svc_id = svc.get("id")
        if not svc_id:
            skipped.append({"reason": "missing service id"})
            continue
        try:
            summary, pending = _do_bustimes_import_one(
                con, org, pid, u["id"], int(svc_id), voice_id, speaker_id, gen_audio=False)
            imported.append(summary)
            all_pending.extend(pending)
        except Exception as e:
            skipped.append({"id": svc_id, "line_name": svc.get("line_name"), "reason": str(e)})
    all_pending = list(dict.fromkeys(all_pending))

    base = {"state": "done", "job_id": None,
            "imported": imported, "skipped": skipped,
            "audio": {"unique_stops": len(all_pending), "with_audio": 0,
                      "pending": len(all_pending), "voice_id": voice_id,
                      "speaker_id": speaker_id}}
    if gen_audio and voice_id and all_pending:
        job = _job_create(org, pid, "", "all services", len(all_pending))
        _threading.Thread(target=_bustimes_audio_worker,
                          args=(job["job_id"], org, pid, voice_id, all_pending, speaker_id),
                          daemon=True).start()
        base.update(state="working", job_id=job["job_id"],
                    audio={"unique_stops": len(all_pending), "with_audio": 0,
                           "pending": len(all_pending), "voice_id": voice_id,
                           "speaker_id": speaker_id,
                           "message": f"imported {len(imported)} service(s); generating audio for {len(all_pending)} stop(s)"})
    return base


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
    for t, cols in (("displays", None), ("services", None), ("destinations", None), ("destination_pages", None),
                    ("stops", None),
                    ("layouts", None), ("layout_templates", None), ("override_presets", None),
                    ("gpio_rules", None)):
        try:
            rows = list(src.execute(f"SELECT * FROM {t}"))
        except Exception:
            continue
        for r in rows:
            d = dict(r)
            o = d.get("id")
            if o:
                d2 = str(uuid.uuid4()); idmap[o] = d2; d["id"] = d2
            for k in ("service_id", "destination_id", "display_id"):
                if k in d and d[k] in idmap:
                    d[k] = idmap[d[k]]
            dst = {"displays": "program_displays", "services": "p_services", "destinations": "p_destinations",
                   "destination_pages": "p_destination_pages",
                   "stops": "p_stops", "layouts": "p_layouts", "layout_templates": "p_templates",
                   "override_presets": "p_presets", "gpio_rules": "p_gpio_rules"}[t]
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
            elif t == "destination_pages":
                con.execute("INSERT INTO p_destination_pages VALUES (?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["destination_id"], d["position"],
                             d.get("via", ""), d.get("dest", "")))
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
            elif t == "gpio_rules":
                con.execute("INSERT INTO p_gpio_rules (id, organisation_id, program_id, label, pin,"
                            " trigger, effect, target, board_text, board_source, destination_id, audio,"
                            " duration_s, pull, active_low, debounce_ms, position)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d.get("label", ""), d["pin"], d["trigger"],
                             d.get("effect", "overlay"), d.get("target", "all"), d.get("board_text", ""),
                             d.get("board_source", "text"), d.get("destination_id"),
                             d.get("audio"), d.get("duration_s", 0), d.get("pull", "up"),
                             d.get("active_low", 1), d.get("debounce_ms", 50), d.get("position", 0)))
            else:
                con.execute("INSERT INTO p_presets VALUES (?,?,?,?,?,?)",
                            (d["id"], organisation_id, nid, d["label"], d["text"], d["position"]))
    con.commit()
    src.close()
    return {"program_id": nid}
