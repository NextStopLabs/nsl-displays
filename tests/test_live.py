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


def _setup(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "l@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": dev["pairing_code"], "name": "bus-1"}, headers=H).json()
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "100", "name": "Line"}, headers=H).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations",
                 json={"code": "10000001", "name": "Burton", "dest": "BURTON"}, headers=H).json()["destination_id"]
    c.put(f"/api/destinations/{did}/stops",
          json={"stops": [{"name": "Alpha"}, {"name": "Beta"}, {"name": "Gamma", "is_terminus": True}]}, headers=H)
    pub = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H).json()
    assert pub["version"] == 1
    c.post(f"/api/programs/{pid}/upload",
           json={"controllers": [dev["controller_id"]], "version": 1}, headers=H)
    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    dest = prog["services"][0]["destinations"][0]
    return c, H, DH, org, dev["controller_id"], pid, sid, dest


def test_live_shows_current_stop(tmp_path, monkeypatch):
    c, H, DH, org, cid, pid, sid, dest = _setup(tmp_path, monkeypatch)
    stops = dest["stops"]
    # bus checks in running destination, sitting at the 2nd stop with an override
    r = c.post("/api/device/heartbeat", json={
        "installed": [{"program_id": pid, "version": 1, "status": "installed"}],
        "state": {"program_id": pid, "program_version": 1, "service_id": sid,
                  "destination_id": dest["id"], "stop_id": stops[1]["id"],
                  "override_text": "DO NOT BOARD", "volume": 60, "muted": 0}}, headers=DH)
    assert r.json()["accepted"] is True
    live = c.get(f"/api/controllers/{cid}/live", headers=H).json()
    assert live["online"] is True
    assert live["program"]["name"] == "P" and live["program"]["version"] == 1
    assert live["service"]["number"] == "100"
    assert live["destination"]["code"] == "10000001"
    assert live["stop"]["position"] == 2 and live["stop"]["count"] == 3
    assert live["stop"]["name"] == "Beta"
    assert live["next_stop"]["name"] == "Gamma"
    assert live["override"] == "DO NOT BOARD"
    assert live["volume"] == 60 and live["muted"] is False
    # controller list carries the raw live state too
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"][0]
    assert ctl["live_state"]["stop_id"] == stops[1]["id"]
    # bus advances a stop -> next heartbeat moves the panel
    c.post("/api/device/heartbeat", json={
        "installed": [],
        "state": {"program_id": pid, "program_version": 1, "service_id": sid,
                  "destination_id": dest["id"], "stop_id": stops[2]["id"],
                  "override_text": None, "volume": 60, "muted": 0}}, headers=DH)
    live2 = c.get(f"/api/controllers/{cid}/live", headers=H).json()
    assert live2["stop"]["name"] == "Gamma"
    assert live2["stop"]["is_terminus"] is True
    assert live2["next_stop"] is None
    assert live2["override"] is None


def test_live_idle_and_isolation(tmp_path, monkeypatch):
    c, H, DH, org, cid, pid, sid, dest = _setup(tmp_path, monkeypatch)
    # paired but never reported state: idle, no program
    live = c.get(f"/api/controllers/{cid}/live", headers=H).json()
    assert live["program"] is None and live["stop"] is None
    # another org cannot see it
    o = c.post("/api/auth/signup", data={"email": "o@x.com", "password": "secret123"}).json()
    r = c.get(f"/api/controllers/{cid}/live",
              headers={"Authorization": f"Bearer {o['token']}"})
    assert r.status_code == 403
    assert c.get(f"/api/controllers/does-not-exist/live", headers=H).status_code == 404
