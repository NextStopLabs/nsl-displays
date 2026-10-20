"""Display service entrypoint. Sole owner of the HUB75 matrix (or virtual PNGs).

Runs on device via systemd (deploy/nsl-display.service). No ports, no portal
contact — front re-renders from Core at 10 Hz (scroll/page animation runs off
wall-clock), receivers poll the front Pi and hold last frame on drop.
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")

RENDER_HZ = 10.0
STATE_REFRESH_S = 0.5


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.display_driver import DisplayDriver
    import json
    import sqlite3
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", "bus-1"))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    outputs = [dict(r) for r in con.execute("SELECT * FROM display_outputs")]
    mo = {}
    try:
        raw = con.execute("SELECT matrix_options FROM controller_config WHERE id=1").fetchone()[0]
        mo = json.loads(raw or "{}")
    except Exception:
        mo = {}
    con.close()
    if not mo:
        mo = {"led-chain": 3 if ROLE == "front" else (2 if ROLE == "side" else 1)}
    drv = DisplayDriver(mo, outputs, virtual_dir=os.path.join(BASE, "virtual"))
    if ROLE != "front":
        from nsl_pi.receiver import Receiver
        w, h = {"side": (160, 40), "rear": (80, 40)}[ROLE]
        print(f"nsl-display polling front state (role={ROLE})", flush=True)
        Receiver(ROLE, w, h).run_forever()
        return
    from nsl_pi.core import Core
    core = Core(db, os.path.join(BASE, "programs"))
    print(f"nsl-display rendering {len(outputs)} output(s) at {RENDER_HZ:g} Hz (role={ROLE})", flush=True)
    snap: dict = {}
    last_fetch = 0.0
    while True:
        now = time.time()
        if not snap or now - last_fetch >= STATE_REFRESH_S:
            try:
                snap = core.display_snapshot()
            except Exception:
                pass  # hold last frame
            last_fetch = now
        if snap:
            try:
                drv.show_snapshot(snap)
            except Exception:
                pass  # hold last frame
        time.sleep(1.0 / RENDER_HZ)


if __name__ == "__main__":
    main()
