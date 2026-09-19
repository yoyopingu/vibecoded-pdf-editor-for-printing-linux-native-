"""
Print-dialog imposition (poster, later n-up and booklet).

Kept out of test_printing.py because that module is already at the heap
limit: extra pdfium/pikepdf work in the same process segfaults later
dialog tests. tests/run.py runs each file in its own process.
"""
import os
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tests.support import _TMP, _app, _spin


def _poster_src(name, n=1, mark="center"):
    """A4 pages with a known mark so tiles can be told apart."""
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    w, h = A4
    for i in range(n):
        c.setFillColorRGB(0, 0, 0)
        if mark == "center":
            c.rect(w / 2 - 6, h / 2 - 40, 12, 80, fill=1, stroke=0)
        elif mark == "blob":
            c.rect(w / 2 - 80, h / 2 - 80, 160, 160, fill=1, stroke=0)
        c.setFont("Helvetica", 36)
        c.drawCentredString(w / 2, 80, f"P{i + 1}")
        c.showPage()
    c.save()
    return src


def _poster_dark(path, page_i, box, scale=1.5):
    """Dark pixels in a PDF-space box (x, y_from_bottom, w, h) on one page."""
    d = pdfium.PdfDocument(path)
    try:
        page = d[page_i]
        ph = page.get_height()
        pil = page.render(scale=scale, fill_color=(255, 255, 255, 255)
                          ).to_pil().convert("L")
    finally:
        d.close()
    x, y, w, h = box
    x0 = max(0, int(x * scale))
    y0 = max(0, int((ph - y - h) * scale))
    x1 = min(pil.size[0], x0 + max(1, int(w * scale)))
    y1 = min(pil.size[1], y0 + max(1, int(h * scale)))
    crop = pil.crop((x0, y0, x1, y1))
    return sum(1 for v in crop.get_flattened_data() if v < 200)


def test_poster_200_percent_a4_is_four_tiles():
    """Acrobat Poster at 200 % of A4 on A4 paper is a 2×2 — four sheets,
    each the paper's size, not one enlarged page."""
    from tools.printing.handling import build_poster_pdf, poster_grid
    from tools.printing.spool import paper_size_pt

    paper = paper_size_pt("A4")
    cols, rows, scale = poster_grid(A4[0], A4[1], paper[0], paper[1], 200)
    assert (cols, rows, scale) == (2, 2, 2.0), (cols, rows, scale)
    one, two = poster_grid(A4[0], A4[1], paper[0], paper[1], 100)[0:2]
    assert (one, two) == (1, 1)

    src = _poster_src("poster_200.pdf", n=1, mark="blob")
    out = os.path.join(_TMP, "poster_200_out.pdf")
    n = build_poster_pdf(src, [0], paper, 200, 0, False, False, out)
    assert n == 4, n
    r = PdfReader(out)
    assert len(r.pages) == 4
    for p in r.pages:
        w, h = float(p.mediabox.width), float(p.mediabox.height)
        assert abs(w - paper[0]) < 0.5 and abs(h - paper[1]) < 0.5, (w, h)

    src2 = _poster_src("poster_2p.pdf", n=2, mark="blob")
    out2 = os.path.join(_TMP, "poster_2p_out.pdf")
    n2 = build_poster_pdf(src2, [0, 1], paper, 200, 0, False, False, out2)
    assert n2 == 8, n2
    return "200 % A4→A4 = 4 tiles; two pages = 8"


def test_poster_overlap_shares_pixels_on_adjacent_tiles():
    """Overlap is extra of the neighbour on the same sheet count — 200 %
    stays four tiles, but the centre mark of the page appears further
    into the right-hand tile once overlap is on."""
    from tools.printing.handling import build_poster_pdf
    from tools.printing.spool import paper_size_pt

    paper = paper_size_pt("A4")
    src = _poster_src("poster_ov.pdf", n=1, mark="center")
    none = os.path.join(_TMP, "poster_ov0.pdf")
    some = os.path.join(_TMP, "poster_ov30.pdf")
    assert build_poster_pdf(src, [0], paper, 200, 0, False, False, none) == 4
    assert build_poster_pdf(src, [0], paper, 200, 30, False, False, some) == 4

    # Top-right tile (col 1, row 0) is page 1. The centre mark sits on
    # the join — left and bottom edges of that tile — so a patch 50 pt
    # in from the left, along the bottom, is empty without overlap and
    # holds the mark once the step shrinks by 30 mm.
    patch = (50, 0, 80, 90)
    assert _poster_dark(none, 1, patch) < 30, "centre mark leaked without overlap"
    assert _poster_dark(some, 1, patch) > 200, "overlap did not pull the join in"
    return "30 mm overlap keeps 4 tiles and shares the centre mark"


def test_poster_cut_marks_and_labels_land_on_the_tile():
    """Cut marks are strokes on the sheet (inward, so they survive the
    cut). Labels are the filename plus the A1-style tile id."""
    from tools.printing.handling import build_poster_pdf
    from tools.printing.spool import paper_size_pt

    paper = paper_size_pt("A4")
    src = _poster_src("poster_marks.pdf", n=1, mark="blob")
    off = os.path.join(_TMP, "poster_marks_off.pdf")
    on = os.path.join(_TMP, "poster_marks_on.pdf")
    build_poster_pdf(src, [0], paper, 200, 0, False, False, off,
                     label_name="job.pdf")
    build_poster_pdf(src, [0], paper, 200, 0, True, True, on,
                     label_name="job.pdf")

    corner = (0, 0, 18, 18)
    assert _poster_dark(off, 0, corner, scale=2) < 8, \
        "corner was not empty without marks"
    assert _poster_dark(on, 0, corner, scale=2) > 15, \
        "cut marks did not stroke the corner"

    label = (4, paper[1] - 22, 90, 16)
    assert _poster_dark(off, 0, label, scale=2) < 8, \
        "top-left was not empty without a label"
    assert _poster_dark(on, 0, label, scale=2) > 20, \
        "filename / tile id did not land on the tile"
    return "cut marks at the corner; label job.pdf A1"


def test_poster_print_path_tiles_then_spools_at_100_percent():
    """_do_print must build the poster PDF and send it as Feste Größe at
    100 %, so CUPS cannot fit a tile that is already the sheet. The
    wiring is in the method body; creating a PrintDialog in the printing
    module to prove it trips the heap fault."""
    import inspect
    from tools.printing.dialog import PrintDialog
    src = inspect.getsource(PrintDialog._do_print)
    assert "build_poster_pdf" in src
    assert 'handling == "poster"' in src
    assert "print_scale = 1" in src
    assert "print_pct = 100" in src
    assert "not as_bitmap" in src
    return "poster jobs tile first, then spool at fixed 100 %"


def test_poster_preview_shows_a_tile_of_the_imposed_pdf():
    """Switching to Poster changes the caption and walks four tiles at
    200 %. The preview still composites the page pixmap (the printing
    module cannot host a second pdfium raster of the tiled PDF); the
    spooled file is the real poster from build_poster_pdf."""
    from tools.viewer.tab import PdfTab
    from tools.printing.dialog import PrintDialog

    src = _poster_src("poster_dlg.pdf", n=1, mark="blob")
    tab = PdfTab(src)
    dlg = PrintDialog(tab.pdf_path, tab.model, tab)
    try:
        idx = dlg.paper_combo.findData("A4")
        if idx < 0:
            dlg.paper_combo.addItem("A4", "A4")
            idx = dlg.paper_combo.findData("A4")
        dlg.paper_combo.setCurrentIndex(idx)
        dlg._handling_bar.setCurrentIndex(1)
        _spin(15)
        assert dlg.handling == "poster"
        assert dlg._handling_panes["poster"].isVisibleTo(dlg)
        cap = dlg._preview._info_lbl.text()
        assert "Poster" in cap and "200" in cap, cap
        nav = dlg._preview._page_lbl.text()
        assert "1" in nav and "4" in nav, nav
        dlg._preview._next_page()
        _spin(10)
        assert "2" in dlg._preview._page_lbl.text()
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()
    return "preview walks 4 tiles at 200 % A4"
