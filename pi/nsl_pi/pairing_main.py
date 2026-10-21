"""Pairing service: show the code, wait for the portal click, finish setup.

Runs on the front Pi via systemd (deploy/nsl-pairing.service). Loop:

  1. If /var/lib/nsl/device-token (or NSL_TOKEN) exists -> already paired,
     sleep quietly. Boards show normal content.
  2. Else register (reuse live pairing.json), print a big banner with the
     6-char code + portal URL to the journal, and poll /api/device/claim.
  3. On paired -> save_token() (token file + agent drop-in + agent restart),
     print success, loop back to idle.

The LED boards render the same code via display_main while unpaired, so the
operator only needs: run installer -> read code off the board/terminal ->
portal Controllers -> Pair controller -> done. Everything after the click is
automatic (token fetch, agent restart, program sync on next heartbeat).
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
POLL_S = max(5, int(os.environ.get("NSL_PAIR_POLL_S", "10") or 10))


def _banner(code: str, portal: str, expires_at: str = "") -> str:
    line = "=" * 52
    return (f"\n{line}\n"
            f"  NSL CONTROLLER - PAIRING REQUIRED\n"
            f"  Code:  {code}\n"
            f"  Portal: {portal}  -> Controllers -> Pair controller\n"
            f"  Enter the code + a bus name, click Pair. The Pi fetches\n"
            f"  its token automatically and starts syncing.\n"
            + (f"  Code expires: {expires_at}\n" if expires_at else "")
            + f"{line}")


def main() -> None:
    from . import pairing as P

    print("nsl-pairing starting", flush=True)
    announced = ""
    announced_n = 0
    while True:
        try:
            if P.is_paired():
                if announced != "paired":
                    print("nsl-pairing: paired, agent syncs from here", flush=True)
                    announced = "paired"
                time.sleep(60)
                continue
            portal = P.get_portal_url()
            try:
                doc = P.ensure_registered(portal)
            except Exception as e:
                print(f"nsl-pairing: register failed ({e}) - "
                      f"join the hotspot/internet, retrying in {POLL_S}s", flush=True)
                time.sleep(POLL_S)
                continue
            code = doc.get("pairing_code", "")
            if code != announced:
                print(_banner(code, portal, doc.get("expires_at", "") or ""), flush=True)
                announced = code
            else:
                # steady heartbeat in the log so "nothing happens" is never
                # the story: code, which controller, which portal base.
                announced_n += 1
                if announced_n % 6 == 0:
                    print(f"nsl-pairing: still waiting - code {code} at {portal} "
                          f"(controller {doc['controller_id'][:8]}…)", flush=True)
            state, tok, portal = P.try_claim(portal, doc["controller_id"])
            if state == "unknown":
                # server pruned our record (expired): drop it, re-register now.
                print("nsl-pairing: server dropped our code (expired) - re-registering",
                      flush=True)
                try:
                    import os as _os
                    _os.remove(P.pairing_path())
                except OSError:
                    pass
                announced = ""
                continue
            if state == "error":
                print("nsl-pairing: claim unreachable - check portal URL/internet, "
                      f"retrying (tried {portal})", flush=True)
                time.sleep(POLL_S)
                continue
            if tok:
                P.save_token(tok, portal)
                print("nsl-pairing: PAIRED - token saved, nsl-agent restarted, "
                      "boards switch to normal content", flush=True)
                try:
                    from .player import play_test_tone
                    rep = play_test_tone(BASE)
                    if rep.get("played"):
                        print(f"nsl-pairing: test audio played on {rep.get('device')} "
                              f"(PA chain OK)", flush=True)
                    else:
                        print(f"nsl-pairing: test audio NOT heard ({rep.get('reason')}) - "
                              f"check USB audio, PA volume, then POST /api/audio-test", flush=True)
                except Exception as e:
                    print(f"nsl-pairing: test audio failed ({e})", flush=True)
                announced = ""
                continue
            time.sleep(POLL_S)
        except Exception as e:  # never crash-loop the service
            print(f"nsl-pairing: loop error ({e})", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
