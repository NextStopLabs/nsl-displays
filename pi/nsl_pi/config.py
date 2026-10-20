"""Matrix options + display outputs config (README 17). Same names as rpi-rgb-led-matrix flags."""
from __future__ import annotations

import json
import sqlite3

DEFAULTS = {
    "led-rows": 40,
    "led-cols": 80,
    "led-chain": 3,
    "led-parallel": 1,
    "led-brightness": 70,
    "led-gpio-mapping": "regular",
    "led-slowdown-gpio": 4,
    "led-row-addr-type": 0,
    "led-multiplexing": 0,
    "led-pwm-bits": 11,
    "led-limit-refresh": 0,
    "no-hardware-pulse": True,
    "hardware-pulse": False,
    "panel-width": 0,
}

# GPIO pins reserved by matrix mapping with parallel=1 (regular mapping, chain [1] only).
# Control lines shared; chain-1 colour data pins. Conservative set.
RESERVED_PINS_REGULAR_P1 = {7, 11, 12, 15, 16, 18, 22, 10, 13, 19, 21, 23, 24, 26}
# physical header pins above; BCM equivalents for config UI greying (approx mapping):
# header 7=BCM4, 11=BCM17, 12=BCM18, 15=BCM22, 16=BCM23, 18=BCM24, 22=BCM25,
# 10=BCM15, 13=BCM27, 19=BCM10, 21=BCM9, 23=BCM11, 24=BCM8, 26=BCM7
RESERVED_BCM_P1 = {4, 17, 18, 22, 23, 24, 25, 15, 27, 10, 9, 11, 8, 7}

WIRING_PRESETS = {
    "bus-10m": [
        {"display_name": "front", "width_px": 240, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "transport": "local", "receiver_addr": None, "program_display": "front"},
        {"display_name": "side", "width_px": 160, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "transport": "network", "receiver_addr": "10.9.0.2", "program_display": "side"},
        {"display_name": "rear", "width_px": 80, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "transport": "network", "receiver_addr": "10.9.0.3", "program_display": "rear"},
    ],
    "bench-3chain": [
        {"display_name": "front", "width_px": 240, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "transport": "local", "receiver_addr": None, "program_display": "front"},
    ],
    "bench-single-6": [
        {"display_name": "front", "width_px": 480, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "transport": "local", "receiver_addr": None, "program_display": "front"},
    ],
}


def get_matrix_options(con: sqlite3.Connection) -> dict:
    row = con.execute("SELECT matrix_options FROM controller_config WHERE id=1").fetchone()
    opts = dict(DEFAULTS)
    if row and row[0]:
        try:
            opts.update(json.loads(row[0]))
        except Exception:
            pass
    return opts


def set_matrix_options(con: sqlite3.Connection, opts: dict):
    import datetime
    merged = dict(DEFAULTS)
    merged.update(opts)
    con.execute("UPDATE controller_config SET matrix_options=?, version=version+1, updated_at=? WHERE id=1",
                (json.dumps(merged), datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    con.commit()


def reserved_bcm_pins(gpio_mapping: str = "regular", parallel: int = 1) -> set[int]:
    if gpio_mapping == "regular" and parallel == 1:
        return set(RESERVED_BCM_P1)
    # conservative: all matrix-capable pins reserved for other mappings
    return set(RESERVED_BCM_P1)
