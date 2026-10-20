"""Network modes: hotspot client vs AP at 192.168.4.1 (README 6.2).

Boot: look for saved hotspot ~60s -> join, else start AP.
While AP has a client, don't switch (single radio can't do both reliably).
Update mode: drop AP, join hotspot N minutes, return to AP.
"""
from __future__ import annotations

import subprocess
import time

AP_IP = "192.168.4.1"
BUS_LAN = "10.9.0.0/24"
STATIC_IPS = {"front": "10.9.0.1/24", "side": "10.9.0.2/24", "rear": "10.9.0.3/24"}
FRONT_STATE_URL = "http://10.9.0.1:8090/state"


def run(*args: str) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout + p.stderr
    except FileNotFoundError:
        return 127, "not found"


def boot_network(saved_ssids: list[str], ap_name: str, role: str = "front",
                wait_s: int = 60) -> str:
    """Return 'hotspot' or 'ap'. Test seam: pass saved_ssids seen by scan."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        code, out = run("nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "no")
        if code == 0:
            seen = set(out.split())
            for s in saved_ssids:
                if s in seen:
                    run("nmcli", "con", "up", s)
                    set_bus_lan(role)
                    return "hotspot"
        else:
            break  # no NM on dev machine -> AP
        time.sleep(5)
    start_ap(ap_name)
    set_bus_lan(role)
    return "ap"


def start_ap(ap_name: str):
    run("nmcli", "con", "up", ap_name)


def set_bus_lan(role: str):
    """Static wired bus LAN (README 6.1): 10.9.0.x/24, no gateway/DNS."""
    ip = STATIC_IPS.get(role, STATIC_IPS["front"])
    run("nmcli", "con", "add", "type", "ethernet", "ifname", "eth0",
        "con-name", "bus-lan", "ip4", ip, "ipv4.never-default", "yes")
    run("nmcli", "con", "up", "bus-lan")


def update_mode(saved_ssid: str, minutes: int = 5):
    run("nmcli", "con", "down", "NSL-ap")
    run("nmcli", "con", "up", saved_ssid)
    time.sleep(minutes * 60)
    run("nmcli", "con", "up", "NSL-ap")
