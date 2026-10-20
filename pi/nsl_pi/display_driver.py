"""Display driver: owns HUB75 matrix, renders layouts with Pillow, holds last frame.

Runs as its own systemd service. Falls back to virtual output (PNG/UDP) for dev.
"""
from __future__ import annotations

import json
import time

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions  # type: ignore
    HAS_MATRIX = True
except ImportError:
    HAS_MATRIX = False


class DisplayDriver:
    def __init__(self, matrix_options: dict, outputs: list[dict], virtual_dir: str | None = None):
        self.opts = matrix_options
        self.outputs = outputs
        self.virtual_dir = virtual_dir
        self.matrix = None
        self.last_frames: dict[str, object] = {}
        # Hardware wins whenever the module is importable; virtual PNGs are the
        # fallback for dev machines. (An always-set virtual_dir must never
        # silently disable the panels on a real bus.)
        if HAS_MATRIX:
            o = RGBMatrixOptions()
            o.rows = int(matrix_options.get("led-rows", 40))
            o.cols = int(matrix_options.get("led-cols", 80))
            o.chain_length = int(matrix_options.get("led-chain", 3))
            o.parallel = int(matrix_options.get("led-parallel", 1))
            o.brightness = int(matrix_options.get("led-brightness", 70))
            o.gpio_mapping = str(matrix_options.get("led-gpio-mapping", "regular"))
            o.slowdown_gpio = int(matrix_options.get("led-slowdown-gpio", 4))
            o.pwm_bits = int(matrix_options.get("led-pwm-bits", 11))
            o.disable_hardware_pulsing = bool(matrix_options.get("no-hardware-pulse", True))
            self.matrix = RGBMatrix(options=o)
        print(f"display driver: {'HUB75 hardware' if self.matrix is not None else 'virtual PNG output ' + str(virtual_dir)} "
              f"(rgbmatrix module {'present' if HAS_MATRIX else 'MISSING'})", flush=True)

    def show_snapshot(self, snapshot: dict):
        """snapshot from Core.display_snapshot(). Renders each enabled output."""
        from .renderer import autofit_layout, current_page, render_frame
        import json as _j
        fields = snapshot.get("fields", {})
        override = snapshot.get("override") or ""
        layouts = snapshot.get("layouts", {})
        state = snapshot.get("state", {})
        for out in self.outputs:
            if not out.get("enabled", 1):
                continue
            prog_display = out.get("program_display") or ""
            w, h = int(out["width_px"]), int(out["height_px"])
            layout = None
            # layouts keyed by display *id*; also match by program display name via portal mapping.
            # Here receivers/front pass layout json directly; fallback autofit.
            cand = layouts.get(prog_display) or layouts.get(out.get("display_name"))
            if cand:
                layout = _j.loads(cand) if isinstance(cand, str) else cand
            else:
                # any default template
                for k, v in layouts.items():
                    if k.startswith("template:"):
                        layout = _j.loads(v) if isinstance(v, str) else v
                        layout = dict(layout)
                        layout["width"], layout["height"] = w, h
                        break
            if layout is None:
                layout = autofit_layout(w, h)
            try:
                frame = render_frame(layout, fields, override)
            except Exception:
                continue  # hold last frame on crash (spec)
            self.last_frames[out["display_name"]] = frame
            self._push(out, frame)

    def _push(self, out: dict, frame):
        if self.matrix is not None:
            try:
                xo = int(out.get("x_offset", 0) or 0)
                self.matrix.SetImage(frame, xo, 0)
            except Exception:
                pass  # hold last frame
        elif self.virtual_dir:
            import os
            os.makedirs(self.virtual_dir, exist_ok=True)
            try:
                frame.save(os.path.join(self.virtual_dir, f"{out['display_name']}.png"))
            except Exception:
                pass

    def test_pattern(self, kind: str = "white"):
        from PIL import Image
        for out in self.outputs:
            w, h = int(out["width_px"]), int(out["height_px"])
            colour = {"white": (255, 255, 255), "red": (255, 0, 0), "amber": (255, 176, 0)}.get(kind, (255, 255, 255))
            self._push(out, Image.new("RGB", (w, h), colour))
