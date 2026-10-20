"""Hanover HELEN .Fnt bitmap fonts (repo HANOVERFONTS/ + /var/lib/nsl/fonts).

File layout (reverse-engineered, verified by rendering A-Z):
  line 0: `<magic> <height> <maxwidth> 0 <x>` (magic 0 or 2; height/maxwidth used)
  line 1: 224 Advance widths, one per char code 32..255
  lines 2..225: one line per code 32..255, exactly <maxwidth> column bitmasks.
Columns are vertical bitmasks, LSB = TOP row. Pure ASCII 32..126 renders true;
codes 127+ use Hanover's custom map (rendered as-is by code point).

Fonts are 1:1 pixels — never scaled. Use font ids ``han-<STEM>`` where STEM is
the file name without extension (e.g. ``han-0705C1E1``).
"""
from __future__ import annotations

import os

MAGIC = {"0", "2"}
CODES = range(32, 256)


def fonts_dir() -> str:
    env = os.environ.get("NSL_FONTS_DIR")
    if env and os.path.isdir(env):
        return env
    sys_dir = "/var/lib/nsl/fonts"
    if os.path.isdir(sys_dir):
        return sys_dir
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    repo = os.path.join(here, "HANOVERFONTS")
    if os.path.isdir(repo):
        return repo
    return sys_dir


def parse_font(path: str) -> dict:
    with open(path, "rb") as f:
        toks = f.read().replace(b"\r\n", b"\n").split()
    strs = [t.decode("ascii", "strict") for t in toks]
    if len(strs) < 5 or strs[0] not in MAGIC:
        raise ValueError(f"not a Hanover .Fnt file: {path}")
    height, maxwidth = int(strs[1]), int(strs[2])
    expect = 5 + 224 + 224 * maxwidth
    if len(strs) != expect:
        raise ValueError(f"bad token count in {path}: {len(strs)} != {expect}")
    widths = list(map(int, strs[5:5 + 224]))
    glyphs: dict[int, tuple[int, list[int]]] = {}
    i = 5 + 224
    for code in CODES:
        cols = list(map(int, strs[i:i + maxwidth]))
        i += maxwidth
        glyphs[code] = (widths[code - 32], cols)
    return {"height": height, "maxwidth": maxwidth, "widths": widths, "glyphs": glyphs}


_cache: dict[str, dict] = {}
_warned_missing: set[str] = set()


def _stem_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def list_fonts(directory: str | None = None) -> list[dict]:
    """[{id, name, height, maxwidth}]. id is ``han-<STEM>``."""
    d = directory or fonts_dir()
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".Fnt"):
            continue
        try:
            f = parse_font(os.path.join(d, fn))
        except Exception:
            continue
        out.append({"id": "han-" + _stem_of(fn), "name": _stem_of(fn),
                    "height": f["height"], "maxwidth": f["maxwidth"],
                    "glyphs": len(f["glyphs"])})
    return out


def get_font(font_id: str, directory: str | None = None) -> dict:
    """font_id ``han-<STEM>`` (case-insensitive stem match). Cached."""
    stem = font_id[4:] if font_id.startswith("han-") else font_id
    key = (directory or fonts_dir()) + "\x00" + stem.lower()
    if key not in _cache:
        d = directory or fonts_dir()
        match = None
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.endswith(".Fnt") and _stem_of(fn).lower() == stem.lower():
                    match = os.path.join(d, fn)
                    break
        if match is None:
            raise KeyError(f"unknown Hanover font {font_id!r} in {d}")
        parsed = parse_font(match)
        parsed["id"] = "han-" + _stem_of(match)
        parsed["name"] = _stem_of(match)
        _cache[key] = parsed
    return _cache[key]


def warn_once_unknown(font_id: str):
    """One log line per unknown id (services run for weeks; never spam)."""
    if font_id not in _warned_missing:
        _warned_missing.add(font_id)
        print(f"display: unknown han font {font_id!r} in {fonts_dir()} — falling back to default font",
              flush=True)


def glyph_of(font: dict, ch: str) -> tuple[int, list[int]] | None:
    code = ord(ch)
    g = font["glyphs"].get(code)
    if g is None and code != 63:
        g = font["glyphs"].get(63)  # '?'
    return g


def text_width(font: dict, text: str) -> int:
    w = 0
    for ch in text:
        g = glyph_of(font, ch)
        if g:
            w += g[0]
    return w


def _colour_tuple(colour) -> tuple[int, int, int]:
    if isinstance(colour, tuple):
        return colour[:3]
    c = str(colour).lstrip("#")
    if len(c) == 3:
        c = "".join(x * 2 for x in c)
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def draw_text(img, font: dict, text: str, x: int, y: int, colour="#FFB000"):
    """Blit 1:1 bitmap text onto a PIL image, clipped to its bounds."""
    rgb = _colour_tuple(colour)
    W, H = img.size
    px = img.load()
    cx = int(x)
    for ch in text:
        g = glyph_of(font, ch)
        if not g:
            continue
        adv, cols = g
        for dx, col in enumerate(cols):
            xx = cx + dx
            if not col or xx < 0 or xx >= W:
                continue
            yy, b = int(y), col
            while b:
                if b & 1 and 0 <= yy < H:
                    px[xx, yy] = rgb
                yy += 1
                b >>= 1
        cx += adv
