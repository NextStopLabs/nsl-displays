"""Seed demo org + Burton routes program (README 13 / plan.md 3.1 seed)."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nslprog import ProgramBuilder  # noqa: E402

FRONT = {"schema": 1, "width": 240, "height": 40, "page_seconds": 3,
         "override": {"mode": "replace", "replaces": "dest", "hide": ["via"]},
         "elements": [
             {"type": "field", "field": "num", "x": 0, "y": 0, "w": 48, "h": 40,
              "font": "pixel-large", "colour": "#FFB000", "align": "centre", "valign": "middle",
              "scroll": "none", "page": 1, "show_if": "always"},
             {"type": "field", "field": "via", "x": 52, "y": 0, "w": 188, "h": 16,
              "font": "pixel-small", "colour": "#FFB000", "align": "left", "valign": "middle",
              "scroll": "if_too_long", "page": 1, "show_if": "has_text"},
             {"type": "field", "field": "dest", "x": 52, "y": 16, "w": 188, "h": 24,
              "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
              "scroll": "if_too_long", "page": 1, "show_if": "always"}]}
SIDE = {"schema": 1, "width": 160, "height": 40, "page_seconds": 3,
        "override": {"mode": "replace", "replaces": "dest", "hide": []},
        "elements": [
            {"type": "field", "field": "num", "x": 0, "y": 0, "w": 40, "h": 40,
             "font": "pixel-large", "colour": "#FFB000", "align": "centre", "valign": "middle",
             "scroll": "none", "page": 1, "show_if": "always"},
            {"type": "field", "field": "dest", "x": 44, "y": 0, "w": 116, "h": 40,
             "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
             "scroll": "if_too_long", "page": 1, "show_if": "always"}]}
REAR = {"schema": 1, "width": 80, "height": 40, "page_seconds": 3,
        "override": {"mode": "full", "replaces": "dest", "hide": []},
        "elements": [
            {"type": "field", "field": "num", "x": 0, "y": 0, "w": 80, "h": 40,
             "font": "pixel-large", "colour": "#FFB000", "align": "centre", "valign": "middle",
             "scroll": "none", "page": 1, "show_if": "always"}]}


def demo_builder() -> ProgramBuilder:
    import hashlib
    import uuid as _uuid
    # deterministic ids so identical data -> identical content_hash (format.md 8.1)
    _seq = {"n": 0}

    def _id(tag: str) -> str:
        _seq["n"] += 1
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"nsl-seed:{tag}"))

    b = ProgramBuilder(program_id=_id("program"), program_name="Burton routes",
                       created_by="seed", generator="seed 1.0.0")
    f = b.add_display("front", 240, 40, 0, id=_id("disp-front"))
    s = b.add_display("side", 160, 40, 1, id=_id("disp-side"))
    r = b.add_display("rear", 80, 40, 2, id=_id("disp-rear"))
    h_next = hashlib.sha256(b"next").hexdigest()
    h_appr = hashlib.sha256(b"approaching").hexdigest()
    h_last = hashlib.sha256(b"terminus").hexdigest()
    for h, lab in ((h_next, "next stop"), (h_appr, "approaching"), (h_last, "terminus")):
        b.add_audio(h, lab, 800, 100)
    svc = b.add_service("100", "Burton to Lichfield", 0, h_next, h_appr, h_last, "both",
                        id=_id("svc-100"))
    d1 = b.add_destination(svc, "10000001", "Burton", "Branston", "BURTON", None, 0, id=_id("dest-burton"))
    d2 = b.add_destination(svc, "10000002", "Lichfield", "", "LICHFIELD", None, 1, id=_id("dest-lich"))
    h1 = hashlib.sha256(b"Lichfield Bus Station").hexdigest()
    h2 = hashlib.sha256(b"Branston Road").hexdigest()
    h3 = hashlib.sha256(b"Burton New Street").hexdigest()
    for h, lab in ((h1, "Lichfield Bus Station"), (h2, "Branston Road"), (h3, "Burton New Street")):
        b.add_audio(h, lab, 900, 100)
    b.add_stop(d1, 1, "Lichfield Bus Station", h1, id=_id("stop-1"))
    b.add_stop(d1, 2, "Branston Road", h2, id=_id("stop-2"))
    b.add_stop(d1, 3, "Burton New Street", h3, board_override="TERMINATING BUS", is_terminus=True,
               id=_id("stop-3"))
    b.add_stop(d2, 1, "Burton New Street", h3, id=_id("stop-4"))
    b.add_stop(d2, 2, "Lichfield Bus Station", h1, is_terminus=True, id=_id("stop-5"))
    for d in (d1, d2):
        b.add_layout(d, f, FRONT, id=_id(f"lay-{d}-front"))
        b.add_layout(d, s, SIDE, id=_id(f"lay-{d}-side"))
        b.add_layout(d, r, REAR, id=_id(f"lay-{d}-rear"))
    b.add_preset("Terminating bus", "TERMINATING BUS", 0, id=_id("preset-term"))
    b.add_preset("Do not board", "DO NOT BOARD", 1, id=_id("preset-dnb"))
    return b


def main(out: str = "seed.nslprog"):
    b = demo_builder()
    info = b.build(out, 1)
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "seed.nslprog")
