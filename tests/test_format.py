import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import demo_builder  # noqa: E402
from nslprog import check_file, content_hash_of_file, validate_file  # noqa: E402


def test_roundtrip_identical_hash(tmp_path):
    b1 = demo_builder()
    p1 = str(tmp_path / "a.nslprog")
    i1 = b1.build(p1, 1)
    b2 = demo_builder()
    p2 = str(tmp_path / "b.nslprog")
    i2 = b2.build(p2, 1)
    assert i1["content_hash"] == i2["content_hash"]
    assert content_hash_of_file(p1) == content_hash_of_file(p2)


def test_app_id_and_version(tmp_path):
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 3)
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    assert con.execute("PRAGMA application_id").fetchone()[0] == 0x4E534C50
    assert con.execute("PRAGMA user_version").fetchone()[0] == 1
    con.close()


def test_unsupported_version_refused(tmp_path):
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 1)
    con = sqlite3.connect(p)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    res = check_file(p)
    assert not res.ok and any("format version" in e.message for e in res.errors)


def test_unknown_optional_json_ignored(tmp_path):
    import json
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 1)
    con = sqlite3.connect(p)
    row = con.execute("SELECT id, layout FROM layouts LIMIT 1").fetchone()
    lj = json.loads(row[1])
    lj["future_thing"] = {"nested": [1, 2, 3]}
    lj["elements"][0]["future_flag"] = True
    con.execute("UPDATE layouts SET layout=? WHERE id=?", (json.dumps(lj), row[0]))
    con.commit()
    con.close()
    res = validate_file(p, check_hash=False)
    assert res.ok, [e.message for e in res.errors]


def test_dup_code_blocked(tmp_path):
    from nslprog import ProgramBuilder
    b = ProgramBuilder(program_name="t")
    f = b.add_display("front", 240, 40, 0)
    s = b.add_service("1", position=0)
    d1 = b.add_destination(s, "10000001", "A", dest="A", position=0)
    b.add_stop(d1, 1, "Only", is_terminus=True)
    d2 = b.add_destination(s, "10000001", "B", dest="B", position=1)
    b.add_stop(d2, 1, "Only", is_terminus=True)
    try:
        b.build(str(tmp_path / "x.nslprog"), 1)
    except ValueError as e:
        assert "10000001" in str(e) or "duplicate" in str(e).lower() or "UNIQUE" in str(e)
    else:
        raise AssertionError("dup code should block publish")


def test_short_codes_build_and_validate(tmp_path):
    from nslprog import ProgramBuilder
    from nslprog import validate_file
    b = ProgramBuilder(program_name="t")
    b.add_display("front", 240, 40, 0)
    s = b.add_service("1", position=0)
    for i, code in enumerate(["40101", "12105", "8070001", "4"]):
        d = b.add_destination(s, code, f"D{i}", dest=f"D{i}", position=i)
        b.add_stop(d, 1, "Only", is_terminus=True)
    p = str(tmp_path / "short.nslprog")
    b.build(p, 1)
    assert validate_file(p).ok
    # 9 digits and non-digits still blocked
    for bad in ["123456789", "12A45", ""]:
        b2 = ProgramBuilder(program_name="t")
        b2.add_display("front", 240, 40, 0)
        s2 = b2.add_service("1", position=0)
        d = b2.add_destination(s2, bad, "Bad", dest="BAD", position=0)
        b2.add_stop(d, 1, "Only", is_terminus=True)
        try:
            b2.build(str(tmp_path / "bad.nslprog"), 1)
        except ValueError:
            pass
        else:
            raise AssertionError(f"bad code {bad!r} should block publish")


def test_stop_gap_and_terminus_rules(tmp_path):
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 1)
    con = sqlite3.connect(p)
    dest = con.execute("SELECT id FROM destinations LIMIT 1").fetchone()[0]
    con.execute("DELETE FROM stops WHERE destination_id=? AND position=2", (dest,))
    con.commit()
    con.close()
    res = validate_file(p, check_hash=False)
    assert any(e.code == "stop_gap" for e in res.errors)


def test_missing_audio_error_and_warnings(tmp_path):
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 1)
    con = sqlite3.connect(p)
    con.execute("DELETE FROM audio WHERE hash=(SELECT audio FROM stops LIMIT 1)")
    con.commit()
    con.close()
    res = validate_file(p, check_hash=False)
    assert any(e.code == "missing_audio" for e in res.errors)


def test_layout_overflow_override_flag(tmp_path):
    import json
    import sqlite3
    p = str(tmp_path / "v.nslprog")
    demo_builder().build(p, 1)
    con = sqlite3.connect(p)
    row = con.execute("SELECT id, layout FROM layouts LIMIT 1").fetchone()
    lj = json.loads(row[1])
    # push the first element partly off the right edge
    lj["elements"][0]["x"] = 230
    lj["elements"][0]["w"] = 240
    con.execute("UPDATE layouts SET layout=? WHERE id=?", (json.dumps(lj), row[0]))
    con.commit()
    con.close()
    res = validate_file(p, check_hash=False)
    assert not res.ok and any(e.code == "layout_overflow" for e in res.errors)
    # opt out per layout: same geometry passes as a warning
    con = sqlite3.connect(p)
    lj["allow_overflow"] = True
    con.execute("UPDATE layouts SET layout=? WHERE id=?", (json.dumps(lj), row[0]))
    con.commit()
    con.close()
    res = validate_file(p, check_hash=False)
    assert res.ok, [e.message for e in res.errors]
    assert any(e.code == "layout_overflow" for e in res.warnings)
