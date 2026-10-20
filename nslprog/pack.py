"""Manifest (format.md 10) + offline .nslpack (format.md 12)."""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile


def build_manifest(bundle_path: str, base_url: str = "/portal/api/device") -> dict:
    from .build import read_meta
    from .hashing import file_sha256
    meta = read_meta(bundle_path)
    con = sqlite3.connect(f"file:{bundle_path}?mode=ro", uri=True)
    try:
        audio = [{"hash": r[0], "bytes": r[1], "url": f"{base_url}/audio/{r[0]}"}
                 for r in con.execute("SELECT hash, bytes FROM audio ORDER BY hash")]
    finally:
        con.close()
    pid, ver = meta["program_id"], int(meta["version"])
    return {
        "manifest_version": 1,
        "program_id": pid,
        "program_name": meta.get("program_name", ""),
        "version": ver,
        "format_version": int(meta.get("format_version", "1")),
        "content_hash": meta.get("content_hash", ""),
        "min_agent_version": meta.get("min_agent_version", "1.0.0"),
        "bundle": {"url": f"{base_url}/programs/{pid}/versions/{ver}/bundle",
                   "sha256": file_sha256(bundle_path),
                   "bytes": os.path.getsize(bundle_path)},
        "audio": audio,
    }


def export_pack(bundle_path: str, audio_dir: str, pack_path: str, base_url: str = ".") -> dict:
    """Write .nslpack zip: manifest.json + program.nslprog + audio/aa/hash.flac."""
    from .build import read_meta
    meta = read_meta(bundle_path)
    con = sqlite3.connect(f"file:{bundle_path}?mode=ro", uri=True)
    try:
        hashes = [r[0] for r in con.execute("SELECT hash FROM audio ORDER BY hash")]
    finally:
        con.close()
    manifest = build_manifest(bundle_path, base_url=base_url)
    # rewrite urls relative for offline use
    manifest["bundle"]["url"] = "program.nslprog"
    for a in manifest["audio"]:
        h = a["hash"]
        a["url"] = f"audio/{h[:2]}/{h}.flac"
    if os.path.exists(pack_path):
        os.remove(pack_path)
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.write(bundle_path, "program.nslprog")
        for h in hashes:
            src = os.path.join(audio_dir, h[:2], h + ".flac")
            if os.path.exists(src):
                z.write(src, f"audio/{h[:2]}/{h}.flac")
    return manifest


def import_pack(pack_path: str, dest_dir: str) -> dict:
    """Unpack + validate. Returns manifest. Raises on bad pack."""
    import tempfile
    from .build import check_file, read_meta
    tmp = tempfile.mkdtemp(prefix="nslpack-")
    with zipfile.ZipFile(pack_path) as z:
        z.extractall(tmp)
    manifest = json.load(open(os.path.join(tmp, "manifest.json")))
    prog = os.path.join(tmp, "program.nslprog")
    res = check_file(prog)
    if not res.ok:
        raise ValueError("pack program invalid: " + "; ".join(e.message for e in res.errors))
    # verify audio present + sizes
    for a in manifest.get("audio", []):
        p = os.path.join(tmp, a["url"])
        if not os.path.exists(p):
            raise ValueError(f"pack missing audio {a['hash']}")
    os.makedirs(dest_dir, exist_ok=True)
    return {"manifest": manifest, "staged_dir": tmp, "meta": read_meta(prog)}
