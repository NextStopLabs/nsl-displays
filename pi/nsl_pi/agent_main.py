"""Agent loop: heartbeat + sync on hotspot, outbox flush."""
from __future__ import annotations

import json
import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")


def main():
    from .store import init_controller_db
    from .agent import Agent
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db)
    portal = os.environ.get("PORTAL_URL", "https://nslboards.com/portal")
    token = os.environ.get("NSL_TOKEN")
    ag = Agent(portal, BASE, token)
    print(f"nsl-agent starting: portal={portal} base={BASE} token={'set' if token else 'MISSING'}",
          flush=True)
    while True:
        try:
            hb = ag.heartbeat(db)
            if isinstance(hb, dict) and hb.get("error"):
                print("heartbeat failed:", hb["error"], flush=True)
            rep = ag.sync_once(db)
            audio = (rep or {}).get("audio", {})
            if (rep or {}).get("programs") or (rep or {}).get("errors") or audio.get("ok") or audio.get("failed"):
                print("sync:", json.dumps(rep)[:2000], flush=True)
        except Exception as e:
            print("agent cycle failed:", e, flush=True)
        time.sleep(60)


if __name__ == "__main__":
    main()
