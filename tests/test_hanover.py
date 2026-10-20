import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pi"))

from nsl_pi import hanover  # noqa: E402

FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HANOVERFONTS")


def _files():
    return sorted(f for f in os.listdir(FONTS_DIR) if f.endswith(".Fnt"))


def test_all_hanover_fonts_parse():
    files = _files()
    assert len(files) >= 70, f"expected the shipped set, got {len(files)}"
    for fn in files:
        f = hanover.parse_font(os.path.join(FONTS_DIR, fn))
        assert f["height"] > 0 and f["maxwidth"] > 0
        assert len(f["glyphs"]) == 224
        assert len(f["widths"]) == 224


def test_hanover_list_and_lookup():
    fonts = hanover.list_fonts(FONTS_DIR)
    assert len(fonts) >= 70
    ids = {f["id"] for f in fonts}
    assert "han-0705C1E1" in ids
    f = hanover.get_font("han-0705C1E1", FONTS_DIR)
    assert f["height"] == 7 and f["maxwidth"] == 5
    # 'A' renders as A: spot-check advance + a known lit row pattern
    adv, cols = f["glyphs"][ord("A")]
    assert adv == 3 and len(cols) == 5
    assert hanover.text_width(f, "BURTON") == sum(f["glyphs"][ord(c)][0] for c in "BURTON")


def test_hanover_render_deterministic(tmp_path):
    from PIL import Image
    f = hanover.get_font("han-0806C1E1", FONTS_DIR)
    a = Image.new("RGB", (240, 40), (0, 0, 0))
    hanover.draw_text(a, f, "100 BURTON", 0, 0)
    b = Image.new("RGB", (240, 40), (0, 0, 0))
    hanover.draw_text(b, f, "100 BURTON", 0, 0)
    assert list(a.getdata()) == list(b.getdata())
    assert sum(1 for px in a.getdata() if px[:3] != (0, 0, 0)) > 100
    # unknown code points fall back to '?' rather than crashing
    hanover.draw_text(b, f, "é☃", 0, 0)


def test_hanover_layout_end_to_end():
    from nsl_pi.renderer import render_frame, autofit_layout
    lay = dict(autofit_layout(240, 40))
    for el in lay["elements"]:
        el["font"] = "han-0705C1E1"
    img = render_frame(lay, {"num": "100", "via": "Branston", "dest": "BURTON"}, now=0.0)
    assert img.size == (240, 40)
    assert sum(1 for px in img.getdata() if px[:3] != (0, 0, 0)) > 50
