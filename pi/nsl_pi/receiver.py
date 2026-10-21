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
        from .renderer import autofit_layout, board_position_for_slot, fields_for_slot, render_frame, slot_for_layout
        import time as _t
        now = _t.time()
        base = snapshot.get("fields", {})
        pages = snapshot.get("pages") or []
        destination = snapshot.get("destination") or {}
        service = snapshot.get("service") or {}
        svc_number = service.get("number", "") if isinstance(service, dict) else ""
        override = snapshot.get("override") or ""
        layouts = snapshot.get("layouts", {})
        layout = None
        if self.display_name in layouts:
            layout = layouts[self.display_name]
            if isinstance(layout, str):
                layout = json.loads(layout)
        if layout is None:
            layout = autofit_layout(self.width, self.height)
        if pages and destination:
            slot = slot_for_layout(layout, now)
            fields = fields_for_slot(destination, svc_number, layout, now, pages)
            for k, v in base.items():
                if k.startswith("__"):
                    fields[k] = v
            frame = render_frame(layout, fields, override, now=now, pages=pages,
                                 board_position=board_position_for_slot(pages, slot))
        else:
            frame = render_frame(layout, base, override)
        if self.virtual_dir:
            import os
            os.makedirs(self.virtual_dir, exist_ok=True)
            frame.save(os.path.join(self.virtual_dir, f"{self.display_name}.png"))
        return frame

    def run_forever(self):
        while True:
            self.poll_once()
            time.sleep(1.0 / POLL_HZ)
