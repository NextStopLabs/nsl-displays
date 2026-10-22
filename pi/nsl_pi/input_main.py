"""Input service: cab USB keypad -> Core commands (front Pi only).

Also handles GPIO per-program display rules. The GPIO path is deliberately
split into three pieces to stay robust on noisy vehicle wiring:

1. poll thread  - samples gpiozero DigitalInputDevice pins at 50 Hz and
                  forwards only *changes* to a queue.
2. stabiliser   - waits for a pin to sit at the same level for its debounce
                  window before treating the change as real.
3. worker thread - owns the Core DB writes, applies fired rules, and syncs
                  latched while_active overlays. All DB mutations from GPIO
                  go through this one thread so they can be batched and
                  never hold controller.db open during polling.

Without gpiozero the GPIO pieces idle and the keypad keeps working.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Optional

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


class _GpioStabilizer:
    """Collapse switch bounce / noise by requiring a level to stay stable
    for its debounce window before it is emitted.
    """

    def __init__(self):
        self.pending: dict[int, tuple[bool, float, float]] = {}
        self.stable: dict[int, bool] = {}

    def reset(self, levels: dict[int, bool]) -> None:
        self.pending.clear()
        self.stable = dict(levels)

    def feed(self, pin: int, level: bool, now: float, debounce_ms: int) -> None:
        self.pending[pin] = (level, now, debounce_ms / 1000.0)

    def ready(self, now: float) -> list[tuple[int, bool]]:
        out: list[tuple[int, bool]] = []
        for pin, (level, t, debounce_s) in list(self.pending.items()):
            if now - t >= debounce_s and self.stable.get(pin) != level:
                out.append((pin, level))
        return out

    def commit(self, pin: int, level: bool) -> None:
        self.stable[pin] = level
        self.pending.pop(pin, None)


class _GpioWorker(threading.Thread):
    """Single thread that owns all GPIO-driven Core mutations.

    Reads raw change events from ``event_q``, stabilises them, runs the
    rules engine, and writes overlays/state. Keeping this on its own thread
    means the fast poll loop never holds controller.db open, and a burst of
    noise cannot starve the agent or local UI.
    """

    def __init__(self, core, event_q: queue.Queue):
        super().__init__(daemon=True, name="nsl-gpio-worker")
        self.core = core
        self.q = event_q
        self.engine: Optional[object] = None
        self.stabilizer = _GpioStabilizer()
        self.active_set: set[int] = set()

    def _sync_active_set(self) -> None:
        cur = {p for p, l in self.stabilizer.stable.items() if l}
        if cur == self.active_set:
            return
        self.core.sync_gpio_levels(cur)
        self.active_set = set(cur)

    def _log_summary(self) -> None:
        shown = sorted(set(self.core.gpio_overlay_map()) | set(self.core.gpio_board_map()))
        print(f"nsl-input: gpio active={sorted(self.active_set)} overlays={shown}", flush=True)

    def run(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=0.05)
            except queue.Empty:
                item = None
            now = time.monotonic()
            if item == "stop":
                break

            if isinstance(item, tuple):
                kind = item[0]
                if kind == "config":
                    _, rules, levels = item
                    from nsl_pi.gpio import GpioRulesEngine
                    self.engine = GpioRulesEngine(rules)
                    self.stabilizer.reset(levels)
                    try:
                        self._sync_active_set()
                        self._log_summary()
                    except Exception as e:
                        print(f"nsl-input: gpio initial sync failed ({e})", flush=True)
                elif kind == "levels" and self.engine is not None:
                    _, changed = item
                    for pin, level in changed.items():
                        cfg = self.engine.pin_config(pin)
                        self.stabilizer.feed(pin, level, now, cfg.get("debounce_ms", 50))

            if self.engine is None:
                continue

            fired_any = False
            for pin, level in self.stabilizer.ready(now):
                fired = self.engine.level_event(pin, level, now=now)
                if fired:
                    try:
                        from nsl_pi.player import play_announcement
                        res = self.core.apply_gpio_fired(fired, source="gpio")
                        if res.get("announcement"):
                            prep = play_announcement(res["announcement"], BASE)
                            print(f"nsl-input: gpio announcement played={prep.get('played')}", flush=True)
                    except Exception as e:
                        print(f"nsl-input: gpio pin {pin} action failed ({e})", flush=True)
                self.stabilizer.commit(pin, level)
                print(f"nsl-input: gpio pin {pin} {'active' if level else 'released'} "
                      f"fired={[r.get('label') or r.get('trigger') for r in fired]}", flush=True)
                fired_any = True

            if fired_any:
                try:
                    self._sync_active_set()
                    self._log_summary()
                except Exception as e:
                    print(f"nsl-input: gpio overlay sync failed ({e})", flush=True)


def _gpio_pull_arg(pull: str):
    """Map a rule's pull setting to gpiozero's ``pull_up`` argument.

    up -> True (pin idles high, switch pulls it to ground),
    down -> False, none -> None (floating, something else drives the line).
    """
    if (pull or "up") == "none":
        return None
    return (pull or "up") != "down"


def _gpio_loop(core):
    """Poll GPIO pins for per-program rules. Never crashes the keypad loop."""
    from nsl_pi.gpio import GpioRulesEngine

    event_q: queue.Queue = queue.Queue(maxsize=4096)
    worker = _GpioWorker(core, event_q)
    worker.start()

    devices: dict[int, object] = {}
    last_sig = None
    last_raw: dict[int, bool] = {}
    engine: Optional[GpioRulesEngine] = None

    print("nsl-input: gpio loop started", flush=True)
    try:
        while True:
            try:
                rules = core.gpio_rules()
            except Exception as e:
                print(f"nsl-input: gpio rules load failed ({e})", flush=True)
                rules = []

            sig = tuple(sorted(
                (int(r["pin"]), int(r.get("active_low", 1)), str(r.get("pull", "up")),
                 int(r.get("debounce_ms") or 50), str(r.get("trigger")))
                for r in rules if str(r.get("pin", "")).lstrip("-").isdigit()))

            if sig != last_sig:
                for dev in devices.values():
                    try:
                        dev.close()
                    except Exception:
                        pass
                devices, last_raw, engine = {}, {}, None
                last_sig = sig
                if not rules:
                    time.sleep(5)
                    continue
                try:
                    from gpiozero import DigitalInputDevice
                except ImportError:
                    print("nsl-input: gpiozero missing - gpio rules idle (keypad unaffected)", flush=True)
                    time.sleep(30)
                    continue

                engine = GpioRulesEngine(rules)
                levels: dict[int, bool] = {}
                for pin in engine.by_pin:
                    cfg = engine.pin_config(pin)
                    try:
                        devices[pin] = DigitalInputDevice(
                            pin, pull_up=_gpio_pull_arg(cfg.get("pull", "up")),
                            bounce_time=max(0.01, min(0.5, cfg.get("debounce_ms", 50) / 1000.0)))
                        print(f"nsl-input: watching gpio pin {pin} (active_low={cfg.get('active_low')})",
                              flush=True)
                    except Exception as e:
                        print(f"nsl-input: pin {pin} unavailable ({e})", flush=True)

                for pin, dev in devices.items():
                    cfg = engine.pin_config(pin)
                    raw = dev.value
                    active = (raw == 0) if cfg.get("active_low", True) else (raw == 1)
                    levels[pin] = active
                    last_raw[pin] = active
                event_q.put(("config", rules, levels))

            if not devices:
                time.sleep(5)
                continue

            changed: dict[int, bool] = {}
            for pin, dev in devices.items():
                cfg = engine.pin_config(pin)
                raw = dev.value
                active = (raw == 0) if cfg.get("active_low", True) else (raw == 1)
                if last_raw.get(pin) != active:
                    changed[pin] = active
                    last_raw[pin] = active
            if changed:
                try:
                    event_q.put(("levels", changed), block=False)
                except queue.Full:
                    print("nsl-input: gpio event queue full, dropping samples", flush=True)
            time.sleep(0.02)
    except Exception as e:
        print(f"nsl-input: gpio poll error ({e})", flush=True)
    finally:
        try:
            event_q.put("stop", timeout=1)
        except queue.Full:
            pass


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.core import Core
    from nsl_pi.input_keypad import KeypadEntry, find_keypad
    from nsl_pi.player import play_announcement
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", ""))
    if ROLE != "front":
        while True:
            time.sleep(3600)
        return
    core = Core(db, os.path.join(BASE, "programs"))
    entry = KeypadEntry()
    import threading as _th
    _gpio = _th.Thread(target=_gpio_loop, args=(core,), daemon=True)
    _gpio.start()
    print("nsl-input watching USB keypad (role=front)", flush=True)
    while True:
        path = find_keypad()
        if not path:
            time.sleep(5)
            continue
        try:
            from evdev import InputDevice
            dev = InputDevice(path)
            try:
                dev.grab()
            except Exception:
                pass
            print(f"nsl-input: keypad {dev.name} on {path}", flush=True)
            for ev in dev.read_loop():
                if ev.type != 1 or ev.value != 1:  # key-down only
                    continue
                try:
                    from evdev import ecodes
                    name = ecodes.KEY[ev.code]
                except Exception:
                    continue
                evt = entry.press(name)
                kind = evt.get("kind")
                if kind == "select":
                    try:
                        core.select_code(evt["code"], source="keypad")
                        print(f"nsl-input: code {evt['code']} selected", flush=True)
                    except Exception as e:
                        print(f"nsl-input: code {evt.get('code')} rejected ({e})", flush=True)
                elif kind == "action":
                    action = evt.get("action")
                    if action == "net_update_mode":
                        print("nsl-input: update-mode key (hotspot join not wired yet)", flush=True)
                        continue
                    try:
                        res = core.apply_command({"command": action}, source="keypad")
                        print(f"nsl-input: action {action} applied", flush=True)
                        if isinstance(res, dict) and res.get("announcement"):
                            rep = play_announcement(res["announcement"], BASE)
                            print(f"nsl-input: announcement played={rep.get('played')}", flush=True)
                    except Exception as e:
                        print(f"nsl-input: action {action} failed ({e})", flush=True)
        except Exception as e:
            print("nsl-input: keypad lost, rescanning (%s)" % e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
