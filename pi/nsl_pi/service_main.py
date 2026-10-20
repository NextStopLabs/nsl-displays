"""Pi service entrypoints. Run on device via systemd (deploy/nsl-core.service)."""
from __future__ import annotations

import json
import os
import sqlite3

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.core import Core
    from nsl_pi.display_driver import DisplayDriver
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", "bus-1"))
    core = Core(db, os.path.join(BASE, "programs"))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    outputs = [dict(r) for r in con.execute("SELECT * FROM display_outputs")]
    mo = {}
    try:
        mo = json.loads(con.execute("SELECT matrix_options FROM controller_config WHERE id=1").fetchone()[0] or "{}")
    except Exception:
        pass
    con.close()
    drv = DisplayDriver(mo or {"led-chain": 3 if ROLE == "front" else (2 if ROLE == "side" else 1)},
                        outputs, virtual_dir=os.path.join(BASE, "virtual"))
    # one snapshot render (systemd keeps process alive; receiver-sync loop publishes state)
    try:
        drv.show_snapshot(core.display_snapshot())
    except Exception:
        pass
    # receiver-sync publish loop (front exposes :8090/state; side/rear poll)
    if ROLE == "front":
        from http.server import BaseHTTPRequestHandler, HTTPServer
        snap = {}
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/state":
                    body = json.dumps(core.display_snapshot()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
            def log_message(self, *a):
                pass
        HTTPServer(("0.0.0.0", 8090), H).serve_forever()
    else:
        from nsl_pi.receiver import Receiver
        w = {"front": (240, 40), "side": (160, 40), "rear": (80, 40)}[ROLE]
        Receiver(ROLE, w[0], w[1]).run_forever()


if __name__ == "__main__":
    main()
