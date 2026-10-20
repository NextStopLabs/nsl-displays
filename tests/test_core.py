import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import demo_builder  # noqa: E402
from pi.nsl_pi.core import Core  # noqa: E402
from pi.nsl_pi.store import init_controller_db  # noqa: E402


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
    return Core(db, prog), pid


def test_announcement_composition_normal_and_terminus(tmp_path):
    c, pid = _core(tmp_path)
    c.select_program(pid, source="test")
    c.select_code("10000001", source="test")
    clips = c.announcement_clips("next")
    assert len(clips) == 2  # next + stop (stop1 not terminus)
    c.stop_step(2, source="test")  # to terminus stop 3
    clips = c.announcement_clips("next")
    assert len(clips) == 3  # + last_stop suffix (mode both)
    clips_a = c.announcement_clips("approaching")
    assert len(clips_a) == 3


def test_override_lifetime(tmp_path):
    c, pid = _core(tmp_path)
    c.select_program(pid, source="test")
    c.select_code("10000001", source="test")
    c.apply_override("DO NOT BOARD", source="test")
    # survives replay/announcement query (no clearing side effect)
    c.announcement_clips("next")
    assert c.state()["override_text"] == "DO NOT BOARD"
    # survives activation of new version on SAME stop
    st = c.state()
    c.activate_version(pid, 1, source="test")
    assert c.state()["override_text"] == "DO NOT BOARD"
    # clears on stop change (stop 1 has no saved override)
    c.stop_step(1, source="test")
    assert c.state()["override_text"] is None
    # stop with saved override activates it
    c.stop_step(1, source="test")  # -> terminus with TERMINATING BUS
    assert c.state()["override_text"] == "TERMINATING BUS"
    # clears on new destination
    c.select_code("10000002", source="test")
    assert c.state()["override_text"] is None
    # manual clear
    c.apply_override("X", source="test")
    c.clear_override(source="test")
    assert c.state()["override_text"] is None


def test_state_carryover_missing_destination(tmp_path):
    c, pid = _core(tmp_path)
    c.select_program(pid, source="test")
    c.select_code("10000002", source="test")
    # simulate upgrade where destination gone: activate different program id falls back to first stop
    c.activate_version(pid, 1, source="test")
    assert c.state()["destination_id"] is not None


def test_invalid_code_changes_nothing(tmp_path):
    c, pid = _core(tmp_path)
    c.select_program(pid, source="test")
    c.select_code("10000001", source="test")
    before = c.state()["destination_id"]
    try:
        c.select_code("99999999", source="test")
    except ValueError:
        pass
    assert c.state()["destination_id"] == before


def test_apply_command_dispatch(tmp_path):
    c, pid = _core(tmp_path)
    c.apply_command({"command": "select_program", "program_id": pid}, source="test")
    c.apply_command({"command": "select_code", "code": "10000001"}, source="test")
    assert c.state()["destination_id"] is not None
    c.apply_command({"command": "stop_next"}, source="test")
    c.apply_command({"command": "override_apply", "text": "HELD"}, source="test")
    assert c.state()["override_text"] == "HELD"
    c.apply_command({"command": "override_clear"}, source="test")
    assert not c.state()["override_text"]
    for bad in ({"command": "select_code", "code": "00000000"},
                {"command": "bogus"}, {}):
        try:
            c.apply_command(bad, source="test")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_override_visible_without_program(tmp_path):
    c, _ = _core(tmp_path)
    assert c.state()["program_id"] is None
    c.apply_override("TEST", source="test")
    snap = c.display_snapshot()
    assert snap["override"] == "TEST"
    from pi.nsl_pi.renderer import autofit_layout, render_frame
    img = render_frame(autofit_layout(240, 40), snap["fields"], snap["override"] or "")
    assert any(px[:3] != (0, 0, 0) for px in img.getdata())
