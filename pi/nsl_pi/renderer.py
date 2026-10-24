"""Board layout renderer - shared by portal preview, main Pi and receivers.

Canvas = real pixels (e.g. 240x40). Layout JSON per format.md 6.2.
Renders with Pillow so portal preview and bus output stay identical.
"""
from __future__ import annotations

import base64
import io
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

# src string -> PIL Image cache for embedded data-url images.
_IMAGE_CACHE: dict[str, Image.Image] = {}


def _parse_data_url(src: str) -> bytes | None:
    if not isinstance(src, str):
        return None
    if src.startswith("data:"):
        try:
            return base64.b64decode(src.split(",", 1)[1])
        except Exception:
            return None
    return None


def _load_image(src: str) -> Image.Image | None:
    if src in _IMAGE_CACHE:
        return _IMAGE_CACHE[src]
    data = _parse_data_url(src)
    if data is None:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        _IMAGE_CACHE[src] = im
        return im
    except Exception:
        return None


def _draw_image(dr: ImageDraw.Draw, img: Image.Image, el: dict):
    """Draw an image element onto the layout canvas."""
    x, y, ww, hh = int(el["x"]), int(el["y"]), int(el["w"]), int(el["h"])
    keep_aspect = el.get("keep_aspect", True)
    iw, ih = img.size
    if keep_aspect:
        scale = min(ww / iw, hh / ih, 1) if iw and ih else 0
        dw, dh = int(iw * scale), int(ih * scale)
        dx, dy = x + (ww - dw) // 2, y + (hh - dh) // 2
    else:
        dw, dh, dx, dy = ww, hh, x, y
    if dw <= 0 or dh <= 0:
        return
    resized = img.resize((dw, dh), Image.NEAREST)
    canvas = dr._image
    colour = el.get("colour", AMBER)
    if el.get("tint", True):
        # Tint every opaque pixel to the chosen colour, preserving alpha.
        tinted = Image.new("RGBA", resized.size, colour)
        canvas.paste(tinted, (dx, dy), resized)
    else:
        canvas.paste(resized, (dx, dy), resized)


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


def _ordered_pages(pages: list[dict] | None) -> list[dict]:
    if not pages:
        return []
    return sorted(pages, key=lambda p: int(p.get("position", 0) or 0))


def board_position_for_slot(pages: list[dict] | None, slot: int) -> int:
    """1-based board-page position active in time slot `slot` (0 when none)."""
    ordered = _ordered_pages(pages)
    if not ordered:
        return 0
    pg = ordered[slot % len(ordered)]
    try:
        return int(pg.get("position", 0) or 0)
    except (TypeError, ValueError):
        return (slot % len(ordered)) + 1


def slot_for_layout(layout: dict | None, now: float | None) -> int:
    """Time slot index for board rotation (`page_seconds` per board page).

    Legacy fixed-timing helper, kept for tests/callers without scrolling
    text. Scroll-aware rotation uses :func:`pick_slot` with per-page
    durations from :func:`page_duration` so a page with scrolling text is
    held until its scroll finishes.
    """
    import time as _t
    secs = 3.0
    if layout:
        try:
            secs = float(layout.get("page_seconds", 3) or 3)
        except (TypeError, ValueError):
            secs = 3.0
    t = _t.time() if now is None else now
    return int(t // secs) if secs > 0 else 0


# Scroll timing: every page entry holds the text static for HOLD_S, then
# scrolls at PX_S, then holds a short blank (END_HOLD_S + GAP_PX) before the
# page changes. GAP/END replace the old `tw + ww` loop whose blank lasted
# `ww / 20` seconds (up to ~8 s on a 240px board).
SCROLL_PX_S = 20.0
SCROLL_HOLD_S = 1.0
SCROLL_END_HOLD_S = 0.5
SCROLL_GAP_PX = 8


def page_seconds_of(layout: dict | None) -> float:
    try:
        return float((layout or {}).get("page_seconds", 3) or 3)
    except (TypeError, ValueError):
        return 3.0


def scroll_offset_for_width(tw: int, page_elapsed: float | None) -> int:
    """Pixel offset into a scrolling strip for time since page entry."""
    try:
        total = int(tw) + int(SCROLL_GAP_PX)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 0
    try:
        el = float(page_elapsed) if page_elapsed is not None else 0.0
    except (TypeError, ValueError):
        el = 0.0
    if el < SCROLL_HOLD_S:
        return 0
    dist = (el - SCROLL_HOLD_S) * SCROLL_PX_S
    if dist <= 0:
        return 0
    return int(min(total, dist))


def _tw_for_element(el: dict, text: str) -> int | None:
    """Measure rendered text width for one element (han scaled or PIL)."""
    if not text:
        return None
    font_id = el.get("font", DEFAULT_FONT)
    if isinstance(font_id, str) and font_id.startswith("han-"):
        try:
            from .hanover import get_font as _get_han, text_width as _han_width
            han = _get_han(font_id)
        except Exception:
            return None
        try:
            spacing = max(-8, min(16, int(el.get("spacing", 0) or 0)))
        except (TypeError, ValueError):
            spacing = 0
        try:
            scale = max(1, min(8, int(el.get("scale", 1) or 1)))
        except (TypeError, ValueError):
            scale = 1
        try:
            return int(_han_width(han, text, spacing) * scale)
        except Exception:
            return None
    fh = FONT_HEIGHTS.get(font_id, 12)
    try:
        hh = int(el.get("h", fh) or fh)
    except (TypeError, ValueError):
        hh = fh
    try:
        fnt = _font_for(font_id, min(hh - 2, fh + 4) if hh > 4 else fh)
        probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        bbox = probe.textbbox((0, 0), text, font=fnt)
        return int(bbox[2] - bbox[0])
    except Exception:
        return None


def _active_elements_for_board(layout: dict, board_position: int | None) -> tuple[list[dict], bool]:
    page_elements = (layout or {}).get("page_elements", {})
    if board_position and page_elements:
        active = page_elements.get(str(board_position))
        if active is None:
            active = (layout or {}).get("elements", [])
        return active, False
    return (layout or {}).get("elements", []), bool(board_position)


def page_scroll_need(layout: dict, fields: dict, override_text: str,
                     board_position: int | None) -> float:
    """Extra seconds a page needs so its scrolling text finishes.

    Max over visible scrolling elements of ``(tw + GAP) / PX_S``; 0 when
    nothing scrolls.
    """
    active_elements, legacy_filter = _active_elements_for_board(layout, board_position)
    ov = (layout or {}).get("override", {})
    mode = ov.get("mode", "replace")
    override_active = bool(override_text)
    if mode == "full" and override_active:
        return 0.0
    hide = set(ov.get("hide", [])) if override_active and mode in ("replace", "hide_others") else set()
    if mode == "hide_others" and override_active:
        replaces = ov.get("replaces", "dest")
        for el in active_elements:
            if el.get("field") == replaces:
                hide = {e.get("field") for e in active_elements if e.get("field") != replaces}
                break
    need = 0.0
    for el in active_elements:
        if legacy_filter and not element_shows_on(el, board_position or 0):
            continue
        if el.get("field") in hide:
            continue
        if el.get("type") == "image":
            continue
        if not _visible(el, fields, override_active):
            continue
        text = _element_text(el, fields, override_text, layout)
        if not text:
            continue
        try:
            ww = int(el["w"])
        except (TypeError, ValueError, KeyError):
            continue
        tw = _tw_for_element(el, text)
        if tw is None:
            continue
        scroll = el.get("scroll", "none")
        if scroll != "always" and not (scroll == "if_too_long" and tw > ww):
            continue
        need = max(need, (float(tw) + float(SCROLL_GAP_PX)) / SCROLL_PX_S)
    return need


def page_duration(layout: dict, fields: dict, override_text: str = "",
                  board_position: int | None = None) -> float:
    """Seconds a board page stays up: `page_seconds` floor, extended until
    any scrolling text on it finishes (hold + scroll + end-hold)."""
    base = page_seconds_of(layout)
    if base <= 0:
        base = 3.0
    need = page_scroll_need(layout, fields, override_text, board_position)
    if need <= 0:
        return base
    return max(base, SCROLL_HOLD_S + need + SCROLL_END_HOLD_S)


def pick_slot(durations: list[float], now: float) -> tuple[int, float]:
    """Map wall-clock to variable-duration rotation. Returns (idx, elapsed)
    where elapsed is seconds since the active page started (drives scroll)."""
    safe = [(float(d) if d and float(d) > 0 else 3.0) for d in durations] if durations else [3.0]
    total = sum(safe)
    if total <= 0:
        return 0, 0.0
    try:
        pos = float(now) % total
    except (TypeError, ValueError):
        return 0, 0.0
    cum = 0.0
    for i, d in enumerate(safe):
        if pos < cum + d:
            return i, pos - cum
        cum += d
    return len(safe) - 1, safe[-1]


def combined_slot(layout: dict | None, now: float | None,
                  n_main: int, n_insert: int) -> tuple[str, int, int]:
    """Map wall-clock to the combined main + insert rotation.

    Normal board pages play first, then the insert pages, then the loop
    repeats. Returns ``(kind, index, total)`` where kind is ``"main"`` or
    ``"insert"`` and index is the 0-based offset inside that group.
    Without an insert this is exactly the legacy main-only rotation.
    """
    n_main = max(1, int(n_main or 1))
    n_insert = max(0, int(n_insert or 0))
    if n_insert <= 0:
        return ("main", slot_for_layout(layout, now) % n_main, n_main)
    total = n_main + n_insert
    slot = slot_for_layout(layout, now) % total
    if slot < n_main:
        return ("main", slot, total)
    return ("insert", slot - n_main, total)


def element_shows_on(el: dict, board_position: int) -> bool:
    """Whether a layout element draws on the given 1-based board page.

    `pages` lists the board-page positions the element belongs to;
    missing/empty means every page. Unknown positions never match.
    Element `page` (singular) is legacy and ignored.
    """
    want = el.get("pages")
    if not want:
        return True
    if not isinstance(want, (list, tuple)):
        return True
    if board_position <= 0:
        return True
    return board_position in want


def fields_for_slot(destination: dict, service_number: str = "",
                      layout: dict | None = None, now: float | None = None,
                      pages: list[dict] | None = None) -> dict:
    """Board text for the time slot active at `now`.

    The destination's board pages (each with its own via/dest) rotate every
    ``page_seconds``. Without board pages the base via/dest is used.
    Service number (num) is constant across pages.
    """
    fields = resolve_fields(destination, service_number)
    ordered = _ordered_pages(pages)
    if not ordered:
        return fields
    slot = slot_for_layout(layout, now)
    pg = ordered[slot % len(ordered)]
    fields["via"] = pg.get("via") or ""
    fields["dest"] = pg.get("dest") or ""
    return fields


def fields_for_page_index(destination: dict, service_number: str = "",
                           pages: list[dict] | None = None, idx: int = 0) -> dict:
    """Board text for an explicit page offset (combined main+insert rotation).

    Unlike :func:`fields_for_slot` this does NOT read the wall clock: the
    caller already mapped wall-clock to ``idx`` via :func:`combined_slot`,
    so the via/dest here always matches the ``board_position`` passed to
    :func:`render_frame`. Using the clock here again is what shifted text
    one page late once an insert stretched the loop.
    """
    fields = resolve_fields(destination, service_number)
    ordered = _ordered_pages(pages)
    if not ordered:
        return fields
    pg = ordered[int(idx or 0) % len(ordered)]
    fields["via"] = pg.get("via") or ""
    fields["dest"] = pg.get("dest") or ""
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


# Fleet auto-fit defaults (operator standard): Hanover bitmap fonts, tracked +1.
HAN_NUM_FONT = "han-1912B3E1"
HAN_DEST_FONT = "han-2423B3E1"
HAN_VIA_FONT = "han-1610N2E1"
HAN_SPACING = 1
HAN_NUM_SCALE = 2


def autofit_layout(width: int, height: int) -> dict:
    """Default board: route number left, destination top-right, via bottom-right.

    Geometry scales from the 240x40 front standard (num 70 wide, dest 25 tall,
    via fills the rest); the same Hanover fonts/scale/spacing apply on every
    board size. Mirrored in portal/frontend/app.js + layout.js - keep identical.
    """
    num_w = max(16, round(width * 70 / 240))
    dest_h = max(8, round(height * 25 / 40))
    return {
        "schema": 1, "width": width, "height": height, "background": "#000000",
        "page_seconds": 3, "override": {"mode": "replace", "replaces": "dest", "hide": []},
        "elements": [
            {"type": "field", "field": "num", "x": 0, "y": 0, "w": num_w, "h": height,
             "font": HAN_NUM_FONT, "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "none", "flash_ms": 0, "page": 1, "show_if": "always",
             "scale": HAN_NUM_SCALE, "spacing": HAN_SPACING},
            {"type": "field", "field": "dest", "x": num_w, "y": 0, "w": width - num_w, "h": dest_h,
             "font": HAN_DEST_FONT, "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "always",
             "scale": 1, "spacing": HAN_SPACING},
            {"type": "field", "field": "via", "x": num_w, "y": dest_h, "w": width - num_w, "h": height - dest_h,
             "font": HAN_VIA_FONT, "colour": AMBER, "align": "centre", "valign": "middle",
             "scroll": "if_too_long", "flash_ms": 0, "page": 1, "show_if": "has_text",
             "scale": 1, "spacing": HAN_SPACING},
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


def render_frame(layout: dict, fields: dict, override_text: str = "",
                 now: float | None = None, scroll_px: int | None = None,
                 pages: list[dict] | None = None,
                 board_position: int | None = None,
                 page_elapsed: float | None = None) -> Image.Image:
    """Render one frame. now controls flash; scroll_px offsets scrolling text.

    Each board page can have its own layout. ``page_elements`` maps a
    board-page position to an element list; if the active page has no
    entry the fallback ``elements`` list is used. Pass ``pages`` (or the
    resolved ``board_position``) so the frame matches the rotation slot;
    without them everything draws.

    ``page_elapsed`` is seconds since the current board page started: the
    scroll offset resets to the text start on every page entry, holds
    static for 1 s, then scrolls. Callers doing scroll-aware rotation
    (``pick_slot`` + ``page_duration``) pass it; without it scrolling falls
    back to the legacy wall-clock offset.
    """
    w, h = int(layout["width"]), int(layout["height"])
    bg = layout.get("background", "#000000")
    img = Image.new("RGB", (w, h), bg)
    dr = ImageDraw.Draw(img)
    now = time.time() if now is None else now
    if board_position is None and pages:
        board_position = board_position_for_slot(pages, slot_for_layout(layout, now))
    override_active = bool(override_text)
    ov = layout.get("override", {})
    mode = ov.get("mode", "replace")
    page_elements = layout.get("page_elements", {})
    if board_position and page_elements:
        active_elements = page_elements.get(str(board_position))
        if active_elements is None:
            active_elements = layout.get("elements", [])
        legacy_page_filter = False
    else:
        active_elements = layout.get("elements", [])
        legacy_page_filter = bool(board_position)

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
        for el in active_elements:
            if el.get("field") == replaces:
                hide = {e.get("field") for e in active_elements if e.get("field") != replaces}
                break

    for el in active_elements:
        if legacy_page_filter and not element_shows_on(el, board_position):
            continue
        if el.get("field") in hide:
            continue
        if not _visible(el, fields, override_active):
            continue
        x, y, ww, hh = int(el["x"]), int(el["y"]), int(el["w"]), int(el["h"])
        if el.get("type") == "image":
            im = _load_image(el.get("src"))
            if im:
                _draw_image(dr, im, el)
            continue
        flash_ms = int(el.get("flash_ms", 0) or 0)
        if flash_ms and (int(now * 1000) % (flash_ms * 2)) >= flash_ms:
            continue  # off phase
        text = _element_text(el, fields, override_text, layout)
        if not text:
            continue
        colour = el.get("colour", AMBER)
        invert = bool(el.get("invert"))
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
            try:
                spacing = max(-8, min(16, int(el.get("spacing", 0) or 0)))
            except (TypeError, ValueError):
                spacing = 0
            tw = _han_width(han, text, spacing)
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
            strip = _han_render(han, text, colour, spacing)
            if scale > 1:
                strip = strip.resize((strip.width * scale, strip.height * scale), Image.NEAREST)
            if invert:
                # colour background, black glyphs
                mask = strip.convert("L").point(lambda p: 255 if p > 0 else 0)
                inv = Image.new("RGB", strip.size, colour)
                inv.paste(bg, (0, 0), mask)
                strip = inv
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
                if scroll_px is not None:
                    off = int(scroll_px)
                elif page_elapsed is not None:
                    off = scroll_offset_for_width(tw, page_elapsed)
                else:
                    off = int(now * 20) % (tw + ww)
                # Element-sized box: text starts at the left edge (off=0)
                # and scrolls out, leaving only a short trailing gap before
                # the page changes (no more `ww`-wide blank dwell).
                box = Image.new("RGB", (ww, hh), colour if invert else bg)
                sy = ty - y
                sx0 = max(0, off)
                sx1 = min(tw, off + ww)
                if sx1 > sx0:
                    dy0 = max(0, sy)
                    dy1 = min(hh, sy + th)
                    if dy1 > dy0:
                        crop = strip.crop((sx0, dy0 - sy, sx1, dy1 - sy))
                        if not invert and crop.mode == "RGBA":
                            box.paste(crop, (sx0 - off, dy0), crop)
                        else:
                            box.paste(crop, (sx0 - off, dy0))
                img.paste(box, (x, y))
            else:
                if invert:
                    # Full-box invert to match portal LED preview:
                    # fill the whole element area, then blit the
                    # (amber-bg / black-glyph) strip centred on it.
                    dr.rectangle([x, y, x + ww - 1, y + hh - 1], fill=colour)
                    img.paste(strip, (tx, ty))
                else:
                    img.paste(strip, (tx, ty), strip)
            continue
        scroll = el.get("scroll", "none")
        needs_scroll = scroll == "always" or (scroll == "if_too_long" and tw > ww)
        if invert:
            dr.rectangle([x, y, x + ww - 1, y + hh - 1], fill=colour)
        if needs_scroll:
            if scroll_px is not None:
                off = int(scroll_px)
            elif page_elapsed is not None:
                off = scroll_offset_for_width(tw, page_elapsed)
            else:
                off = int(now * 20) % (tw + ww)
            box = Image.new("RGB", (ww, hh), colour if invert else bg)
            dbox = ImageDraw.Draw(box)
            dbox.text((-off, hh // 2 if valign == "middle" else 0), text,
                      fill=(bg if invert else colour), font=fnt,
                      anchor="lm" if valign == "middle" else "lt")
            img.paste(box, (x, y))
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
            dr.text((tx, ty), text, fill=(bg if invert else colour), font=fnt, anchor=f"{anchor_x}{anchor_y}")
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
