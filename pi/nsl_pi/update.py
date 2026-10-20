"""A/B software update with health check + rollback (README 12).

Slots: /opt/nsl/current -> /opt/nsl/vN. Install bundle into inactive slot,
switch symlink, restart, health-check (agent, core, player, display, local UI).
Roll back on failure, report via outbox.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

ROOT = os.environ.get("NSL_ROOT", "/opt/nsl")
SERVICES = ["nsl-agent", "nsl-core", "nsl-player", "nsl-display", "nsl-local-ui"]


def slots() -> tuple[str, str]:
    cur = os.path.join(ROOT, "current")
    active = os.path.realpath(cur) if os.path.islink(cur) else os.path.join(ROOT, "v1")
    n = 2 if active.endswith("v1") else 1
    return active, os.path.join(ROOT, f"v{n}")


def install_bundle(bundle_tar: str, version: str) -> str:
    active, inactive = slots()
    if os.path.isdir(inactive):
        shutil.rmtree(inactive)
    os.makedirs(inactive, exist_ok=True)
    shutil.unpack_archive(bundle_tar, inactive)
    return inactive


def health_check(timeout_s: int = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok = True
        for s in SERVICES:
            p = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
            if p.stdout.strip() != "active":
                ok = False
                break
        if ok:
            return True
        time.sleep(2)
    return False


def activate(version: str) -> bool:
    _, inactive = slots()
    prev = os.path.realpath(os.path.join(ROOT, "current"))
    os.symlink(inactive, os.path.join(ROOT, "current.new"))
    os.replace(os.path.join(ROOT, "current.new"), os.path.join(ROOT, "current"))
    subprocess.run(["systemctl", "restart"] + SERVICES)
    if health_check():
        return True
    # rollback
    os.symlink(prev, os.path.join(ROOT, "current.new"))
    os.replace(os.path.join(ROOT, "current.new"), os.path.join(ROOT, "current"))
    subprocess.run(["systemctl", "restart"] + SERVICES)
    return False
