import os
import sys
import time

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


def _fake_wav(appmod, seed_text="x"):
    import struct
    fill = 1000 + (abs(hash(seed_text)) % 20000)
    return appmod._wav_bytes_from_pcm(struct.pack("<2205h", *([fill] * 2205)))


def _mock_bustimes(btmod):
    btmod.cache_clear()
    btmod.fetch_service_detail = lambda sid: {
        "id": 84914, "line_name": "SFA", "slug": "s",
        "description": "Tamworth - Lichfield - St. Francis of Assisi"}
    btmod.fetch_timetable = lambda sid: [
        {"stops": ["Perry Crofts", "Mid Point", "St Francis College"], "trips": 1},
        {"stops": ["St Francis College", "Mid Point", "Perry Crofts"], "trips": 1}]
    btmod.fetch_services = lambda **kw: {"count": 1, "next": None, "previous": None, "results": [
        {"id": 84914, "line_name": "SFA", "slug": "s",
         "description": "Tamworth - Lichfield - St. Francis of Assisi"}]}


def _wait_job(c, jid, H, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        job = c.get(f"/api/bustimes/jobs/{jid}", headers=H).json()
        assert job["state"] in ("working", "done", "error"), job
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {jid} never finished")


def test_split_description_cases():
    from portal.backend.bustimes import split_description
    assert split_description("Fradley - The Friary School") == ["Fradley", "The Friary School"]
    assert split_description("Lichfield City Circular") == ["Lichfield City Circular"]
    assert split_description("Tamworth - Lichfield - St. Francis of Assisi") == \
        ["Tamworth", "Lichfield", "St. Francis of Assisi"]
    assert split_description("Burton upon Trent - Uttoxeter") == ["Burton upon Trent", "Uttoxeter"]
    assert split_description("") == []


def test_spoken_stop_name():
    from portal.backend.bustimes import spoken_stop_name as s
    # place + qualifier stripped, full stop added
    assert s("Tutbury, adj St Christopher Church") == "St Christopher Church."
    assert s("Bolehall, opp Kettlebrook Road") == "Kettlebrook Road."
    assert s("Christ Church, in The Friary School") == "The Friary School."
    assert s("Stonydelph, nr Crowden Road") == "Crowden Road."
    # no comma: kept whole, period added
    assert s("Lichfield Bus Station (Stand 2)") == "Lichfield Bus Station (Stand 2)."
    assert s("St Christopher Church") == "St Christopher Church."
    # existing terminal punctuation respected
    assert s("Foo.") == "Foo."
    assert s("Bar, opp Foo!") == "Foo!"
    assert s("") == ""
    # trailing qualifier parentheticals stripped, informative ones kept
    assert s("Shobnall Street (opp)") == "Shobnall Street."
    assert s("Netherstowe Lane (opp)") == "Netherstowe Lane."
    assert s("Glascote Primary Academy (adj)") == "Glascote Primary Academy."
    assert s("Lichfield Bus Station (Stand 2)") == "Lichfield Bus Station (Stand 2)."
    assert s("Foo (Stand 1) (opp)") == "Foo (Stand 1)."
    # full qualifier family, not just adj/opp (surveyed from bustimes data)
    assert s("Market Place, at Post Office") == "Post Office."
    assert s("High Street, opposite Tesco") == "Tesco."
    assert s("London Road, before Station") == "Station."
    assert s("London Road, after Station") == "Station."
    assert s("Station, o/s Entrance") == "Entrance."
    assert s("X, next to Library") == "Library."
    assert s("X, for Town Centre") == "Town Centre."
    assert s("Station, O/S Entrance") == "Entrance."
    assert s("Foo, OPP Bar") == "Bar."
    assert s("Foo (at)") == "Foo."
    assert s("Foo (before Bar)") == "Foo."
    assert s("Depot (inward)") == "Depot (inward)."
    # capitalised real words are kept (could be a name)
    assert s("X, By Station Road") == "By Station Road."
    # bare qualifier with nothing after: fall back to the head
    assert s("Foo, opp") == "Foo."
    # "PH" (public house) is spoken as "pub", never spelled out
    assert s("Freeford, adj Horse and Jockey PH") == "Horse and Jockey pub."
    assert s("Station (PH)") == "Station (pub)."
    assert s("Phoenix Road") == "Phoenix Road."


def test_parse_timetable_two_directions():
    from portal.backend.bustimes import parse_timetable_csv
    text = ("stop,NaPTAN code,ATCO code,X66,X66\r\n"
            "A,a1,t1,07:25,08:25\r\nB,b1,t2,07:26,08:26\r\n\r\n"
            "stop,NaPTAN code,ATCO code,X66\r\nB,b1,t2,08:00\r\nA,a1,t1,08:05\r\n")
    dirs = parse_timetable_csv(text)
    assert [d["stops"] for d in dirs] == [["A", "B"], ["B", "A"]]


def test_bustimes_import_service_with_background_audio(tmp_path, monkeypatch):
    """Import returns immediately (working + job_id); audio fills in behind,
    with progress visible on the job endpoint and a confirmation payload."""
    c, appmod = _client(tmp_path, monkeypatch)
    import portal.backend.bustimes as btmod
    _mock_bustimes(btmod)

    def fake_synth(texts, voice_id, org, speaker_id=None):
        return {t: ((_fake_wav(appmod, t), 100), "") for t in texts}
    monkeypatch.setattr(appmod, "_synth_many_piper", fake_synth)

    r = c.post("/api/auth/signup", data={"email": "b@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]

    rs = c.get("/api/bustimes/services", params={"operator": "BULI"}, headers=H).json()
    assert rs["results"][0]["destinations"] == ["Tamworth", "Lichfield", "St. Francis of Assisi"]
    rd = c.get("/api/bustimes/services/84914", headers=H).json()
    assert len(rd["directions"]) == 2 and rd["directions"][0]["count"] == 3

    # progress is observable while the job runs
    ri = c.post("/api/bustimes/import", json={
        "program_id": pid, "bustimes_id": 84914, "voice_id": "v1",
        "destinations": [{"name": "Tamworth", "direction": 1},
                         {"name": "St. Francis of Assisi", "direction": 0}]}, headers=H)
    assert ri.status_code == 200, ri.text
    j = ri.json()
    assert j["state"] == "working" and j["job_id"], j
    assert j["audio"]["pending"] == 3  # Perry Crofts, Mid Point, St Francis College
    job = _wait_job(c, j["job_id"], H)
    assert job["state"] == "done", job
    assert job["done"] == job["total"] == 3, job
    assert job["result"]["generated"] == 3, job["result"]

    prog = c.get(f"/api/programs/{pid}", headers=H).json()
    assert prog["services"][0]["number"] == "SFA"
    d0 = prog["services"][0]["destinations"][0]
    assert [s["name"] for s in d0["stops"]] == ["St Francis College", "Mid Point", "Perry Crofts"]
    assert d0["stops"][-1]["is_terminus"] == 1
    assert all(s["audio"] for s in d0["stops"])
    # clips use the spoken form: slug + tts text, with trailing period
    clips = c.get("/api/audio", params={"organisation_id": org}, headers=H).json()["clips"]
    by_name = {a["name"]: a for a in clips}
    assert "st-francis-college" in by_name, sorted(by_name)
    assert "mid-point" in by_name and "perry-crofts" in by_name
    # re-import reuses the clips: no new job, nothing pending
    ri2 = c.post("/api/bustimes/import", json={
        "program_id": pid, "bustimes_id": 84914, "voice_id": "v1",
        "destinations": [{"name": "Tamworth-again", "direction": 1}]}, headers=H).json()
    assert ri2["state"] == "done" and ri2["job_id"] is None, ri2
    # publish validates with the imported rows
    pub = c.post(f"/api/programs/{pid}/publish", json={"notes": "v1"}, headers=H)
    assert pub.status_code == 200, pub.text


def test_bustimes_import_no_audio_is_synchronous(tmp_path, monkeypatch):
    c, appmod = _client(tmp_path, monkeypatch)
    import portal.backend.bustimes as btmod
    _mock_bustimes(btmod)
    r = c.post("/api/auth/signup", data={"email": "n@x.com", "password": "secret123"}).json()
    H, org = {"Authorization": f"Bearer {r['token']}"}, r["organisation_id"]
    pid = c.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H).json()["program_id"]
    ri = c.post("/api/bustimes/import", json={
        "program_id": pid, "bustimes_id": 84914, "generate_audio": False,
        "destinations": [{"name": "Tamworth", "direction": 1}]}, headers=H).json()
    assert ri["state"] == "done" and ri["job_id"] is None, ri
    assert ri["audio"]["silent"] == ["St Francis College", "Mid Point", "Perry Crofts"] or \
        sorted(ri["audio"]["silent"]) == sorted(["Perry Crofts", "Mid Point", "St Francis College"])


def test_synth_many_falls_back_to_cli_without_piper_package(tmp_path, monkeypatch):
    """No `piper` Python package in this env -> parallel CLI fallback, one
    call per text (no per-stop model-load serialisation in our code)."""
    c, appmod = _client(tmp_path, monkeypatch)
    calls = []

    def fake_cli(text, model, config, speaker_id=None):
        calls.append(text)
        return ((_fake_wav(appmod), 100), "")

    monkeypatch.setattr(appmod, "_piper_cli_wav", fake_cli)
    monkeypatch.setattr(appmod, "_resolve_piper_voice",
                        lambda vid, org: ("m.onnx", "m.onnx.json", ""))
    out = appmod._synth_many_piper(["Alpha Stop", "Beta Stop"], "v1", "org")
    assert set(out) == {"Alpha Stop", "Beta Stop"}
    assert all(v[0] is not None for v in out.values()), out
    assert sorted(calls) == ["Alpha Stop", "Beta Stop"]
