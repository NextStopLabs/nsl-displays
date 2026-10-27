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
    assert snap["fields"]["via"] == "Lichfield Bus Station"
    assert snap["fields"]["dest"] == "Burton New Street"
    assert snap["pages"] == []  # single page, no rotation


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
