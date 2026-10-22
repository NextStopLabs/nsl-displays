#!/usr/bin/env python3
"""Standalone helper: activate a staged A/B update and report result.

Called by the agent after it has staged a new release. This runs in its own
session so it survives the nsl-agent service restart. It waits a moment for
the agent to exit, then switches the symlink, restarts services, runs the
health check, rolls back on failure, and writes the result to the controller
database outbox so the next heartbeat reports it to the portal.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time

from .db import connect as _db_connect

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    version = (sys.argv[1] if len(sys.argv) > 1 else None) or os.environ.get("NSL_UPDATE_VERSION")
    if not version:
        print("usage: update_helper.py <version>", file=sys.stderr)
        sys.exit(1)
    # Give the agent a moment to finish its cycle and exit cleanly.
    time.sleep(2)
    from .update import activate, pending_activation
    # If marker was cleared or changed, abort.
    pending = pending_activation()
    if not pending or pending.get("version") != version:
        print(f"pending marker mismatch: {pending!r} != {version!r}; aborting", file=sys.stderr)
        sys.exit(0)
    result = activate(version)
    print(f"software {version}: activation "
          + ("done" if result.get("ok") else f"FAILED ({result.get('error', 'unknown')})"),
          flush=True)
    from .update import clear_activation_failure, record_activation_failure
    if result.get("ok"):
        clear_activation_failure(version)
    else:
        record_activation_failure(version, str(result.get("error", "unknown")))
    # Report result via outbox for next heartbeat.
    db = os.path.join(BASE, "controller.db")
    try:
        con = _db_connect(db)
        try:
            con.execute(
                "INSERT INTO outbox (kind, payload, created_at) VALUES (?,?,?)",
                ("software_update", json.dumps(result), _utcnow()))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print(f"could not write update result to outbox: {e}", file=sys.stderr)
    print(json.dumps(result))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
