import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

from nsl_pi.display_driver import DisplayDriver  # noqa: E402


def _outputs():
    return [
        {"display_name": "front", "width_px": 240, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "enabled": 1, "transport": "local", "program_display": "front"},
        {"display_name": "side", "width_px": 160, "height_px": 40, "parallel_chain": 1,
         "x_offset": 0, "enabled": 1, "transport": "network", "receiver_addr": "10.9.0.2",
         "program_display": "side"},
    ]


def test_network_outputs_not_rendered_locally(tmp_path):
    drv = DisplayDriver({}, _outputs(), virtual_dir=str(tmp_path / "v"))
    snap = {"state": {}, "fields": {"num": "100", "via": "", "dest": "BURTON"},
            "layouts": {}, "override": ""}
    drv.show_snapshot(snap)
    assert (tmp_path / "v" / "front.png").exists()
    assert not (tmp_path / "v" / "side.png").exists()
