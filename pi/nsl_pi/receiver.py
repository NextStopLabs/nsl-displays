"""Receiver sync: front Pi publishes state; side/rear poll over bus LAN.

Protocol v1: GET http://10.9.0.1:8090/state -> {fields, layouts, override, version}.
Receivers render with the same Pillow renderer, hold last frame on drop.
"""
from __future__ import annotations

import json
import time
import urllib.request

POLL_HZ = 10


def fetch_state(host: str = "10.9.0.1", port: int = 8090, timeout: float = 2.0) -> dict | None:
    url = f"http://{host}:{port}/state"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None  # hold last frame


class Receiver:
    def __init__(self, display_name: str, width: int, height: int, front_host: str = "10.9.0.1",
                 virtual_dir: str | None = None):
        self.display_name = display_name
        self.width = width
        self.height = height
        self.front_host = front_host
        self.virtual_dir = virtual_dir
        self.last: dict | None = None

    def poll_once(self) -> bool:
        st = fetch_state(self.front_host)
        if st is None:
            return False  # link down: hold last frame
        self.last = st
        self.render(st)
        return True

    def render(self, snapshot: dict):
        from .renderer import (autofit_layout, board_position_for_slot, combined_slot,
                               fields_for_page_index, fields_for_slot, render_frame, slot_for_layout)
        import time as _t
        now = _t.time()
        base = snapshot.get("fields", {})
        pages = snapshot.get("pages") or []
        destination = snapshot.get("destination") or {}
        service = snapshot.get("service") or {}
        svc_number = service.get("number", "") if isinstance(service, dict) else ""
        override = snapshot.get("override") or ""
        layouts = snapshot.get("layouts", {})
        insert = snapshot.get("insert") or None
        if insert and not insert.get("destination"):
            insert = None
        departure = snapshot.get("departure") or {}
        dep_active = bool(departure.get("active")) and bool(departure.get("mins_remaining"))
        if dep_active:
            # F-key departure: every board locks on P1, inserts suppressed.
            # Only the front replaces via; side/rear just freeze.
            insert = None
        layout = None
        if self.display_name in layouts:
            layout = layouts[self.display_name]
            if isinstance(layout, str):
                layout = json.loads(layout)
        if layout is None:
            layout = autofit_layout(self.width, self.height)
        if insert and destination:
            from .renderer import autofit_layout as _af
            ins_pages = insert.get("pages") or []
            ins_layouts = insert.get("layouts") or {}
            kind, idx, _total = combined_slot(
                layout, now, len(pages) if pages else 1,
                len(ins_pages) if ins_pages else 1)
            if kind == "insert":
                ins_layout = ins_layouts.get(self.display_name)
                if isinstance(ins_layout, str):
                    ins_layout = json.loads(ins_layout)
                if ins_layout is None:
                    for k, v in ins_layouts.items():
                        if k.startswith("template:"):
                            ins_layout = json.loads(v) if isinstance(v, str) else dict(v)
                            ins_layout["width"], ins_layout["height"] = self.width, self.height
                            break
                if ins_layout is None:
                    ins_layout = _af(self.width, self.height)
                ins_svc = insert.get("service") or {}
                ins_num = ins_svc.get("number", "") if isinstance(ins_svc, dict) else ""
                ins_dest = insert.get("destination") or {}
                if ins_pages and ins_dest:
                    ordered = sorted(ins_pages, key=lambda p: int(p.get("position", 0) or 0))
                    bpos = int(ordered[idx % len(ordered)].get("position", 0) or 0) if ordered else 0
                    flds = fields_for_page_index(ins_dest, ins_num, ins_pages, idx)
                    frame = render_frame(ins_layout, flds, override, now=now,
                                         pages=ins_pages, board_position=bpos)
                else:
                    frame = render_frame(ins_layout, insert.get("fields", {}), override)
                if self.virtual_dir:
                    import os
                    os.makedirs(self.virtual_dir, exist_ok=True)
                    frame.save(os.path.join(self.virtual_dir, f"{self.display_name}.png"))
                return frame
            if pages and destination:
                ordered = sorted(pages, key=lambda p: int(p.get("position", 0) or 0))
                bpos = int(ordered[idx % len(ordered)].get("position", 0) or 0) if ordered else 0
                fields = fields_for_page_index(destination, svc_number, pages, idx)
                for k, v in base.items():
                    if k.startswith("__"):
                        fields[k] = v
                frame = render_frame(layout, fields, override, now=now, pages=pages,
                                     board_position=bpos)
            else:
                frame = render_frame(layout, base, override)
            if self.virtual_dir:
                import os
                os.makedirs(self.virtual_dir, exist_ok=True)
                frame.save(os.path.join(self.virtual_dir, f"{self.display_name}.png"))
            return frame
        if pages and destination:
            if dep_active:
                fields = fields_for_page_index(destination, svc_number, pages, 0)
                for k, v in base.items():
                    if k.startswith("__"):
                        fields[k] = v
                if self.display_name == "front":
                    mins = int(departure.get("mins_remaining") or 0)
                    fields["via"] = f"Departing in {mins} min" if mins == 1 else f"Departing in {mins} mins"
                ordered0 = sorted(pages, key=lambda p: int(p.get("position", 0) or 0))
                bpos0 = int(ordered0[0].get("position", 0) or 0) if ordered0 else 0
                frame = render_frame(layout, fields, override, now=now, pages=pages,
                                     board_position=bpos0)
            else:
                slot = slot_for_layout(layout, now)
                fields = fields_for_slot(destination, svc_number, layout, now, pages)
                for k, v in base.items():
                    if k.startswith("__"):
                        fields[k] = v
                frame = render_frame(layout, fields, override, now=now, pages=pages,
                                     board_position=board_position_for_slot(pages, slot))
        else:
            flds = dict(base)
            if dep_active and self.display_name == "front":
                mins = int(departure.get("mins_remaining") or 0)
                flds["via"] = f"Departing in {mins} min" if mins == 1 else f"Departing in {mins} mins"
            frame = render_frame(layout, flds, override)
        if self.virtual_dir:
            import os
            os.makedirs(self.virtual_dir, exist_ok=True)
            frame.save(os.path.join(self.virtual_dir, f"{self.display_name}.png"))
        return frame

    def run_forever(self):
        while True:
            self.poll_once()
            time.sleep(1.0 / POLL_HZ)
