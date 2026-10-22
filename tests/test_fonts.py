import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path, monkeypatch, fonts=True):
    db = str(tmp_path / "dev.db")
    store = str(tmp_path / "storage")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:{db}")
    monkeypatch.setenv("STORAGE_DIR", store)
    if fonts:
        monkeypatch.setenv("NSL_FONTS_DIR", str(tmp_path / "fonts"))
    os.makedirs(store, exist_ok=True)
    import portal.backend.app as appmod
    appmod.app.state._db = None
    return TestClient(appmod.app)


def _signup(c, email="ed@x.com"):
    r = c.post("/api/auth/signup", data={"email": email, "password": "secret123", "name": "E"})
    assert r.status_code == 200, r.text
    return r.json()["token"], r.json()["organisation_id"]


def _fnt_bytes():
    with open("HANOVERFONTS/0705C1E1.Fnt", "rb") as f:
        return f.read()


def test_font_upload_roundtrip(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    tok, _ = _signup(c)
    H = {"Authorization": f"Bearer {tok}"}
    r = c.post("/api/fonts", files={"file": ("0705C1E1.Fnt", _fnt_bytes())}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "han-0705C1E1"
    assert r.json()["height"] == 7
    # immediately readable (cache invalidated) and listed
    r = c.get("/api/fonts/han-0705C1E1", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["height"] == 7 and len(r.json()["glyphs"]) == 224
    ids = [f["id"] for f in c.get("/api/fonts", headers=H).json()["fonts"]]
    assert "han-0705C1E1" in ids


def test_font_upload_rejects_garbage(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    tok, _ = _signup(c, "ed2@x.com")
    H = {"Authorization": f"Bearer {tok}"}
    r = c.post("/api/fonts", files={"file": ("nope.Fnt", b"hello world")}, headers=H)
    assert r.status_code == 400, r.text
    r = c.post("/api/fonts", files={"file": ("a.txt", _fnt_bytes())}, headers=H)
    assert r.status_code == 400, r.text


def test_font_upload_needs_editor(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    tok, org = _signup(c, "owner@x.com")
    H = {"Authorization": f"Bearer {tok}"}
    # make a viewer-only user in the same org
    import portal.backend.app as appmod
    import uuid as _uuid

    con = appmod.db()
    uid = str(_uuid.uuid4())
    con.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                (uid, "view@x.com", "x", "V", None, "2026-01-01T00:00:00Z"))
    con.execute("INSERT INTO memberships VALUES (?,?,?)", (uid, org, "viewer"))
    con.commit()
    # mint a token for the viewer the same way the app does
    import hashlib as _h
    import hmac as _hm

    sig = _hm.new(appmod.JWT_SECRET.encode(), uid.encode(), _h.sha256).hexdigest()[:32]
    VH = {"Authorization": f"Bearer {uid}.{sig}"}
    r = c.post("/api/fonts", files={"file": ("0705C1E1.Fnt", _fnt_bytes())}, headers=VH)
    assert r.status_code == 403, r.text
