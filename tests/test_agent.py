import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

from nslprog import read_meta  # noqa: E402
from scripts.seed import demo_builder  # noqa: E402


def _agent(tmp_path, monkeypatch):
    from pi.nsl_pi.agent import Agent
    from pi.nsl_pi.store import init_controller_db
    base = tmp_path / "nsl"
    db = str(base / "controller.db")
    init_controller_db(db)
    return Agent("http://portal.invalid", str(base), token="t"), db, str(base / "programs")


def test_sync_auto_selects_first_install(tmp_path, monkeypatch):
    ag, db, _ = _agent(tmp_path, monkeypatch)
    p = str(tmp_path / "seed.nslprog")
    demo_builder().build(p, 1)
    pid = read_meta(p)["program_id"]
    import shutil
    monkeypatch.setattr(ag, "fetch_manifest",
                        lambda *a: {"programs": [{"program_id": pid, "version": 1,
                                                 "program_name": "Demo",
                                                 "bundle": {"url": "/b", "sha256": "x", "bytes": 1}}]})
    monkeypatch.setattr(ag, "download", lambda url, dest, sha: shutil.copy(p, dest))
    rep = ag.sync_once(db)
    assert rep["programs"] == [f"{pid}@1"]
    assert rep.get("activated"), rep
    import sqlite3
    con = sqlite3.connect(db)
    st = con.execute("SELECT program_id, program_version FROM state WHERE id=1").fetchone()
    con.close()
    assert st[0] == pid and st[1] == 1


def test_sync_leaves_existing_state_alone(tmp_path, monkeypatch):
    from pi.nsl_pi.core import Core
    ag, db, progs = _agent(tmp_path, monkeypatch)
    p = str(tmp_path / "seed.nslprog")
    demo_builder().build(p, 1)
    pid = read_meta(p)["program_id"]
    import shutil
    # pre-select something else by hand: state parked on another program
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE state SET program_id='other', program_version=9 WHERE id=1")
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("other", 9, "Other", "x", "y", "/nowhere", 1, "installed", "portal",
                 "2026-01-01T00:00:00Z", None))
    con.commit()
    con.close()
    monkeypatch.setattr(ag, "fetch_manifest",
                        lambda *a: {"programs": [{"program_id": pid, "version": 1,
                                                 "program_name": "Demo",
                                                 "bundle": {"url": "/b", "sha256": "x", "bytes": 1}}]})
    monkeypatch.setattr(ag, "download", lambda url, dest, sha: shutil.copy(p, dest))
    rep = ag.sync_once(db)
    assert not rep.get("activated"), rep
    con = sqlite3.connect(db)
    st = con.execute("SELECT program_id FROM state WHERE id=1").fetchone()
    con.close()
    assert st[0] == "other"
