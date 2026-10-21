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
        self._buf = None  # offscreen FrameCanvas, created on first hardware draw
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
                          f"(library version?) - skipped", flush=True)
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
                      "(library version?) - file access may break after init", flush=True)
            self.matrix = RGBMatrix(options=o)
        print(f"display driver: {'HUB75 hardware' if self.matrix is not None else 'virtual PNG output ' + str(virtual_dir)} "
              f"(rgbmatrix module {'present' if HAS_MATRIX else 'MISSING'})", flush=True)

    def show_snapshot(self, snapshot: dict):
        """snapshot from Core.display_snapshot(). Renders each LOCAL output.

        Outputs with transport 'network' belong to other Pis (their own
        nsl-display renders them) - drawing them here would composite several
        frames at the same origin, i.e. garbled flickering text.
        """
        from .renderer import autofit_layout, board_position_for_slot, fields_for_slot, render_frame, slot_for_layout
        import json as _j
        import time as _t
        fields = snapshot.get("fields", {})
        pages = snapshot.get("pages") or []
        destination = snapshot.get("destination") or {}
        service = snapshot.get("service") or {}
        svc_number = service.get("number", "") if isinstance(service, dict) else ""
        override = snapshot.get("override") or ""
        now = _t.time()
        layouts = snapshot.get("layouts", {})
        state = snapshot.get("state", {})
        for out in self.outputs:
            if not out.get("enabled", 1):
                continue
            if (out.get("transport") or "local") != "local":
                continue  # another Pi drives this sign over the bus LAN
            prog_display = out.get("program_display") or ""
            w, h = int(out["width_px"]), int(out["height_px"])
            layout = None
            # layouts keyed by display NAME (front/side/rear); fallback to default
            # template, then auto-fit.
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
                if pages and destination:
                    slot = slot_for_layout(layout, now)
                    page_fields = fields_for_slot(
                        destination, svc_number, layout, now, pages)
                    # keep stop-derived fields (__board_text etc.) from snapshot
                    for k, v in fields.items():
                        if k.startswith("__"):
                            page_fields[k] = v
                    frame = render_frame(layout, page_fields, override,
                                         now=now, pages=pages,
                                         board_position=board_position_for_slot(pages, slot))
                else:
                    frame = render_frame(layout, fields, override)
            except Exception:
                continue  # hold last frame on crash (spec)
            self.last_frames[out["display_name"]] = frame
            self._draw(out, frame)
        self.present()

    def _draw(self, out: dict, frame):
        """Draw one frame into the offscreen buffer (hardware) or a PNG (virtual)."""
        name = out.get("display_name", "?")
        if self.matrix is not None:
            if self._buf is None:
                try:
                    self._buf = self.matrix.CreateFrameCanvas()
                except Exception as e:
                    self._push_failed(name, e)
                    return
            try:
                self._buf.SetImage(frame, int(out.get("x_offset", 0) or 0), 0)
            except Exception as e:
                self._push_failed(name, e)
            return
        if self.virtual_dir:
            import os
            os.makedirs(self.virtual_dir, exist_ok=True)
            try:
                frame.save(os.path.join(self.virtual_dir, f"{name}.png"))
            except Exception:
                pass

    def present(self):
        """Swap the offscreen buffer live (vsync): one swap per frame, no tearing."""
        if self.matrix is None or self._buf is None:
            return
        try:
            self._buf = self.matrix.SwapOnVSync(self._buf)
            if self._push_failures.pop("swap", 0):
                print("display driver: SwapOnVSync recovered", flush=True)
        except Exception as e:
            self._push_failed("swap", e)

    def _push_failed(self, name: str, e: Exception):
        n = self._push_failures.get(name, 0) + 1
        self._push_failures[name] = n
        detail = "SwapOnVSync FAILED" if name == "swap" else f"{name} SetImage FAILED"
        import time as _t
        last = self._push_logged.get(name, 0)
        if n == 1 or _t.time() - last > 60:
            print(f"display driver: {detail} x{n} ({e}) - "
                  f"panels dark but software alive; check power/HAT/ribbon", flush=True)
            self._push_logged[name] = _t.time()

    def _push(self, out: dict, frame):
        """Legacy single-frame push (draw + present). Prefer show_snapshot()."""
        self._draw(out, frame)
        self.present()
        # debug screenshots: NSL_DEBUG_PNG=1 writes what we SEND (proves content
        # vs hardware). Throttled; view at local-ui /api/debug.png?output=<name>.
        import os as _os
        if self.virtual_dir and _os.environ.get("NSL_DEBUG_PNG") and self.matrix is not None:
            import time as _t
            name = out.get("display_name", "?")
            last_png = self._push_logged.get("png:" + name, 0)
            if _t.time() - last_png < 2.0:
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
