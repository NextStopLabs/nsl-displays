import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.seed import FRONT  # noqa: E402
from pi.nsl_pi.renderer import autofit_layout, fields_for_slot, render_frame  # noqa: E402


def test_render_identical_portal_vs_pi():
    """Same renderer in portal preview and Pi: pixel-identical for same inputs."""
    layout = dict(FRONT)
    fields = {"num": "100", "via": "Branston", "dest": "BURTON"}
    a = render_frame(layout, fields, "", now=0.0)
    b = render_frame(layout, fields, "", now=0.0)
    assert list(a.getdata()) == list(b.getdata())
    assert a.size == (240, 40)


def test_board_pages_cycle_without_layout_pages():
    """Board pages rotate on their own timer; layout elements have no pages."""
    dest = {"num": None, "via": "Sorry", "dest": "Not in Service", "extra_fields": "{}"}
    pages = [{"position": i + 1, "via": v, "dest": "Not in Service"}
             for i, v in enumerate(["Sorry", "Group", "Drive", "example.com"])]
    lay = autofit_layout(240, 40)
    assert [e.get("page", 1) for e in lay["elements"]] == [1, 1, 1]
    seen = [fields_for_slot(dest, "000", lay, t, pages)["via"]
            for t in (0.0, 3.5, 6.5, 9.5, 12.5)]
    assert seen == ["Sorry", "Group", "Drive", "example.com", "Sorry"], seen
    # no board pages: base fields
    assert fields_for_slot(dest, "000", lay, 99.0, [])["via"] == "Sorry"
    assert fields_for_slot(dest, "000", lay, 99.0)["dest"] == "Not in Service"


def test_override_replace_and_full():
    from scripts.seed import REAR
    layout = dict(REAR)
    img = render_frame(layout, {"num": "100", "via": "", "dest": "X"}, "NOT IN SERVICE", now=0.0)
    assert img.size == (80, 40)


def test_per_page_layouts():
    """Each board page can have its own layout: elements filter per slot."""
    from pi.nsl_pi.renderer import board_position_for_slot, element_shows_on, slot_for_layout
    el_all, el_p2 = {"field": "dest"}, {"field": "via", "pages": [2]}
    assert element_shows_on(el_all, 1) and element_shows_on(el_all, 2)
    assert not element_shows_on(el_p2, 1) and element_shows_on(el_p2, 2)
    assert element_shows_on(el_p2, 0)
    pages = [{"position": 1, "via": "A", "dest": "D"}, {"position": 2, "via": "B", "dest": "D"}]
    lay = {"page_seconds": 3}
    assert board_position_for_slot(pages, slot_for_layout(lay, 0.0)) == 1
    assert board_position_for_slot(pages, slot_for_layout(lay, 3.5)) == 2
    dest = {"num": "1", "via": "A", "dest": "D", "extra_fields": "{}"}
    grid = dict(autofit_layout(240, 40))
    grid["elements"] = [dict(e, pages=[1]) for e in grid["elements"][:1]] + \
        [dict(e, pages=[2]) for e in grid["elements"][1:2]] + grid["elements"][2:]
    f1 = fields_for_slot(dest, "1", grid, 0.0, pages)
    f2 = fields_for_slot(dest, "1", grid, 3.5, pages)
    img1 = render_frame(grid, f1, "", now=0.0, pages=pages)
    img2 = render_frame(grid, f2, "", now=3.5, pages=pages)
    assert (f1["via"], f2["via"]) == ("A", "B")
    assert img1.size == img2.size == (240, 40)
    assert list(img1.getdata()) != list(img2.getdata())


def test_page_elements_layouts():
    """page_elements gives each board page its own independent element list."""
    pages = [{"position": 1, "via": "A", "dest": "D"}, {"position": 2, "via": "B", "dest": "D"}]
    lay = {
        "width": 240, "height": 40, "background": "#000000", "page_seconds": 3,
        "elements": [{"type": "field", "field": "dest", "x": 0, "y": 0, "w": 240, "h": 40,
                      "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
                      "scroll": "none", "show_if": "always"}],
        "page_elements": {
            "1": [{"type": "field", "field": "dest", "x": 0, "y": 0, "w": 240, "h": 40,
                   "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
                   "scroll": "none", "show_if": "always"}],
            "2": [{"type": "field", "field": "via", "x": 0, "y": 0, "w": 240, "h": 40,
                   "font": "pixel-medium", "colour": "#FFB000", "align": "left", "valign": "middle",
                   "scroll": "none", "show_if": "always"}]
        }
    }
    img1 = render_frame(lay, {"num": "1", "via": "A", "dest": "D"}, "", now=0.0, pages=pages)
    img2 = render_frame(lay, {"num": "1", "via": "B", "dest": "D"}, "", now=3.5, pages=pages)
    assert img1.size == img2.size == (240, 40)
    assert list(img1.getdata()) != list(img2.getdata())


def test_image_element():
    """Image elements render from a base64 data URL, respecting keep_aspect."""
    from io import BytesIO
    import base64
    from PIL import Image
    dot = Image.new("RGBA", (8, 8), (255, 176, 0, 255))
    buf = BytesIO()
    dot.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    lay = {
        "width": 240, "height": 40, "background": "#000000", "page_seconds": 3,
        "elements": [
            {"type": "image", "src": data_url, "x": 0, "y": 0, "w": 16, "h": 16,
             "keep_aspect": True, "show_if": "always"},
            {"type": "image", "src": data_url, "x": 20, "y": 0, "w": 40, "h": 16,
             "keep_aspect": False, "show_if": "always"}
        ]
    }
    img = render_frame(lay, {"num": "", "via": "", "dest": ""}, "", now=0.0)
    assert img.size == (240, 40)
    # both images should have drawn non-black pixels
    assert sum(1 for px in img.getdata() if px[:3] != (0, 0, 0)) > 100
