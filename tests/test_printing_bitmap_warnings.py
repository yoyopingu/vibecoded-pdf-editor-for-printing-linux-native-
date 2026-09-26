"""Printing: bitmap behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_a_file_the_print_path_redraws_says_so_before_it_is_printed():
    """The first version of this warning asked whether fonts were embedded.
    That is one reason among several, and the first real file to misprint had
    its fonts embedded and produced no warning at all — a broken font program,
    a Type 3 or a bad CID map would each have slipped past the same way.

    So the check runs a page through the print path and redraws it with the
    preview's renderer, and warns about what it sees rather than about the one
    cause somebody thought of first."""
    import time
    import pikepdf
    from tools.printing.dialog import PrintDialog
    from tools.viewer.tab import PdfTab

    # A font that is only referenced, with metrics Ghostscript will not match.
    src = os.path.join(_TMP, "warn_redrawn.pdf")
    pdf = pikepdf.new()
    font = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/Type1"),
        BaseFont=pikepdf.Name("/ArialNarrow"),
        Encoding=pikepdf.Name("/WinAnsiEncoding")))
    page = pdf.add_blank_page(page_size=(595, 842))
    page.obj["/Resources"] = pikepdf.Dictionary(
        Font=pikepdf.Dictionary(F1=font))
    page.contents_add(pikepdf.Stream(pdf,
        b"BT /F1 28 Tf 40 700 Td (HAMBURGEFONTSIV the quick brown fox) Tj ET"))
    pdf.save(src)

    tab = PdfTab(src); dlg = PrintDialog(src, tab.model, tab)
    try:
        for _ in range(250):
            _app.processEvents(); time.sleep(0.02)
            if dlg.status_lbl.text():
                break
        assert dlg._print_differs, \
            "the print path redraws this page and the check did not notice"
        said = dlg.status_lbl.text()
        assert said, "nothing was said about a page that will print wrong"
        assert "Bitmap" in said, \
            "the warning does not point at the setting that fixes it"
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "warns on what the print path actually does to the page"


def test_a_file_with_nothing_to_warn_about_stays_quiet():
    """A warning shown on every job is one nobody reads."""
    import time
    import pikepdf
    from tools.printing.dialog import PrintDialog
    from tools.viewer.tab import PdfTab

    src = os.path.join(_TMP, "no_fonts_at_all.pdf")
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(595, 842))
    page.contents_add(pikepdf.Stream(pdf, b"0 0 1 rg 100 100 200 300 re f"))
    pdf.save(src)

    tab = PdfTab(src); dlg = PrintDialog(src, tab.model, tab)
    try:
        for _ in range(150):
            _app.processEvents(); time.sleep(0.02)
        assert dlg._unembedded == [], dlg._unembedded
        assert not dlg._print_differs, "a blank page was said to redraw"
        assert dlg.status_lbl.text() == "", \
            f"warned about a file with no fonts: {dlg.status_lbl.text()!r}"
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "no fonts, no warning"
