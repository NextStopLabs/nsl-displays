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
    return TestClient(appmod.app), appmod


def test_split_description_cases():
    from portal.backend.bustimes import split_description
    assert split_description("Fradley - The Friary School") == ["Fradley", "The Friary School"]
    assert split_description("Lichfield City Circular") == ["Lichfield City Circular"]
    assert split_description("Tamworth - Lichfield - St. Francis of Assisi") == \
        ["Tamworth", "Lichfield", "St. Francis of Assisi"]
    assert split_description("Burton upon Trent - Uttoxeter") == ["Burton upon Trent", "Uttoxeter"]
    assert split_description("") == []


def test_parse_timetable_two_directions():
    from portal.backend.bustimes import parse_timetable_csv
    text = ("stop,NaPTAN code,ATCO code,X66,X66\r\n"
            "A,a1,t1,07:25,08:25\r\nB,b1,t2,07:26,08:26\r\n\r\n"
            "stop,NaPTAN code,ATCO code,X66\r\nB,b1,t2,08:00\r\nA,a1,t1,08:05\r\n")
    dirs = parse_timetable_csv(text)
    assert [d["stops"] for d in dirs] == [["A", "B"], ["B", "A"]]


def test_bustimes_import_service(tmp_path, monkeypatch):
    c, appmod = _client(tmp_path, monkeypatch)
    import portal.backend.bustimes as btmod
    r = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]

    btmod.fetch_service_detail = lambda sid: {
        "id": 84914, "line_name": "SFA", "slug": "s",
        "description": "Tamworth - Lichfield - St. Francis of Assisi"}
    btmod.fetch_timetable = lambda sid: [
        {"stops": ["Perry Crofts", "Mid Point", "St Francis College"], "trips": 1},
        {"stops": ["St Francis College", "Mid Point", "Perry Crofts"], "trips": 1}]
    btmod.fetch_services = lambda **kw: {"count": 1, "next": None, "previous": None, "results": [
        {"id": 84914, "line_name": "SFA", "slug": "s",
         "description": "Tamworth - Lichfield - St. Francis of Assisi"}]}

    def fake_piper(text, voice_id, org2):
        import struct
        wav = appmod._wav_bytes_from_pcm(struct.pack("<2205h", *([1000] * 2205)))
        return (wav, 100), ""
    appmod._try_piper_wav = fake_piper

    rs = c.get("/api/bustimes/services", params={"operator": "BULI"}, headers=H).json()
    assert rs["results"][0]["destinations"] == ["Tamworth", "Lichfield", "St. Francis of Assisi"]
    rd = c.get("/api/bustimes/services/84914", headers=H).json()
    assert len(rd["directions"]) == 2 and rd["directions"][0]["count"] == 3

    ri = c.post("/api/bustimes/import", json={
        "program_id": pid, "bustimes_id": 84914, "voice_id": "v1",
        "destinations": [{"name": "Tamworth", "direction": 1},
                         {"name": "St. Francis of Assisi", "direction": 0}]}, headers=H)
    assert ri.status_code == 200, ri.text
    j = ri.json()
    assert len(j["destinations"]) == 2
    assert j["destinations"][0]["code"] != j["destinations"][1]["code"]
    assert j["audio"]["with_audio"] == 3  # Perry Crofts, Mid Point, St Francis College

    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert prog["services"][0]["number"] == "SFA"
    d0 = prog["services"][0]["destinations"][0]
    assert [s["name"] for s in d0["stops"]] == ["St Francis College", "Mid Point", "Perry Crofts"]
    assert d0["stops"][-1]["is_terminus"] == 1
    assert all(s["audio"] for s in d0["stops"])
    # publish validates with the imported rows
    pub = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H)
    assert pub.status_code == 200, pub.text
