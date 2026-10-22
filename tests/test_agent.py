import hashlib
import os
import shutil
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


def test_player_announce_writes_wav_without_alsa(tmp_path):
    import io
    import wave
    from pi.nsl_pi.player import announce
    adir = tmp_path / "au"
    (adir / "ab").mkdir(parents=True)
    h = "ab" * 32
    p = adir / "ab" / (h + ".wav")
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"\x00\x10" * 2205)
    out = str(tmp_path / "chain.wav")
    rep = announce([h], str(adir), device="no-such-device-xyz", out_wav=out)
    assert rep["clips"] == 1 and rep["played"] is False
    assert os.path.getsize(out) > 1000


def _release_agent(tmp_path, monkeypatch):
    """Create an agent with NSL_ROOT pointing into tmp_path so update slots
    are created there instead of under /opt/nsl."""
    root = tmp_path / "nslroot"
    root.mkdir()
    monkeypatch.setenv("NSL_ROOT", str(root))
    return _agent(tmp_path, monkeypatch)


def test_sync_downloads_release_when_newer(tmp_path, monkeypatch):
    """Agent checks /api/device/release, downloads the bundle, and stages it."""
    import tarfile
    ag, db, _ = _release_agent(tmp_path, monkeypatch)
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as t:
        import io as _io
        data = b"v2 code"
        info = tarfile.TarInfo(name="README.txt")
        info.size = len(data)
        t.addfile(info, _io.BytesIO(data))
    h = hashlib.sha256(bundle.read_bytes()).hexdigest()
    monkeypatch.setenv("NSL_SKIP_VERIFY", "1")
    # Don't actually fork the activation helper in tests.
    monkeypatch.setattr("pi.nsl_pi.update.run_activation_helper", lambda v: None)
    monkeypatch.setattr(ag, "fetch_manifest", lambda *a: {"programs": []})
    monkeypatch.setattr(ag, "fetch_release", lambda *a: {
        "version": "2.0.0", "bundle_url": "/rel", "sha256": h,
        "signature": "00" * 64, "bytes": bundle.stat().st_size})
    def fake_download(url, dest, sha):
        shutil.copy(str(bundle), dest)
        return dest
    monkeypatch.setattr(ag, "download", fake_download)
    rep = ag.sync_once(db)
    rel = rep.get("release", {})
    assert rel.get("ok") is True, rel
    assert rel.get("version") == "2.0.0"
    # Inactive slot should contain the unpacked bundle.
    from pi.nsl_pi.update import slots
    active, inactive = slots()
    assert open(os.path.join(inactive, ".nsl-version")).read().strip() == "2.0.0"
    assert open(os.path.join(inactive, "README.txt")).read() == "v2 code"


def test_sync_release_with_real_signature(tmp_path, monkeypatch):
    """Agent verifies a real Ed25519 signature before staging."""
    import tarfile
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = Ed25519PrivateKey.generate()
    pub_pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    ag, db, _ = _release_agent(tmp_path, monkeypatch)
    bundle = tmp_path / "release.tar.gz"
    with tarfile.open(bundle, "w:gz") as t:
        import io as _io
        data = b"signed v2 code"
        info = tarfile.TarInfo(name="signed.txt")
        info.size = len(data)
        t.addfile(info, _io.BytesIO(data))
    sig = key.sign(bundle.read_bytes()).hex()
    h = hashlib.sha256(bundle.read_bytes()).hexdigest()
    # Write public key to a temp file and point NSL_RELEASE_PUBLIC_KEY at it.
    pub_path = tmp_path / "release.pub"
    pub_path.write_text(pub_pem)
    monkeypatch.setenv("NSL_RELEASE_PUBLIC_KEY", str(pub_path))
    monkeypatch.setattr("pi.nsl_pi.update.run_activation_helper", lambda v: None)
    monkeypatch.setattr(ag, "fetch_manifest", lambda *a: {"programs": []})
    monkeypatch.setattr(ag, "fetch_release", lambda *a: {
        "version": "2.0.1", "bundle_url": "/rel", "sha256": h,
        "signature": sig, "bytes": bundle.stat().st_size})
    def fake_download(url, dest, sha):
        shutil.copy(str(bundle), dest)
        return dest
    monkeypatch.setattr(ag, "download", fake_download)
    rep = ag.sync_once(db)
    rel = rep.get("release", {})
    assert rel.get("ok") is True, rel
    from pi.nsl_pi.update import slots
    _, inactive = slots()
    assert open(os.path.join(inactive, "signed.txt")).read() == "signed v2 code"


def test_activation_helper_runs_outside_agent_cgroup(tmp_path, monkeypatch):
    """run_activation_helper must launch via systemd-run --scope so the
    service restart during activation can't kill it (regression: bare Popen
    stayed in the agent cgroup, died mid-activation, marker never cleared,
    slots flip-flopped forever)."""
    import pi.nsl_pi.update as upd
    calls = {}
    def fake_run(cmd, **kw):
        calls["run"] = cmd
        return None
    def fake_popen(cmd, **kw):
        calls["popen"] = cmd
        raise AssertionError("fallback Popen must not run when systemd-run works")
    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    monkeypatch.setattr(upd.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("NSL_BASE", str(tmp_path))
    upd.run_activation_helper("9.9.9")
    assert calls["run"][:3] == ["systemd-run", "--scope", "--quiet"]
    assert any(a.startswith("--unit=nsl-update-9.9.9-") for a in calls["run"])
    assert calls["run"][-2:] == ["-m", "nsl_pi.update_helper", "9.9.9"] or \
        calls["run"][-3:] == ["-m", "nsl_pi.update_helper", "9.9.9"]


def test_activation_helper_falls_back_without_systemd(tmp_path, monkeypatch):
    import pi.nsl_pi.update as upd
    calls = {}
    def fake_run(cmd, **kw):
        raise FileNotFoundError("no systemd-run")
    def fake_popen(cmd, **kw):
        calls["popen"] = cmd
        return None
    monkeypatch.setattr(upd.subprocess, "run", fake_run)
    monkeypatch.setattr(upd.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("NSL_BASE", str(tmp_path))
    upd.run_activation_helper("9.9.9")
    assert calls["popen"][-2:] == ["nsl_pi.update_helper", "9.9.9"]
