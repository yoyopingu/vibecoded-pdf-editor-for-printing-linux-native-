"""Printing: prefs behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _stub_queue, _fake_qt_printer


def test_the_queue_answer_does_not_overwrite_a_deliberate_choice():
    """Asking CUPS what the queue defaults to happens in the background, and on
    a network queue that takes a moment. If the operator has changed the paper
    or the sides in the meantime, the answer must fill in blanks rather than
    undo them."""
    import tools.printing.dialog as dialog_mod
    from tools.printing import prefs

    queue = ("PageSize/Media Size: *A4 Letter A3\n"
             "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")
    prefs.forget()
    tab, dlg = _print_dialog(3, "race_tab.pdf")
    try:
        _spin(60, 0.0)                       # the real printer list, out of the way
        with _stub_queue(queue):
            dialog_mod._QUEUE_INFO_CACHE.clear()
            dlg.printer_combo.blockSignals(True)
            dlg.printer_combo.clear()
            dlg.printer_combo.addItem("office", "office")
            dlg.printer_combo.blockSignals(False)
            dlg._on_printer_changed()

            # …the operator picks, before CUPS has answered
            dlg.paper_combo.setCurrentIndex(dlg.paper_combo.findData("A3"))
            dlg.duplex_check.setChecked(True)
            assert dlg._settings_touched, "the change was not noticed"

            _spin(40, 0.0)                   # now the answer lands
            assert dlg.paper_combo.currentData() == "A3", \
                "the queue default overwrote the operator's paper"
            assert dlg.duplex_check.isChecked() is True, \
                "the queue default overwrote the operator's sides"
    finally:
        prefs.forget()
        dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

