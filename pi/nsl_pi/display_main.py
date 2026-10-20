"""Display loop: render snapshots at ~10 Hz (front) or on state change."""
from __future__ import annotations

import os

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")


def main():
    from .service_main import main as svc
    svc()


if __name__ == "__main__":
    main()
