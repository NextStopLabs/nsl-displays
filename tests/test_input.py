import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pi.nsl_pi.input_keypad import KeypadEntry  # noqa: E402
from pi.nsl_pi.gpio import GpioDispatcher  # noqa: E402


def test_autoselect_8th():
    k = KeypadEntry(auto_select=True)
    ev = None
    for d in "1000000":
        ev = k.press(f"KEY_{d}")
        assert ev["kind"] == "digit"
    ev = k.press("KEY_1")
    assert ev["kind"] == "select" and ev["code"] == "10000001"


def test_enter_required_mode():
    k = KeypadEntry(auto_select=False)
    for d in "10000001":
        k.press(f"KEY_{d}")
    ev = k.press("KEY_ENTER")
    assert ev["kind"] == "select"


def test_backspace_clear_timeout():
    k = KeypadEntry(auto_select=False, timeout_ms=1)
    k.press("KEY_1")
    import time
    time.sleep(0.01)
    ev = k.press("KEY_2")
    assert ev["buf"] == "2"  # timed out, restarted
    k.press("KEY_3")
    ev = k.press("KEY_BACKSPACE")
    assert ev["buf"] == "2"
    ev = k.press("KEY_ESC")
    assert ev["buf"] == ""


def test_numpad_action_keys():
    k = KeypadEntry()
    assert k.press("KEY_KPPLUS")["action"] == "stop_next"
    assert k.press("KEY_KPMINUS")["action"] == "stop_prev"
    assert k.press("KEY_KPASTERISK")["action"] == "announce_next"
    assert k.press("KEY_KPSLASH")["action"] == "override_clear"
    assert k.press("KEY_KPDOT")["action"] == "net_update_mode"


def test_enter_selects_short_code():
    k = KeypadEntry(auto_select=True)
    for d in "40101":
        ev = k.press(f"KEY_{d}")
    assert ev["kind"] == "digit"  # short codes never auto-select
    ev = k.press("KEY_ENTER")
    assert ev["kind"] == "select" and ev["code"] == "40101"
    ev = k.press("KEY_ENTER")
    assert ev["kind"] == "enter"  # empty buffer: nothing to submit


def test_gpio_press_long_double():
    ds = GpioDispatcher(
        [{"id": "b1", "pin": 17, "active_low": 1}],
        [{"id": "a1", "binding_id": "b1", "press_type": "press", "step_order": 0, "action": "stop_next", "action_param": None},
         {"id": "a2", "binding_id": "b1", "press_type": "long", "step_order": 0, "action": "net_update_mode", "action_param": None}])
    assert ds.edge(17, 0, now=100.0) == []
    acts = ds.edge(17, 1, now=100.2)
    assert acts and acts[0]["action"] == "stop_next"
    ds.edge(17, 0, now=200.0)
    acts = ds.edge(17, 1, now=201.0)
    assert acts and acts[0]["action"] == "net_update_mode"
