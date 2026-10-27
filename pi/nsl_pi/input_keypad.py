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

# Faceplate T9 legends (NC1 keypad): 1=ABC 2=DEF 3=GHI 4=JKL 5=MNO
# 6=PQR 7=STU 8=VWX 9=YZ 0=space. Multi-tap cycles the group.
T9_GROUPS = {
    "1": "ABC", "2": "DEF", "3": "GHI", "4": "JKL", "5": "MNO",
    "6": "PQR", "7": "STU", "8": "VWX", "9": "YZ", "0": " ",
}


def t9_letters(digit: str) -> str:
    """Letter group for one digit key (faceplate legends)."""
    return T9_GROUPS.get(str(digit or ""), "")


def encode_t9(text: str) -> str:
    """Map letters to their digit keys (predictive search, e.g. SOT -> 757)."""
    out = []
    for ch in str(text or "").upper():
        if ch == " ":
            out.append("0")
            continue
        for digit, group in T9_GROUPS.items():
            if ch in group:
                out.append(digit)
                break
    return "".join(out)


class T9Tap:
    """Multi-tap letter entry on the digit keys for rail station names.

    Repeated presses of the SAME digit cycle its letter group (7 -> S, T,
    U, 7); pressing a different digit commits the pending letter and starts
    a new one. A pause longer than ``cycle_ms`` also commits.
    """

    def __init__(self, cycle_ms: int = 1000):
        self.text = ""
        self._pending = ""
        self._digit = ""
        self._idx = 0
        self.cycle_ms = cycle_ms
        self._last = time.monotonic()

    def _commit(self) -> None:
        if self._pending:
            self.text += self._pending
            self._pending, self._digit, self._idx = "", "", 0

    def press(self, digit: str, now: float | None = None) -> str:
        """Feed one digit press. Returns the display buffer (committed +
        pending letter)."""
        now = time.monotonic() if now is None else now
        group = t9_letters(digit)
        if not group:
            return self.text + self._pending
        cycle = group + digit  # S,T,U,7… (classic multi-tap ends on the digit)
        if digit == self._digit and (now - self._last) * 1000 <= self.cycle_ms:
            self._idx = (self._idx + 1) % len(cycle)
            self._pending = cycle[self._idx]
        else:
            self._commit()
            self._digit, self._idx = digit, 0
            self._pending = cycle[0]
        self._last = now
        return self.text + self._pending

    def backspace(self) -> str:
        if self._pending:
            self._pending, self._digit, self._idx = "", "", 0
        else:
            self.text = self.text[:-1]
        return self.text + self._pending

    def finish(self) -> str:
        self._commit()
        return self.text

    def clear(self) -> str:
        self.text = ""
        self._pending, self._digit, self._idx = "", "", 0
        return ""
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
