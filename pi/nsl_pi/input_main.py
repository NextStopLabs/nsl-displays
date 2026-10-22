"""Input service: cab USB keypad -> Core commands (front Pi only).

Reads evdev key-down events, runs them through KeypadEntry, dispatches to
Core.apply_command. Announcement results are spoken through the PA.
Entry echo on the boards and hotspot update-mode are logged, not yet wired.

Also polls GPIO pins for per-program display rules (door, reverse, toggle
switches): level changes go through GpioRulesEngine, one-shot effects via
Core.apply_gpio_fired and latched while_active boards via
Core.sync_gpio_levels. Without gpiozero installed (dev laptop) the GPIO
loop idles - keypad keeps working.
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


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
    engine = None
    devices: dict[int, object] = {}
    last_sig = None
    last_levels: dict[int, bool] = {}
    last_active_set: set[int] = set()
    print("nsl-input: gpio loop started", flush=True)
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
            devices, last_levels = {}, {}
            engine = GpioRulesEngine(rules)
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
            # a held switch (reverse gear, toggle on) must assert immediately,
            # not wait for an edge
            if devices:
                try:
                    active = {p for p, d in devices.items()
                              if ((d.value == 0) if engine.pin_config(p).get("active_low", True)
                                  else (d.value == 1))}
                    for p in active:
                        engine.level_event(p, True)
                    core.sync_gpio_levels(active)
                    last_active_set = set(active)
                except Exception as e:
                    print(f"nsl-input: gpio initial sync failed ({e})", flush=True)
        if not devices:
            time.sleep(5)
            continue
        try:
            for pin, dev in list(devices.items()):
                cfg = engine.pin_config(pin)
                raw = dev.value
                active = (raw == 0) if cfg.get("active_low", True) else (raw == 1)
                if last_levels.get(pin) != active:
                    last_levels[pin] = active
                    fired = engine.level_event(pin, active)
                    if fired:
                        try:
                            from nsl_pi.player import play_announcement
                            res = core.apply_gpio_fired(fired, source="gpio")
                            print(f"nsl-input: gpio pin {pin} {'active' if active else 'released'} "
                                  f"fired {[r.get('label') or r.get('trigger') for r in fired]}", flush=True)
                            if res.get("announcement"):
                                prep = play_announcement(res["announcement"], BASE)
                                print(f"nsl-input: gpio announcement played={prep.get('played')}", flush=True)
                        except Exception as e:
                            print(f"nsl-input: gpio pin {pin} action failed ({e})", flush=True)
                    # latched boards follow the live level (reverse off restores).
                    # Only write to controller.db when the active set actually
                    # changes; constant commits every 20 ms starve other services.
                    try:
                        cur = {p for p, d in devices.items()
                               if last_levels.get(p, False)}
                        if cur != last_active_set:
                            core.sync_gpio_levels(cur)
                            last_active_set = set(cur)
                        shown = sorted(set(core.gpio_overlay_map()) | set(core.gpio_board_map()))
                        print(f"nsl-input: gpio pin {pin} {'active' if active else 'released'} "
                              f"fired={[r.get('label') or r.get('trigger') for r in fired]} "
                              f"overlays={shown}", flush=True)
                    except Exception as e:
                        print(f"nsl-input: gpio overlay sync failed ({e})", flush=True)
        except Exception as e:
            print(f"nsl-input: gpio poll error ({e})", flush=True)
        time.sleep(0.02)


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
