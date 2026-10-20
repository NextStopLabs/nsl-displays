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
    r = c.post("/api/device/heartbeat", json={"installed": []}, headers=DH)
    assert r.json()["accepted"] is True
    # remote command queued
    r = c.post(f"/api/controllers/{cid}/command", json={"command": "stop_next"}, headers=H)
    assert r.json()["queued"] is True


def test_tenant_isolation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    a = c.post("/api/auth/signup", data={"email": "a@x.com", "password": "secret123"}).json()
    b = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    # b cannot list a's programs
    r = c.get("/api/programs", params={"organisation_id": a["organisation_id"]},
              headers={"Authorization": f"Bearer {b['token']}"})
    assert r.status_code == 403
