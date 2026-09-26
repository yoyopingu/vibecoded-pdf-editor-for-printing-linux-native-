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


def test_the_queue_wins_over_qt_on_every_open_not_just_the_first():
    """CUPS is asked in the background and the answer is cached for the session.
    A cached answer is applied on the spot — so when the ask happened at the top
    of _on_printer_changed, everything below it promptly overwrote the answer
    with Qt's, and the dialog came back to Letter and two-sided on the second
    and every later open. The first open looked right, which is what made it
    easy to believe it was fixed.

    Qt is consulted only because it answers instantly. It is the weakest source
    and has to be applied first, not last."""
    import tools.printing.dialog as dialog_mod
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageSize
    from tools.printing import prefs

    queue = ("PageSize/Media Size: *A4 Letter A3\n"
             "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")
    prefs.forget()
    dialog_mod._QUEUE_INFO_CACHE.clear()
    opened = []
    try:
        with _fake_qt_printer(QPageSize.PageSizeId.Letter,
                              QPrinter.DuplexMode.DuplexLongSide), _stub_queue(queue):
            for attempt in range(3):
                tab, dlg = _print_dialog(3, f"qt_vs_cups_{attempt}.pdf")
                opened.append((tab, dlg))
                _spin(60, 0.0)
                dlg.printer_combo.blockSignals(True)
                dlg.printer_combo.clear()
                dlg.printer_combo.addItem("office", "office")
                dlg.printer_combo.blockSignals(False)
                dlg._on_printer_changed()
                _spin(40, 0.0)
                # What this has always guarded is that Qt's guess does not
                # win. It cannot now: no paper is chosen at all, so nothing
                # is sent and the queue's own A4 stands. Letter appearing
                # here would still be the bug it always was.
                assert dlg.paper_combo.currentData() == "", (
                    f"open {attempt + 1}: a paper was chosen for the operator")
                assert dlg.paper_combo.currentData() != "Letter", (
                    f"open {attempt + 1}: Qt's Letter beat the queue")
                assert dlg.duplex_check.isChecked() is False, (
                    f"open {attempt + 1}: Qt's duplex beat the queue's one-sided")
    finally:
        prefs.forget()
        for tab, dlg in opened:
            dlg.deleteLater(); tab.deleteLater()
        _app.processEvents()

