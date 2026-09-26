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


def test_print_spools_exactly_what_was_asked_for():
    """The job handed to the spooler has to be the pages the operator picked, in
    the order and rotation the viewer is showing — including page-manager edits
    that have not been saved to the file yet."""
    tab, dlg = _print_dialog()
    from tools.printing.spool import write_subset_pdf

    def spool(tag):
        out = os.path.join(_TMP, f"spool_{tag}.pdf")
        # The spooler itself, given the document and the pages the dialog
        # resolved — which is exactly what the print job hands it.
        write_subset_pdf(dlg.pdf_path, dlg.model, dlg._get_pages(), out)
        return _page_labels(out), out

    try:
        dlg.radio_range.setChecked(True); dlg.range_edit.setText("3-5, 8")
        assert spool("range")[0] == ["P3", "P4", "P5", "P8"]
        assert [p + 1 for p in dlg._preview_pages()] == [3, 4, 5, 8], \
            "the preview disagrees with the job"

        tab.single._current = 6
        dlg.radio_current.setChecked(True)
        assert spool("current")[0] == ["P7"], "'current page' printed the wrong sheet"

        dlg.radio_all.setChecked(True)
        assert len(spool("all")[0]) == 10

        # Unsaved reorder + rotation must reach the printer.
        tab.model.move(0, 10)
        tab.model.selected = {tab.model.order[0]}
        tab.model.rotate_selected(90)
        dlg.radio_range.setChecked(True); dlg.range_edit.setText("1-3")
        labels, out = spool("edited")
        assert labels == ["P2", "P3", "P4"], f"page-manager order ignored: {labels}"
        rot = [int(p.get("/Rotate", 0) or 0) for p in PdfReader(out).pages]
        assert rot == [90, 0, 0], f"page-manager rotation ignored: {rot}"
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()

