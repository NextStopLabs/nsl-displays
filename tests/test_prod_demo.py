import io
import os
import sys
import wave

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


def _wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x10\x20" * 2205)
    return buf.getvalue()


def test_pair_code_single_use_and_claim(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "a@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    code, cid = dev["pairing_code"], dev["controller_id"]
    # Pi polls before pairing
    assert c.post("/api/device/claim", json={"controller_id": cid}).json()["paired"] is False
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": code, "name": "bus-1"}, headers=H)
    assert pr.status_code == 200, pr.text
    # Pi claims its token
    claim = c.post("/api/device/claim", json={"controller_id": cid}).json()
    assert claim["paired"] is True and claim["device_token"] == pr.json()["device_token"]
    # token is one-time
    assert c.post("/api/device/claim", json={"controller_id": cid}).json()["paired"] is False
    # code cannot be reused
    r2 = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": code, "name": "bus-2"}, headers=H)
    assert r2.status_code == 400


def test_publish_only_bundles_referenced_audio(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    raw = _wav_bytes()
    raw2 = _wav_bytes().replace(b"\x10\x20", b"\x20\x10", 1)
    h1 = c.post("/api/audio", data={"organisation_id": org, "name": "used-clip"},
                files={"file": ("a.wav", raw, "audio/wav")}, headers=H).json()["sha256"]
    h2 = c.post("/api/audio", data={"organisation_id": org, "name": "unused-clip"},
                files={"file": ("b.wav", raw2, "audio/wav")}, headers=H).json()["sha256"]
    assert h1 != h2
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=H).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations",
                 json={"code": "00000001", "name": "A"}, headers=H).json()["destination_id"]
    c.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "S1", "audio": h1}]}, headers=H)
    pub = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H).json()
    assert pub["audio_count"] == 1, pub
    assert pub["audio_bytes"] > 0


def test_device_bundle_and_audio_tenant_isolation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    a = c.post("/api/auth/signup", data={"email": "a@x.com", "password": "secret123"}).json()
    b = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    HA, HB = {"Authorization": f"Bearer {a['token']}"}, {"Authorization": f"Bearer {b['token']}"}
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": a["organisation_id"], "code": dev["pairing_code"], "name": "bus"},
                headers=HA).json()
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    pid = c.post("/api/programs", json={"organisation_id": a["organisation_id"], "name": "P"},
                 headers=HA).json()["program_id"]
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=HA).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations",
                 json={"code": "00000001", "name": "A"}, headers=HA).json()["destination_id"]
    c.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "S1"}]}, headers=HA)
    ver = c.post(f"/api/programs/{pid}/publish", json={}, headers=HA).json()["version"]
    # not queued for this bus -> 404, not a leak
    assert c.get(f"/api/device/programs/{pid}/versions/{ver}/bundle", headers=DH).status_code == 404
    # org B's bus cannot fetch org A's bundle even if it guesses the pid
    dev2 = c.post("/api/device/register", json={}).json()
    pr2 = c.post("/api/controllers/pair",
                 data={"organisation_id": b["organisation_id"], "code": dev2["pairing_code"], "name": "bus2"},
                 headers=HB).json()
    DH2 = {"Authorization": f"Bearer {pr2['device_token']}"}
    assert c.get(f"/api/device/programs/{pid}/versions/{ver}/bundle", headers=DH2).status_code == 404
    # audio isolation: clip in org A invisible to org B's device
    raw = _wav_bytes()
    h = c.post("/api/audio", data={"organisation_id": a["organisation_id"], "name": "clip"},
               files={"file": ("a.wav", raw, "audio/wav")}, headers=HA).json()["sha256"]
    assert c.get(f"/api/device/audio/{h}", headers=DH).status_code == 200
    assert c.get(f"/api/device/audio/{h}", headers=DH2).status_code == 404


def test_stops_reject_empty_names(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "s@x.com", "password": "secret123"}).json()
    H = {"Authorization": f"Bearer {r['token']}"}
    pid = c.post("/api/programs", json={"organisation_id": r["organisation_id"], "name": "P"},
                 headers=H).json()["program_id"]
    sid = c.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=H).json()["service_id"]
    did = c.post(f"/api/services/{sid}/destinations",
                 json={"code": "00000001", "name": "A"}, headers=H).json()["destination_id"]
    res = c.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "  "}]}, headers=H)
    assert res.status_code == 400


def test_handshake_claim_and_remove(tmp_path, monkeypatch):
    from tests.test_portal import _client
    c = _client(tmp_path, monkeypatch)
    r = c.post("/api/auth/signup", data={"email": "h@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    dev = c.post("/api/device/register", json={}).json()
    pr = c.post("/api/controllers/pair",
                data={"organisation_id": org, "code": dev["pairing_code"], "name": "test"},
                headers=H).json()
    # portal shows waiting-for-Pi until the Pi claims
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"]
    assert ctl[0]["awaiting_pi"] is True
    claim = c.post("/api/device/claim", json={"controller_id": dev["controller_id"]}).json()
    assert claim["paired"] is True and claim["device_token"] == pr["device_token"]
    ctl = c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"]
    assert ctl[0]["awaiting_pi"] is False
    # claimed token actually works (the reported failure mode was endless 401s)
    DH = {"Authorization": f"Bearer {pr['device_token']}"}
    assert c.post("/api/device/heartbeat", json={"installed": []}, headers=DH).json()["accepted"] is True
    assert c.get("/api/device/manifest", headers=DH).status_code == 200
    # remove deletes the row (and its programs mapping)
    assert c.delete(f"/api/controllers/{dev['controller_id']}", headers=H).json() == {"ok": True}
    assert c.get("/api/controllers", params={"organisation_id": org}, headers=H).json()["controllers"] == []
    assert c.get("/api/device/manifest", headers=DH).status_code == 401


def test_pairing_layout_never_overlaps():
    from pi.nsl_pi.display_main import pairing_frame, pairing_layout
    for w, h in ((240, 40), (160, 40), (80, 40)):
        rows = pairing_layout(w, h, "KQ7Z2M", "nsldisplays.com")
        assert len(rows) >= 2
        spans = [(y, y + th) for _, _, y, th in rows]
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            assert a1 + 1 <= b0, f"overlap on {w}x{h}: {spans}"
        assert spans[-1][1] <= h and spans[0][0] >= 0
        img = pairing_frame({"width_px": w, "height_px": h}, "KQ7Z2M", "nsldisplays.com")
        assert img.size == (w, h)
        assert sum(1 for px in img.getdata() if px[:3] != (0, 0, 0)) > 50
