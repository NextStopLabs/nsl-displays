"""Backup: nightly pg_dump + audio/voices/programs/releases copy. Restore drill in docs/operations.md."""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys

STORAGE = os.environ.get("STORAGE_DIR", "./storage")
BACKUP_ROOT = os.environ.get("BACKUP_DIR", "./backups")


def backup() -> str:
    day = datetime.datetime.now().strftime("%Y-%m-%d")
    dest = os.path.join(BACKUP_ROOT, day)
    os.makedirs(dest, exist_ok=True)
    # db
    dburl = os.environ.get("DATABASE_URL", "sqlite:./dev.db")
    if dburl.startswith("sqlite:"):
        src = dburl.split("sqlite:", 1)[1]
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dest, "portal.db"))
    else:
        subprocess.run(["pg_dump", dburl, "-f", os.path.join(dest, "portal.sql")], check=False)
    # storage
    if os.path.isdir(STORAGE):
        shutil.copytree(STORAGE, os.path.join(dest, "storage"),
                        ignore=shutil.ignore_patterns("incoming"), dirs_exist_ok=True)
    print(f"backup -> {dest}")
    return dest


if __name__ == "__main__":
    backup()
