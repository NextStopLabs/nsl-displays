"""Pi core service entrypoint. Owns state + the bus-LAN state server. Never touches GPIO.

Runs on device via systemd (deploy/nsl-core.service). Rendering belongs to
nsl_pi.display_main (deploy/nsl-display.service) — two drivers on one matrix
corrupts output, and two servers on :8090 crash each other.
"""
from __future__ import annotations

import json
import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.core import Core
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", "bus-1"))
    if ROLE != "front":
        # receivers: display service owns rendering; core has nothing to serve.
        while True:
            time.sleep(3600)
        return
    core = Core(db, os.path.join(BASE, "programs"))
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True

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

    print(f"nsl-core serving state on :8090 (role={ROLE})", flush=True)
    ReuseHTTPServer(("0.0.0.0", 8090), H).serve_forever()


if __name__ == "__main__":
    main()
