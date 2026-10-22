"""A/B software update with signature verify + health check + rollback (README 12).

Slots: /opt/nsl/current -> /opt/nsl/vN. Install bundle into inactive slot,
switch symlink, restart services, health-check (agent, core, player, display, local UI).
Roll back on failure, report via outbox.

Bundles are Ed25519-signed tar.gz archives. The public key is baked in at
install time (or set via NSL_RELEASE_PUBLIC_KEY). Dev/test may set
NSL_SKIP_VERIFY=1 to bypass signature checks.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time

ROOT = os.environ.get("NSL_ROOT", "/opt/nsl")
SERVICES = ["nsl-agent", "nsl-core", "nsl-player", "nsl-display", "nsl-local-ui"]
BUILTIN_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAplaceholder-replace-at-install-time-or-via-env\n"
    "-----END PUBLIC KEY-----\n"
)


def _public_key() -> bytes:
    p = os.environ.get("NSL_RELEASE_PUBLIC_KEY")
    if p:
        if os.path.exists(p):
            return open(p, "rb").read()
        return p.encode()
    key_path = "/etc/nsl/release.pub"
    if os.path.exists(key_path):
        return open(key_path, "rb").read()
    return BUILTIN_PUBLIC_KEY.encode()


def _skip_verify() -> bool:
    return os.environ.get("NSL_SKIP_VERIFY", "").lower() in ("1", "true", "yes")


def verify_bundle(bundle_path: str, signature_hex: str) -> bool:
    """Verify Ed25519 signature of bundle file."""
    if _skip_verify():
        return True
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.exceptions import InvalidSignature
    except Exception:
        # cryptography not available: only allow if explicitly skipped
        return _skip_verify()
    try:
        key = load_pem_public_key(_public_key())
        if not isinstance(key, Ed25519PublicKey):
            return False
        with open(bundle_path, "rb") as f:
            key.verify(bytes.fromhex(signature_hex), f.read())
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


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
    # Write version marker so the helper knows what it is activating.
    with open(os.path.join(inactive, ".nsl-version"), "w") as f:
        f.write(version + "\n")
    return inactive


def health_check(timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok = True
        for s in SERVICES:
            p = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
            if p.stdout.strip() != "active":
                ok = False
                break
        if ok:
            # Also check local UI is responsive.
            try:
                r = subprocess.run(
                    ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:80/api/health"],
                    capture_output=True, timeout=10)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        time.sleep(2)
    return False


def _atomic_symlink(target: str, link_path: str):
    new_link = link_path + ".new"
    try:
        os.remove(new_link)
    except FileNotFoundError:
        pass
    os.symlink(target, new_link)
    os.replace(new_link, link_path)


def _refresh_fonts(slot: str):
    """Copy Hanover fonts from the new slot into /var/lib/nsl/fonts.

    fonts_dir() prefers /var/lib/nsl/fonts when it exists, so without this
    a release carrying updated fonts would still render with stale ones.
    Best-effort: failures must not fail the activation.
    """
    try:
        src_dir = os.path.join(slot, "HANOVERFONTS")
        names = [n for n in os.listdir(src_dir) if n.endswith(".Fnt")]
        if not names:
            return
        dst_dir = "/var/lib/nsl/fonts"
        os.makedirs(dst_dir, exist_ok=True)
        for n in names:
            shutil.copy(os.path.join(src_dir, n), os.path.join(dst_dir, n))
    except Exception:
        pass


def activate(version: str) -> dict:
    """Switch symlink to inactive slot, restart services, rollback on failure.

    Returns a dict with success/failure and previous/current slot paths.
    This function is normally called by update_helper.py after the agent has
    exited, so that it can survive the service restart.
    """
    _, inactive = slots()
    prev = os.path.realpath(os.path.join(ROOT, "current"))
    marker = os.path.join(ROOT, ".pending_activate")
    result = {"ok": False, "version": version, "previous": prev, "current": inactive}
    if not os.path.isdir(inactive):
        print(f"activate {version}: inactive slot missing ({inactive}); aborting", flush=True)
        result["error"] = f"inactive slot not installed: {inactive}"
        return result
    print(f"activate {version}: switching current {prev} -> {inactive}", flush=True)
    _atomic_symlink(inactive, os.path.join(ROOT, "current"))
    print(f"activate {version}: restarting {', '.join(SERVICES)}", flush=True)
    subprocess.run(["systemctl", "restart"] + SERVICES, check=False)
    if health_check():
        print(f"activate {version}: health check passed", flush=True)
        result["ok"] = True
        _refresh_fonts(inactive)
        try:
            os.remove(marker)
        except FileNotFoundError:
            pass
        return result
    # rollback
    print(f"activate {version}: health check FAILED; rolling back to {prev}", flush=True)
    _atomic_symlink(prev, os.path.join(ROOT, "current"))
    subprocess.run(["systemctl", "restart"] + SERVICES, check=False)
    try:
        os.remove(marker)
    except FileNotFoundError:
        pass
    result["error"] = "health check failed; rolled back"
    result["rolled_back_to"] = prev
    return result


def schedule_activation(version: str):
    """Write marker so update_helper knows what to activate on next run."""
    import json
    os.makedirs(ROOT, exist_ok=True)
    with open(os.path.join(ROOT, ".pending_activate"), "w") as f:
        json.dump({"version": version, "scheduled_at": time.time()}, f)


def pending_activation() -> dict | None:
    marker = os.path.join(ROOT, ".pending_activate")
    if not os.path.exists(marker):
        return None
    try:
        import json
        with open(marker) as f:
            return json.load(f)
    except Exception:
        return None


def run_activation_helper(version: str):
    """Fork a background process that will activate the update and survive
    the agent service restart. Helper output goes to NSL_BASE/update.log
    (journal would lose it: the helper outlives the agent service)."""
    # Always invoke as a module so PYTHONPATH (set by the installer) is used.
    cmd = [sys.executable, "-m", "nsl_pi.update_helper", version]
    base = os.environ.get("NSL_BASE", "/var/lib/nsl")
    try:
        os.makedirs(base, exist_ok=True)
        log = open(os.path.join(base, "update.log"), "ab")
    except OSError:
        log = subprocess.DEVNULL
    # start_new_session keeps the helper alive after agent exits
    subprocess.Popen(cmd, start_new_session=True, stdout=log, stderr=subprocess.STDOUT)


def check_and_apply(release_info: dict, bundle_path: str) -> dict:
    """Verify, install to inactive slot, and schedule activation."""
    version = release_info.get("version")
    sig = release_info.get("signature", "")
    sha = release_info.get("sha256", "")
    result = {"version": version, "phase": "verify"}
    if not version:
        result["error"] = "missing version"
        return result
    if sha and sha256_file(bundle_path) != sha.lower():
        result["error"] = "hash mismatch"
        return result
    if not verify_bundle(bundle_path, sig):
        result["error"] = "signature verification failed"
        return result
    result["phase"] = "install"
    try:
        install_bundle(bundle_path, version)
    except Exception as e:
        result["error"] = f"install failed: {e}"
        return result
    schedule_activation(version)
    result["phase"] = "scheduled"
    result["ok"] = True
    return result


def current_version() -> str | None:
    """Read the version marker from the currently active slot."""
    active = os.path.realpath(os.path.join(ROOT, "current"))
    marker = os.path.join(active, ".nsl-version")
    try:
        with open(marker) as f:
            return f.read().strip()
    except Exception:
        return None


def staged_version() -> str | None:
    """Read the version marker from the inactive slot (already downloaded)."""
    _, inactive = slots()
    marker = os.path.join(inactive, ".nsl-version")
    try:
        with open(marker) as f:
            return f.read().strip()
    except Exception:
        return None
