"""Board layout renderer — shared by portal preview, main Pi and receivers.

Canvas = real pixels (e.g. 240x40). Layout JSON per format.md 6.2.
Renders with Pillow so portal preview and bus output stay identical.
"""
from __future__ import annotations

import json
import time

from PIL import Image, ImageDraw, ImageFont

# font id -> pixel height mapping (built-in bitmap-ish scaling of default font)
FONT_HEIGHTS = {
    "pixel-small": 8,
    "pixel-medium": 12,
    "pixel-large": 20,
    "pixel-huge": 32,
    "mono-5x7": 7,
    "mono-6x8": 8,
}
DEFAULT_FONT = "pixel-medium"
AMBER = "#FFB000"


def _font_for(font_id: str, h: int):
    try:
        # try a nicer bitmap-ish truetype if present, else default
        return ImageFont.load_default(size=max(8, h))
    except TypeError:
        return ImageFont.load_default()


def resolve_fields(destination: dict, service_number: str = "") -> dict:
    """Runtime field values per format.md 6.2 resolution paragraph."""
    import json as _j
    fields = {
        "num": destination.get("num") or service_number or "",
        "via": destination.get("via") or "",
        "dest": destination.get("dest") or "",
    }
    try:
        extra = _j.loads(destination.get("extra_fields") or "{}")
        for k, v in extra.items():
            fields[k] = v
    except Exception:
        pass
    return fields


def resolve_layout(con, destination_id: str, display_id: str) -> dict | None:
    """destination layout -> default template -> None (caller uses auto-fit)."""
    row = con.execute("SELECT layout FROM layouts WHERE destination_id=? AND display_id=?",
                      (destination_id, display_id)).fetchone()
    if row:
        return json.loads(row[0])
    row = con.execute("SELECT layout FROM layout_templates WHERE display_id=? AND is_default=1",
                      (display_id,)).fetchone()
    if row:
        return json.loads(row[0])
    return None


def autofit_layout(width: int, height: int) -> dict:
    """Default board: route number left, destination top-right, via bottom-right."""
    num_w = max(16, width // 4)
    dest_h = max(8, round(height * 25 / 40))
    return {
        "schema": 1, "width": width, "height": height, "background": "#000000",
        "page_seconds": 3, "override": {"mode": "replace", "replaces": "dest", "hide": []},
        "elements": [
            {"type": "field", "field": "num", "x": 0, "y": 0, "w": num_w, "h": height,
             "font": "pixel-huge", "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "none", "flash_ms": 0, "page": 1, "show_if": "always"},
            {"type": "field", "field": "dest", "x": num_w, "y": 0, "w": width - num_w, "h": dest_h,
             "font": "pixel-large", "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "always"},
            {"type": "field", "field": "via", "x": num_w, "y": dest_h, "w": width - num_w, "h": height - dest_h,
             "font": "pixel-medium", "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "has_text"},
        ],
    }


def _visible(el: dict, fields: dict, override_active: bool) -> bool:
    show = el.get("show_if", "always")
    if show == "always":
        return True
    if show == "has_text":
        if el.get("type") == "field":
            return bool(fields.get(el.get("field", ""), ""))
        return bool(el.get("text", ""))
    if show == "override_active":
        return override_active
    if show == "override_inactive":
        return not override_active
    return True


def _element_text(el: dict, fields: dict, override_text: str, layout: dict) -> str:
    if el.get("type") == "text":
        return el.get("text", "")
    field = el.get("field", "")
    ov = layout.get("override", {})
    if override_text and ov.get("replaces", "dest") == field and ov.get("mode") in ("replace", "hide_others", "full"):
        return override_text
    return fields.get(field, "")


def _pages_used(layout: dict) -> list[int]:
    pages = sorted({int(el.get("page", 1)) for el in layout.get("elements", [])})
    return pages or [1]


def current_page(layout: dict, now: float | None = None) -> int:
    pages = _pages_used(layout)
    if len(pages) == 1:
        return pages[0]
    secs = float(layout.get("page_seconds", 3) or 3)
    now = time.time() if now is None else now
    idx = int(now // secs) % len(pages)
    return pages[idx]


def render_frame(layout: dict, fields: dict, override_text: str = "",
                 now: float | None = None, scroll_px: int | None = None) -> Image.Image:
    """Render one frame. now controls paging/flash; scroll_px offsets scrolling text."""
    w, h = int(layout["width"]), int(layout["height"])
    bg = layout.get("background", "#000000")
    img = Image.new("RGB", (w, h), bg)
    dr = ImageDraw.Draw(img)
    now = time.time() if now is None else now
    override_active = bool(override_text)
    ov = layout.get("override", {})
    mode = ov.get("mode", "replace")
    page = current_page(layout, now)

    if mode == "full" and override_active:
        # override fills display
        dr.rectangle([0, 0, w - 1, h - 1], fill=bg)
        fnt = _font_for("pixel-large", h // 2)
        dr.text((2, h // 2), override_text, fill=AMBER, font=fnt, anchor="lm")
        return img

    hide = set(ov.get("hide", [])) if override_active and mode in ("replace", "hide_others") else set()
    if mode == "hide_others" and override_active:
        # only the replaced element shows (with override text)
        replaces = ov.get("replaces", "dest")
        for el in layout.get("elements", []):
            if el.get("field") == replaces:
                hide = {e.get("field") for e in layout.get("elements", []) if e.get("field") != replaces}
                break

    for el in layout.get("elements", []):
        if int(el.get("page", 1)) != page:
            continue
        if el.get("field") in hide:
            continue
        if not _visible(el, fields, override_active):
            continue
        flash_ms = int(el.get("flash_ms", 0) or 0)
        if flash_ms and (int(now * 1000) % (flash_ms * 2)) >= flash_ms:
            continue  # off phase
        text = _element_text(el, fields, override_text, layout)
        if not text:
            continue
        x, y, ww, hh = int(el["x"]), int(el["y"]), int(el["w"]), int(el["h"])
        colour = el.get("colour", AMBER)
        font_id = el.get("font", DEFAULT_FONT)
        han = None
        if isinstance(font_id, str) and font_id.startswith("han-"):
            from .hanover import get_font as _get_han, warn_once_unknown as _han_warn
            try:
                han = _get_han(font_id)
            except Exception:
                han = None  # unknown han- id: fall back to default font below
                _han_warn(font_id)
        if han is not None:
            from .hanover import text_width as _han_width
            fh = int(han["height"])
            tw = _han_width(han, text)
        else:
            fh = FONT_HEIGHTS.get(font_id, 12)
            fnt = _font_for(font_id, min(hh - 2, fh + 4) if hh > 4 else fh)
            # measure
            bbox = dr.textbbox((0, 0), text, font=fnt)
            tw = bbox[2] - bbox[0]
        # vertical anchor
        valign = el.get("valign", "top")
        ty = y + (0 if valign == "top" else (hh // 2 if valign == "middle" else hh))
        anchor_y = "t" if valign == "top" else ("m" if valign == "middle" else "b")
        align = el.get("align", "left")
        if han is not None:
            # 1:1 bitmap blit, integer-scaled (crisp NEAREST). Same scroll
            # semantics as vector text. `scale` lets a 19px font fill 40px.
            from .hanover import render_text as _han_render
            try:
                scale = max(1, min(8, int(el.get("scale", 1) or 1)))
            except (TypeError, ValueError):
                scale = 1
            strip = _han_render(han, text, colour)
            if scale > 1:
                strip = strip.resize((strip.width * scale, strip.height * scale), Image.NEAREST)
            tw, th = strip.size
            if align in ("centre", "center"):
                tx = x + (ww - tw) // 2
            elif align == "right":
                tx = x + ww - tw
            else:
                tx = x + 1
            ty = y if valign == "top" else (y + (hh - th) // 2 if valign == "middle" else y + hh - th)
            scroll = el.get("scroll", "none")
            if scroll == "always" or (scroll == "if_too_long" and tw > ww):
                off = scroll_px if scroll_px is not None else int(now * 20) % (tw + ww)
                tmp = Image.new("RGB", (tw + ww, hh), bg)
                tmp.paste(strip, (0, ty - y), strip)
                window = tmp.crop((off, 0, min(off + ww, tmp.width), hh))
                img.paste(window, (x, y))
            else:
                img.paste(strip, (tx, ty), strip)
            continue
        scroll = el.get("scroll", "none")
        needs_scroll = scroll == "always" or (scroll == "if_too_long" and tw > ww)
        if needs_scroll:
            off = scroll_px if scroll_px is not None else int(now * 20) % (tw + ww)
            tx = x + ww - off
            # clip to element box
            clip = img.crop((x, y, x + ww, y + hh))
            tmp = Image.new("RGB", (tw + ww, hh), bg)
            d2 = ImageDraw.Draw(tmp)
            d2.text((0, hh // 2 if valign == "middle" else 0), text, fill=colour, font=fnt,
                    anchor="lm" if valign == "middle" else "lt")
            # scroll window
            sx = off
            window = tmp.crop((sx, 0, min(sx + ww, tmp.width), hh))
            img.paste(window, (x, y))
        else:
            if align in ("centre", "center"):
                tx = x + ww // 2
                anchor_x = "m"
            elif align == "right":
                tx = x + ww - 1
                anchor_x = "r"
            else:
                tx = x + 1
                anchor_x = "l"
            dr.text((tx, ty), text, fill=colour, font=fnt, anchor=f"{anchor_x}{anchor_y}")
    return img


def render_dotmatrix_preview(img: Image.Image, scale: int = 2) -> Image.Image:
    """LED-style preview: black bg, round dots. scale = screen px per LED."""
    w, h = img.size
    out = Image.new("RGB", (w * scale, h * scale), "#0a0a0a")
    dr = ImageDraw.Draw(out)
    px = img.load()
    for yy in range(h):
        for xx in range(w):
            r, g, b = px[xx, yy][:3]
            if r or g or b:
                dr.ellipse([xx * scale, yy * scale, xx * scale + scale - 1, yy * scale + scale - 1],
                           fill=(r, g, b))
            else:
                dr.ellipse([xx * scale, yy * scale, xx * scale + scale - 1, yy * scale + scale - 1],
                           fill="#1a1206")
    return out
