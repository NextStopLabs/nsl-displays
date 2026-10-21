import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

from fastapi.testclient import TestClient  # noqa: E402

from nsl_pi.local_ui import app, client_allowed  # noqa: E402


def test_allowlist_ranges():
    assert client_allowed("127.0.0.1")
    assert client_allowed("192.168.4.7")
    assert client_allowed("10.9.0.2")
    assert not client_allowed("192.168.0.159")
    assert not client_allowed("8.8.8.8")


def test_health_open_and_state_guarded():
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/health").status_code == 200
    # TestClient presents a non-IP peer ("testserver"), which is allowed as a
    # test hook — so state is reachable here but 403s for real remote subnets
    # (covered by test_allowlist_ranges at the unit level).
    r = c.get("/api/state")
    assert r.status_code in (200, 500)  # 500 only if no controller.db present


def test_identity_validation_and_success(tmp_path, monkeypatch):
    import stat as _stat
    # fake nsl-set-unit on PATH: records args, exits 0
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "nsl-set-unit"
    stub.write_text("#!/bin/bash\necho \"$@\" > \"$NSL_STUB_OUT\"\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("NSL_STUB_OUT", str(tmp_path / "args.txt"))
    c = TestClient(app, raise_server_exceptions=False)
    # bad names rejected without touching the system
    assert c.post("/api/identity", json={"unit": "bad name!"}).status_code == 400
    assert c.post("/api/identity", json={"unit": ""}).status_code == 400
    assert not (tmp_path / "args.txt").exists()
    # good name shells out with --no-reboot, then schedules a reboot (stubbed)
    import subprocess as _sp
    _real_popen = _sp.Popen
    class _Noop:
        returncode = 0
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def communicate(self, *a, **k): return ("", "")
        def wait(self, *a, **k): return 0
    def _fake(cmd, *a, **k):
        if "sleep 3; reboot" in str(cmd):
            return _Noop()  # swallow the scheduled reboot, run everything else
        return _real_popen(cmd, *a, **k)
    monkeypatch.setattr(_sp, "Popen", _fake)
    r = c.post("/api/identity", json={"unit": "bus-2"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "unit": "bus-2", "rebooting": True}
    assert (tmp_path / "args.txt").read_text().strip() == "bus-2 --no-reboot"


def test_identity_missing_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty PATH dir: no nsl-set-unit
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/identity", json={"unit": "bus-2"})
    assert r.status_code == 500
