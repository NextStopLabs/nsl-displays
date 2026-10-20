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
        self._push_failures: dict[str, int] = {}
        self._push_logged: dict[str, float] = {}
        # Hardware wins whenever the module is importable; virtual PNGs are the
        # fallback for dev machines. (An always-set virtual_dir must never
        # silently disable the panels on a real bus.)
        if HAS_MATRIX:
            o = RGBMatrixOptions()
            # our config keys -> upstream RGBMatrixOptions properties
            # (verified against bindings/python/rgbmatrix/core.pyx)
            wanted = {
                "rows": int(matrix_options.get("led-rows", 40)),
                "cols": int(matrix_options.get("led-cols", 80)),
                "chain_length": int(matrix_options.get("led-chain", 3)),
                "parallel": int(matrix_options.get("led-parallel", 1)),
                "brightness": int(matrix_options.get("led-brightness", 70)),
                "hardware_mapping": str(matrix_options.get("led-gpio-mapping", "regular")),
                "gpio_slowdown": int(matrix_options.get("led-slowdown-gpio", 4)),
                "pwm_bits": int(matrix_options.get("led-pwm-bits", 11)),
                "disable_hardware_pulsing": bool(matrix_options.get("no-hardware-pulse", True)),
            }
            for attr, val in wanted.items():
                if hasattr(o, attr):
                    setattr(o, attr, val)
                else:
                    # never crash-loop the bus over a renamed option; shout instead
                    print(f"display driver: RGBMatrixOptions has no {attr!r} "
                          f"(library version?) — skipped", flush=True)
            print(f"display driver: options rows={wanted['rows']} cols={wanted['cols']} "
                  f"chain={wanted['chain_length']} parallel={wanted['parallel']} "
                  f"brightness={wanted['brightness']} mapping={wanted['hardware_mapping']} "
                  f"slowdown={wanted['gpio_slowdown']} pwm_bits={wanted['pwm_bits']} "
                  f"no_hw_pulse={wanted['disable_hardware_pulsing']}", flush=True)
            # The library drops root -> daemon on matrix creation by default.
            # We must stay root: state DB, virtual PNGs and lazy imports all
            # live under root-owned paths (a daemon-uid process gets EACCES,
            # surfacing as bogus "No module named" errors). Same as upstream
            # sample flag --led-no-drop-privs.
            if hasattr(o, "drop_privileges"):
                o.drop_privileges = False
            else:
                print("display driver: RGBMatrixOptions has no 'drop_privileges' "
                      "(library version?) — file access may break after init", flush=True)
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
        name = out.get("display_name", "?")
        pushed_hw = False
        if self.matrix is not None:
            try:
                xo = int(out.get("x_offset", 0) or 0)
                self.matrix.SetImage(frame, xo, 0)
                pushed_hw = True
                if self._push_failures.get(name):
                    print(f"display driver: {name} SetImage recovered after "
                          f"{self._push_failures[name]} failures", flush=True)
                    self._push_failures[name] = 0
            except Exception as e:
                n = self._push_failures.get(name, 0) + 1
                self._push_failures[name] = n
                import time as _t
                last = self._push_logged.get(name, 0)
                if n == 1 or _t.time() - last > 60:
                    print(f"display driver: {name} SetImage FAILED x{n} ({e}) — "
                          f"panels dark but software alive; check power/HAT/ribbon", flush=True)
                    self._push_logged[name] = _t.time()
        # debug screenshots: NSL_DEBUG_PNG=1 writes what we SEND (proves content
        # vs hardware). Throttled; view at local-ui /api/debug.png?output=<name>.
        import os as _os
        if self.virtual_dir and (_os.environ.get("NSL_DEBUG_PNG") or self.matrix is None):
            import time as _t
            last_png = self._push_logged.get("png:" + name, 0)
            if pushed_hw and _t.time() - last_png < 2.0:
                return
            self._push_logged["png:" + name] = _t.time()
            try:
                _os.makedirs(self.virtual_dir, exist_ok=True)
                frame.save(_os.path.join(self.virtual_dir, f"{name}.png"))
            except Exception:
                pass

    def test_pattern(self, kind: str = "white"):
        from PIL import Image
        for out in self.outputs:
            w, h = int(out["width_px"]), int(out["height_px"])
            colour = {"white": (255, 255, 255), "red": (255, 0, 0), "amber": (255, 176, 0)}.get(kind, (255, 255, 255))
            self._push(out, Image.new("RGB", (w, h), colour))
