"""Agent loop: heartbeat + sync on hotspot, outbox flush."""
from __future__ import annotations

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
    while True:
        try:
            ag.heartbeat(db)
            ag.sync_once(db)
        except Exception as e:
            print("agent cycle failed:", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
