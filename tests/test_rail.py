import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import demo_builder  # noqa: E402
from pi.nsl_pi.core import Core  # noqa: E402
from pi.nsl_pi.store import init_controller_db  # noqa: E402
from pi.nsl_pi.input_keypad import T9Tap, encode_t9, t9_letters  # noqa: E402


def _core(tmp_path):
    base = str(tmp_path / "nsl")
    os.makedirs(base, exist_ok=True)
    db = os.path.join(base, "controller.db")
    init_controller_db(db)
    prog = os.path.join(base, "programs")
    p = str(tmp_path / "seed.nslprog")
    demo_builder().build(p, 1)
    import shutil
    pid = __import__("nslprog").read_meta(p)["program_id"]
    dest = os.path.join(prog, pid, "1.nslprog")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(p, dest)
    import sqlite3
    con = sqlite3.connect(db)
    import datetime
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, 1, "Burton routes", "x", "y", dest, 10, "active", "portal",
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
    con.commit()
    con.close()
    c = Core(db, prog)
    c.select_program(pid, source="test")
    c.select_code("10000001", source="test")  # 3 stops: Lichfield / Branston / Burton
    stops = c.browse()["services"][0]["destinations"][0]["stops"]
    return c, stops


def test_browse_includes_stops(tmp_path):
    _, stops = _core(tmp_path)
    assert [s["position"] for s in stops] == [1, 2, 3]
    assert stops[0]["name"] == "Lichfield Bus Station"
    assert all("board_text" in s for s in stops)


def test_set_rail_window_and_snapshot(tmp_path):
    c, stops = _core(tmp_path)
    c.set_rail(stops[0]["id"], stops[2]["id"], source="test")
    st = c.state()
    assert st["rail_from_stop_id"] == stops[0]["id"]
    assert st["rail_to_stop_id"] == stops[2]["id"]
    assert st["stop_id"] == stops[0]["id"]  # jumps to window start
    snap = c.display_snapshot()
    assert snap["rail"]["from_name"] == "Lichfield Bus Station"
    assert snap["rail"]["to_name"] == "Burton New Street"
    # via rotates origin + calling points, dest stays on the terminus
    assert snap["rail"]["calling"] == ["Lichfield Bus Station", "Branston Road"]
    assert [p["via"] for p in snap["pages"]] == ["Lichfield Bus Station", "Branston Road"]
    assert {p["dest"] for p in snap["pages"]} == {"Burton New Street"}
    assert snap["fields"]["via"] == "Lichfield Bus Station"
    assert snap["fields"]["dest"] == "Burton New Street"


def test_set_rail_auto_swaps_reversed(tmp_path):
    c, stops = _core(tmp_path)
    c.set_rail(stops[2]["id"], stops[0]["id"], source="test")
    st = c.state()
    assert (st["rail_from_stop_id"], st["rail_to_stop_id"]) == (stops[0]["id"], stops[2]["id"])


def test_set_rail_rejects_bad_stops(tmp_path):
    c, stops = _core(tmp_path)
    for bad in [("nope", stops[0]["id"]), (stops[0]["id"], "nope"),
                (stops[0]["id"], stops[0]["id"][:5])]:
        try:
            c.set_rail(*bad, source="test")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")
    assert c.state()["rail_from_stop_id"] is None


def test_rail_clamps_stop_step_and_goto(tmp_path):
    c, stops = _core(tmp_path)
    c.set_rail(stops[0]["id"], stops[1]["id"], source="test")
    c.stop_step(5, source="test")  # clamped at window end
    assert c.state()["stop_id"] == stops[1]["id"]
    c.stop_step(-5, source="test")  # clamped at window start
    assert c.state()["stop_id"] == stops[0]["id"]
    try:
        c.goto_stop(stops[2]["id"], source="test")
    except ValueError:
        pass
    else:
        raise AssertionError("goto outside window should fail")
    assert c.state()["stop_id"] == stops[0]["id"]


def test_rail_cleared_on_destination_change(tmp_path):
    c, stops = _core(tmp_path)
    c.set_rail(stops[0]["id"], stops[2]["id"], source="test")
    c.select_code("10000002", source="test")
    st = c.state()
    assert st["rail_from_stop_id"] is None and st["rail_to_stop_id"] is None
    assert c.display_snapshot()["rail"] is None


def test_rail_command_dispatch_and_clear(tmp_path):
    c, stops = _core(tmp_path)
    c.apply_command({"command": "rail_set", "from_stop_id": stops[1]["id"],
                     "to_stop_id": stops[2]["id"]}, source="test")
    assert c.display_snapshot()["rail"]["from_name"] == "Branston Road"
    c.apply_command({"command": "rail_clear"}, source="test")
    assert c.display_snapshot()["rail"] is None


def test_t9_faceplate_mapping():
    assert t9_letters("7") == "STU" and t9_letters("9") == "YZ" and t9_letters("0") == " "
    assert encode_t9("SOT") == "757"  # 7=STU 5=MNO 7=STU
    assert encode_t9("MAN") == "515"


def test_t9_multitap_cycles_and_commits():
    t = T9Tap()
    assert t.press("7", now=0.0) == "S"
    assert t.press("7", now=0.2) == "T"  # same key fast = cycle
    assert t.press("7", now=0.4) == "U"
    assert t.press("7", now=0.6) == "7"  # wraps past the group
    t2 = T9Tap()
    t2.press("7", now=0.0)
    assert t2.press("5", now=5.0) == "SM"  # pause commits, new key starts O-group
    assert t2.finish() == "SM"
    assert t2.backspace() == "S"


def _crs_core(tmp_path):
    """Harness with a rail destination whose stops carry CRS codes."""
    import datetime
    import shutil
    import sqlite3
    from nslprog import ProgramBuilder
    base = str(tmp_path / "nslcrs")
    os.makedirs(base, exist_ok=True)
    db = os.path.join(base, "controller.db")
    init_controller_db(db)
    prog = os.path.join(base, "programs")
    b = ProgramBuilder(program_name="Rail")
    b.add_display("front", 240, 40, 0)
    svc = b.add_service("RR", "Rail replacement", 0)
    d = b.add_destination(svc, "8001", "Rail replacement", dest="RAIL REPLACEMENT", position=0)
    b.add_stop(d, 1, "Stoke-on-Trent", crs="SOT")
    b.add_stop(d, 2, "Stockport", crs="SPT")
    b.add_stop(d, 3, "Manchester Piccadilly", crs="MAN", is_terminus=True)
    p = str(tmp_path / "rail.nslprog")
    b.build(p, 1)
    pid = __import__("nslprog").read_meta(p)["program_id"]
    dest = os.path.join(prog, pid, "1.nslprog")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(p, dest)
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, 1, "Rail", "x", "y", dest, 10, "active", "portal",
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
    con.commit()
    con.close()
    c = Core(db, prog)
    c.select_program(pid, source="test")
    c.select_code("8001", source="test")
    return c


def test_browse_exposes_crs(tmp_path):
    c = _crs_core(tmp_path)
    stops = c.browse()["services"][0]["destinations"][0]["stops"]
    assert [(s["crs"], s["position"]) for s in stops] == [("SOT", 1), ("SPT", 2), ("MAN", 3)]


def test_rail_snapshot_carries_crs(tmp_path):
    c = _crs_core(tmp_path)
    stops = c.browse()["services"][0]["destinations"][0]["stops"]
    sot = next(s for s in stops if s["crs"] == "SOT")
    man = next(s for s in stops if s["crs"] == "MAN")
    c.set_rail(sot["id"], man["id"], source="test")
    snap = c.display_snapshot()
    assert snap["rail"]["from_crs"] == "SOT" and snap["rail"]["to_crs"] == "MAN"
    # Euston -> Stoke shape: via steps origin + middles, dest is the terminus
    assert [p["via"] for p in snap["pages"]] == ["Stoke-on-Trent", "Stockport"]
    assert all(p["dest"] == "Manchester Piccadilly" for p in snap["pages"])


def test_crs_validation_rules(tmp_path):
    import pytest as _pytest
    from nslprog import ProgramBuilder, validate_file

    def _build(stop_rows, path):
        b = ProgramBuilder(program_name="t")
        b.add_display("front", 240, 40, 0)
        s = b.add_service("1", position=0)
        d = b.add_destination(s, "1", "D", dest="D", position=0)
        for i, kw in enumerate(stop_rows, 1):
            b.add_stop(d, i, kw.get("name", f"S{i}"), crs=kw.get("crs"),
                       is_terminus=(i == len(stop_rows)))
        return b.build(path, 1)

    _build([{"crs": "SOT"}, {"crs": "MAN"}], str(tmp_path / "ok.nslprog"))
    assert validate_file(str(tmp_path / "ok.nslprog"), check_hash=False).ok
    with _pytest.raises(ValueError):  # bad shape blocked at publish
        _build([{"crs": "SOT1"}, {"crs": "MAN"}], str(tmp_path / "bad.nslprog"))
    _build([{"crs": "sot"}, {}], str(tmp_path / "low.nslprog"))  # lowercase normalised
    assert validate_file(str(tmp_path / "low.nslprog"), check_hash=False).ok
    with _pytest.raises(ValueError):  # duplicate CRS in one destination
        _build([{"crs": "SOT"}, {"crs": "SOT"}], str(tmp_path / "dup.nslprog"))


def test_old_bundle_without_crs_still_works(tmp_path):
    import sqlite3
    c = _crs_core(tmp_path)
    # find the installed bundle and drop the crs column to simulate v1 files
    con = sqlite3.connect(c.controller_db)
    row = con.execute("SELECT path FROM installed_programs LIMIT 1").fetchone()
    con.close()
    bc = sqlite3.connect(row[0])
    try:
        bc.execute("ALTER TABLE stops DROP COLUMN crs")
        bc.commit()
    except Exception:
        bc.close()
        return  # old sqlite without DROP COLUMN: nothing to simulate
    bc.close()
    stops = c.browse()["services"][0]["destinations"][0]["stops"]
    assert all("crs" not in s for s in stops)
    c.set_rail(stops[0]["id"], stops[2]["id"], source="test")
    snap = c.display_snapshot()
    assert snap["rail"]["from_name"] == "Stoke-on-Trent"
    assert snap["rail"]["from_crs"] == ""
