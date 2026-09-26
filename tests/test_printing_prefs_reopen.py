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


def test_the_dialog_reopens_on_what_was_used_last():
    """It opened on the queue's defaults every time, so the same job printed the
    same way all day meant re-picking the tray, the paper and the sides on every
    document.

    What was used last outranks the queue's default; the queue's default
    outranks whatever Qt guessed. A printer never used before still opens on its
    own settings."""
    import tools.printing.dialog as dialog_mod
    from tools.printing import prefs

    queue = ("PageSize/Media Size: *A4 Letter A3\n"
             "InputSlot/Media Source: Auto *Tray1 Tray2\n"
             "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")

    def opened():
        tab, dlg = _print_dialog(3, "prefs_tab.pdf")
        # Let the real printer-list fetch land first: it re-runs
        # _on_printer_changed when it does, which would undo the stand-in queue
        # set up below.
        _spin(60, 0.0)
        dialog_mod._QUEUE_INFO_CACHE.clear()
        dlg.printer_combo.blockSignals(True)
        dlg.printer_combo.clear()
        dlg.printer_combo.addItem("office", "office")
        dlg.printer_combo.blockSignals(False)
        dlg._on_printer_changed()
        _spin(40, 0.0)
        return tab, dlg

    prefs.forget()
    try:
        with _stub_queue(queue):
            tab, dlg = opened()
            # The queue's own paper is no longer copied into the combo: the
            # dialog stays on "leave it to the printer", which is the same
            # sheet without naming it on the job. Sides still come from the
            # queue, because a duplex setting has to be sent to take effect.
            assert dlg.paper_combo.currentData() == "", \
                "the queue's paper was copied into the job as an override"
            assert dlg.duplex_check.isChecked() is False, "ignored the queue's sides"

            dlg.paper_combo.setCurrentIndex(dlg.paper_combo.findData("A3"))
            dlg.duplex_check.setChecked(True)
            dlg.source_combo.setCurrentIndex(dlg.source_combo.findData("Tray2"))
            dlg.copies_spin.setValue(5)
            dlg.reverse_check.setChecked(True)
            dlg.poster_pct.setValue(150)
            dlg.nup_count.setCurrentIndex(0)          # 2
            dlg.booklet_bind.setCurrentIndex(1)       # right
            dlg.by_page_size_check.setChecked(True)
            dlg._set_handling("nup")
            prefs.remember("office", dlg._current_settings())
            dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

            tab2, dlg2 = opened()
            assert dlg2.paper_combo.currentData() == "A3", "forgot the paper"
            assert dlg2.duplex_check.isChecked() is True, "forgot the sides"
            assert dlg2.source_combo.currentData() == "Tray2", "forgot the tray"
            assert dlg2.handling == "nup", "forgot the handling tab"
            assert dlg2.nup_count.currentData() == 2, "forgot pages/sheet"
            assert dlg2.poster_pct.value() == 150, "forgot tile scale"
            assert dlg2.booklet_bind.currentData() == "right", "forgot bind"
            assert dlg2.by_page_size_check.isChecked() is True
            assert dlg2._job_paper() == "", "by-page-size still sends no media"
            assert dlg2.copies_spin.value() == 1, "copies are a job, not a habit"
            assert dlg2.reverse_check.isChecked() is False, "reverse is a job"
            assert prefs.last_printer() == "office"
            prefs.remember("office", {**dlg2._current_settings(), "nup_count": 7})
            dlg2._restore_saved("office")
            assert dlg2.nup_count.currentData() == 4, "invalid nup count must fall back to 4"
            dlg2.deleteLater(); tab2.deleteLater(); _app.processEvents()
    finally:
        prefs.forget()

