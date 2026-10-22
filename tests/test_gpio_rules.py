import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pi.nsl_pi.gpio import GpioDispatcher, GpioRulesEngine  # noqa: E402


def _rules():
    return [
        {"id": "door-open", "label": "Door", "pin": 5, "trigger": "while_active",
         "effect": "overlay", "target": "all", "board_text": "DOORS OPEN",
         "audio": None, "duration_s": 0, "pull": "up", "active_low": 1,
         "debounce_ms": 50, "position": 0},
        {"id": "door-shut", "label": "Door", "pin": 5, "trigger": "on_release",
         "effect": "overlay", "target": "all", "board_text": "MIND THE DOORS",
         "audio": "clip1", "duration_s": 5, "pull": "up", "active_low": 1,
         "debounce_ms": 50, "position": 1},
        {"id": "rev", "label": "Reverse", "pin": 6, "trigger": "while_active",
         "effect": "overlay", "target": "rear", "board_text": "REVERSING",
         "audio": None, "duration_s": 0, "pull": "up", "active_low": 0,
         "debounce_ms": 50, "position": 2},
        {"id": "btn", "label": "Button", "pin": 12, "trigger": "press",
         "effect": "override", "target": "all", "board_text": "HELLO",
         "audio": None, "duration_s": 0, "pull": "up", "active_low": 1,
         "debounce_ms": 50, "position": 3},
    ]


def test_engine_edges_and_latched():
    e = GpioRulesEngine(_rules())
    # rising edge on door pin fires nothing one-shot (while_active is latched)
    assert e.level_event(5, True, now=100.0) == []
    assert [r["id"] for r in e.active_overlays()] == ["door-open"]
    # falling edge fires on_release, clears latched
    fired = e.level_event(5, False, now=100.5)
    assert [r["id"] for r in fired] == ["door-shut"]
    assert e.active_overlays() == []
    # no edge, no fire
    assert e.level_event(5, False, now=101.0) == []


def test_engine_press_timing():
    e = GpioRulesEngine(_rules())
    e.level_event(12, True, now=200.0)
    fired = e.level_event(12, False, now=200.2)
    assert [r["id"] for r in fired] == ["btn"]
    # long hold: no press rule matches trigger long (btn is press-only)
    e.level_event(12, True, now=300.0)
    assert e.level_event(12, False, now=301.0) == []


def test_engine_active_high_pin():
    e = GpioRulesEngine(_rules())
    assert e.pin_config(6)["active_low"] is False
    e.level_event(6, True, now=100.0)
    assert [r["id"] for r in e.active_overlays()] == ["rev"]


def test_old_dispatcher_untouched():
    ds = GpioDispatcher(
        [{"id": "b1", "pin": 17, "active_low": 1}],
        [{"id": "a1", "binding_id": "b1", "press_type": "press", "step_order": 0,
          "action": "stop_next", "action_param": None}])
    assert ds.edge(17, 0, now=100.0) == []
    assert ds.edge(17, 1, now=100.2)[0]["action"] == "stop_next"


def _bundle_with_rules(tmp_path):
    from scripts.seed import demo_builder
    b = demo_builder()
    b.add_gpio_rule(label="Door", pin=5, trigger="while_active", effect="overlay",
                    target="all", board_text="DOORS OPEN", position=0)
    b.add_gpio_rule(label="Door", pin=5, trigger="on_release", effect="overlay",
                    target="all", board_text="MIND THE DOORS", duration_s=5, position=1)
    b.add_gpio_rule(label="Bell", pin=12, trigger="press", effect="override",
                    target="all", board_text="HELLO", position=2)
    p = str(tmp_path / "gpio.nslprog")
    b.build(p, 1)
    return p


def test_bundle_roundtrip_and_validate(tmp_path):
    from nslprog import check_file
    p = _bundle_with_rules(tmp_path)
    res = check_file(p)
    assert res.ok, [e.message for e in res.errors]


def test_bundle_rejects_bad_target(tmp_path):
    from scripts.seed import demo_builder
    b = demo_builder()
    b.add_gpio_rule(label="X", pin=5, trigger="press", target="nosuchdisplay",
                    board_text="HI", position=0)
    p = str(tmp_path / "badt.nslprog")
    try:
        b.build(p, 1)
    except ValueError as ex:
        assert "target" in str(ex).lower() or "display" in str(ex).lower()
        return
    from nslprog import check_file
    res = check_file(p)
    assert not res.ok


def _bundle_with_board_rule(tmp_path):
    from scripts.seed import demo_builder
    b = demo_builder()
    dest_id = next(d["id"] for d in b.destinations if d["code"] == "10000002")
    b.add_gpio_rule(label="Reversing", pin=16, trigger="while_active", effect="overlay",
                    target="all", board_source="destination", destination_id=dest_id,
                    position=0)
    p = str(tmp_path / "gpio-board.nslprog")
    b.build(p, 1)
    return p, dest_id


def test_bundle_board_rule_roundtrip(tmp_path):
    import sqlite3
    from nslprog import check_file
    p, dest_id = _bundle_with_board_rule(tmp_path)
    res = check_file(p)
    assert res.ok, [e.message for e in res.errors]
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT board_source, destination_id FROM gpio_rules").fetchone()
    finally:
        con.close()
    assert row[0] == "destination" and row[1] == dest_id


def test_bundle_rejects_unknown_board_destination(tmp_path):
    from scripts.seed import demo_builder
    b = demo_builder()
    b.add_gpio_rule(label="X", pin=5, trigger="press", board_source="destination",
                    destination_id="nope", position=0)
    import pytest
    with pytest.raises(ValueError, match="unknown destination"):
        b.build(str(tmp_path / "badb.nslprog"), 1)


def _core_with_board_rule(tmp_path):
    from pi.nsl_pi.core import Core
    from pi.nsl_pi.store import init_controller_db
    base = str(tmp_path / "nslb")
    os.makedirs(base, exist_ok=True)
    db = os.path.join(base, "controller.db")
    init_controller_db(db)
    prog = os.path.join(base, "programs")
    p, dest_id = _bundle_with_board_rule(tmp_path)
    import shutil
    pid = __import__("nslprog").read_meta(p)["program_id"]
    dest = os.path.join(prog, pid, "1.nslprog")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(p, dest)
    import sqlite3
    import datetime
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, 1, "Burton routes", "x", "y", dest, 10, "active", "portal",
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
    con.commit()
    con.close()
    c = Core(db, prog)
    c.select_program(pid, source="test")
    return c, dest_id


def test_core_gpio_board_snapshot_and_restore(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c, dest_id = _core_with_board_rule(tmp_path)
    e = GpioRulesEngine(c.gpio_rules())
    e.level_event(16, True, now=100.0)
    c.sync_gpio_levels({16}, now=100.0)
    snap = c.display_snapshot()
    assert snap["gpio_overlay"] == {}
    assert set(snap["gpio_boards"]) == {"all"}
    board = snap["gpio_boards"]["all"]
    assert board["destination"]["id"] == dest_id
    assert board["destination"]["code"] == "10000002"
    assert "front" in board["layouts"] or any(k.startswith("template:") for k in board["layouts"])
    # driver renders the referenced board (not the live destination)
    from pi.nsl_pi.display_driver import DisplayDriver
    d = DisplayDriver({"led-rows": 40, "led-cols": 80},
                      [{"display_name": "front", "width_px": 240, "height_px": 40,
                        "enabled": 1, "transport": "local", "program_display": "front",
                        "x_offset": 0}], virtual_dir=str(tmp_path / "virt"))
    d.show_snapshot(snap)
    assert "front" in d.last_frames
    # pin released: board drops, normal display returns
    e.level_event(16, False, now=101.0)
    c.sync_gpio_levels(set(), now=101.0)
    snap2 = c.display_snapshot()
    assert snap2["gpio_boards"] == {}
    d.show_snapshot(snap2)
    assert "front" in d.last_frames


def test_core_gpio_board_override_uses_driver_message(tmp_path):
    c, dest_id = _core_with_board_rule(tmp_path)
    rules = c.gpio_rules()
    assert rules[0]["board_source"] == "destination"
    res = c.apply_gpio_fired(
        [dict(rules[0], effect="override", trigger="press")], now=100.0, source="test")
    assert c.state()["override_text"] == "Lichfield"
    assert res["announcement"] == []


def _core_with_rules(tmp_path):
    from pi.nsl_pi.core import Core
    from pi.nsl_pi.store import init_controller_db
    base = str(tmp_path / "nsl")
    os.makedirs(base, exist_ok=True)
    db = os.path.join(base, "controller.db")
    init_controller_db(db)
    prog = os.path.join(base, "programs")
    p = _bundle_with_rules(tmp_path)
    import shutil
    pid = __import__("nslprog").read_meta(p)["program_id"]
    dest = os.path.join(prog, pid, "1.nslprog")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy(p, dest)
    import sqlite3
    import datetime
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO installed_programs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pid, 1, "Burton routes", "x", "y", dest, 10, "active", "portal",
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
    con.commit()
    con.close()
    c = Core(db, prog)
    c.select_program(pid, source="test")
    return c


def test_core_overlay_and_override(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c = _core_with_rules(tmp_path)
    assert len(c.gpio_rules()) == 3
    e = GpioRulesEngine(c.gpio_rules())
    # door opens: latched overlay via sync
    e.level_event(5, True, now=100.0)
    c.sync_gpio_levels({5}, now=100.0)
    snap = c.display_snapshot()
    assert snap["gpio_overlay"] == {"all": "DOORS OPEN"}
    # door closes: one-shot overlay + override rule untouched
    fired = e.level_event(5, False, now=101.0)
    assert [r["trigger"] for r in fired] == ["on_release"]
    res = c.apply_gpio_fired(fired, now=101.0, source="test")
    assert res["overlay"] == {"all": "MIND THE DOORS"}
    assert c.state()["override_text"] is None  # overlay effect, not override
    # button press applies a driver override
    e.level_event(12, True, now=102.0)
    fired = e.level_event(12, False, now=102.2)
    c.apply_gpio_fired(fired, now=102.2, source="test")
    assert c.state()["override_text"] == "HELLO"


def test_core_oneshot_cleared_on_stop_change(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c = _core_with_rules(tmp_path)
    e = GpioRulesEngine(c.gpio_rules())
    c.select_code("10000001", source="test")
    e.level_event(5, True, now=100.0)
    c.sync_gpio_levels({5}, now=100.0)
    assert c.gpio_overlay_map(now=100.0) == {"all": "DOORS OPEN"}
    # one-shot must not survive a stop change; latched stays (pin still held)
    fired = e.level_event(5, False, now=101.0)
    assert [r["trigger"] for r in fired] == ["on_release"]
    c.sync_gpio_levels(set(), now=101.0)  # pin released: latched board drops
    c.apply_gpio_fired(fired, now=101.0, source="test")
    assert c.gpio_overlay_map(now=101.5) == {"all": "MIND THE DOORS"}
    c.stop_step(1, source="test")
    assert c.gpio_overlay_map(now=102.0) == {}
    # re-assert held pin after program reselect restores latched board
    c.sync_gpio_levels({5}, now=103.0)
    assert c.gpio_overlay_map(now=103.0) == {"all": "DOORS OPEN"}


def test_portal_gpio_crud_publish(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    db = str(tmp_path / "dev.db")
    store = str(tmp_path / "storage")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:{db}")
    monkeypatch.setenv("STORAGE_DIR", store)
    os.makedirs(store, exist_ok=True)
    import portal.backend.app as appmod
    appmod.app.state._db = None
    cl = TestClient(appmod.app)
    r = cl.post("/api/auth/signup",
                data={"email": "g@x.com", "password": "secret123", "name": "G", "org": "O"})
    assert r.status_code == 200, r.text
    tok, org = r.json()["token"], r.json()["organisation_id"]
    H = {"Authorization": f"Bearer {tok}"}
    r = cl.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H)
    pid = r.json()["program_id"]
    # reserved matrix pin rejected
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Bad", "pin": 18, "trigger": "press",
                      "effect": "overlay", "target": "all", "board_text": "X"}, headers=H)
    assert r.status_code == 400, r.text
    # non-gpio header pin rejected
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Bad", "pin": 99, "trigger": "press",
                      "effect": "overlay", "target": "all", "board_text": "X"}, headers=H)
    assert r.status_code == 400, r.text
    # free pin ok
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Door", "pin": 5, "trigger": "while_active",
                      "effect": "overlay", "target": "all", "board_text": "DOORS OPEN"}, headers=H)
    assert r.status_code == 200, r.text
    rid = r.json()["rule_id"]
    r = cl.get(f"/api/programs/{pid}/gpio-rules", headers=H)
    assert len(r.json()["rules"]) == 1
    # need a destination for a publishable program
    r = cl.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=H)
    sid = r.json()["service_id"]
    r = cl.post(f"/api/services/{sid}/destinations",
                json={"code": "1", "name": "A"}, headers=H)
    did = r.json()["destination_id"]
    r = cl.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "A"}]}, headers=H)
    assert r.status_code == 200, r.text
    r = cl.post(f"/api/programs/{pid}/publish", json={"notes": "gpio"}, headers=H)
    assert r.status_code == 200, r.text
    # rule survives a duplicate
    r = cl.post(f"/api/programs/{pid}/duplicate", headers=H)
    nid = r.json()["program_id"]
    r = cl.get(f"/api/programs/{nid}", headers=H)
    assert len(r.json()["gpio_rules"]) == 1
    # delete
    r = cl.delete(f"/api/programs/{pid}/gpio-rules/{rid}", headers=H)
    assert r.status_code == 200, r.text
    r = cl.get(f"/api/programs/{pid}/gpio-rules", headers=H)
    assert r.json()["rules"] == []


def test_portal_gpio_board_rule(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    db = str(tmp_path / "dev.db")
    store = str(tmp_path / "storage")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:{db}")
    monkeypatch.setenv("STORAGE_DIR", store)
    os.makedirs(store, exist_ok=True)
    import portal.backend.app as appmod
    appmod.app.state._db = None
    cl = TestClient(appmod.app)
    r = cl.post("/api/auth/signup",
                data={"email": "h@x.com", "password": "secret123", "name": "H", "org": "O"})
    assert r.status_code == 200, r.text
    tok, org = r.json()["token"], r.json()["organisation_id"]
    H = {"Authorization": f"Bearer {tok}"}
    r = cl.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H)
    pid = r.json()["program_id"]
    r = cl.post(f"/api/programs/{pid}/services", json={"number": "R"}, headers=H)
    sid = r.json()["service_id"]
    r = cl.post(f"/api/services/{sid}/destinations",
                json={"code": "999", "name": "THIS BUS IS REVERSING"}, headers=H)
    rev_id = r.json()["destination_id"]
    r = cl.put(f"/api/destinations/{rev_id}/stops", json={"stops": [{"name": "Depot"}]}, headers=H)
    assert r.status_code == 200, r.text
    # board from another program / unknown id rejected
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Rev", "pin": 16, "trigger": "while_active",
                      "effect": "overlay", "target": "all",
                      "board_source": "destination", "destination_id": "nope"}, headers=H)
    assert r.status_code == 400, r.text
    # destination board rule ok - no text needed
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Rev", "pin": 16, "trigger": "while_active",
                      "effect": "overlay", "target": "all",
                      "board_source": "destination", "destination_id": rev_id}, headers=H)
    assert r.status_code == 200, r.text
    r = cl.get(f"/api/programs/{pid}/gpio-rules", headers=H)
    rules = r.json()["rules"]
    assert len(rules) == 1 and rules[0]["destination_id"] == rev_id
    # publish bundles the board reference
    r = cl.post(f"/api/programs/{pid}/publish", json={"notes": "rev board"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    # duplicate remaps the board reference to the copied destination
    r = cl.post(f"/api/programs/{pid}/duplicate", headers=H)
    nid = r.json()["program_id"]
    r = cl.get(f"/api/programs/{nid}", headers=H)
    dup_rules = r.json()["gpio_rules"]
    assert len(dup_rules) == 1
    dup_dest_ids = [d["id"] for s in r.json()["services"] for d in s["destinations"]]
    assert dup_rules[0]["destination_id"] in dup_dest_ids
    assert dup_rules[0]["destination_id"] != rev_id


def test_engine_toggle_fires_on_release():
    from pi.nsl_pi.gpio import GpioRulesEngine
    e = GpioRulesEngine([
        {"id": "t1", "pin": 20, "trigger": "toggle", "position": 0},
    ])
    assert e.level_event(20, True, now=100.0) == []  # press edge: nothing
    assert [r["id"] for r in e.level_event(20, False, now=100.2)] == ["t1"]
    # hold-release also flips (toggle doesn't care about hold time)
    e.level_event(20, True, now=200.0)
    assert [r["id"] for r in e.level_event(20, False, now=201.5)] == ["t1"]


def _tap(e, c, pin, t0):
    e.level_event(pin, True, now=t0)
    fired = e.level_event(pin, False, now=t0 + 0.2)
    return c.apply_gpio_fired(fired, now=t0 + 0.2, source="test")


def test_core_toggle_overlay_flips_and_survives_stop(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c, dest_id = _core_with_board_rule(tmp_path)
    c.select_code("10000001", source="test")
    rules = c.gpio_rules()
    assert rules[0]["trigger"] == "while_active"
    toggle_rule = dict(rules[0], id="tog1", trigger="toggle", pin=21, board_text="",
                       position=9)
    e = GpioRulesEngine(rules + [toggle_rule])
    # first tap: board shows
    res = _tap(e, c, 21, 100.0)
    snap = c.display_snapshot()
    assert set(snap["gpio_boards"]) == {"all"}
    assert snap["gpio_boards"]["all"]["destination"]["id"] == dest_id
    # no audio configured on this rule
    assert res["announcement"] == []
    # survives a stop change (latched, not one-shot)
    c.stop_step(1, source="test")
    assert set(c.display_snapshot()["gpio_boards"]) == {"all"}
    # while_active sync for another pin must not wipe the toggle board
    c.sync_gpio_levels(set(), now=102.0)
    assert set(c.display_snapshot()["gpio_boards"]) == {"all"}
    # second tap: old board restores
    _tap(e, c, 21, 103.0)
    snap = c.display_snapshot()
    assert snap["gpio_boards"] == {}
    assert snap["gpio_overlay"] == {}


def test_core_toggle_override_sets_and_restores(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c, _dest_id = _core_with_board_rule(tmp_path)
    c.select_code("10000001", source="test")
    rule = {"id": "tog2", "label": "T", "pin": 21, "trigger": "toggle",
            "effect": "override", "target": "all", "board_text": "DEPOT",
            "board_source": "text", "destination_id": None, "audio": "clip9",
            "duration_s": 0, "pull": "up", "active_low": 1,
            "debounce_ms": 50, "position": 0}
    e = GpioRulesEngine([rule])
    res = _tap(e, c, 21, 100.0)
    assert c.state()["override_text"] == "DEPOT"
    assert res["announcement"] == ["clip9"]  # audio on switch-on
    # driver changes the message meanwhile: toggle-off must not wipe it
    c.apply_override("DRIVER NOTE", source="test")
    res = _tap(e, c, 21, 101.0)
    assert c.state()["override_text"] == "DRIVER NOTE"
    assert res["announcement"] == []
    # toggle on then off with no interference restores cleanly
    _tap(e, c, 21, 102.0)
    assert c.state()["override_text"] == "DEPOT"
    _tap(e, c, 21, 103.0)
    assert c.state()["override_text"] is None


def _audio_only_rule(**kw):
    r = {"id": "wel", "label": "Welcome", "pin": 21, "trigger": "on_release",
         "effect": "overlay", "target": "all", "board_text": "",
         "board_source": "text", "destination_id": None, "audio": "clip-welcome",
         "duration_s": 0, "pull": "up", "active_low": 1,
         "debounce_ms": 50, "position": 0}
    r.update(kw)
    return r


def test_core_audio_only_rule_plays_no_board_change(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c, _dest_id = _core_with_board_rule(tmp_path)
    c.select_code("10000001", source="test")
    e = GpioRulesEngine([_audio_only_rule()])
    e.level_event(21, True, now=100.0)
    fired = e.level_event(21, False, now=100.2)
    assert [r["id"] for r in fired] == ["wel"]
    res = c.apply_gpio_fired(fired, now=100.2, source="test")
    assert res["announcement"] == ["clip-welcome"]
    assert c.display_snapshot()["gpio_overlay"] == {}
    assert c.display_snapshot()["gpio_boards"] == {}
    assert c.state()["override_text"] is None


def test_core_toggle_audio_only_plays_every_tap(tmp_path):
    from pi.nsl_pi.gpio import GpioRulesEngine
    c, _dest_id = _core_with_board_rule(tmp_path)
    c.select_code("10000001", source="test")
    e = GpioRulesEngine([_audio_only_rule(id="welt", trigger="toggle")])
    assert _tap(e, c, 21, 100.0)["announcement"] == ["clip-welcome"]
    assert _tap(e, c, 21, 101.0)["announcement"] == ["clip-welcome"]
    assert c.display_snapshot()["gpio_overlay"] == {}
    assert c.display_snapshot()["gpio_boards"] == {}


def test_portal_gpio_audio_only_and_edit(tmp_path, monkeypatch):
    import io
    import wave
    from fastapi.testclient import TestClient
    db = str(tmp_path / "dev.db")
    store = str(tmp_path / "storage")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:{db}")
    monkeypatch.setenv("STORAGE_DIR", store)
    os.makedirs(store, exist_ok=True)
    import portal.backend.app as appmod
    appmod.app.state._db = None
    cl = TestClient(appmod.app)
    r = cl.post("/api/auth/signup",
                data={"email": "w@x.com", "password": "secret123", "name": "W", "org": "O"})
    assert r.status_code == 200, r.text
    tok, org = r.json()["token"], r.json()["organisation_id"]
    H = {"Authorization": f"Bearer {tok}"}
    r = cl.post("/api/programs", json={"organisation_id": org, "name": "P"}, headers=H)
    pid = r.json()["program_id"]
    r = cl.post(f"/api/programs/{pid}/services", json={"number": "1"}, headers=H)
    sid = r.json()["service_id"]
    r = cl.post(f"/api/services/{sid}/destinations",
                json={"code": "1", "name": "A"}, headers=H)
    did = r.json()["destination_id"]
    r = cl.put(f"/api/destinations/{did}/stops", json={"stops": [{"name": "A"}]}, headers=H)
    assert r.status_code == 200, r.text
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\x00\x10" * 2205)
    up = cl.post("/api/audio", data={"organisation_id": org, "name": "welcome"},
                 files={"file": ("welcome.wav", buf.getvalue(), "audio/wav")}, headers=H).json()
    sha = up["sha256"]
    # audio-only: no board text, just the clip
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Welcome", "pin": 5, "trigger": "on_release",
                      "effect": "overlay", "target": "all",
                      "board_source": "text", "board_text": "", "audio": sha}, headers=H)
    assert r.status_code == 200, r.text
    rid = r.json()["rule_id"]
    # text rule with neither text nor audio still rejected
    r = cl.post(f"/api/programs/{pid}/gpio-rules",
                json={"label": "Empty", "pin": 6, "trigger": "press",
                      "effect": "overlay", "target": "all",
                      "board_source": "text", "board_text": ""}, headers=H)
    assert r.status_code == 400, r.text
    # edit: rename + switch to a board text
    r = cl.put(f"/api/programs/{pid}/gpio-rules/{rid}",
               json={"label": "Welcome aboard", "board_text": "WELCOME"}, headers=H)
    assert r.status_code == 200, r.text
    r = cl.get(f"/api/programs/{pid}/gpio-rules", headers=H)
    rules = r.json()["rules"]
    assert len(rules) == 1 and rules[0]["label"] == "Welcome aboard"
    assert rules[0]["board_text"] == "WELCOME" and rules[0]["audio"] == sha
    # edit back to audio-only
    r = cl.put(f"/api/programs/{pid}/gpio-rules/{rid}",
               json={"board_text": ""}, headers=H)
    assert r.status_code == 200, r.text
    # audio clip is bundled on publish
    r = cl.post(f"/api/programs/{pid}/publish", json={"notes": "welcome"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["audio_count"] >= 1


def test_gpio_pull_arg_mapping():
    from pi.nsl_pi.input_main import _gpio_pull_arg
    assert _gpio_pull_arg("up") is True
    assert _gpio_pull_arg("down") is False
    assert _gpio_pull_arg("none") is None
    assert _gpio_pull_arg("") is True
