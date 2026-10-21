"""Pairing handshake: register -> show code -> poll claim -> save token.

Fresh front Pi has no token. This module is the glue between the board,
the terminal and the portal so pairing needs one code and one click:

  1. ensure_registered() calls POST /api/device/register, stores
     /var/lib/nsl/pairing.json {controller_id, pairing_code, expires_at}.
  2. display_main + pairing_main show that code on the LED boards and in
     the journal/terminal.
  3. Operator enters the code in portal Controllers -> Pair controller.
  4. wait_for_claim() polls POST /api/device/claim; the portal returns the
     one-time device_token once paired.
  5. save_token() writes the token to disk (0600) + a systemd drop-in for
     nsl-agent, reloads and restarts the agent. Boards switch from the
     pairing frame to normal content automatically.

All network contact is Pi-initiated (hotspot-friendly). Safe to re-run:
registration is reused until expiry, claiming is one-time, saving is atomic.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

DEFAULT_PORTAL = "https://nsldisplays.com/portal"
BASE_ENV = "NSL_BASE"
TOKEN_ENV = "NSL_TOKEN"
PORTAL_ENV = "PORTAL_URL"


def base_dir() -> str:
    return os.environ.get(BASE_ENV, "/var/lib/nsl")


def pairing_path(base: str | None = None) -> str:
    return os.path.join(base or base_dir(), "pairing.json")


def token_path(base: str | None = None) -> str:
    return os.path.join(base or base_dir(), "device-token")


def portal_override_path() -> str:
    return "/etc/systemd/system/nsl-agent.service.d/nsl-portal.conf"


def get_portal_url() -> str:
    if os.environ.get(PORTAL_ENV):
        return os.environ[PORTAL_ENV].rstrip("/")
    # fall back to whatever the last pairing run used
    try:
        with open(pairing_path()) as f:
            doc = json.load(f)
        if doc.get("portal_url"):
            return str(doc["portal_url"]).rstrip("/")
    except Exception:
        pass
    return DEFAULT_PORTAL


def get_token(base: str | None = None) -> str | None:
    if os.environ.get(TOKEN_ENV):
        return os.environ[TOKEN_ENV]
    try:
        with open(token_path(base)) as f:
            tok = f.read().strip()
        return tok or None
    except Exception:
        return None


def is_paired(base: str | None = None) -> bool:
    return bool(get_token(base))


def load_pairing(base: str | None = None) -> dict | None:
    try:
        with open(pairing_path(base)) as f:
            return json.load(f)
    except Exception:
        return None


def pairing_valid(doc: dict | None) -> bool:
    if not doc or not doc.get("controller_id") or not doc.get("pairing_code"):
        return False
    try:
        # expires_at is "%Y-%m-%dT%H:%M:%SZ" — UTC, so calendar.timegm, NOT
        # time.mktime (mktime reads it as local time; on UTC+ zones a fresh
        # code looked expired and the Pi re-registered in a tight loop).
        import calendar as _cal
        exp = _cal.timegm(time.strptime(doc.get("expires_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
        return exp > time.time() + 60
    except Exception:
        return False


def ensure_registered(portal_url: str | None = None, base: str | None = None) -> dict:
    """Register with the portal unless a live pairing record already exists."""
    from .agent import Agent

    portal_url = (portal_url or get_portal_url()).rstrip("/")
    base = base or base_dir()
    doc = load_pairing(base)
    if pairing_valid(doc) and doc.get("portal_url", portal_url) == portal_url:
        return doc
    ag = Agent(portal_url, base)
    reg = ag.register()
    doc = {"controller_id": reg["controller_id"], "pairing_code": reg["pairing_code"],
           "expires_at": reg.get("expires_at", ""), "portal_url": portal_url, "paired": False}
    os.makedirs(base, exist_ok=True)
    tmp = pairing_path(base) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f)
    os.replace(tmp, pairing_path(base))
    return doc


def _toggle_portal_base(portal_url: str) -> str | None:
    """Root-hosted <-> /portal-hosted. Returns the alternate base or None."""
    base = portal_url.rstrip("/")
    if base.endswith("/portal"):
        return base[: -len("/portal")]
    return base + "/portal"


def _post_json(portal_url: str, path: str, body: dict, timeout: float = 20.0) -> tuple[dict, str]:
    """POST JSON, tolerating a root-vs-/portal base mismatch.

    Returns (doc, working_base). A 404 whose body is not JSON means the path
    missed the app entirely (proxy has no such prefix) — retry once with the
    alternate base. A JSON 404 (e.g. unknown controller) is authoritative.
    """
    import urllib.error
    import urllib.request
    tried = []
    for base in (portal_url.rstrip("/"), _toggle_portal_base(portal_url.rstrip("/"))):
        if base is None or base in tried:
            continue
        tried.append(base)
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode()), base
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode()
            except Exception:
                raw = ""
            try:
                doc = json.loads(raw)
            except Exception:
                doc = None
            if e.code == 404 and doc is None:
                continue  # proxy miss (non-JSON 404) — try the alternate base
            if doc is not None:
                raise urllib.error.HTTPError(e.url, e.code, json.dumps(doc),
                                             e.hdrs, e.fp)
            raise
    raise urllib.error.HTTPError(portal_url, 404, "portal not found at root or /portal base",
                                 None, None)


def claim_once(portal_url: str, controller_id: str, timeout: float = 20.0) -> dict:
    doc, _ = _post_json(portal_url, "/api/device/claim",
                        {"controller_id": controller_id}, timeout)
    return doc


def try_claim(portal_url: str, controller_id: str) -> tuple[str, str | None, str]:
    """One claim poll. Returns (state, token, working_base) where state is
    'paired' | 'waiting' | 'unknown' (server pruned us — re-register)."""
    import urllib.error
    try:
        doc, base = _post_json(portal_url, "/api/device/claim",
                               {"controller_id": controller_id})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                detail = json.loads(str(e.msg or "")) .get("detail", "")
            except Exception:
                detail = ""
            if "unknown controller" in detail:
                return "unknown", None, portal_url
        return "error", None, portal_url
    except Exception:
        return "error", None, portal_url
    if doc.get("paired") and doc.get("device_token"):
        return "paired", doc["device_token"], base
    return "waiting", None, base


def wait_for_claim(portal_url: str, controller_id: str, poll_s: int = 10,
                   timeout_s: int = 15 * 60) -> str | None:
    """Poll until the portal pairs this controller. Returns device_token or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            doc = claim_once(portal_url, controller_id)
        except Exception:
            time.sleep(poll_s)
            continue
        if doc.get("paired") and doc.get("device_token"):
            return doc["device_token"]
        time.sleep(poll_s)
    return None


def save_token(token: str, portal_url: str | None = None,
               base: str | None = None) -> str:
    """Persist the device token: file (0600) + systemd drop-in + agent restart."""
    base = base or base_dir()
    portal_url = (portal_url or get_portal_url()).rstrip("/")
    os.makedirs(base, exist_ok=True)
    tmp = token_path(base) + ".tmp"
    with open(tmp, "w") as f:
        f.write(token.strip() + "\n")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, token_path(base))
    # mark pairing record consumed (keep controller_id for diagnostics)
    try:
        doc = load_pairing(base) or {}
        doc["paired"] = True
        doc.pop("pairing_code", None)
        doc.pop("expires_at", None)
        doc["portal_url"] = portal_url
        with open(pairing_path(base) + ".tmp", "w") as f:
            json.dump(doc, f)
        os.replace(pairing_path(base) + ".tmp", pairing_path(base))
    except Exception:
        pass
    _write_agent_override(portal_url, token.strip())
    return token_path(base)


def _write_agent_override(portal_url: str, token: str) -> None:
    path = portal_override_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[Service]\n"
                    f'Environment="PORTAL_URL={portal_url}"\n'
                    f'Environment="NSL_TOKEN={token}"\n')
    except OSError:
        # dev machines / tests: token file is enough
        return
    for cmd in (["systemctl", "daemon-reload"],
                ["systemctl", "restart", "nsl-agent"]):
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            pass
