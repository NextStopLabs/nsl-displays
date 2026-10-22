"""Display service entrypoint. Sole owner of the HUB75 matrix (or virtual PNGs).

Runs on device via systemd (deploy/nsl-display.service). No ports, no portal
contact - front re-renders from Core at 10 Hz (scroll/page animation runs off
wall-clock), receivers poll the front Pi and hold last frame on drop.
"""
from __future__ import annotations

import os
import time

BASE = os.environ.get("NSL_BASE", "/var/lib/nsl")
ROLE = os.environ.get("NSL_ROLE", "front")

RENDER_HZ = 10.0
STATE_REFRESH_S = 0.5


def pairing_layout(w: int, h: int, code: str, portal_host: str = "") -> list:
    """Stacked (text, font, y_top, height) rows for the pairing frame.

    Measured with textbbox and vertically centred with gaps - rows never
    overlap and never leave the canvas, on 240x40 down to 80x40.
    """
    from PIL import Image, ImageDraw, ImageFont
    try:
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        probe.textbbox((0, 0), "X", font=ImageFont.load_default(size=10))

        def _font(sz: int):
            return ImageFont.load_default(size=sz)
    except TypeError:  # ancient Pillow without size=
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

        def _font(sz: int):
            return ImageFont.load_default()

    def _meas(text: str, fnt) -> tuple[int, int]:
        bb = probe.textbbox((0, 0), text, font=fnt)
        return max(1, bb[2] - bb[0]), max(1, bb[3] - bb[1])

    code = (code or "------").strip()
    host = (portal_host or "").replace("https://", "").replace("http://", "")[:28]
    _lw, lh = _meas("PAIR", _font(10))
    size = max(12, min(28, h // 2))
    while size > 12:
        cw, ch = _meas(code, _font(size))
        if cw <= w - 8 and ch <= h // 2:
            break
        size -= 1
    rows = [("PAIR", _font(10), lh), (code, _font(size), _meas(code, _font(size))[1])]
    if host and w >= 120:
        hw, hh = _meas(host, _font(10))
        if hw <= w - 8 and lh + ch + hh + 4 <= h:
            rows.append((host, _font(10), hh))
    gap = 2
    total = sum(r[2] for r in rows) + gap * (len(rows) - 1)
    y = max(0, (h - total) // 2)
    out = []
    for text, fnt, th in rows:
        out.append((text, fnt, y, th))
        y += th + gap
    return out


def pairing_frame(out: dict, code: str, portal_host: str = ""):
    """Unpaired boards show the pairing code big instead of blank."""
    from PIL import Image, ImageDraw
    w, h = int(out["width_px"]), int(out["height_px"])
    img = Image.new("RGB", (w, h), "#000000")
    dr = ImageDraw.Draw(img)
    for text, fnt, y, _th in pairing_layout(w, h, code, portal_host):
        dr.text((w // 2, y), text, fill="#FFB000", font=fnt, anchor="ma")
    return img


SPLASH_HOLD_S = 5.0
SPLASH_SCROLL_PX_S = 60.0


def _splash_logo(out: dict):
    """Full-colour logo frame for one output: NSL-Displays-3.png scaled to
    fit (aspect kept) and centred on black. Falls back to blank on error."""
    from PIL import Image
    w, h = int(out["width_px"]), int(out["height_px"])
    frame = Image.new("RGB", (w, h), "#000000")
    try:
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "NSL-Displays-3.png")
        logo = Image.open(logo_path).convert("RGB")
        scale = min(w / logo.width, h / logo.height)
        if scale <= 0:
            return frame
        nw, nh = max(1, int(logo.width * scale)), max(1, int(logo.height * scale))
        logo = logo.resize((nw, nh), Image.LANCZOS)
        frame.paste(logo, ((w - nw) // 2, (h - nh) // 2))
    except Exception as e:
        print(f"display: splash logo failed ({e}) - holding black", flush=True)
    return frame


def _splash_version_frames(out: dict, version: str):
    """Frames scrolling the version string right-to-left across one output."""
    from PIL import Image, ImageDraw, ImageFont
    w, h = int(out["width_px"]), int(out["height_px"])
    try:
        fnt = ImageFont.load_default(size=min(24, h // 2 + 8))
    except TypeError:  # ancient Pillow without size=
        fnt = ImageFont.load_default()
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    try:
        bb = probe.textbbox((0, 0), version, font=fnt)
        tw = max(1, bb[2] - bb[0])
        ascent, descent = fnt.getmetrics() if hasattr(fnt, "getmetrics") else (h // 2, 0)
        th = max(1, ascent + descent)
    except Exception:
        tw, th = max(1, len(version) * 6), 10
    y = max(0, (h - th) // 2)
    step_px = max(1, int(SPLASH_SCROLL_PX_S / RENDER_HZ))
    frames = []
    for x in range(w, -tw - 1, -step_px):
        img = Image.new("RGB", (w, h), "#000000")
        ImageDraw.Draw(img).text((x, y), version, fill="#FFB000", font=fnt)
        frames.append(img)
    return frames


def boot_splash(drv, outputs):
    """Reboot splash: logo in full colour for 5 s, then scroll the running
    software version across, then return (caller boots into normal loop,
    which shows a blank board until a destination is selected)."""
    try:
        from nsl_pi.update import current_version
        version = current_version() or "unknown"
    except Exception:
        version = "unknown"
    local = [o for o in outputs
             if o.get("enabled", 1) and (o.get("transport") or "local") == "local"]
    if not local:
        return
    print(f"display: boot splash logo 5s + version scroll ({version})", flush=True)
    for out in local:
        try:
            drv.last_frames[out["display_name"]] = _splash_logo(out)
            drv._draw(out, drv.last_frames[out["display_name"]])
        except Exception as e:
            print(f"display: splash logo draw failed ({e})", flush=True)
    try:
        drv.present()
    except Exception as e:
        print(f"display: splash logo present failed ({e})", flush=True)
    time.sleep(SPLASH_HOLD_S)
    strips = [(out, _splash_version_frames(out, version)) for out in local]
    depth = max((len(fr) for _, fr in strips), default=0)
    blank = {}
    for i in range(depth):
        for out, fr in strips:
            if i < len(fr):
                try:
                    drv.last_frames[out["display_name"]] = fr[i]
                    drv._draw(out, fr[i])
                except Exception:
                    pass
            elif out["display_name"] not in blank:
                # smaller boards finish first; park them on blank while the
                # widest board finishes its pass.
                try:
                    from PIL import Image as _I
                    drv._draw(out, _I.new("RGB", (int(out["width_px"]),
                                                 int(out["height_px"])), "#000000"))
                    blank[out["display_name"]] = True
                except Exception:
                    pass
        try:
            drv.present()
        except Exception:
            pass
        time.sleep(1.0 / RENDER_HZ)


def pairing_status():
    """(code, portal_url) when this front Pi is unpaired, else (None, portal)."""
    from nsl_pi import pairing as _P
    if _P.is_paired():
        return None, _P.get_portal_url()
    doc = _P.load_pairing() or {}
    code = doc.get("pairing_code")
    return (code, doc.get("portal_url") or _P.get_portal_url())


def main():
    from nsl_pi.store import init_controller_db
    from nsl_pi.display_driver import DisplayDriver
    import json
    import sqlite3
    db = os.path.join(BASE, "controller.db")
    init_controller_db(db, controller_name=os.environ.get("NSL_UNIT", ""))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    outputs = [dict(r) for r in con.execute("SELECT * FROM display_outputs")]
    mo = {}
    try:
        raw = con.execute("SELECT matrix_options FROM controller_config WHERE id=1").fetchone()[0]
        mo = json.loads(raw or "{}")
    except Exception:
        mo = {}
    con.close()
    if not mo:
        mo = {"led-chain": 3 if ROLE == "front" else (2 if ROLE == "side" else 1)}
    drv = DisplayDriver(mo, outputs, virtual_dir=os.path.join(BASE, "virtual"))
    try:
        from nsl_pi.hanover import fonts_dir as _han_dir, list_fonts as _han_list
        _han_found = _han_list()
        print(f"display: {len(_han_found)} Hanover fonts in {_han_dir()}", flush=True)
    except Exception as e:
        print(f"display: Hanover font scan failed ({e})", flush=True)
    # reboot splash: logo in full colour for 5 s, then scroll the running
    # software version across. Afterwards the normal loop takes over, which
    # shows a blank board until a destination/program is selected.
    try:
        boot_splash(drv, outputs)
    except Exception as e:
        print(f"display: boot splash failed ({e})", flush=True)
    if ROLE != "front":
        from nsl_pi.receiver import Receiver
        w, h = {"side": (160, 40), "rear": (80, 40)}[ROLE]
        print(f"nsl-display polling front state (role={ROLE})", flush=True)
        Receiver(ROLE, w, h).run_forever()
        return
    from nsl_pi.core import Core
    core = Core(db, os.path.join(BASE, "programs"))
    print(f"nsl-display rendering {len(outputs)} output(s) at {RENDER_HZ:g} Hz (role={ROLE})", flush=True)
    snap: dict = {}
    last_fetch = 0.0
    last_pair_code: str | None = "__init__"
    last_summary = ""
    frames = 0
    fetch_fails = 0
    empty_snaps = 0
    while True:
        now = time.time()
        if not snap or now - last_fetch >= STATE_REFRESH_S:
            try:
                snap = core.display_snapshot()
            except Exception as e:
                import traceback as _tb
                fetch_fails += 1
                if fetch_fails == 1 or fetch_fails % 20 == 0:
                    print(f"display: snapshot failed x{fetch_fails} ({e}) - holding last frame",
                          flush=True)
                if fetch_fails == 1:
                    print("".join(_tb.format_exception(type(e), e, e.__traceback__)), flush=True)
            last_fetch = now
        code, portal = pairing_status()
        if code and ROLE == "front":
            if code != last_pair_code:
                # static frame: render once per code, not every second
                try:
                    from urllib.parse import urlparse as _up
                    host = _up(portal).netloc or portal
                    for out in drv.outputs:
                        if not out.get("enabled", 1):
                            continue
                        if (out.get("transport") or "local") != "local":
                            continue
                        drv.last_frames[out["display_name"]] = pairing_frame(out, code, host)
                        drv._draw(out, drv.last_frames[out["display_name"]])
                    drv.present()
                    print(f"display: pairing code {code} on boards", flush=True)
                    last_pair_code = code
                except Exception as e:
                    print(f"display: pairing frame failed ({e})", flush=True)
            time.sleep(1.0)
            continue
        last_pair_code = None
        if snap:
            st = snap.get("state", {})
            if not st.get("program_id") and not st.get("override_text"):
                empty_snaps += 1
                if empty_snaps == 1 or empty_snaps % 40 == 0:
                    print("display: nothing selected and no override - boards show blank "
                          f"(x{empty_snaps}; select a destination to fill them)", flush=True)
            else:
                empty_snaps = 0
            summary = (f"prog={st.get('program_id') or '-'} dest={st.get('destination_id') or '-'} "
                       f"stop={st.get('stop_id') or '-'} override={st.get('override_text') or '-'} "
                       f"fields={','.join(sorted((snap.get('fields') or {}).keys())) or '-'} "
                       f"layouts={len(snap.get('layouts') or {})} "
                       f"gpio={','.join(sorted(set(snap.get('gpio_overlay') or {}) | set(snap.get('gpio_boards') or {}))) or '-'}")
            if summary != last_summary:
                print(f"display state: {summary}", flush=True)
                last_summary = summary
            try:
                drv.show_snapshot(snap)
                frames += 1
                if frames == 1:
                    w0 = outputs[0]["width_px"] if outputs else "?"
                    print(f"display: first frame pushed ({len(outputs)} output(s), first {w0}px wide)",
                          flush=True)
            except Exception as e:
                print(f"display: render failed ({e}) - holding last frame", flush=True)
        time.sleep(1.0 / RENDER_HZ)


if __name__ == "__main__":
    main()
