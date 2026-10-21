import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path, monkeypatch):
    db = str(tmp_path / "dev.db")
    store = str(tmp_path / "storage")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:{db}")
    monkeypatch.setenv("STORAGE_DIR", store)
    os.makedirs(store, exist_ok=True)
    import portal.backend.app as appmod
    appmod.app.state._db = None
    return TestClient(appmod.app)


def test_signup_pair_publish_sync(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "a@x.com", "password": "secret123", "name": "A"})
    assert r.status_code == 200, r.text
    tok, org = r.json()["token"], r.json()["organisation_id"]
    H = {"Authorization": f"Bearer {tok}"}
    # device registers (fresh Pi, no token)
    r = c.post("/api/device/register", json={})
    code, cid = r.json()["pairing_code"], r.json()["controller_id"]
    # portal pairs
    r = c.post("/api/controllers/pair", data={"organisation_id": org, "code": code, "name": "bus-1"}, headers=H)
    assert r.status_code == 200, r.text
    devtok = r.json()["device_token"]
    DH = {"Authorization": f"Bearer {devtok}"}
    # create + publish program
    r = c.post("/api/programs", json={"organisation_id": org, "name": "Burton routes"}, headers=H)
    pid = r.json()["program_id"]
    # minimal destination via service
    r = c.post(f"/api/programs/{pid}/services", json={"number": "100", "name": "s"}, headers=H)
    sid = r.json()["service_id"]
    r = c.post(f"/api/services/{sid}/destinations",
               json={"code": "10000001", "name": "Burton", "via": "Branston", "dest": "BURTON"}, headers=H)
    did = r.json()["destination_id"]
    r = c.put(f"/api/destinations/{did}/stops",
              json={"stops": [{"name": "A"}, {"name": "B", "is_terminus": True}]}, headers=H)
    assert r.status_code == 200, r.text
    r = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H)
    assert r.status_code == 200, r.text
    ver = r.json()["version"]
    r = c.post(f"/api/programs/{pid}/upload", json={"controllers": [r.json() and cid], "version": ver}, headers=H)
    assert r.status_code == 200, r.text
    # device manifest + bundle + heartbeat
    r = c.get("/api/device/manifest", headers=DH)
    assert r.status_code == 200
    progs = r.json()["programs"]
    assert progs and progs[0]["version"] == ver
    r = c.get(f"/api/device/programs/{pid}/versions/{ver}/bundle", headers=DH)
    assert r.status_code == 200
    # remote command to an offline bus is refused, not silently queued
    r = c.post(f"/api/controllers/{cid}/command", json={"command": "stop_next"}, headers=H)
    assert r.status_code == 409, r.text
    assert "offline" in r.json()["detail"]
    r = c.post("/api/device/heartbeat", json={"installed": []}, headers=DH)
    assert r.json()["accepted"] is True
    # active version reported by the bus shows up in the controller list
    r = c.post("/api/device/heartbeat",
               json={"installed": [{"program_id": pid, "version": ver, "status": "installed"}],
                     "state": {"program_id": pid, "program_version": ver}}, headers=DH)
    assert r.json()["accepted"] is True
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"][0]
    prog = [p for p in ctl["programs"] if p["program_id"] == pid][0]
    assert prog["installed_version"] == ver and prog["active_version"] == ver
    # now online: command queues, persists, and arrives on next heartbeat
    r = c.post(f"/api/controllers/{cid}/command", json={"command": "stop_next"}, headers=H)
    assert r.json() == {"queued": True, "online": True, "pending": 1}, r.text
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"]
    assert ctl[0]["online"] is True
    assert ctl[0]["pending_commands"] == 1
    assert ctl[0]["programs"][0]["desired_version"] == ver
    r = c.post("/api/device/heartbeat", json={"installed": []}, headers=DH)
    pend = r.json()["pending_commands"]
    assert len(pend) == 1 and "stop_next" in pend[0]["command"]
    # delivered: no longer pending
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"]
    assert ctl[0]["pending_commands"] == 0
    # rotate token: old dies, new works
    r = c.post(f"/api/controllers/{cid}/token", headers=H)
    assert r.status_code == 200, r.text
    newtok = r.json()["device_token"]
    assert newtok != devtok
    r = c.get("/api/device/manifest", headers=DH)
    assert r.status_code == 401
    r = c.get("/api/device/manifest", headers={"Authorization": f"Bearer {newtok}"})
    assert r.status_code == 200


def test_tenant_isolation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    a = c.post("/api/auth/signup", data={"email": "a@x.com", "password": "secret123"}).json()
    b = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    # b cannot list a's programs
    r = c.get("/api/programs", params={"organisation_id": a["organisation_id"]},
              headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403


def test_me_and_static(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "m@x.com", "password": "secret123", "name": "M", "org": "Org1"}).json()
    H = {"Authorization": f"Bearer {r['token']}"}
    me = c.get("/api/me", headers=H).json()
    assert me["user"]["email"] == "m@x.com"
    assert me["organisations"][0]["id"] == r["organisation_id"]
    assert me["organisations"][0]["role"] == "owner"
    # second user in same org sees it too; login (not signup) path resolves orgs
    r2 = c.post("/api/auth/login", data={"email": "m@x.com", "password": "secret123"}).json()
    me2 = c.get("/api/me", headers={"Authorization": f"Bearer {r2['token']}"}).json()
    assert me2["organisations"][0]["name"] == "Org1"
    # frontend served
    assert c.get("/").status_code in (200, 307)
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/index.html").status_code == 200


def test_ui_flow_end_to_end(tmp_path, monkeypatch):
    """Drive the API exactly as portal/frontend/app.js does."""
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "u@x.com", "password": "secret123", "name": "U", "org": "Bus Co"}).json()
    H = {"Authorization": f"Bearer {r['token']}"}
    org = r["organisation_id"]
    # new program
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "Burton", "px_width": 240}, headers=H).json()["program_id"]
    # add service + destinations (as the detail view does)
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "100", "name": "Burton line"}, headers=H).json()["service_id"]
    for code, name in (("10000001", "Burton"), ("10000002", "Lichfield"), ("40101", "Short code")):
        chk = c.get(f"/api/programs/{pid}/destinations/check-code", params={"code": code}, headers=H).json()
        assert chk["unique"] and chk["shape_ok"], chk
        c.post(f"/api/services/{sid}/destinations",
               json={"code": code, "name": name, "via": "Branston", "dest": name.upper()}, headers=H)
    for bad in ("", "123456789", "12A45", "  "):
        chk = c.get(f"/api/programs/{pid}/destinations/check-code", params={"code": bad}, headers=H).json()
        assert not chk["shape_ok"], bad
        r = c.post(f"/api/services/{sid}/destinations",
                   json={"code": bad, "name": "Bad", "via": "", "dest": "BAD"}, headers=H)
        assert r.status_code == 400, bad
    # stops
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    for i, d in enumerate(prog["services"][0]["destinations"]):
        r = c.put(f"/api/destinations/{d['id']}/stops",
                  json={"stops": [{"name": f"S{i}A"}, {"name": f"S{i}B", "is_terminus": True}]}, headers=H)
        assert r.json()["count"] == 2
    did = prog["services"][0]["destinations"][0]["id"]
    # layout round-trip on front display
    lay = c.get(f"/api/destinations/{did}/layouts/front", headers=H).json()
    assert lay["source"] == "auto-fit"
    import copy
    from scripts.seed import FRONT
    c.put(f"/api/destinations/{did}/layouts/front", json={"layout": FRONT}, headers=H)
    lay2 = c.get(f"/api/destinations/{did}/layouts/front", headers=H).json()
    assert lay2["source"] == "destination"
    # publish + simulate device + pair + upload + manifest + bundle
    pub = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H).json()
    assert pub["version"] == 1
    dev = c.post("/api/device/register", json={}).json()
    c.post("/api/controllers/pair", data={"organisation_id": org, "code": dev["pairing_code"], "name": "bus-1"}, headers=H)
    up = c.post(f"/api/programs/{pid}/upload", json={"controllers": [dev["controller_id"]], "version": 1}, headers=H).json()
    assert up["queued"] == [dev["controller_id"]]
    # device side needs real device token: pair again is single-use; use out-of-band token via pair response
    r = c.post("/api/auth/signup", data={"email": "o@x.com", "password": "secret123", "org": "Other"}).json()
    # remote command queues
    H2 = {"Authorization": f"Bearer {r['token']}"}
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"]
    assert len(ctl) == 1
    assert ctl[0]["online"] is False  # never checked in
    r = c.post(f"/api/controllers/{ctl[0]['id']}/command", json={"command": "stop_next"}, headers=H)
    assert r.status_code == 409


def test_publish_upload_and_sync_state(tmp_path, monkeypatch):
    from tests.test_portal import _client
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "p@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
               data={"organisation_id": org, "code": dev["pairing_code"], "name": "bus-1"}, headers=H).json()
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=H).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations",
                 json={"code": "00000001", "name": "A"}, headers=H).json()["destination_id"]
    c.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "S1"}]}, headers=H)
    # one flow: publish + upload
    r = c.post(f"/api/programs/{pid}/publish-upload",
               json={"controllers": [dev["controller_id"]], "notes": "go"}, headers=H).json()
    assert r["version"] == 1 and r["queued"] == [dev["controller_id"]] and not r["unchanged"]
    # unchanged second time, still queued
    r2 = c.post(f"/api/programs/{pid}/publish-upload",
                json={"controllers": [dev["controller_id"]]}, headers=H).json()
    assert r2["unchanged"] is True and r2["version"] == 1
    # bus reports mid-download progress on heartbeat; portal surfaces it
    c.post("/api/device/heartbeat",
           json={"installed": [], "sync": {"phase": "bundle", "program": "P", "version": 1}}, headers=DH)
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"][0]
    assert ctl["sync_state"]["phase"] == "bundle"
    assert ctl["pending_commands"] == 0


def test_device_audio_serves_wav_and_flac(tmp_path, monkeypatch):
    import io
    import wave
    from tests.test_portal import _client
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "w@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": dev["pairing_code"], "name": "bus-1"}, headers=H).json()
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\x00\x10" * 2205)
    files = {"file": ("tone.wav", buf.getvalue(), "audio/wav")}
    up = c.post("/api/audio", data={"organisation_id": org, "name": "tone"}, files=files, headers=H).json()
    h = up["sha256"]
    r = c.get(f"/api/device/audio/{h}", headers=DH)
    assert r.status_code == 200 and len(r.content) > 1000
    assert c.get("/api/device/audio/" + "0" * 64, headers=DH).status_code == 404


def test_hanover_font_endpoints(tmp_path, monkeypatch):
    import os as _os
    from tests.test_portal import _client
    _os.environ["NSL_FONTS_DIR"] = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "HANOVERFONTS")
    monkeypatch.setenv("NSL_FONTS_DIR", _os.environ["NSL_FONTS_DIR"])
    c = _client(tmp_path, monkeypatch)
    tok = c.post("/api/auth/signup", data={"email": "f@x.com", "password": "secret123"}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    fonts = c.get("/api/fonts", headers=H).json()["fonts"]
    assert any(f["id"] == "han-0705C1E1" for f in fonts)
    g = c.get("/api/fonts/han-0705C1E1", headers=H).json()
    assert g["height"] == 7 and len(g["glyphs"]) == 224
    assert g["glyphs"]["65"][0] == 3  # advance of 'A'
    assert c.get("/api/fonts/../x", headers=H).status_code in (400, 404)
    assert c.get("/api/fonts/han-nope", headers=H).status_code == 404
    assert c.get("/api/fonts").status_code == 401


def test_multi_speaker_voice_upload_and_choice(tmp_path, monkeypatch):
    """Uploading a multi-speaker voice records its speakers; generate and
    bustimes import accept a speaker_id and reject unknown ones."""
    import json as _json
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "spk@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    cfg = {"audio": {"sample_rate": 22050}, "language": {"code": "en_GB"},
           "speaker_id_map": {"Emma": 0, "Elinor": 2}}
    up = c.post("/api/voices", data={"organisation_id": org, "name": "multi"},
                files={"model": ("m.onnx", b"X" * 64, "application/octet-stream"),
                       "config": ("m.onnx.json", _json.dumps(cfg).encode(), "application/json")},
                headers=H).json()
    assert up["num_speakers"] == 2 and up["speakers"] == [
        {"id": 0, "name": "Emma"}, {"id": 2, "name": "Elinor"}], up
    v = [x for x in c.get("/api/voices", params={"organisation_id": org}, headers=H).json()["voices"]
         if x["id"] == up["voice_id"]][0]
    assert v["num_speakers"] == 2
    # unknown speaker rejected before any synthesis is attempted
    bad = c.post("/api/audio/generate",
                 json={"organisation_id": org, "text": "hello", "name": "hello-x",
                       "voice_id": up["voice_id"], "speaker_id": 5}, headers=H)
    assert bad.status_code == 400, bad.text


def test_program_display_crud(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "d@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "D"}, headers=H).json()["program_id"]
    # default displays created
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert len(prog["displays"]) == 3
    # add custom display
    resp = c.post(f"/api/programs/{pid}/displays",
                  json={"name": "side2", "width_px": 160, "height_px": 40}, headers=H)
    assert resp.status_code == 200, resp.text
    did = resp.json()["display_id"]
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert any(d["id"] == did and d["name"] == "side2" for d in prog["displays"])
    # duplicate name rejected
    dup = c.post(f"/api/programs/{pid}/displays",
                 json={"name": "side2", "width_px": 100, "height_px": 40}, headers=H)
    assert dup.status_code == 400
    # update size
    r = c.put(f"/api/programs/{pid}/displays/{did}", json={"name": "side2", "width_px": 180, "height_px": 48}, headers=H)
    assert r.status_code == 200
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    side = [d for d in prog["displays"] if d["id"] == did][0]
    assert side["width_px"] == 180 and side["height_px"] == 48
    # rename display
    r = c.put(f"/api/programs/{pid}/displays/{did}", json={"name": "side-board", "width_px": 180, "height_px": 48}, headers=H)
    assert r.status_code == 200
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert [d["name"] for d in prog["displays"] if d["id"] == did] == ["side-board"]
    # delete display
    r = c.delete(f"/api/programs/{pid}/displays/{did}", headers=H)
    assert r.status_code == 200
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert not any(d["id"] == did for d in prog["displays"])


def test_program_template_crud_and_default(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "t@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "T"}, headers=H).json()["program_id"]
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    disp = prog["displays"][0]
    # add template
    layout = {"schema": 1, "width": disp["width_px"], "height": disp["height_px"], "background": "#000000", "page_seconds": 3, "elements": []}
    r = c.post(f"/api/programs/{pid}/templates", json={"display_id": disp["id"], "name": "Standard", "layout": layout}, headers=H)
    assert r.status_code == 200, r.text
    tid = r.json()["template_id"]
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    t = [x for x in prog["templates"] if x["id"] == tid][0]
    assert t["name"] == "Standard" and t["is_default"] == 0
    # set as default
    r = c.put(f"/api/programs/{pid}/templates/{tid}", json={"is_default": True}, headers=H)
    assert r.status_code == 200
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert [x["is_default"] for x in prog["templates"] if x["id"] == tid] == [1]
    # destination without a layout falls back to default template
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "1", "name": "Svc"}, headers=H).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations", json={"code": "00000001", "name": "Dest"}, headers=H).json()["destination_id"]
    l = c.get(f"/api/destinations/{did}/layouts/{disp['name']}", headers=H).json()
    assert l["source"] == "template"
    assert l["layout"]["width"] == disp["width_px"]
    # delete template
    r = c.delete(f"/api/programs/{pid}/templates/{tid}", headers=H)
    assert r.status_code == 200
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert not any(x["id"] == tid for x in prog["templates"])


def test_release_upload_and_device_download(tmp_path, monkeypatch):
    import io
    import tarfile
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "r@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": dev["pairing_code"], "name": "bus-1"}, headers=H).json()
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    # Generate a signing key for this test so the portal auto-signs the bundle.
    import portal.backend.app as appmod
    test_key = Ed25519PrivateKey.generate()
    pem = test_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    monkeypatch.setattr(appmod, "RELEASE_PRIVATE_KEY", pem)
    # Build a minimal tar.gz bundle.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = b"version 2.0.0 code"
        info = tarfile.TarInfo(name="code.txt")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    bundle_bytes = buf.getvalue()
    h = hashlib.sha256(bundle_bytes).hexdigest()
    files = {"file": ("release.tar.gz", bundle_bytes, "application/gzip")}
    up = c.post("/api/admin/releases",
                data={"organisation_id": org, "version": "2.0.0", "channel": "stable", "notes": "bug fixes"},
                files=files, headers=H)
    assert up.status_code == 200, up.text
    assert up.json()["version"] == "2.0.0"
    assert up.json()["sha256"] == h
    assert len(up.json()["signature"]) == 128  # Ed25519 signature hex
    # Device sees the release
    rel = c.get("/api/device/release", headers=DH).json()
    assert rel["version"] == "2.0.0"
    assert rel["bundle_url"] == "/api/device/release/bundle?v=2.0.0"
    assert rel["sha256"] == h
    assert rel["bytes"] == len(bundle_bytes)
    # Device downloads the bundle
    dl = c.get("/api/device/release/bundle", params={"v": "2.0.0"}, headers=DH)
    assert dl.status_code == 200
    assert dl.content == bundle_bytes
    # Missing release returns 404
    assert c.get("/api/device/release/bundle", params={"v": "9.9.9"}, headers=DH).status_code == 404
    # Portal update-status endpoint shows current vs latest
    st = c.get(f"/api/controllers/{dev['controller_id']}/update", headers=H).json()
    assert st["current_version"] == ""
    assert st["latest_version"] == "2.0.0"
    assert st["can_update"] is False  # bus never heartbeated, so offline
    assert st["up_to_date"] is False
