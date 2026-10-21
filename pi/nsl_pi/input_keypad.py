"""USB keypad input via evdev (README 9). Number row + numpad, NumLock-independent."""
from __future__ import annotations

import time

# evdev key names -> (kind, value)
DIGITS = {
    "KEY_0": "0", "KEY_1": "1", "KEY_2": "2", "KEY_3": "3", "KEY_4": "4",
    "KEY_5": "5", "KEY_6": "6", "KEY_7": "7", "KEY_8": "8", "KEY_9": "9",
    "KEY_KP0": "0", "KEY_KP1": "1", "KEY_KP2": "2", "KEY_KP3": "3", "KEY_KP4": "4",
    "KEY_KP5": "5", "KEY_KP6": "6", "KEY_KP7": "7", "KEY_KP8": "8", "KEY_KP9": "9",
}
DEFAULT_MAP = {
    # key_name -> role config
    **{k: {"role": "digit", "digit": v} for k, v in DIGITS.items()},
    "KEY_ENTER": {"role": "enter"},
    "KEY_KPENTER": {"role": "enter"},
    "KEY_BACKSPACE": {"role": "clear_one"},
    "KEY_DELETE": {"role": "clear_all"},
    "KEY_ESC": {"role": "clear_all"},
    "KEY_KPPLUS": {"role": "action", "action": "stop_next"},
    "KEY_KPMINUS": {"role": "action", "action": "stop_prev"},
    "KEY_KPASTERISK": {"role": "action", "action": "announce_next"},
    "KEY_KPSLASH": {"role": "action", "action": "override_clear"},
    "KEY_KPDOT": {"role": "action", "action": "net_update_mode"},
}


class KeypadEntry:
    """1-8 digit entry state machine. Auto-selects at 8 digits (max length),
    or on Enter for shorter codes."""

    def __init__(self, auto_select: bool = True, timeout_ms: int = 5000):
        self.buf = ""
        self.auto_select = auto_select
        self.timeout_ms = timeout_ms
        self._last = time.monotonic()

    def press(self, key_name: str, keymap: dict | None = None) -> dict:
        """Returns event dict: {kind: digit|enter|clear|select|action|invalid|noop}."""
        now = time.monotonic()
        if (now - self._last) * 1000 > self.timeout_ms:
            self.buf = ""
        self._last = now
        cfg = (keymap or DEFAULT_MAP).get(key_name)
        if not cfg:
            return {"kind": "noop", "buf": self.buf}
        role = cfg.get("role")
        if role == "digit":
            if len(self.buf) < 8:
                self.buf += cfg.get("digit", "")
            if self.auto_select and len(self.buf) == 8:
                code, self.buf = self.buf, ""
                return {"kind": "select", "code": code, "buf": ""}
            return {"kind": "digit", "buf": self.buf}
        if role == "enter":
            # Enter confirms any 1-8 digit code (short codes never auto-select).
            if self.buf and len(self.buf) <= 8:
                code, self.buf = self.buf, ""
                return {"kind": "select", "code": code, "buf": ""}
            return {"kind": "enter", "buf": self.buf}
        if role == "clear_one":
            self.buf = self.buf[:-1]
            return {"kind": "clear", "buf": self.buf}
        if role == "clear_all":
            self.buf = ""
            return {"kind": "clear", "buf": self.buf}
        if role == "action":
            return {"kind": "action", "action": cfg.get("action"), "param": cfg.get("action_param"), "buf": self.buf}
        return {"kind": "noop", "buf": self.buf}


def find_keypad(device_match: str = ""):
    """Locate USB keypad via evdev. Returns device path or None (dev machines)."""
    try:
        from evdev import InputDevice, list_devices
    except ImportError:
        return None
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if device_match.lower() in (dev.name or "").lower() or not device_match:
                # heuristic: has KEY_KP0 / KEY_1
                caps = dev.capabilities()
                return path
        except Exception:
            continue
    return None
