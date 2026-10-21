import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))


def test_pairing_register_claim_save(tmp_path, monkeypatch):
    from pi.nsl_pi import pairing as P
    base = str(tmp_path / "nsl")
    os.makedirs(base, exist_ok=True)
    monkeypatch.setenv("NSL_BASE", base)

    reg = {"controller_id": "cid-1", "pairing_code": "ABC123",
           "expires_at": "2099-01-01T00:00:00Z"}

    class FakeAgent:
        def __init__(self, portal_url, base_dir, token=None):
            self.portal_url = portal_url
        def register(self, pairing_code=None):
            return dict(reg)

    monkeypatch.setattr("pi.nsl_pi.agent.Agent", FakeAgent)
    doc = P.ensure_registered("https://portal.test/portal", base)
    assert doc["pairing_code"] == "ABC123"
    assert P.pairing_valid(doc)
    # second call reuses live registration (no new network)
    doc2 = P.ensure_registered("https://portal.test/portal", base)
    assert doc2["controller_id"] == "cid-1"

    # claim not yet paired, then paired
    calls = {"n": 0}
    def fake_claim(portal, cid, timeout=20.0):
        calls["n"] += 1
        assert cid == "cid-1"
        if calls["n"] < 2:
            return {"paired": False, "controller_id": cid}
        return {"paired": True, "device_token": "tok-xyz", "controller_id": cid}
    monkeypatch.setattr(P, "claim_once", fake_claim)
    monkeypatch.setattr(P, "portal_override_path", lambda: str(tmp_path / "nsl-portal.conf"))
    tok = P.wait_for_claim("https://portal.test/portal", "cid-1", poll_s=0, timeout_s=5)
    assert tok == "tok-xyz"
    P.save_token(tok, "https://portal.test/portal", base)
    assert P.get_token(base) == "tok-xyz"
    assert P.is_paired(base)
    # pairing code cleared after save
    with open(P.pairing_path(base)) as f:
        assert "pairing_code" not in json.load(f)


def test_pairing_frame_renders_code():
    from pi.nsl_pi.display_main import pairing_frame
    for w, h in ((240, 40), (160, 40), (80, 40)):
        img = pairing_frame({"width_px": w, "height_px": h}, "KQ7Z2M", "nslboards.com/portal")
        assert img.size == (w, h)
        assert sum(1 for px in img.getdata() if px[:3] != (0, 0, 0)) > 50


def test_agent_reads_token_file(tmp_path, monkeypatch):
    base = tmp_path / "nsl"
    base.mkdir()
    (base / "device-token").write_text("file-tok\n")
    monkeypatch.setenv("NSL_BASE", str(base))
    monkeypatch.delenv("NSL_TOKEN", raising=False)
    from pi.nsl_pi import pairing as P
    assert P.get_token(str(base)) == "file-tok"


def test_test_tone_writes_wav(tmp_path):
    from pi.nsl_pi.player import play_test_tone, test_tone_pcm
    pcm, rate = test_tone_pcm()
    assert rate == 22050 and len(pcm) > 10000
    out = str(tmp_path / "tone.wav")
    rep = play_test_tone(str(tmp_path), out_wav=out)
    assert rep["bytes"] == len(pcm) and rep["rate"] == 22050
    import wave
    with wave.open(out, "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 22050
        assert w.getnframes() > 10000


def test_local_ui_audio_test_endpoint(tmp_path, monkeypatch):
    import sys
    from fastapi.testclient import TestClient
    monkeypatch.setenv("NSL_BASE", str(tmp_path))
    sys.path.insert(0, "pi")
    from nsl_pi import local_ui
    local_ui.CORE = None
    c = TestClient(local_ui.app, raise_server_exceptions=False)
    r = c.post("/api/audio-test")
    assert r.status_code == 200
    body = r.json()
    assert body["bytes"] > 10000 and "device" in body
