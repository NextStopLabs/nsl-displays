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


def test_scaled_render_doubles_size():
    from PIL import Image
    from nsl_pi import hanover
    from nsl_pi.renderer import render_frame, autofit_layout
    f = hanover.get_font("han-19ArialB")
    lay = dict(autofit_layout(240, 40))
    for el in lay["elements"]:
        el["font"] = "han-19ArialB"
        el["scale"] = 1  # normalize: current defaults carry scale 2 on num
        el["spacing"] = 0
    plain = render_frame(lay, {"num": "1", "via": "", "dest": "A"}, now=0.0)
    for el in lay["elements"]:
        el["scale"] = 2
    big = render_frame(lay, {"num": "1", "via": "", "dest": "A"}, now=0.0)
    assert big.size == (240, 40)
    n_plain = sum(1 for px in plain.getdata() if px[:3] != (0, 0, 0))
    n_big = sum(1 for px in big.getdata() if px[:3] != (0, 0, 0))
    assert n_big > n_plain * 2  # 2x scale ≈ 4x ink


def test_scale_validation():
    import uuid
    import pytest
    from nslprog import ProgramBuilder
    lay = dict(__import__("nsl_pi.renderer", fromlist=["autofit_layout"]).autofit_layout(240, 40))
    for el in lay["elements"]:
        el["font"] = "han-0705C1E1"
        el["scale"] = 99
    b = ProgramBuilder(program_id=str(uuid.uuid4()), program_name="S", px_width=240,
                       created_by="t", generator="t")
    f = b.add_display("front", 240, 40, 0)
    s = b.add_service("1", "", 0, None, None, None, "both")
    d = b.add_destination(s, "00000001", "A", "", "A", "1", 0, {})
    b.add_layout(d, f, lay)
    with pytest.raises(ValueError):
        b.build("/tmp/han-badscale.nslprog", 1, default_display_id=f)
    for el in lay["elements"]:
        el["scale"] = 2
    b2 = ProgramBuilder(program_id=str(uuid.uuid4()), program_name="S", px_width=240,
                        created_by="t", generator="t")
    f2 = b2.add_display("front", 240, 40, 0)
    s2 = b2.add_service("1", "", 0, None, None, None, "both")
    d2 = b2.add_destination(s2, "00000001", "A", "", "A", "1", 0, {})
    b2.add_layout(d2, f2, lay)
    b2.build("/tmp/han-scale2.nslprog", 1, default_display_id=f2)


def test_letter_spacing_widens_render():
    from nsl_pi.renderer import render_frame, autofit_layout
    lay = dict(autofit_layout(240, 40))
    for el in lay["elements"]:
        el["font"] = "han-0705C1E1"
    a = render_frame(lay, {"num": "100", "via": "", "dest": "AB"}, now=0.0)
    for el in lay["elements"]:
        el["spacing"] = 3
    b = render_frame(lay, {"num": "100", "via": "", "dest": "AB"}, now=0.0)
    na = sum(1 for px in a.getdata() if px[:3] != (0, 0, 0))
    nb = sum(1 for px in b.getdata() if px[:3] != (0, 0, 0))
    assert nb >= na  # wider tracking never loses ink
    from nsl_pi import hanover
    f = hanover.get_font("han-0705C1E1")
    assert hanover.text_width(f, "AB", 3) == hanover.text_width(f, "AB") + 6


def test_spacing_validation():
    import uuid
    import pytest
    from nslprog import ProgramBuilder
    lay = dict(__import__("nsl_pi.renderer", fromlist=["autofit_layout"]).autofit_layout(240, 40))
    for el in lay["elements"]:
        el["font"] = "han-0705C1E1"
        el["spacing"] = 99
    b = ProgramBuilder(program_id=str(uuid.uuid4()), program_name="S", px_width=240,
                       created_by="t", generator="t")
    f = b.add_display("front", 240, 40, 0)
    s = b.add_service("1", "", 0, None, None, None, "both")
    d = b.add_destination(s, "00000001", "A", "", "A", "1", 0, {})
    b.add_layout(d, f, lay)
    with pytest.raises(ValueError):
        b.build("/tmp/han-badspacing.nslprog", 1, default_display_id=f)


def test_autofit_hanover_defaults_render():
    """Fleet defaults must render with the real shipped .Fnt files (no fallback)."""
    import os
    os.environ["NSL_FONTS_DIR"] = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HANOVERFONTS")
    from nsl_pi import hanover as _han
    from nsl_pi.renderer import autofit_layout, render_frame
    for fid in ("han-1912B3E1", "han-2423B3E1", "han-1610N2E1"):
        f = _han.get_font(fid)  # raises if the file is missing/broken
        assert f["height"] > 0
    for w, h in ((240, 40), (160, 40), (80, 40)):
        img = render_frame(autofit_layout(w, h),
                           {"num": "100", "via": "Branston", "dest": "BURTON"}, now=0.0)
        assert img.size == (w, h)
        assert sum(1 for px in img.getdata() if px[:3] != (0, 0, 0)) > 50
