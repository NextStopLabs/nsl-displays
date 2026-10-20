"""Agent loop: heartbeat + sync on hotspot, outbox flush."""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    from .store import init_controller_db
    from .agent import Agent
    from .core import Core
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db)
    portal = os.environ.get("PORTAL_URL", "https://nslboards.com/portal")
    token = os.environ.get("NSL_TOKEN")
    ag = Agent(portal, BASE, token)
    core = Core(db, os.path.join(BASE, "programs"))
    print(f"nsl-agent starting: portal={portal} base={BASE} token={'set' if token else 'MISSING'}",
          flush=True)
    while True:
        try:
            hb = ag.heartbeat(db)
            if isinstance(hb, dict) and hb.get("error"):
                print("heartbeat failed:", hb["error"], flush=True)
            for pc in ((hb or {}).get("pending_commands") or []):
                cmd = pc.get("command") or {}
                print("portal command received:", json.dumps(cmd)[:300], flush=True)
                try:
                    res = core.apply_command(cmd, source="portal")
                    status, detail = "applied", json.dumps(res)[:200]
                except Exception as e:
                    status, detail = "failed", str(e)
                print(f"portal command {status}: {detail}", flush=True)
                con = sqlite3.connect(db)
                try:
                    con.execute("INSERT INTO outbox (kind, payload, created_at) VALUES (?,?,?)",
                                ("portal_command", json.dumps({"command": cmd, "status": status,
                                                               "detail": detail}), _utcnow()))
                    con.commit()
                finally:
                    con.close()
            rep = ag.sync_once(db)
            audio = (rep or {}).get("audio", {})
            if (rep or {}).get("programs") or (rep or {}).get("errors") or (rep or {}).get("skipped") \
                    or audio.get("ok") or audio.get("failed"):
                print("sync:", json.dumps(rep)[:2000], flush=True)
        except Exception as e:
            print("agent cycle failed:", e, flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
