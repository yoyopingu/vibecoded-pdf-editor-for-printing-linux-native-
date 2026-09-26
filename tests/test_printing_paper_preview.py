"""Printing: paper behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_tab


def _preview_for(paper_key, margin_mm=3.0):
    """A print preview settled on one paper choice, with its rendered canvas."""
    from tools.printing.preview import _PrintPreview
    # This checks the standalone preview. Creating a PrintDialog here used to
    # leave its background workers and Qt children alive between screenshots.
    tab = _print_tab(2, f"prev_{paper_key or 'none'}.pdf")
    pv = _PrintPreview(tab.pdf_path, tab.model, None)
    pv.resize(300, 420)
    _spin(60, 0.0)
    pv.update_settings(0, paper_key, 0, margin_mm, 100)
    _spin(30, 0.0)
    return tab, pv


def _dashes(pixmap):
    """How many pixels of the margin indicator's dashed red are in the canvas."""
    from PyQt6.QtGui import QImage
    img = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)
    hits = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            if (150 < c.red() < 215 and 70 < c.green() < 135
                    and 70 < c.blue() < 135):
                hits += 1
    return hits


def test_the_preview_draws_no_sheet_when_no_paper_was_chosen():
    """With the paper left to the printer there is no sheet to draw the page
    against, and the preview was drawing one anyway — an A4 one, because its
    own copy of the size table answered A4 for anything it did not recognise.

    Everything the sheet is used for is a claim about a sheet nobody picked:
    the white rectangle, the dashed printable-area boundary, the fitting, and
    the warning about edges that will be cut. None of it is drawn now.
    """
    tab, pv = _preview_for("")
    try:
        assert pv._paper_dims_mm() is None, \
            "the preview invented a sheet for 'as set on the printer'"
        marks = _dashes(pv._canvas.pixmap())
        assert marks == 0, \
            f"{marks} pixels of bleed marking drawn for an unknown sheet"
        info = pv._info_lbl.text()
        assert "→" not in info, f"the info line names a target sheet: {info!r}"
        assert "Drucker" in info or "printer" in info, info
    finally:
        pv.deleteLater(); tab.deleteLater(); _app.processEvents()

    # And with a real sheet the marks are still there — this must not have
    # turned the indicator off for everyone.
    tab, pv = _preview_for("A4")
    try:
        assert pv._paper_dims_mm() is not None
        assert _dashes(pv._canvas.pixmap()) > 0, \
            "the margin indicator vanished for a chosen paper too"
        assert "→" in pv._info_lbl.text()
    finally:
        pv.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "no sheet, no bleed marks; a chosen sheet still shows them"


def test_the_preview_uses_the_size_that_was_actually_chosen():
    """It kept its own nine-entry copy of the paper table, with the same A4
    answer for anything missing from it — so choosing SRA3 previewed an A4
    sheet while the job went out as SRA3. One table now, the spooler's."""
    tab, pv = _preview_for("SRA3")
    try:
        dims = pv._paper_dims_mm()
        assert dims is not None, "SRA3 is unknown to the preview"
        w, h = dims
        assert abs(w - 320) < 1 and abs(h - 450) < 1, \
            f"the preview thinks SRA3 is {w:.0f}x{h:.0f} mm"
        assert "320×450" in pv._info_lbl.text(), pv._info_lbl.text()
    finally:
        pv.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "SRA3 previews as 320x450, not as A4"

