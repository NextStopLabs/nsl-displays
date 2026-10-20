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
    for code, name in (("10000001", "Burton"), ("10000002", "Lichfield")):
        chk = c.get(f"/api/programs/{pid}/destinations/check-code", params={"code": code}, headers=H).json()
        assert chk["unique"] and chk["shape_ok"], chk
        c.post(f"/api/services/{sid}/destinations",
               json={"code": code, "name": name, "via": "Branston", "dest": name.upper()}, headers=H)
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
