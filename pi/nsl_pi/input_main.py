"""Input service: cab USB keypad -> Core commands (front Pi only).

Reads evdev key-down events, runs them through KeypadEntry, dispatches to
Core.apply_command. Announcement results are spoken through the PA.
Entry echo on the boards and hotspot update-mode are logged, not yet wired.
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


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
