"""Player service: audible announcements on the bus PA (front Pi only).

Watches Core state: when the current stop changes, plays the next-stop chain
for the new stop through the configured ALSA device (USB audio — onboard
audio is off because HUB75 uses the same PWM hardware). Manual triggers
(keypad/portal/local UI) play at their call site; this loop covers the
automatic stop-advance announcements.

Runs on device via systemd (deploy/nsl-player.service).
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.core import Core
    from nsl_pi.player import play_announcement
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", ""))
    if ROLE != "front":
        while True:
            time.sleep(3600)
        return
    core = Core(db, os.path.join(BASE, "programs"))
    last_stop = core.state().get("stop_id")
    print("nsl-player watching stops (role=front)", flush=True)
    while True:
        try:
            st = core.state()
            if st["program_id"] and st["stop_id"] and st["stop_id"] != last_stop:
                last_stop = st["stop_id"]
                clips = core.announcement_clips("next")
                if clips:
                    rep = play_announcement(clips, BASE)
                    print(f"player: next-stop {len(clips)} clip(s) played={rep.get('played')} "
                          f"device={rep.get('device')}", flush=True)
                else:
                    print("player: stop changed, no audio clips (silent)", flush=True)
        except Exception as e:
            print("player cycle failed:", e, flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
