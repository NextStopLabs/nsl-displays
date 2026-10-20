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
import time
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import connect

APP_VERSION = "1.0.0"
JWT_SECRET = os.environ.get("PORTAL_SECRET", "dev-secret-change-me")
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")
BASE_PATH = "/portal"

app = FastAPI(title="NSL Portal", root_path=BASE_PATH)


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db() -> sqlite3.Connection:
    con = getattr(app.state, "_db", None)
    if con is None:
        con = connect()
        app.state._db = con
    return con


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


# ---------- controllers & pairing ----------
@app.get("/api/controllers")
def list_controllers(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    rows = db().execute("SELECT * FROM controllers WHERE organisation_id=?", (organisation_id,)).fetchall()
    return {"controllers": [dict(r) for r in rows]}


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
    con.execute("UPDATE controllers SET organisation_id=?, name=?, device_token_hash=?, status='paired' WHERE id=?",
                (organisation_id, name, th, row["id"]))
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
    # audio referenced
    audio_needed: dict[str, dict] = {}
    for a in con.execute("SELECT * FROM audio_files WHERE organisation_id=?", (p["organisation_id"],)):
        audio_needed[a["sha256"]] = dict(a)
    for a in audio_needed.values():
        b.add_audio(a["sha256"], a["filename"], a["duration_ms"] or 1000, 1000)
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
    from nslprog import content_hash_of_file
    con = db()
    p = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    nxt = (p["published_version"] or 0) + 1
    # build to temp version path first to compute content_hash
    out, info = _build_nslprog(pid, nxt, u["email"] if isinstance(u, dict) else "")
    latest = con.execute("SELECT content_hash FROM program_versions WHERE program_id=? ORDER BY version DESC LIMIT 1",
                         (pid,)).fetchone()
    if latest and latest[0] == info["content_hash"]:
        os.remove(out)
        return {"ok": True, "version": latest and nxt - 1, "unchanged": True, "content_hash": info["content_hash"]}
    # audio totals
    import sqlite3 as _s
    c2 = _s.connect(f"file:{out}?mode=ro", uri=True)
    acount, abytes = c2.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM audio").fetchone()
    c2.close()
    con.execute("INSERT INTO program_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), pid, nxt, f"programs/{pid}/{nxt}.nslprog",
                 info["bundle_sha256"], info["content_hash"], info["bytes"], acount, abytes,
                 body.get("notes", ""), u["id"], _utcnow()))
    con.execute("UPDATE programs SET published_version=?, updated_at=? WHERE id=?", (nxt, _utcnow(), pid))
    con.commit()
    total = info["bytes"]  # + audio computed at upload dialog via manifest
    return {"ok": True, "version": nxt, "content_hash": info["content_hash"],
            "bundle_sha256": info["bundle_sha256"], "bytes": info["bytes"],
            "audio_count": acount, "audio_bytes": abytes}


@app.post("/api/programs/{pid}/upload")
def upload(pid: str, body: dict, u: dict = Depends(current_user)):
    org = _org_of_program(pid)
    require_role(u["id"], org, "owner", "editor")
    con = db()
    p = con.execute("SELECT * FROM programs WHERE id=?", (pid,)).fetchone()
    ver = body.get("version") or p["published_version"]
    for cid in body.get("controllers", []):
        c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
        if not c or c["organisation_id"] != org:
            raise HTTPException(400, f"bad controller {cid}")
        con.execute("INSERT OR REPLACE INTO controller_programs VALUES (?,?,?,?,?,?,?)",
                    (cid, pid, ver, None, "pending", None, _utcnow()))
    con.commit()
    return {"ok": True, "version": ver, "queued": body.get("controllers", [])}


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


# ---------- audio + voices (upload / Piper stub) ----------
@app.post("/api/audio")
def upload_audio(organisation_id: str = Form(...), file: UploadFile = File(...),
                 u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor")
    raw = file.file.read()
    if len(raw) > 50 * 1024 * 1024:
        raise HTTPException(400, "file too large")
    h = hashlib.sha256(raw).hexdigest()
    # transcode stub: store as-is under hash name; real pipeline normalises to mono FLAC
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".flac")
    if not os.path.exists(sp):
        with open(sp, "wb") as f:
            f.write(raw)
    con = db()
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), organisation_id, h, file.filename, f"audio/{h[:2]}/{h}.flac",
                 1000, "upload", None, None, None, _utcnow()))
    con.commit()
    return {"sha256": h, "bytes": len(raw)}


@app.post("/api/audio/generate")
def generate_audio(body: dict, u: dict = Depends(current_user)):
    """Piper TTS stub: synthesises placeholder tone WAV (real Piper runs in tts worker)."""
    require_role(u["id"], body["organisation_id"], "owner", "editor")
    text = body.get("text", "")
    h = hashlib.sha256(f"tts:{body.get('voice_id')}:{text}".encode()).hexdigest()
    os.makedirs(os.path.join(STORAGE_DIR, "audio", h[:2]), exist_ok=True)
    sp = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".flac")
    if not os.path.exists(sp):
        import wave as _w
        import struct as _s
        with _w.open(sp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(_s.pack(f"<{22050}h", *[0] * 22050))  # 1 s silence placeholder
    con = db()
    con.execute("INSERT OR IGNORE INTO audio_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), body["organisation_id"], h, body.get("name", text[:40]),
                 f"audio/{h[:2]}/{h}.flac", 1000, "tts", text, body.get("voice_id"),
                 json.dumps({"speed": body.get("speed", 1.0)}), _utcnow()))
    con.commit()
    return {"sha256": h}


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
    open(os.path.join(STORAGE_DIR, "voices", vid, "config.json"), "wb").write(craw)
    con = db()
    con.execute("INSERT INTO voices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, organisation_id, name, f"voices/{vid}/model.onnx",
                 f"voices/{vid}/config.json", cfg.get("language", ""), cfg.get("sample_rate", 22050),
                 0, "", u["id"], len(mraw), _utcnow()))
    con.commit()
    return {"voice_id": vid, "sample_rate": cfg.get("sample_rate", 22050)}


@app.get("/api/voices")
def list_voices(organisation_id: str, u: dict = Depends(current_user)):
    require_role(u["id"], organisation_id, "owner", "editor", "viewer")
    rows = db().execute(
        "SELECT * FROM voices WHERE organisation_id IS NULL OR organisation_id=?", (organisation_id,)).fetchall()
    return {"voices": [dict(r) for r in rows]}


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
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(6))
    cid = str(uuid.uuid4())
    exp = datetime.now(timezone.utc).timestamp() + 15 * 60
    exp_s = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con.execute("INSERT INTO controllers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, None, "unpaired", None, "pending", None, "", "stable", "1",
                 code, exp_s, 1, _utcnow()))
    con.commit()
    return {"controller_id": cid, "pairing_code": code, "expires_at": exp_s}


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
    device_controller(authorization)
    path = os.path.join(STORAGE_DIR, "programs", pid, f"{v}.nslprog")
    if not os.path.exists(path):
        raise HTTPException(404, "no bundle")
    return FileResponse(path, filename=f"{v}.nslprog")


@app.get("/api/device/audio/{h}")
def device_audio(h: str, authorization: str | None = Header(default=None)):
    device_controller(authorization)
    path = os.path.join(STORAGE_DIR, "audio", h[:2], h + ".flac")
    if not os.path.exists(path):
        raise HTTPException(404, "no clip")
    return FileResponse(path, filename=h + ".flac")


@app.post("/api/device/heartbeat")
def device_heartbeat(body: dict, authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    con = db()
    con.execute("UPDATE controllers SET last_seen_at=?, status='online', app_version=? WHERE id=?",
                (_utcnow(), body.get("app_version", ""), c["id"]))
    for inst in body.get("installed", []):
        con.execute("UPDATE controller_programs SET installed_version=?, status=?, updated_at=? WHERE controller_id=? AND program_id=?",
                    (inst.get("version"), inst.get("status", "installed"), _utcnow(), c["id"], inst.get("program_id")))
    for ev in body.get("events", []):
        con.execute("INSERT INTO command_log (controller_id, user_id, source, command, at) VALUES (?,?,?,?,?)",
                    (c["id"], None, "device", json.dumps(ev)[:500], _utcnow()))
    con.commit()
    return {"accepted": True, "now": _utcnow()}


@app.get("/api/device/release")
def device_release(authorization: str | None = Header(default=None)):
    c = device_controller(authorization)
    row = db().execute("SELECT * FROM releases WHERE channel=? ORDER BY created_at DESC LIMIT 1",
                       (c["channel"] or "stable",)).fetchone()
    if not row:
        return {"version": None}
    return {"version": row["version"], "bundle_key": row["bundle_key"], "signature": row["signature"]}


# remote command fan-out: portal queues, Pi polls via WS / heartbeat
COMMAND_BOX: dict[str, list] = {}


@app.post("/api/controllers/{cid}/command")
def remote_command(cid: str, body: dict, u: dict = Depends(current_user)):
    con = db()
    c = con.execute("SELECT * FROM controllers WHERE id=?", (cid,)).fetchone()
    if not c:
        raise HTTPException(404, "no controller")
    require_role(u["id"], c["organisation_id"], "owner", "editor")
    COMMAND_BOX.setdefault(cid, []).append({"command": body, "from": u["id"], "at": _utcnow()})
    con.execute("INSERT INTO command_log (controller_id, user_id, source, command, at) VALUES (?,?,?,?,?)",
                (cid, u["id"], "portal", json.dumps(body)[:500], _utcnow()))
    con.commit()
    return {"queued": True}


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
        await ws.send_json({"hello": "ok", "pending": COMMAND_BOX.get(cid, [])})
        COMMAND_BOX[cid] = []
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == "state":
                await ws.send_json({"ack": msg.get("seq", 0)})
            cmds = COMMAND_BOX.get(cid, [])
            if cmds:
                await ws.send_json({"commands": cmds})
                COMMAND_BOX[cid] = []
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
FRONT = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(FRONT):
    app.mount("/static", StaticFiles(directory=FRONT), name="static")


@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}
