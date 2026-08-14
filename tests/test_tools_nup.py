"""
Tools Nup.
"""
import os, hashlib, time
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from tools.app_state import AppState
import pypdfium2 as pdfium
from tools.panels.impose import ImposePanel, _booklet_sides
from tools.panels.nup import NUpPanel, _build_nup
from tests.support import FX, MM, _BK_GREY, _TMP, _app, _ink_margins, _nup, _open, _sync_async


def _booklet_halves(path):
    """Decode an imposed booklet: one (left, right) tuple per sheet, each entry
    the 1-based source page that landed there or "blank"."""
    d = pdfium.PdfDocument(path)
    out = []
    for pg in d:
        im = pg.render(scale=0.25).to_pil().convert("L")
        w, h = im.size
        row = []
        for k in (0, 1):
            half = im.crop((w * k // 2, 0, w * (k + 1) // 2, h))
            hw, hh = half.size
            patch = half.crop((int(hw*.3), int(hh*.3), int(hw*.7), int(hh*.7)))
            mean = sum(patch.get_flattened_data()) / (patch.size[0]*patch.size[1]) / 255.0
            if mean > 0.94:
                row.append("blank"); continue
            best = min(range(32), key=lambda j: abs(_BK_GREY(j) - mean))
            row.append(best + 1 if abs(_BK_GREY(best) - mean) < 0.008 else f"?{mean:.3f}")
        out.append(tuple(row))
    d.close()
    return out


def _impose(path, open_first=True, **kw):
    """Run the Broschüre tool on `path` and return the output file.

    `open_first=False` keeps the PageModel AppState already carries — the point
    of the page-manager test is that the tool follows that model."""
    if open_first:
        _open(path)
    p = ImposePanel(); _sync_async(p)
    for attr, val in kw.items():
        w = getattr(p, attr)
        (w.setCurrentIndex if hasattr(w, "setCurrentIndex") else w.setChecked)(val)
    o = os.path.join(_TMP, f"impose_{os.path.basename(path)}")
    p.save_pdf = lambda *a, **k: o
    cap = {}; p.open_result = lambda path_, t="": cap.update(p=path_)
    p._run_action()
    return cap["p"]


def test_nup_source_modes():
    _open(FX["normal"])      # 5 pages
    def sheets(mode):
        p = NUpPanel(); p.cols.setValue(2); p.rows.setValue(2); p.src_combo.setCurrentIndex(mode)
        out = os.path.join(_TMP, f"nup{mode}.pdf"); p.save_pdf = lambda *a, **k: out
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path); _sync_async(p)
        p._run_action(); return len(PdfReader(cap["p"]).pages)
    assert sheets(0) == 2, "sequential 5p 2x2 should be 2 sheets"
    assert sheets(1) == 5, "repeat-each-page should be 5 sheets"


def test_nup_preview_differs_by_mode():
    _open(FX["normal"])
    nup = NUpPanel(); nup.cols.setValue(2); nup.rows.setValue(2)
    nup.resize(900, 600); nup.show(); _app.processEvents()
    def h(mode):
        nup.src_combo.setCurrentIndex(mode)
        for _ in range(600):
            nup._pane.refresh(); _app.processEvents()
            if "rendert" not in nup._preview_info.text(): break
            time.sleep(0.005)
        nup._pane.refresh(); _app.processEvents()
        pm = nup._preview.pixmap(); img = pm.toImage(); ptr = img.bits(); ptr.setsize(img.sizeInBytes())
        return hashlib.md5(bytes(ptr)).hexdigest()
    assert h(0) != h(1), "preview identical for both source modes"


def test_nup_crop_marks():
    _open(FX["normal"])
    A4w, A4h = A4; ml = mt = 14.0; gap = 8.5
    sw = (A4w-2*ml-gap)/2; sh = (A4h-2*mt-gap)/2
    params = (A4w, A4h, mt, mt, ml, ml, gap, gap, sw, sh, 2, 2)
    nomark = os.path.join(_TMP, "nm.pdf"); withm = os.path.join(_TMP, "wm.pdf")
    _build_nup(FX["normal"], nomark, [0, 1, 2, 3], params, 4, lambda m: None, crop_marks=False)
    _build_nup(FX["normal"], withm, [0, 1, 2, 3], params, 4, lambda m: None, crop_marks=True)
    def left_margin_dark(path):
        d = pdfium.PdfDocument(path); im = d[0].render(scale=2).to_pil().convert("L"); d.close()
        return min(im.crop((0, 0, int(ml*2), im.size[1])).get_flattened_data())
    assert left_margin_dark(nomark) > 200 and left_margin_dark(withm) < 80


def test_nup_centres_content():
    """The placed page must sit exactly in the middle of its slot — whatever
    boxes or /Rotate the source carries. A stale/inset CropBox or a rotation used
    to shift it by millimetres because the layout was measured on the MediaBox
    while qpdf placed the TrimBox."""
    for fx in ("framed", "framed_cropbox", "framed_rot90"):
        out, _ = _nup(FX[fx], margins=20.0, name=f"centre_{fx}.pdf")
        L, R, B, Tp = _ink_margins(out)
        assert abs(L-R) < 0.4 and abs(B-Tp) < 0.4, \
            f"{fx}: not centred — L={L:.2f} R={R:.2f} B={B:.2f} T={Tp:.2f} mm"
        assert min(L, R, B, Tp) >= 19.5, f"{fx}: content inside the 20 mm margin"


def test_nup_auto_format_keeps_margins():
    """"Wie Quellseite × Raster" must add the margins around the page instead of
    scaling the page down inside them — 10 mm requested is 10 mm on all four
    sides, not 10 mm at the sides and 14 mm top/bottom."""
    for fx in ("framed", "framed_rot90"):
        out, _ = _nup(FX[fx], fmt=None, margins=10.0, name=f"auto_{fx}.pdf")
        for m in _ink_margins(out):
            assert abs(m - 10.0) < 0.4, f"{fx}: margins {_ink_margins(out)} != 10 mm"
        d = pdfium.PdfDocument(FX[fx]); sw, sh = d[0].get_width(), d[0].get_height(); d.close()
        d = pdfium.PdfDocument(out);    ow, oh = d[0].get_width(), d[0].get_height(); d.close()
        assert abs(ow - (sw + 20*MM)) < 1 and abs(oh - (sh + 20*MM)) < 1, \
            f"{fx}: sheet {ow:.0f}x{oh:.0f} should be the page plus 2x10 mm"


def test_nup_rejects_impossible_margins():
    """Margins wider than the sheet must be reported, not silently squeezed into
    a 1 pt slot in the corner."""
    _open(FX["framed"])
    p = NUpPanel()
    p.out_fmt.set_format("A4  (210x297mm)")
    p.margin_l.setValue(120.0); p.margin_r.setValue(120.0)
    p.save_pdf = lambda *a, **k: os.path.join(_TMP, "never.pdf")
    _sync_async(p)
    try:
        p._run_action()
        raise AssertionError("no error for margins wider than the sheet")
    except ValueError:
        pass
    pm, info = p._render_preview(400, 400, 1.0)
    assert pm is None and "Platz" in info, f"preview should explain the problem, got {info!r}"


def test_booklet_pads_to_multiples_of_four():
    """A folded sheet carries four pages, so the imposition always rounds up to a
    multiple of four — and it must do that by adding blank *slots* at the end,
    never by dropping or duplicating a source page."""
    for n in (1, 2, 3, 4, 5, 6, 29, 30, 31, 32):
        sides = _booklet_sides(n)
        flat  = [i for side in sides for i in side]
        assert sorted(i for i in flat if i is not None) == list(range(n)), \
            f"n={n}: pages lost or duplicated — {flat}"
        assert len(flat) % 4 == 0 and 0 <= len(flat) - n < 4, \
            f"n={n}: padded to {len(flat)} slots"
    # 4 pages fold as [back|front] then [inside-left|inside-right]
    assert _booklet_sides(4) == [[3, 0], [1, 2]]


def test_booklet_page_order():
    """32 pages must come out in saddle-stitch order with nothing blank: the back
    cover (page 32) shares the first sheet with the front cover, and page 2 lands
    on the reverse of it."""
    out = _impose(FX["booklet32"], mode=0)
    got = _booklet_halves(out)
    want = [(32, 1), (2, 31), (30, 3), (4, 29), (28, 5), (6, 27), (26, 7), (8, 25),
            (24, 9), (10, 23), (22, 11), (12, 21), (20, 13), (14, 19), (18, 15), (16, 17)]
    assert got == want, f"booklet order wrong:\n got {got}\nwant {want}"


def test_booklet_keeps_rotation_and_visible_box():
    """The imposition must place pages the way the viewer shows them. Measuring
    the raw MediaBox with pypdf's merge_transformed_page dropped /Rotate and let
    a print PDF's bleed decide the sheet size."""
    import pikepdf
    src = os.path.join(_TMP, "booklet32_boxes.pdf")
    with pikepdf.open(FX["booklet32"]) as pdf:
        pdf.pages[16].obj["/Rotate"] = 180              # upside down in the viewer
        for i in (0, 31):                               # 3 mm bleed around the trim
            pdf.pages[i].obj["/MediaBox"] = pikepdf.Array([-8.5, -8.5, 603.78, 850.39])
            pdf.pages[i].obj["/CropBox"]  = pikepdf.Array([0, 0, 595.28, 841.89])
        pdf.save(src)
    out = _impose(src, mode=0)

    d = pdfium.PdfDocument(out)
    w, h = d[0].get_width(), d[0].get_height()
    assert abs(w - 2*595.28) < 1 and abs(h - 841.89) < 1, \
        f"bleed inflated the sheet to {w:.0f}x{h:.0f}, expected A3 landscape"
    # page 17 sits on the right of the last sheet; turned 180° its black bar is
    # at the top, not the bottom.
    im = d[15].render(scale=0.25).to_pil().convert("L"); d.close()
    iw, ih = im.size
    half = im.crop((iw // 2, 0, iw, ih)); hw, hh = half.size
    def _mean(box):
        c = half.crop(box)
        return sum(c.get_flattened_data()) / (c.size[0] * c.size[1]) / 255.0
    assert _mean((0, 0, hw, int(hh*.07))) < _mean((0, int(hh*.93), hw, hh)), \
        "the /Rotate of the source page was dropped"
    assert _booklet_halves(out)[0] == (32, 1), "bleed/rotation changed the page order"


def test_booklet_annotation_content_survives():
    """Content that lives in an annotation appearance (a stamp, a filled form
    field) is on the page for the user, so it must be on the printed sheet too —
    a page turned into a Form XObject leaves its annotations behind."""
    import pikepdf
    src = os.path.join(_TMP, "booklet32_stamp.pdf")
    with pikepdf.open(FX["booklet32"]) as pdf:
        ap = pikepdf.Stream(pdf, b"0 0 0 rg 0 0 300 300 re f")
        ap.Type = pikepdf.Name.XObject; ap.Subtype = pikepdf.Name.Form
        ap.BBox = pikepdf.Array([0, 0, 300, 300])
        pdf.pages[31].obj["/Annots"] = pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Stamp,
                               Rect=pikepdf.Array([150, 250, 450, 550]), F=4,
                               AP=pikepdf.Dictionary(N=ap)))])
        pdf.save(src)
    out = _impose(src, mode=0)
    d = pdfium.PdfDocument(out)
    im = d[0].render(scale=0.25).to_pil().convert("L"); d.close()
    w, h = im.size
    left = im.crop((0, 0, w // 2, h))
    px = list(left.get_flattened_data())
    black = sum(1 for v in px if v < 60) / len(px)
    assert black > 0.15, \
        f"the stamp on page 32 is missing from the sheet (black {black:.3f})"


def test_tools_see_the_page_manager():
    """Tools must process the document "Seiten verwalten" is showing. The page
    order, rotations and inserted pages live in an in-memory PageModel until the
    user saves; reading the file instead imposed the pre-edit document — and
    since "Leere Seite einfuegen" appends the blank to the end of the file, it
    surfaced as the back of the cover."""
    from tools._base import ensure_view_snapshot
    from tools.viewer.tab import PdfTab

    # unedited document -> the original file, no copy
    _open(FX["booklet32"])
    st = AppState.get()
    assert ensure_view_snapshot(st.current_pdf) == FX["booklet32"], \
        "an unedited document should not be rewritten"

    # 31 pages + a blank inserted after page 1 == 32 pages on screen
    src31 = os.path.join(_TMP, "booklet31.pdf")
    w = PdfWriter()
    for pg in PdfReader(FX["booklet32"], strict=False).pages[:31]:
        w.add_page(pg)
    with open(src31, "wb") as f: w.write(f)

    tab = PdfTab(src31)
    st.open_pdf(tab.pdf_path); st.page_model = tab.model
    tab._build_manage_once()
    tab.model.selected = {tab.model.order[0]}
    tab._manage_panel._insert_blank()
    assert len(tab.model.order) == 32, "the blank was not inserted"
    assert st.current_pdf == tab.pdf_path, \
        "the rebuilt file was not published — tools still read the old one"

    out = _impose(st.current_pdf, open_first=False, mode=0)
    got = _booklet_halves(out)
    assert got[0] == (31, 1), \
        f"the back of the cover should carry page 31, got {got[0]}"
    assert got[1] == ("blank", 30), \
        f"the inserted blank belongs on the inside of the cover, got {got[1]}"
