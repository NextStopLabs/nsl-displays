"""Canonical content_hash per format.md 8.1.

SHA-256 of canonical text dump of every table except meta:
tables in alphabetical order, rows sorted by primary key,
each row one line of JSON with sorted keys and no extra whitespace.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

HASH_TABLES = [
    "audio",
    "destination_pages",
    "destinations",
    "displays",
    "gpio_rules",
    "layout_templates",
    "layouts",
    "override_presets",
    "services",
    "stops",
]


def _table_pk(cur: sqlite3.Cursor, table: str) -> str:
    cols = cur.execute(f"PRAGMA table_info({table})").fetchall()
    # row: cid, name, type, notnull, dflt, pk
    pks = [(r[5], r[1]) for r in cols if r[5]]
    pks.sort()
    if pks:
        return pks[0][1]
    return "rowid"


def canonical_dump(con: sqlite3.Connection) -> str:
    cur = con.cursor()
    lines: list[str] = []
    for table in sorted(HASH_TABLES):
        try:
            cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
            if not cur.fetchone():
                continue
            pk = _table_pk(cur, table)
            order = f'ORDER BY "{pk}"' if pk != "rowid" else "ORDER BY rowid"
            rows = cur.execute(f'SELECT * FROM "{table}" {order}').fetchall()
            colnames = [d[0] for d in cur.description]
            for row in rows:
                obj = {colnames[i]: row[i] for i in range(len(colnames))}
                # normalise None stays None; ints stay ints
                lines.append(table + ":" + json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        except sqlite3.Error:
            continue
    return "\n".join(lines) + ("\n" if lines else "")


def content_hash_of_connection(con: sqlite3.Connection) -> str:
    dump = canonical_dump(con)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


def content_hash_of_file(path: str) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        return content_hash_of_connection(con)
    finally:
        con.close()


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
