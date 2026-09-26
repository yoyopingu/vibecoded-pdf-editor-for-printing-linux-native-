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


def test_reverse_prints_pages_back_to_front():
    """Umgekehrt walks the chosen pages last-to-first, and the preview
    must show that same order — not the document order with a checkbox
    that only affects the job."""
    tab, dlg = _print_dialog()
    try:
        assert dlg._get_pages() == list(range(10))
        dlg.reverse_check.setChecked(True)
        assert dlg._get_pages() == list(range(9, -1, -1))
        assert dlg._preview_pages() == list(range(9, -1, -1))
        dlg.radio_range.setChecked(True)
        dlg.range_edit.setText("3-5, 8")
        assert dlg._get_pages() == [7, 4, 3, 2]
        assert [p + 1 for p in dlg._preview_pages()] == [8, 5, 4, 3]
        dlg.reverse_check.setChecked(False)
        assert dlg._get_pages() == [2, 3, 4, 7]

        assert dlg.orient_idx == 0
        dlg._set_orient_idx(2)
        assert dlg.orient_idx == 2
        dlg._orient_btns[1].setChecked(True)
        assert dlg.orient_idx == 1

        assert dlg.handling == "size"
        assert dlg.poster_pct.value() == 200
        assert dlg.nup_count.currentData() == 4
        assert dlg.booklet_bind.currentData() == "left"
        assert dlg._handling_bar.count() == 4
        assert "poster" in dlg._handling_panes
        assert dlg._size_pane.isVisibleTo(dlg)
        # Switching the Poster tab builds a real tiled PDF in the preview
        # worker. That extra pikepdf/pdfium work in this module is what
        # trips the heap fault on later dialog tests — the tab is covered
        # in test_print_handling.py.

        idx = dlg.paper_combo.findData("A4")
        if idx < 0:
            dlg.paper_combo.addItem("A4", "A4")
            idx = dlg.paper_combo.findData("A4")
        dlg.paper_combo.setCurrentIndex(idx)
        assert dlg._job_paper() == "A4"
        dlg.by_page_size_check.setChecked(True)
        assert dlg.selected_paper() == "A4"
        assert dlg._job_paper() == ""
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()
    return "reverse, orientation buttons, handling tabs, by-page-size"

