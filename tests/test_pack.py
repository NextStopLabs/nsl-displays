import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import demo_builder  # noqa: E402
from nslprog import build_manifest, check_file, export_pack, import_pack  # noqa: E402


def test_pack_roundtrip(tmp_path):
    import sqlite3
    b = demo_builder()
    bundle = str(tmp_path / "p.nslprog")
    b.build(bundle, 1)
    # fake audio store: create a file per referenced hash
    adir = str(tmp_path / "audio")
    con = sqlite3.connect(f"file:{bundle}?mode=ro", uri=True)
    hashes = [r[0] for r in con.execute("SELECT hash FROM audio")]
    con.close()
    for h in hashes:
        os.makedirs(os.path.join(adir, h[:2]), exist_ok=True)
        open(os.path.join(adir, h[:2], h + ".flac"), "wb").write(b"fake")
    # real hashes won't exist -> pack still valid (audio optional in pack, verified on import only if listed)
    pack = str(tmp_path / "p.nslpack")
    m = export_pack(bundle, adir, pack, base_url=".")
    assert m["program_id"]
    out = import_pack(pack, str(tmp_path / "out"))
    assert out["meta"]["program_id"] == m["program_id"]
    assert check_file(os.path.join(out["staged_dir"], "program.nslprog")).ok


def test_manifest_shape(tmp_path):
    bundle = str(tmp_path / "p.nslprog")
    demo_builder().build(bundle, 7)
    m = build_manifest(bundle)
    assert m["version"] == 7
    assert m["bundle"]["sha256"] and m["format_version"] == 1
    assert isinstance(m["audio"], list)
