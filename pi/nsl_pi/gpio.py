"""Optional GPIO bindings (plan.md 6.7). Only free pins; matrix pins greyed out.

Two models, both hardware-agnostic and tested without hardware:

- :class:`GpioDispatcher` - momentary buttons: pin events -> ordered action
  lists for press / long / double press (keypad-style actions).
- :class:`GpioRulesEngine` - per-program display rules: pin + trigger ->
  board/audio effect. Triggers are ``press`` / ``long`` / ``double``
  (momentary), ``on_active`` (rising-edge one-shot, e.g. door just opened),
  ``while_active`` (latched overlay while the pin is held, e.g. reverse gear
  or a toggle switch on), ``on_release`` (falling-edge one-shot, e.g.
  door just closed) and ``toggle`` (each tap flips a latched board on/off).
"""""
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


LEVEL_TRIGGERS = ("press", "long", "double", "on_active", "while_active", "on_release", "toggle")


class GpioRulesEngine:
    """Matches pin level changes against per-program gpio rules.

    Rules are plain dicts with ``pin``, ``trigger``, ``active_low``,
    ``position`` (and effect fields the caller applies). Electrical config
    is per pin: the lowest-position rule on a pin defines ``active_low`` /
    ``debounce_ms`` for that pin.
    """

    def __init__(self, rules: list[dict]):
        self.rules = sorted(rules or [], key=lambda r: (r.get("position") or 0))
        self.by_pin: dict[int, list[dict]] = {}
        for r in self.rules:
            try:
                pin = int(r["pin"])
            except (TypeError, ValueError):
                continue
            self.by_pin.setdefault(pin, []).append(r)
        self.level: dict[int, bool] = {}  # pin -> currently active
        self._press_t: dict[int, float] = {}
        self._last_tap: dict[int, float] = {}

    def pin_config(self, pin: int) -> dict:
        """Electrical config for a pin (first rule wins)."""
        rs = self.by_pin.get(int(pin), [])
        if not rs:
            return {"active_low": True, "pull": "up", "debounce_ms": 50}
        return {"active_low": bool(rs[0].get("active_low", 1)),
                "pull": rs[0].get("pull") or "up",
                "debounce_ms": int(rs[0].get("debounce_ms") or 50)}

    def level_event(self, pin: int, active: bool, now: float | None = None) -> list[dict]:
        """Feed a debounced level change. Returns one-shot rules that fired
        (``press`` / ``long`` / ``double`` / ``on_active`` / ``on_release`` /
        ``toggle``). ``while_active`` rules never fire here - read them via
        :meth:`active_overlays`. ``toggle`` fires on every release (tap or
        hold); the caller flips the latched board on/off."""
        now = time.monotonic() if now is None else now
        pin = int(pin)
        rules = self.by_pin.get(pin, [])
        if not rules:
            return []
        was = self.level.get(pin, False)
        self.level[pin] = bool(active)
        fired: list[dict] = []
        if active and not was:
            # rising edge: press-timing starts, on_active fires
            self._press_t[pin] = now
            fired.extend(r for r in rules if r.get("trigger") == "on_active")
        elif not active and was:
            # falling edge: resolve press/long/double from hold time
            t0 = self._press_t.pop(pin, now)
            dur_ms = (now - t0) * 1000
            if dur_ms >= PRESS_WINDOW["long_ms"]:
                ptype = "long"
            else:
                last = self._last_tap.get(pin, 0)
                ptype = "double" if (now - last) * 1000 < PRESS_WINDOW["double_ms"] else "press"
                self._last_tap[pin] = now
            fired.extend(r for r in rules if r.get("trigger") == ptype)
            fired.extend(r for r in rules if r.get("trigger") == "on_release")
            fired.extend(r for r in rules if r.get("trigger") == "toggle")
        fired.sort(key=lambda r: (r.get("position") or 0))
        return fired

    def active_overlays(self) -> list[dict]:
        """``while_active`` rules whose pin is currently held active."""
        return [r for r in self.rules
                if r.get("trigger") == "while_active" and self.level.get(int(r["pin"]), False)]
