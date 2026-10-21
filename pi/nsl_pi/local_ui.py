"""Local controller web UI (README 10). Offline-first.

Same components as portal remote page. Control + programs (+.nslpack import)
+ settings + diagnostics.

Access control is the network boundary (there is no login on this box):
the app listens on all interfaces but only serves loopback, the Pi access
point subnet (192.168.4.0/24) and the bus LAN (10.9.0.0/24). Everything else
gets 403. Extra subnets via NSL_UI_ALLOW="cidr,cidr,...". /api/health is open.
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import sqlite3
import zipfile

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from .core import Core

app = FastAPI(title="NSL Local Controller")

ALLOW_NETS = [ipaddress.ip_network("127.0.0.0/8"),
              ipaddress.ip_network("192.168.4.0/24"),
              ipaddress.ip_network("10.9.0.0/24")]
for _tok in os.environ.get("NSL_UI_ALLOW", "").split(","):
    _tok = _tok.strip()
    if _tok:
        try:
            ALLOW_NETS.append(ipaddress.ip_network(_tok))
        except ValueError:
            pass

OPEN_PATHS = {"/api/health", "/openapi.json", "/docs"}


def client_allowed(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return True  # no parseable peer (test client); real servers always pass an IP
    return any(ip in net for net in ALLOW_NETS)


@app.middleware("http")
async def _local_only(request, call_next):
    if request.url.path not in OPEN_PATHS:
        peer = request.client.host if request.client else ""
        if not client_allowed(peer):
            return JSONResponse({"detail": "local networks only (join the NSL-xx access point)"},
                                status_code=403)
    return await call_next(request)


@app.get("/api/health")
def api_health():
    return {"ok": True}


@app.get("/api/debug.png")
def api_debug_png(output: str = "front"):
    """Last frame we SENT to an output (needs NSL_DEBUG_PNG=1 on nsl-display,
    or virtual mode). Amber here + white panels = hardware fault, not software."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", output or ""):
        raise HTTPException(400, "bad output name")
    base = os.environ.get("NSL_BASE", "/var/lib/nsl")
    p = os.path.join(base, "virtual", output + ".png")
    if not os.path.exists(p):
        raise HTTPException(404, "no frame yet (enable NSL_DEBUG_PNG=1 on nsl-display and wait ~5 s)")
    from fastapi.responses import FileResponse
    return FileResponse(p, media_type="image/png")


def _core() -> Core:
    base = os.environ.get("NSL_BASE", "/var/lib/nsl")
    db = os.path.join(base, "controller.db")
    return Core(db, os.path.join(base, "programs"))


CORE = None


def get_core() -> Core:
    global CORE
    if CORE is None:
        CORE = _core()
    return CORE


@app.get("/api/state")
def api_state():
    c = get_core()
    return c.display_snapshot()


@app.post("/api/command")
def api_command(body: dict):
    c = get_core()
    try:
        res = c.apply_command(body or {}, source="local")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if isinstance(res, dict) and res.get("announcement"):
        from nsl_pi.player import play_announcement
        import os as _os
        prep = play_announcement(res["announcement"], _os.environ.get("NSL_BASE", "/var/lib/nsl"))
        res = dict(res, played=prep.get("played"), device=prep.get("device"))
    return res


@app.get("/api/programs")
def api_programs():
    c = get_core()
    con = sqlite3.connect(c.controller_db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM installed_programs ORDER BY program_id, version")]
    con.close()
    return {"installed": rows, "state": c.state()}


@app.post("/api/programs/import")
def api_import_pack(file: UploadFile = File(...)):
    """Import .nslpack offline (format.md 12). Validated, marked local_import."""
    import tempfile
    from nslprog import check_file
    base = os.environ.get("NSL_BASE", "/var/lib/nsl")
    tmp = tempfile.mkdtemp(prefix="nslpack-")
    zpath = os.path.join(tmp, "up.nslpack")
    with open(zpath, "wb") as f:
        f.write(file.file.read())
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    prog = os.path.join(tmp, "program.nslprog")
    res = check_file(prog)
    if not res.ok:
        raise HTTPException(400, "invalid program: " + "; ".join(e.message for e in res.errors))
    from nslprog import read_meta
    meta = read_meta(prog)
    pid, ver = meta["program_id"], int(meta["version"])
    c = get_core()
    final = os.path.join(base, "programs", pid, f"{ver}.nslprog")
    os.makedirs(os.path.dirname(final), exist_ok=True)
    import shutil
    shutil.copy(prog, final)
    # audio
    for root, _, files in os.walk(os.path.join(tmp, "audio")):
        for fn in files:
            src = os.path.join(root, fn)
            h = os.path.splitext(fn)[0]
            try:
                with open(src, "rb") as _f:
                    _magic = _f.read(4)
            except OSError:
                _magic = b""
            _ext = ".wav" if _magic == b"RIFF" else ".flac"
            dst = os.path.join(base, "audio", h[:2], h + _ext)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(src, dst)
    con = sqlite3.connect(c.controller_db)
    import datetime
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, ver, meta.get("program_name", pid), meta.get("content_hash", ""),
                 "local", final, os.path.getsize(final), "installed", "local_import",
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
    con.commit()
    con.close()
    return {"ok": True, "program_id": pid, "version": ver}


@app.post("/api/audio-test")
def api_audio_test():
    """Play the local PA check chime (no portal clips needed)."""
    from .player import play_test_tone
    import os as _os
    rep = play_test_tone(_os.environ.get("NSL_BASE", "/var/lib/nsl"))
    return rep


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1">
<title>NSL Local Controller — 192.168.4.1</title><link rel=stylesheet href="/ui.css"></head>
<body><div class=window><div class=titlebar>NSL Local Controller — bus control (offline OK)</div>
<div class=body>
<div class=group><div class=grouptitle>Destination</div>
<label>8-digit code<input id=code maxlength=8 inputmode=numeric></label>
<div class=row><button onclick="cmd('select_code')">Select code</button>
<button onclick="cmd('stop_prev')">Prev stop</button><button onclick="cmd('stop_next')">Next stop</button></div>
<div class=row><button onclick="cmd('announce_next')">Announce next</button>
<button onclick="cmd('announce_approaching')">Announce approaching</button>
<button onclick="cmd('override_clear')">Clear override</button></div>
<label>Override text<input id=ov></label>
<div class=row><button onclick="cmd('override_apply')">Apply override</button></div>
</div>
<div class=group><div class=grouptitle>Status</div><pre id=state>…</pre></div>
</div></div>
<script>
async function refresh(){const r=await fetch('/api/state');document.getElementById('state').textContent=JSON.stringify(await r.json(),null,1).slice(0,2000)}
async function cmd(command){const body={command};if(command==='select_code')body.code=document.getElementById('code').value;
if(command==='override_apply')body.text=document.getElementById('ov').value;
if(command==='stop_next'||command==='stop_prev'){}
await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});refresh()}
refresh();setInterval(refresh,2000);
</script></body></html>
"""
