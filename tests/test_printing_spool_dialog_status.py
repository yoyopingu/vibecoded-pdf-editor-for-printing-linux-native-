"""Printing: spool behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_print_reports_the_sheets_it_actually_sent():
    """Unreadable pages are dropped from the job, so counting the requested
    pages told the operator more sheets were coming than the printer got — while
    listing the skipped ones in the same sentence."""
    tab, dlg = _print_dialog()
    try:
        dlg._progress = None
        dlg._after_print_close = lambda: None
        dlg._finish(list(range(10)), 2, [3, 7])
        text = dlg.status_lbl.text()
        assert "8" in text and "16" in text, \
            f"expected 8 pages x 2 copies = 16 sheets, got: {text}"
        assert "3, 7" in text or "[3, 7]" in text, f"skipped pages not named: {text}"
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()

