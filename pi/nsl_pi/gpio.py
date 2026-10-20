"""Optional GPIO bindings (plan.md 6.7). Only free pins; matrix pins greyed out."""
from __future__ import annotations

import time

PRESS_WINDOW = {"double_ms": 400, "long_ms": 800}


class GpioDispatcher:
    """Maps pin events -> ordered action lists. Hardware-agnostic for tests."""

    def __init__(self, bindings: list[dict], actions: list[dict]):
        self.bindings = {b["id"]: b for b in bindings}
        self.by_pin = {}
        for b in bindings:
            self.by_pin.setdefault(int(b["pin"]), []).append(b)
        self.actions: dict[tuple[str, str], list[dict]] = {}
        for a in actions:
            self.actions.setdefault((a["binding_id"], a["press_type"]), []).append(a)
        for v in self.actions.values():
            v.sort(key=lambda r: r["step_order"])
        self._press_t: dict[int, float] = {}
        self._last_tap: dict[int, float] = {}

    def edge(self, pin: int, level: int, now: float | None = None) -> list[dict]:
        """Feed a level change; returns action lists to run (possibly empty)."""
        now = time.monotonic() if now is None else now
        out: list[dict] = []
        bs = self.by_pin.get(int(pin), [])
        if not bs:
            return out
        b = bs[0]
        active_low = bool(b.get("active_low", 1))
        pressed = (level == 0) if active_low else (level == 1)
        if pressed:
            self._press_t[pin] = now
        else:
            t0 = self._press_t.pop(pin, now)
            dur_ms = (now - t0) * 1000
            if dur_ms >= PRESS_WINDOW["long_ms"]:
                ptype = "long"
            else:
                last = self._last_tap.get(pin, 0)
                ptype = "double" if (now - last) * 1000 < PRESS_WINDOW["double_ms"] else "press"
                self._last_tap[pin] = now
            out = list(self.actions.get((b["id"], ptype), []))
        return out
