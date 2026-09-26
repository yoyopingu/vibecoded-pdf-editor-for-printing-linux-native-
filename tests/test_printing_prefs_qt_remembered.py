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


def test_a_remembered_setting_survives_qt_disagreeing_with_it():
    """The restore happens in the same step as the queue defaults, so it was
    overwritten the same way — which is why the two-sided setting in particular
    never came back: Qt reports duplex for most office printers."""
    import tools.printing.dialog as dialog_mod
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageSize
    from tools.printing import prefs

    queue = ("PageSize/Media Size: *A4 Letter A3\n"
             "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")
    prefs.forget()
    # Remembered: A3 and two-sided. The queue says A4/one-sided, Qt says
    # Letter/one-sided. What the operator used last must win over both.
    prefs.remember("office", {"paper": "A3", "duplex": True,
                              "duplex_edge": "short"})
    tab = dlg = None
    try:
        with _fake_qt_printer(QPageSize.PageSizeId.Letter,
                              QPrinter.DuplexMode.DuplexNone), _stub_queue(queue):
            dialog_mod._QUEUE_INFO_CACHE.clear()
            tab, dlg = _print_dialog(3, "remembered_tab.pdf")
            _spin(60, 0.0)
            dlg.printer_combo.blockSignals(True)
            dlg.printer_combo.clear()
            dlg.printer_combo.addItem("office", "office")
            dlg.printer_combo.blockSignals(False)
            dlg._on_printer_changed()
            _spin(40, 0.0)
            assert dlg.paper_combo.currentData() == "A3", "forgot the paper"
            assert dlg.duplex_check.isChecked() is True, "forgot two-sided"
            assert dlg.duplex_edge_combo.currentData() == "short", "forgot the edge"
    finally:
        prefs.forget()
        if dlg is not None:
            dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

