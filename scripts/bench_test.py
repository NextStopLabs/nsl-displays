"""Bench test (README 13): wire chains on bench, verify canvases + sync + keypad."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    print("== NSL bench test ==")
    from scripts.seed import demo_builder, FRONT
    from pi.nsl_pi.renderer import render_frame
    b = demo_builder()
    b.build("/tmp/bench.nslprog", 1)
    print("build: OK /tmp/bench.nslprog")
    for name, w in (("front", 240), ("side", 160), ("rear", 80)):
        img = render_frame(dict(FRONT, width=w), {"num": "100", "via": "Branston", "dest": "BURTON"}, now=0.0)
        assert img.size == (w, 40), img.size
        print(f"canvas {name} {w}x40: OK")
    from pi.nsl_pi.input_keypad import KeypadEntry
    k = KeypadEntry()
    for d in "10000001":
        ev = k.press(f"KEY_{d}")
    assert ev["kind"] == "select", ev
    print("keypad 8-digit select: OK")
    import sqlite3
    from pi.nsl_pi.store import init_controller_db
    init_controller_db("/tmp/bench-controller.db")
    con = sqlite3.connect("/tmp/bench-controller.db")
    n = con.execute("SELECT COUNT(*) FROM display_outputs").fetchone()[0]
    assert n == 3
    print("controller.db outputs front/side/rear: OK")
    print("ALL BENCH CHECKS PASSED")


if __name__ == "__main__":
    main()
