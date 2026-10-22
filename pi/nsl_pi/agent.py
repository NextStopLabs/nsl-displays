"""Agent: pairing, sync, program/audio download, heartbeat, updates, net modes.

Implements format.md 10/11 + README 6.2/12.
All portal contact is Pi-initiated HTTPS (hotspot). Receivers never call portal.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.request

MANIFEST_PATH = "manifest.json"
APP_VERSION = "1.0.0"


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
        self.progress: dict | None = None  # what sync is doing right now (reported via heartbeat)
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

    def claim(self, controller_id: str) -> dict:
        """Poll after showing the pairing code; returns paired + device_token once."""
        body = json.dumps({"controller_id": controller_id}).encode()
        _, raw, _ = _req(self._api("/api/device/claim"), data=body,
                          headers={"Content-Type": "application/json"})
        doc = json.loads(raw.decode())
        if doc.get("paired") and doc.get("device_token"):
            self.token = doc["device_token"]
        return doc

    def fetch_manifest(self, program_id: str | None = None) -> dict:
        status, raw, _ = _req(self._api("/api/device/manifest"), token=self.token)
        return json.loads(raw.decode())

    def fetch_release(self) -> dict | None:
        status, raw, _ = _req(self._api("/api/device/release"), token=self.token)
        return json.loads(raw.decode())

    def download(self, url: str, dest: str, sha256: str, resume: bool = True):
        """Range-resumed download, hash-verified. Partial stays in incoming/."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        start = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
        if url.startswith("/"):
            url = self.portal_url + url
        # 416 recovery: the local partial is already complete (or newer than
        # the server file). If its hash matches, we are done; otherwise the
        # partial is stale/corrupt - discard it and download fresh instead of
        # re-requesting the same unsatisfiable range forever.
        if start and sha256:
            try:
                h0 = hashlib.sha256()
                with open(dest, "rb") as f:
                    for ch in iter(lambda: f.read(65536), b""):
                        h0.update(ch)
                if h0.hexdigest() == sha256.lower():
                    return dest
            except OSError:
                start = 0
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
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                # Server without Range support answers 200 with the full body.
                # Appending that to a partial file corrupts it forever, so restart.
                try:
                    status = getattr(r, "status", 200)
                except Exception:
                    status = 200
                if start and status != 206:
                    h = hashlib.sha256()
                    mode = "wb"
                with open(dest, mode) as f:
                    for chunk in iter(lambda: r.read(65536), b""):
                        h.update(chunk)
                        f.write(chunk)
        except urllib.error.HTTPError as e:
            if getattr(e, "code", None) == 416 and start:
                # Requested range beyond EOF: local file is complete or
                # larger than the server object. Re-check hash (server may
                # have answered 416 with zero body while our pre-check raced
                # a concurrent writer); else discard and fetch fresh.
                try:
                    h1 = hashlib.sha256()
                    with open(dest, "rb") as f:
                        for ch in iter(lambda: f.read(65536), b""):
                            h1.update(ch)
                    if sha256 and h1.hexdigest() == sha256.lower():
                        return dest
                except OSError:
                    pass
                try:
                    os.remove(dest)
                except OSError:
                    pass
                if resume:
                    return self.download(url, dest, sha256, resume=False)
                raise ValueError(f"range not satisfiable for {dest} (local {start} bytes)")
            raise
        if sha256 and h.hexdigest() != sha256.lower():
            # Remove the bad file so the next sync retries from scratch
            # instead of resuming from a corrupt offset and hitting 416.
            try:
                os.remove(dest)
            except OSError:
                pass
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
                b = m["bundle"]
                # idempotent sync: this exact build is already on disk - record desire, skip download
                have = con.execute("SELECT content_hash, bundle_sha256 FROM installed_programs WHERE program_id=? AND version=?",
                                   (pid, ver)).fetchone()
                if have and have[0] == (m.get("content_hash") or "") and have[1] == b["sha256"]:
                    con.execute(
                        "INSERT OR REPLACE INTO desired_programs (program_id, desired_version, manifest, received_at) VALUES (?,?,?,datetime('now'))",
                        (pid, ver, json.dumps(m)))
                    con.commit()
                    report.setdefault("skipped", []).append(f"{m.get('program_name', pid)} v{ver} already installed")
                    self.progress = None
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO desired_programs (program_id, desired_version, manifest, received_at) VALUES (?,?,?,datetime('now'))",
                    (pid, ver, json.dumps(m)))
                con.commit()
                # 1. program file first (tiny)
                dest = os.path.join(self.incoming, f"{pid}-{ver}.nslprog")
                self.progress = {"phase": "bundle", "program": m.get("program_name", pid), "version": ver}
                try:
                    self.download(b["url"], dest, b["sha256"])
                except Exception as e:
                    self.progress = None
                    report["errors"].append(f"{pid}@{ver} bundle: {e}")
                    continue
                res = check_file(dest)
                if not res.ok:
                    report["errors"].append(f"{pid}@{ver} invalid: " + "; ".join(x.message for x in res.errors))
                    continue
                # 2. audio second (only missing hashes; either extension counts)
                def _have(h: str) -> bool:
                    d = os.path.join(self.audio_dir, h[:2])
                    return os.path.exists(os.path.join(d, h + ".flac")) or \
                        os.path.exists(os.path.join(d, h + ".wav"))
                missing = [a for a in m.get("audio", []) if not _have(a["hash"])]
                for i, a in enumerate(missing, 1):
                    self.progress = {"phase": "audio", "program": m.get("program_name", pid),
                                     "done": i, "total": len(missing)}
                    tmp = os.path.join(self.incoming, a["hash"] + ".part")
                    try:
                        self.download(a["url"], tmp, a["hash"])
                        # extension by magic bytes (server stores .wav, legacy .flac)
                        with open(tmp, "rb") as f:
                            magic = f.read(4)
                        ext = ".wav" if magic == b"RIFF" else ".flac"
                        apath = os.path.join(self.audio_dir, a["hash"][:2], a["hash"] + ext)
                        if os.path.exists(apath):
                            os.remove(tmp)
                        else:
                            os.makedirs(os.path.dirname(apath), exist_ok=True)
                            os.replace(tmp, apath)
                        report["audio"]["ok"] += 1
                    except Exception as e:
                        report["audio"]["failed"].append(f"{a['hash'][:12]}: {e}")
                        try:
                            if os.path.exists(tmp):
                                os.remove(tmp)
                        except OSError:
                            pass
                # 3. install (atomic move + registry) - activation is Core's job at safe moment
                import shutil
                self.progress = {"phase": "installing", "program": m.get("program_name", pid), "version": ver}
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
                self.progress = None
                # first install wins the idle bus: no active program means no
                # mid-run state to protect, so select it and light the boards
                # immediately instead of waiting for a manual program_select.
                try:
                    st = con.execute("SELECT program_id FROM state WHERE id=1").fetchone()
                except Exception:
                    st = None
                if st is not None and not st[0]:
                    try:
                        from .core import Core
                        Core(controller_db, self.programs_dir).select_program(pid, source="sync")
                        report.setdefault("activated", []).append(f"{meta_name} v{ver} (first install)")
                    except Exception as e:
                        report["errors"].append(f"{pid}@{ver} auto-select: {e}")
            # 4. software last: check release
            report["release"] = self._sync_release()
        finally:
            con.close()
        return report

    def _sync_release(self) -> dict:
        """Check portal release, download+verify if newer, stage activation."""
        try:
            rel = self.fetch_release()
        except Exception as e:
            return {"info": f"no release check: {e}"}
        if not rel or not rel.get("version"):
            return {"info": "no release available"}
        new_ver = str(rel["version"])
        # Don't re-download same version.
        from .update import current_version, staged_version
        if current_version() == new_ver:
            return {"version": new_ver, "info": "already running"}
        if staged_version() == new_ver:
            return {"version": new_ver, "info": "already staged"}
        if rel.get("min_agent_version") and rel["min_agent_version"] > APP_VERSION:
            return {"version": new_ver, "error": f"needs agent >= {rel['min_agent_version']}"}
        url = rel.get("bundle_url") or rel.get("url")
        if not url:
            return {"version": new_ver, "error": "no bundle_url in release"}
        bundle_dest = os.path.join(self.incoming, f"release-{new_ver}.tar.gz")
        self.progress = {"phase": "software", "version": new_ver}
        try:
            self.download(url, bundle_dest, rel.get("sha256", ""))
        except Exception as e:
            self.progress = None
            return {"version": new_ver, "error": f"download failed: {e}"}
        from .update import check_and_apply, run_activation_helper
        res = check_and_apply(rel, bundle_dest)
        self.progress = None
        if res.get("ok"):
            # Trigger the background helper; the agent will restart as part of
            # service restart, and the helper will report the final result.
            run_activation_helper(new_ver)
        return {"version": new_ver, **res}

    def heartbeat(self, controller_db: str, extra: dict | None = None) -> dict:
        con = sqlite3.connect(controller_db)
        try:
            installed = [dict(zip(["program_id", "version", "status"], r)) for r in
                         con.execute("SELECT program_id, version, status FROM installed_programs")]
            outbox = [dict(zip(["id", "kind", "payload"], r)) for r in
                      con.execute("SELECT id, kind, payload FROM outbox WHERE sent_at IS NULL LIMIT 100")]
            st = con.execute("SELECT program_id, program_version, service_id, destination_id,"
                             " stop_id, override_text, volume, muted FROM state WHERE id=1").fetchone()
            active = ({"program_id": st[0], "program_version": st[1], "service_id": st[2],
                       "destination_id": st[3], "stop_id": st[4], "override_text": st[5],
                       "volume": st[6], "muted": st[7]} if st else None)
        finally:
            con.close()
        body = json.dumps({"installed": installed, "events": outbox, "state": active,
                           "sync": self.progress, **(extra or {})}).encode()
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
