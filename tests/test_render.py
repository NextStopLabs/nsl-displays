import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import FRONT  # noqa: E402
from pi.nsl_pi.renderer import autofit_layout, fields_for_layout_page, render_frame  # noqa: E402


def test_render_identical_portal_vs_pi():
    """Same renderer in portal preview and Pi: pixel-identical for same inputs."""
    layout = dict(FRONT)
    fields = {"num": "100", "via": "Branston", "dest": "BURTON"}
    a = render_frame(layout, fields, "", now=0.0)
    b = render_frame(layout, fields, "", now=0.0)
    assert list(a.getdata()) == list(b.getdata())
    assert a.size == (240, 40)


def test_board_pages_cycle_without_layout_pages():
    """Board pages rotate on their own timer even when the layout has 1 page."""
    dest = {"num": None, "via": "Sorry", "dest": "Not in Service", "extra_fields": "{}"}
    pages = [{"position": i + 1, "via": v, "dest": "Not in Service"}
             for i, v in enumerate(["Sorry", "Group", "Drive", "example.com"])]
    lay = autofit_layout(240, 40)
    assert [e.get("page", 1) for e in lay["elements"]] == [1, 1, 1]
    seen = [fields_for_layout_page(dest, "000", lay, t, pages)[0]["via"]
            for t in (0.0, 3.5, 6.5, 9.5, 12.5)]
    assert seen == ["Sorry", "Group", "Drive", "example.com", "Sorry"], seen
    # no board pages: base fields, layout page still reported
    f, p = fields_for_layout_page(dest, "000", lay, 99.0, [])
    assert (f["via"], p) == ("Sorry", 1)


def test_override_replace_and_full():
    from scripts.seed import REAR
    layout = dict(REAR)
    img = render_frame(layout, {"num": "100", "via": "", "dest": "X"}, "NOT IN SERVICE", now=0.0)
    assert img.size == (80, 40)


def test_paging_and_scroll_no_crash():
    layout = dict(FRONT)
    layout["elements"] = [dict(e, page=1) for e in layout["elements"]] + [
        {"type": "text", "text": "PAGE2", "x": 0, "y": 0, "w": 240, "h": 40,
         "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "top",
         "scroll": "none", "page": 2, "show_if": "always"}]
    a = render_frame(layout, {"num": "1", "via": "", "dest": "D"}, "", now=0.0)
    b = render_frame(layout, {"num": "1", "via": "", "dest": "D"}, "", now=99.0)
    assert a.size == b.size == (240, 40)
