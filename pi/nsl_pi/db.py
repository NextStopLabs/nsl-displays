"""Shared SQLite connection helper for Pi services.

Every Pi process (nsl-agent, nsl-input, nsl-display, nsl-core, nsl-player,
nsl-local-ui, update_helper) opens the same controller.db. The defaults of
``sqlite3.connect`` (5 s timeout, no busy_timeout, whatever journal mode the
file happens to be in) produce ``database is locked`` storms under normal
multi-process contention and sustained lock errors when one service holds a
write transaction while another tries to write.

All Pi code must open controller.db through :func:`connect`, which sets:

* ``timeout=30`` + ``PRAGMA busy_timeout = 30000`` - writers wait instead of
  failing on transient contention.
* ``PRAGMA journal_mode = WAL`` - readers never block writers.
* ``PRAGMA synchronous = NORMAL`` - WAL-safe, avoids stalls on slow SD cards.
* ``PRAGMA foreign_keys = ON`` - same integrity behaviour as before.
"""
from __future__ import annotations

import sqlite3

BUSY_TIMEOUT_MS = 30000


def connect(path: str, *, timeout: float = 30.0,
            row_factory=None) -> sqlite3.Connection:
    """Open controller.db (or any Pi sqlite file) with contention-safe PRAGMAs."""
    con = sqlite3.connect(path, timeout=timeout, check_same_thread=False)
    try:
        con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
    except Exception:
        con.close()
        raise
    if row_factory is not None:
        con.row_factory = row_factory
    return con
