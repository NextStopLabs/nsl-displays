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
    # boot self-test: full-white 1.5 s proves panels/power/HAT before any content.
    # If this flash never appears, the fault is hardware, not software.
    try:
        print("display self-test: white pattern 1.5s — look at the panels", flush=True)
        drv.test_pattern("white")
    except Exception as e:
        print(f"display self-test failed: {e}", flush=True)
    time.sleep(1.5)
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
    last_summary = ""
    frames = 0
    fetch_fails = 0
    empty_snaps = 0
    while True:
        now = time.time()
        if not snap or now - last_fetch >= STATE_REFRESH_S:
            try:
                snap = core.display_snapshot()
            except Exception as e:
                fetch_fails += 1
                if fetch_fails == 1 or fetch_fails % 20 == 0:
                    print(f"display: snapshot failed x{fetch_fails} ({e}) — holding last frame",
                          flush=True)
            last_fetch = now
        if snap:
            st = snap.get("state", {})
            if not st.get("program_id") and not st.get("override_text"):
                empty_snaps += 1
                if empty_snaps == 1 or empty_snaps % 40 == 0:
                    print("display: nothing selected and no override — boards show blank "
                          f"(x{empty_snaps}; select a destination to fill them)", flush=True)
            else:
                empty_snaps = 0
            summary = (f"prog={st.get('program_id') or '—'} dest={st.get('destination_id') or '—'} "
                       f"stop={st.get('stop_id') or '—'} override={st.get('override_text') or '—'} "
                       f"fields={','.join(sorted((snap.get('fields') or {}).keys())) or '—'} "
                       f"layouts={len(snap.get('layouts') or {})}")
            if summary != last_summary:
                print(f"display state: {summary}", flush=True)
                last_summary = summary
            try:
                drv.show_snapshot(snap)
                frames += 1
                if frames == 1:
                    w0 = outputs[0]["width_px"] if outputs else "?"
                    print(f"display: first frame pushed ({len(outputs)} output(s), first {w0}px wide)",
                          flush=True)
            except Exception as e:
                print(f"display: render failed ({e}) — holding last frame", flush=True)
        time.sleep(1.0 / RENDER_HZ)


if __name__ == "__main__":
    main()
