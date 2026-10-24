"""Board layout geometry helpers - shared by portal backend and repair scripts.

A layout stores its own ``width``/``height`` plus element boxes (``x``/``y``/
``w``/``h``) in ``elements`` and per-board-page ``page_elements``. When a
display is resized the stored layouts must follow, otherwise every later
save fails with "element lies outside the display".
"""
from __future__ import annotations


def _scale_elements(els: list, sx: float, sy: float, new_w: int, new_h: int) -> None:
    for el in els:
        if not isinstance(el, dict):
            continue
        try:
            x = int(el.get("x", 0))
            y = int(el.get("y", 0))
            w = int(el.get("w", 0))
            h = int(el.get("h", 0))
        except (TypeError, ValueError):
            continue
        # proportional move/resize, then clamp inside the new bounds
        x = max(0, min(int(round(x * sx)), max(0, new_w - 1)))
        y = max(0, min(int(round(y * sy)), max(0, new_h - 1)))
        w = max(1, int(round(w * sx)))
        h = max(1, int(round(h * sy)))
        w = min(w, max(1, new_w - x))
        h = min(h, max(1, new_h - y))
        el["x"], el["y"], el["w"], el["h"] = x, y, w, h


def rescale_layout(layout: dict, new_w: int, new_h: int) -> dict:
    """Scale a layout's canvas and element boxes to a new display size.

    Mutates and returns ``layout``. Layouts already at the target size are
    left untouched; layouts without a usable old size just get the new
    canvas size. Every element ends up inside the display (moved/resized
    as needed), so the result always passes the bounds check.
    """
    new_w, new_h = max(1, int(new_w)), max(1, int(new_h))
    try:
        old_w, old_h = int(layout.get("width", 0)), int(layout.get("height", 0))
    except (TypeError, ValueError):
        old_w, old_h = 0, 0
    layout["width"], layout["height"] = new_w, new_h
    if old_w <= 0 or old_h <= 0 or (old_w == new_w and old_h == new_h):
        return layout
    sx, sy = new_w / old_w, new_h / old_h
    els = layout.get("elements")
    if isinstance(els, list):
        _scale_elements(els, sx, sy, new_w, new_h)
    pe = layout.get("page_elements")
    if isinstance(pe, dict):
        for els in pe.values():
            if isinstance(els, list):
                _scale_elements(els, sx, sy, new_w, new_h)
    return layout


def layout_overflows(layout: dict, disp_w: int, disp_h: int) -> list[str]:
    """Human-readable list of elements lying outside a WxH display.

    Covers ``elements`` ("default") and each ``page_elements`` entry
    ("page <pos>, element #<n>") - the same naming the layout editor's
    save check reports.
    """
    out: list[str] = []

    def _check(els, ref: str):
        for i, el in enumerate(els or []):
            if not isinstance(el, dict):
                continue
            try:
                x, y, w, h = (int(el.get("x", 0)), int(el.get("y", 0)),
                              int(el.get("w", 0)), int(el.get("h", 0)))
            except (TypeError, ValueError):
                continue
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > disp_w or y + h > disp_h:
                out.append(f"{ref}, element #{i + 1}")

    if not isinstance(layout, dict):
        return out
    _check(layout.get("elements"), "default")
    pe = layout.get("page_elements")
    if isinstance(pe, dict):
        for pos, els in pe.items():
            _check(els if isinstance(els, list) else [], f"page {pos}")
    return out
