"""Printing: dialog behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _settle, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_original_size_takes_a_percentage_that_reaches_the_file():
    """"Originalgrösse" could only ever mean 100 %. It now has a percentage
    beside it, and the number has to change the file that is sent to the
    printer — not merely the preview.

    The box only appears for that option: a percentage of "fit to page" or of
    "shrink only" is not a thing, since both of those already decide the size."""
    from tools.printing.dialog import PrintDialog
    from tools.printing.spool import recenter_on_paper
    from tools.viewer.tab import PdfTab
    import pypdfium2 as pdfium

    tab = PdfTab(FX["normal"])
    dlg = PrintDialog(tab.pdf_path, tab.model, tab)
    try:
        assert dlg._scale_index() == 0, "the dialog should open on 'fit to page'"
        dlg.scale_fixed.setChecked(True)             # Feste Größe
        _spin(5)
        assert dlg._scale_index() == 1
        assert dlg.scale_pct.isEnabled(), "no percentage beside Feste Größe"
        assert dlg.scale_pct.value() == 100, "the default is not 100 %"
        dlg.scale_fit.setChecked(True)               # An Seite anpassen
        _spin(5)
        assert not dlg.scale_pct.isEnabled(), \
            "a percentage of 'fit to page' is not a thing"
        # One at a time: the three modes are exclusive, so they are radio
        # buttons on show rather than a dropdown to open.
        dlg.scale_shrink.setChecked(True)
        assert [b.isChecked() for b in dlg._scale_buttons()] == [False, False, True]
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()

    # And the number really scales the content that goes to the printer.
    def ink_span(path):
        doc = pdfium.PdfDocument(path)
        try:
            im = doc[0].render(scale=0.5).to_pil().convert("L")
            px = list(im.get_flattened_data()); w, h = im.size
            cols = [x for x in range(w) if any(px[y * w + x] < 200 for y in range(h))]
            rows = [y for y in range(h) if any(px[y * w + x] < 200 for x in range(w))]
            return (max(cols) - min(cols), max(rows) - min(rows)) if cols and rows else (0, 0)
        finally:
            doc.close()

    # Measured on a rectangle and nothing else. This used to use the framed
    # fixture, on the reasoning that a drawn rectangle has the width it has
    # while base-14 text does not — pdfium substitutes whatever the system
    # offers, stable within a process and not between them. But that fixture
    # draws "CENTER" in Helvetica as well as the frame, so the substituted
    # font was still in the measurement and this still failed about half the
    # time. A page with no text in it cannot have that problem.
    from reportlab.pdfgen import canvas as _canvas
    plain = os.path.join(_TMP, "pct_src.pdf")
    c = _canvas.Canvas(plain, pagesize=A4)
    c.setLineWidth(3); c.rect(20, 20, A4[0] - 40, A4[1] - 40)
    c.showPage(); c.save()

    full = os.path.join(_TMP, "pct_100.pdf")
    half = os.path.join(_TMP, "pct_50.pdf")
    recenter_on_paper(plain, full, 595.276, 841.89, factor=1.0)
    recenter_on_paper(plain, half, 595.276, 841.89, factor=0.5)
    fw, fh = ink_span(full)
    hw, hh = ink_span(half)
    assert fw > 0 and hw > 0, "nothing was drawn to measure"
    assert abs(hw / fw - 0.5) < 0.05 and abs(hh / fh - 0.5) < 0.05, \
        f"50 % produced ink of {hw}x{hh} against {fw}x{fh} at 100 %"
    return f"100 % -> {fw}x{fh} px of ink, 50 % -> {hw}x{hh}"


def test_the_page_range_starts_from_what_is_selected_in_the_page_manager():
    """The range field starts as the pages picked in "Seiten verwalten", written
    the way that window writes them — _positions_to_str is shared, so the two
    read identically rather than merely similarly. Nothing picked there means an
    empty field here."""
    from tools.printing.dialog import PrintDialog
    from tools.viewer.tab import PdfTab

    tab = PdfTab(FX["normal"])
    tab._build_manage_once()
    _spin(10)
    try:
        for picked, expected in (([], ""), ([0], "1"), ([0, 1, 2, 4], "1-3, 5")):
            tab.model.selected = {tab.model.order[i] for i in picked}
            dlg = PrintDialog(tab.pdf_path, tab.model, tab)
            try:
                assert dlg.range_edit.text() == expected, (
                    f"pages {picked} in the page manager gave "
                    f"{dlg.range_edit.text()!r}, expected {expected!r}")
                # The radio is left alone: the pages are ready if wanted, not
                # chosen on the operator's behalf.
                assert dlg.radio_all.isChecked(), \
                    "filling the field must not change what gets printed"
            finally:
                dlg.close(); _app.processEvents()
    finally:
        tab.deleteLater(); _app.processEvents()
    return "empty, single page, and a mixed run all match the page manager"


def test_the_preview_only_warns_when_something_will_actually_be_clipped():
    """The preview went red as soon as the page was geometrically larger than
    the printable area — which nearly every page is, because a printer cannot
    reach the outer 3.5 mm and nearly every page has a white border wider than
    that. A warning that cries wolf on every file is one nobody reads on the
    file that deserves it.

    It now asks whether anything is actually drawn out there."""
    from PyQt6.QtGui import QPixmap, QPainter, QColor
    from tools.printing.preview import _PrintPreview
    from tools.viewer.tab import PdfTab

    tab = PdfTab(FX["normal"])
    _spin(10)
    pv = _PrintPreview(tab.pdf_path, tab.model)
    try:
        pv._page_w_pt, pv._page_h_pt = 595.276, 841.89
        pv._margin_mm = 3.5

        def page(edge_to_edge):
            pm = QPixmap(400, 566); pm.fill(QColor("white"))
            p = QPainter(pm)
            if edge_to_edge:
                p.fillRect(0, 0, 400, 566, QColor(30, 30, 30))
            else:
                p.fillRect(60, 80, 280, 400, QColor(30, 30, 30))
            p.end()
            return pm

        # Content 9 mm wider than the printable area, either way.
        big_w, big_h, pr_w, pr_h = 222.0, 314.0, 203.0, 290.0

        pv._pixmap = page(False)
        assert not pv._ink_outside(big_w, big_h, pr_w, pr_h), \
            "warned about an overhang that is blank paper"

        pv._pixmap = page(True)
        assert pv._ink_outside(big_w, big_h, pr_w, pr_h), \
            "did not warn about ink that will be cut off"

        # Nothing sticking out: nothing to warn about, whatever is on the page.
        assert not pv._ink_outside(200.0, 280.0, pr_w, pr_h)

        # And with no render to look at, err towards warning.
        pv._pixmap = None
        assert pv._ink_outside(big_w, big_h, pr_w, pr_h), \
            "an unmeasurable page should still warn"
    finally:
        pv.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "blank overhang stays quiet, ink at the edge warns"


def test_enter_prints_instead_of_turning_the_preview_page():
    """Enter runs the tool in every panel, and the print dialog is the one
    place a run costs paper — so it had better be the run it starts.

    It was not. Every QPushButton in a QDialog is autoDefault, so Qt made the
    first in tab order the default, and the first here belongs to the embedded
    preview: the ▶ arrow built in printing/preview.py. Enter turned the
    preview page. Not a missing feature so much as the key already being
    answered by the wrong control.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton
    from tools.printing.dialog import PrintDialog

    printed = []
    real = PrintDialog._do_print
    PrintDialog._do_print = lambda self: printed.append(1)
    try:
        tab, dlg = _print_dialog()
        try:
            dlg.show(); dlg.activateWindow(); dlg.raise_(); _app.processEvents()
            assert dlg.isActiveWindow(), "fixture never got window focus"

            # The arrows must no longer be able to answer for Enter.
            defaults = [b.text().strip() for b in dlg.findChildren(QPushButton)
                        if b.isDefault()]
            assert defaults == ["Drucken"], \
                f"the default button is {defaults}, not the print button"

            QTest.keyClick(dlg, Qt.Key.Key_Return); _app.processEvents()
            assert printed, "Enter did not print"

            # Typing a page range and pressing Enter is the natural way in.
            printed.clear()
            dlg.range_edit.setFocus(); _app.processEvents()
            QTest.keyClick(dlg.range_edit, Qt.Key.Key_Return); _app.processEvents()
            assert printed, "Enter in the range box did not print"

            # The numeric keypad's Enter is a different key, and the panels
            # bind both.
            printed.clear()
            QTest.keyClick(dlg, Qt.Key.Key_Enter); _app.processEvents()
            assert printed, "the keypad's Enter did not print"
        finally:
            dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    finally:
        PrintDialog._do_print = real
    return "Enter prints, from the dialog, the range box and the keypad"


def test_enter_on_a_focused_cancel_still_cancels():
    """Cancel keeps its own Enter. A focused Cancel that prints instead is how
    a copyshop finds out it has run a hundred sheets nobody asked for — and
    every other button gave Enter up precisely so this one could keep it."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QPushButton
    from tools.printing.dialog import PrintDialog

    printed = []
    real = PrintDialog._do_print
    PrintDialog._do_print = lambda self: printed.append(1)
    try:
        tab, dlg = _print_dialog()
        try:
            dlg.show(); dlg.activateWindow(); dlg.raise_(); _app.processEvents()
            cancel = [b for b in dlg.findChildren(QPushButton)
                      if b.text().strip() == "Abbrechen"]
            assert cancel, "no Cancel button to check"
            cancel[0].setFocus(); _app.processEvents()
            QTest.keyClick(cancel[0], Qt.Key.Key_Return); _app.processEvents()
            assert not printed, "Enter on a focused Cancel started a print job"
        finally:
            dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    finally:
        PrintDialog._do_print = real
    return "a focused Cancel keeps Enter to itself"


def test_scale_pct_is_not_remembered_with_the_rest():
    """The percentage beside Feste Größe is part of this job, not of how
    this printer is usually used. It goes back to 100 % when the dialog
    closes, including a close that did not print."""
    from tools.printing import prefs
    prefs.forget()
    try:
        prefs.remember("office", {"scale": 1, "scale_pct": 70,
                                  "comments_forms": "form_fields_only",
                                  "handling": "poster", "tile_pct": 180,
                                  "nup_count": 9, "booklet_bind": "right",
                                  "by_page_size": True, "copies": 9,
                                  "reverse": True})
        saved = prefs.for_printer("office")
        assert "scale_pct" not in saved, saved
        assert saved.get("scale") == 1, saved
        assert saved.get("comments_forms") == "form_fields_only", saved
        assert saved.get("handling") == "poster", saved
        assert saved.get("tile_pct") == 180, saved
        assert saved.get("nup_count") == 9, saved
        assert saved.get("booklet_bind") == "right", saved
        assert saved.get("by_page_size") is True, saved
        assert "copies" not in saved and "reverse" not in saved, saved

        tab, dlg = _print_dialog(2, "pct_reset.pdf")
        try:
            dlg.scale_fixed.setChecked(True)
            dlg.scale_pct.setValue(70)
            assert dlg.scale_pct.value() == 70
            dlg.done(0)
            _app.processEvents()
        finally:
            tab.deleteLater(); _app.processEvents()

        tab2, dlg2 = _print_dialog(2, "pct_reset_next.pdf")
        try:
            assert dlg2.scale_pct.value() == 100, dlg2.scale_pct.value()
            # An old saved 70 must not be put back when the printer's
            # other settings are restored.
            dlg2._restore_saved("office")
            assert dlg2.scale_pct.value() == 100, dlg2.scale_pct.value()
        finally:
            dlg2.close(); tab2.deleteLater(); _app.processEvents()
    finally:
        prefs.forget()
    return "scale percent is not kept; the next dialog opens at 100 %"


def test_the_print_button_is_fully_inside_a_short_dialog():
    """The buttons used to paint past the bottom of the window once the
    dialog was shorter than its hint — a 52px bar under a button that,
    with its padding, was taller than the bar."""
    from PyQt6.QtWidgets import QPushButton
    tab, dlg = _print_dialog(2, "short_dlg.pdf")
    try:
        dlg.resize(1000, 540)
        _app.processEvents()
        btn = next(b for b in dlg.findChildren(QPushButton)
                   if b.text().strip() == "Drucken")
        bottom = btn.mapTo(dlg, btn.rect().bottomLeft()).y()
        assert bottom <= dlg.height() - 1, (bottom, dlg.height(), btn.height())
        # The last settings row has to belong to the scrollable pane, not
        # be sliced off the bottom of it.
        pane = dlg._settings_pane
        dlg._content_height()
        combo_bottom = dlg.comments_combo.mapTo(
            pane, dlg.comments_combo.rect().bottomLeft()).y()
        assert combo_bottom <= pane.minimumHeight(), (
            combo_bottom, pane.minimumHeight(), pane.sizeHint().height())
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()
    return "Drucken stays inside the window, and the last row can be scrolled to"


def test_print_to_pdf_is_a_printer_and_writes_the_chosen_pages():
    """Acrobat's Adobe PDF: a target in the printer list. Drucken saves a
    PDF of the pages that were picked, on the sheet that was picked, instead
    of sending them to a queue. Copies and duplex do not apply to a file."""
    from pypdf import PdfReader
    from tools.printing.dialog import PDF_PRINTER
    from tools.printing.spool import build_print_pdf
    from tools.viewer.model import PageModel

    tab, dlg = _print_dialog(4, "to_pdf_src.pdf")
    try:
        # The virtual printer is there even before a queue answers.
        assert dlg.printer_combo.findData(PDF_PRINTER) == 0
        # Let the real enumeration finish before installing the stand-in queue;
        # a late background answer would replace it during processEvents().
        assert _settle(dlg, lambda: dlg.printer_combo.findData("none") < 0,
                       tries=200), "printer enumeration never finished"
        dlg._apply_printer_list(["Officejet"], "Officejet")
        assert dlg.printer_combo.findData(PDF_PRINTER) == 0
        assert dlg.printer_combo.currentData() == "Officejet"
        dlg.printer_combo.setCurrentIndex(0)
        _app.processEvents()
        assert dlg._is_pdf_printer()
        assert not dlg.copies_spin.isEnabled()
        assert not dlg.duplex_check.isEnabled()
        assert not dlg.source_combo.isEnabled()
        # A real queue gets those controls back.
        dlg.printer_combo.setCurrentIndex(dlg.printer_combo.findData("Officejet"))
        _app.processEvents()
        assert dlg.copies_spin.isEnabled()
        assert dlg.duplex_check.isEnabled()
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()

    src = os.path.join(_TMP, "to_pdf_src.pdf")
    model = PageModel(4)
    out = os.path.join(_TMP, "to_pdf_out.pdf")
    skipped = build_print_pdf(
        src, model, [0, 2], out, scale_idx=1, scale_pct=100,
        paper_key="A4", orient_idx=1, color_mode="auto")
    assert skipped == []
    doc = PdfReader(out)
    assert len(doc.pages) == 2, len(doc.pages)
    assert "P1" in (doc.pages[0].extract_text() or "")
    assert "P3" in (doc.pages[1].extract_text() or "")
    box = doc.pages[0].mediabox
    # Placed on A4, not left at whatever the source happened to be.
    assert abs(float(box.width) - 595.28) < 2, float(box.width)
    assert abs(float(box.height) - 841.89) < 2, float(box.height)

    # A page smaller than A4, fitted, still comes out as an A4 sheet.
    small = os.path.join(_TMP, "to_pdf_small.pdf")
    c = canvas.Canvas(small, pagesize=(200, 200))
    c.setFont("Helvetica", 20); c.drawString(20, 100, "TINY"); c.showPage(); c.save()
    fitted = os.path.join(_TMP, "to_pdf_fitted.pdf")
    build_print_pdf(small, PageModel(1), [0], fitted,
                    scale_idx=0, scale_pct=100, paper_key="A4", orient_idx=1)
    fbox = PdfReader(fitted).pages[0].mediabox
    assert abs(float(fbox.width) - 595.28) < 2
    assert abs(float(fbox.height) - 841.89) < 2

    # No sheet chosen: the page keeps its own size.
    native = os.path.join(_TMP, "to_pdf_native.pdf")
    build_print_pdf(small, PageModel(1), [0], native,
                    scale_idx=0, paper_key="", orient_idx=0)
    nbox = PdfReader(native).pages[0].mediabox
    assert abs(float(nbox.width) - 200) < 2, float(nbox.width)
    return "Print to PDF is in the list and saves the picked pages on the sheet"
