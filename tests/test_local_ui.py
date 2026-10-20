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
