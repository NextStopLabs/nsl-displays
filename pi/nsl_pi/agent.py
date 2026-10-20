"""Agent: pairing, sync, program/audio download, heartbeat, updates, net modes.

Implements format.md 10/11 + README 6.2/12.
All portal contact is Pi-initiated HTTPS (hotspot). Receivers never call portal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.request

MANIFEST_PATH = "manifest.json"


def _req(url: str, token: str | None = None, data: bytes | None = None,
         headers: dict | None = None, timeout: float = 20.0):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


class Agent:
    def __init__(self, portal_url: str, base_dir: str, token: str | None = None):
        self.portal_url = portal_url.rstrip("/")
        self.base_dir = base_dir
        self.token = token
        self.programs_dir = os.path.join(base_dir, "programs")
        self.audio_dir = os.path.join(base_dir, "audio")
        self.incoming = os.path.join(base_dir, "incoming")
        for d in (self.programs_dir, self.audio_dir, self.incoming):
            os.makedirs(d, exist_ok=True)

    def _api(self, path: str) -> str:
        return self.portal_url + path

    def register(self, pairing_code: str | None = None) -> dict:
        body = json.dumps({"code": pairing_code}).encode()
        _, raw, _ = _req(self._api("/api/device/register"), data=body,
                         headers={"Content-Type": "application/json"})
        return json.loads(raw.decode())

    def fetch_manifest(self, program_id: str | None = None) -> dict:
        status, raw, _ = _req(self._api("/api/device/manifest"), token=self.token)
        return json.loads(raw.decode())

    def download(self, url: str, dest: str, sha256: str, resume: bool = True):
        """Range-resumed download, hash-verified. Partial stays in incoming/."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        start = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
        if url.startswith("/"):
            url = self.portal_url + url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if start:
            headers["Range"] = f"bytes={start}-"
        req = urllib.request.Request(url, headers=headers)
        h = hashlib.sha256()
        if start:  # re-hash prefix for verification simplicity: re-read
            with open(dest, "rb") as f:
                for ch in iter(lambda: f.read(65536), b""):
                    h.update(ch)
        mode = "ab" if start else "wb"
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, mode) as f:
            for chunk in iter(lambda: r.read(65536), b""):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != sha256.lower():
            raise ValueError(f"hash mismatch for {dest}")
        return dest

    def sync_once(self, controller_db: str) -> dict:
        """Order: program data + config first, audio second, software last (README 12)."""
        from nslprog import check_file
        report = {"programs": [], "audio": {"ok": 0, "failed": []}, "errors": []}
        manifest_doc = self.fetch_manifest()
        items = manifest_doc.get("programs", [manifest_doc] if "bundle" in manifest_doc else [])
        con = sqlite3.connect(controller_db)
        try:
            for m in items:
                pid, ver = m["program_id"], int(m["version"])
                con.execute(
                    "INSERT OR REPLACE INTO desired_programs (program_id, desired_version, manifest, received_at) VALUES (?,?,?,datetime('now'))",
                    (pid, ver, json.dumps(m)))
                con.commit()
                # 1. program file first (tiny)
                b = m["bundle"]
                dest = os.path.join(self.incoming, f"{pid}-{ver}.nslprog")
                try:
                    self.download(b["url"], dest, b["sha256"])
                except Exception as e:
                    report["errors"].append(f"{pid}@{ver} bundle: {e}")
                    continue
                res = check_file(dest)
                if not res.ok:
                    report["errors"].append(f"{pid}@{ver} invalid: " + "; ".join(x.message for x in res.errors))
                    continue
                # 2. audio second (only missing hashes)
                for a in m.get("audio", []):
                    apath = os.path.join(self.audio_dir, a["hash"][:2], a["hash"] + ".flac")
                    if os.path.exists(apath):
                        continue
                    tmp = os.path.join(self.incoming, a["hash"] + ".flac")
                    try:
                        self.download(a["url"], tmp, a["hash"])
                        os.makedirs(os.path.dirname(apath), exist_ok=True)
                        os.replace(tmp, apath)
                        report["audio"]["ok"] += 1
                    except Exception as e:
                        report["audio"]["failed"].append(f"{a['hash'][:12]}: {e}")
                # 3. install (atomic move + registry) — activation is Core's job at safe moment
                import shutil
                final = os.path.join(self.programs_dir, pid, f"{ver}.nslprog")
                os.makedirs(os.path.dirname(final), exist_ok=True)
                shutil.move(dest, final)
                meta_name = m.get("program_name", pid)
                con.execute(
                    "INSERT OR REPLACE INTO installed_programs (program_id, version, program_name, content_hash, bundle_sha256, path, bytes, status, source, installed_at, last_error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, ver, meta_name, m.get("content_hash", ""), b["sha256"], final, b["bytes"],
                     "installed", "portal", __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None))
                con.commit()
                report["programs"].append(f"{pid}@{ver}")
            # 4. software last: check release
            try:
                _, raw, _ = _req(self._api("/api/device/release"), token=self.token)
                report["release"] = json.loads(raw.decode())
            except Exception as e:
                report["release"] = {"info": f"no release check: {e}"}
        finally:
            con.close()
        return report

    def heartbeat(self, controller_db: str, extra: dict | None = None) -> dict:
        con = sqlite3.connect(controller_db)
        try:
            installed = [dict(zip(["program_id", "version", "status"], r)) for r in
                         con.execute("SELECT program_id, version, status FROM installed_programs")]
            outbox = [dict(zip(["id", "kind", "payload"], r)) for r in
                      con.execute("SELECT id, kind, payload FROM outbox WHERE sent_at IS NULL LIMIT 100")]
        finally:
            con.close()
        body = json.dumps({"installed": installed, "events": outbox, **(extra or {})}).encode()
        try:
            _, raw, _ = _req(self._api("/api/device/heartbeat"), token=self.token, data=body,
                             headers={"Content-Type": "application/json"})
            ack = json.loads(raw.decode())
            if outbox and ack.get("accepted"):
                c2 = sqlite3.connect(controller_db)
                c2.execute("UPDATE outbox SET sent_at=datetime('now') WHERE sent_at IS NULL")
                c2.commit()
                c2.close()
            return ack
        except Exception as e:
            return {"error": str(e)}
