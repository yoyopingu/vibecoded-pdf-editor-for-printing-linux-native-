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


def test_print_preview_and_job_agree_on_a_bad_range():
    """The preview used to clamp an out-of-range entry while the job rejected it,
    so "5-99" on a ten-page file previewed six printable pages and then refused
    to print. Showing a job that cannot run is its own kind of lie."""
    tab, dlg = _print_dialog()
    try:
        dlg.radio_range.setChecked(True)
        dlg.range_edit.setText("5-99")
        assert dlg._get_pages() is None, "the job accepted an out-of-range request"
        assert not dlg._preview_pages(), "the preview promised pages that will not print"
        dlg.range_edit.setText("4-6")
        assert dlg._get_pages() == [3, 4, 5]
        assert [p + 1 for p in dlg._preview_pages()] == [4, 5, 6]
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()

