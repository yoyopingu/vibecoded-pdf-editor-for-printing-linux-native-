"""
Printing.
"""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin


def _print_dialog(n_pages=10, name="print_src.pdf"):
    from tools.viewer.tab import PdfTab
    from tools.printing.dialog import PrintDialog
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 90); c.drawCentredString(300, 400, f"P{i+1}"); c.showPage()
    c.save()
    tab = PdfTab(src)
    return tab, PrintDialog(tab.pdf_path, tab.model, tab)


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


def test_print_preview_and_job_agree_on_a_bad_range():
    """The preview used to clamp an out-of-range entry while the job rejected it,
    so "5-99" on a ten-page file previewed six printable pages and then refused
    to print. Showing a job that cannot run is its own kind of lie."""
    tab, dlg = _print_dialog()
    dlg.radio_range.setChecked(True)
    dlg.range_edit.setText("5-99")
    assert dlg._get_pages() is None, "the job accepted an out-of-range request"
    assert not dlg._preview_pages(), "the preview promised pages that will not print"
    dlg.range_edit.setText("4-6")
    assert dlg._get_pages() == [3, 4, 5]
    assert [p + 1 for p in dlg._preview_pages()] == [4, 5, 6]


def test_print_reports_the_sheets_it_actually_sent():
    """Unreadable pages are dropped from the job, so counting the requested
    pages told the operator more sheets were coming than the printer got — while
    listing the skipped ones in the same sentence."""
    tab, dlg = _print_dialog()
    dlg._progress = None
    dlg._after_print_close = lambda: None
    dlg._finish(list(range(10)), 2, [3, 7])
    text = dlg.status_lbl.text()
    assert "8" in text and "16" in text, \
        f"expected 8 pages x 2 copies = 16 sheets, got: {text}"
    assert "3, 7" in text or "[3, 7]" in text, f"skipped pages not named: {text}"


def test_print_never_destroys_colour_in_the_spooled_file():
    """Choosing greyscale must ask the *printer* for monochrome, not bake it
    into the job.

    Qt reports defaultColorMode() == GrayScale for driverless/IPP queues that
    are plainly colour (an EPSON ET-8500 and two Xerox presses on this machine),
    so the dialog opened in Graustufen — and greyscale was applied by converting
    the PDF with Ghostscript before spooling. The colour was gone for good: a job
    re-routed to a colour printer, or settings chosen on another machine, could
    not bring it back. Every mode now leaves the file's colour intact and
    expresses the choice as a CUPS option."""
    import subprocess
    from reportlab.lib import colors
    src = os.path.join(_TMP, "print_colour.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFillColor(colors.HexColor("#d02030")); c.rect(60, 500, 460, 220, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#1f77d0")); c.rect(60, 250, 460, 220, fill=1, stroke=0)
    c.showPage(); c.save()

    def saturation(path):
        d = pdfium.PdfDocument(path)
        pil = d[0].render(scale=0.4).to_pil().convert("RGB"); d.close()
        return _hist_stats(_colour_histogram(pil), 20)[0]

    src_sat = saturation(src)
    assert src_sat > 100, "fixture is not colourful enough to test with"

    tab, dlg = _print_dialog(1, "print_colour_tab.pdf")
    dlg = None
    from tools.viewer.tab import PdfTab
    from tools.printing.dialog import PrintDialog
    tab = PdfTab(src)
    dlg = PrintDialog(tab.pdf_path, tab.model, tab)

    assert dlg.color_combo.isEnabled(), \
        "the colour control is locked — the user cannot override a wrong guess"
    assert dlg.color_combo.currentData() == "auto", \
        "the dialog does not open on 'printer decides'"

    captured = {}
    real = subprocess.run
    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "lp":
            captured["opts"] = [x for i, x in enumerate(cmd) if cmd[i-1] == "-o"]
            keep = os.path.join(_TMP, f"spooled_{captured['tag']}.pdf")
            shutil.copyfile(cmd[-1], keep)
            captured["file"] = keep
            class R: returncode = 0; stdout = "request id is test-1"; stderr = ""
            return R()
        return real(cmd, *a, **k)

    from tools.printing.spool import print_via_gs

    expected = {"auto": [], "color": ["print-color-mode=color"],
                "mono":  ["print-color-mode=monochrome", "ColorModel=Gray"]}
    for mode, want in expected.items():
        captured.clear(); captured["tag"] = mode
        subprocess.run = spy
        try:
            print_via_gs(dlg.pdf_path, dlg.model,
                         [0], 1, mode, False, False, "long", 0,
                         "test-printer", 0, "A4", 0, 3.0, lambda m: None)
        finally:
            subprocess.run = real
        got = [o for o in captured["opts"]
               if "color" in o.lower() or o.startswith("ColorModel")]
        assert got == want, f"{mode}: sent {got}, expected {want}"
        assert saturation(captured["file"]) == src_sat, \
            f"{mode}: the spooled file lost its colour — it cannot be recovered"
    return "auto / color / mono, colour intact"


def _lp_options(**kw):
    """Run the spooler with lp stubbed out, and return the -o options it sent."""
    import subprocess
    from tools.printing.spool import print_via_gs
    from tools.viewer.model import PageModel

    captured = []
    real_run = subprocess.run

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def spy(cmd, *a, **k):
        captured.append(cmd)
        return _Ok() if cmd and cmd[0] == "lp" else real_run(cmd, *a, **k)

    src = FX["normal"]
    _open(src)
    subprocess.run = spy
    try:
        print_via_gs(src, PageModel(5), [0], 1, "auto", True,
                     kw.pop("duplex"), kw.pop("edge", "long"), 0,
                     "test-printer", 0, "A4", 0, 3.0, lambda m: None, **kw)
    finally:
        subprocess.run = real_run
    lp = [c for c in captured if c and c[0] == "lp"]
    assert lp, "the job never reached lp"
    return [lp[-1][i + 1] for i, x in enumerate(lp[-1]) if x == "-o"]


def test_unticking_two_sided_says_one_sided():
    """Saying nothing is not the same as saying no.

    With the box unticked the job carried no sides option at all, so the
    queue's own default decided — and a queue defaulted to duplex, which is the
    usual office setting, printed both sides anyway. Unticking the box did
    nothing at all.

    Both spellings, as when it is on: the IPP attribute for driverless queues
    and the PPD keyword for driver ones. GTK's print dialog and Qt's CUPS
    plugin both send one-sided explicitly."""
    off = _lp_options(duplex=False)
    assert "sides=one-sided" in off, off
    assert "Duplex=None" in off, off

    long_edge = _lp_options(duplex=True, edge="long")
    assert "sides=two-sided-long-edge" in long_edge, long_edge
    assert "Duplex=DuplexNoTumble" in long_edge, long_edge

    short_edge = _lp_options(duplex=True, edge="short")
    assert "sides=two-sided-short-edge" in short_edge, short_edge
    assert "Duplex=DuplexTumble" in short_edge, short_edge


def test_the_chosen_paper_tray_reaches_lp():
    """Under the keyword the queue itself uses — InputSlot on a driver queue,
    media-source on a driverless one — and nothing at all when the user leaves
    it on the printer's default."""
    assert "InputSlot=Tray2" in _lp_options(duplex=False,
                                            paper_source=("InputSlot", "Tray2"))
    assert "media-source=tray-2" in _lp_options(
        duplex=False, paper_source=("media-source", "tray-2"))
    default = _lp_options(duplex=False, paper_source=None)
    assert not [o for o in default if "InputSlot" in o or "media-source" in o], default


def test_paper_sources_are_read_from_the_queue():
    """Parsed from `lpoptions -p NAME -l`, which is what CUPS documents as the
    way to ask. Both kinds of queue answer, in their own vocabulary."""
    import subprocess
    import tools.printing.spool as spool

    def with_output(text):
        real_run = subprocess.run

        class _R:
            stdout = text
        subprocess.run = lambda *a, **k: _R()
        try:
            return spool.printer_options("q"), spool.paper_sources("q")
        finally:
            subprocess.run = real_run

    opts, source = with_output(
        "PageSize/Media Size: *A4 Letter\n"
        "InputSlot/Media Source: Auto *Tray1 Tray2 Manual\n"
        "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n")
    assert source == ("InputSlot", ["Auto", "Tray1", "Tray2", "Manual"], "Tray1"), source
    assert "Duplex" in opts and opts["PageSize"][2] == "A4"

    _opts, source = with_output(
        "media-source/Media Source: *auto tray-1 tray-2\n")
    assert source == ("media-source", ["auto", "tray-1", "tray-2"], "auto"), source

    # A queue with one tray is not worth asking about.
    _opts, source = with_output("InputSlot/Media Source: *Auto\n")
    assert source is None, source


def _stub_queue(text):
    """Run something with lpoptions answering `text`."""
    import subprocess
    from contextlib import contextmanager

    @contextmanager
    def ctx():
        real = subprocess.run

        class _R:
            stdout = text
        subprocess.run = lambda *a, **k: _R()
        try:
            yield
        finally:
            subprocess.run = real
    return ctx()


def test_queue_defaults_come_from_cups_not_qt():
    """The dialog opened on Letter and on two-sided for a queue set to A4 and
    one-sided in the desktop's printer settings.

    Both defaults were read from QPrinterInfo, and on a CUPS queue that is not
    reliable — the note on the duplex control already recorded
    defaultDuplexMode() reporting DuplexNone for printers that plainly duplex.
    The starred choice in `lpoptions -l` is what the queue is actually set to,
    and it is what the desktop writes when the user sets one there."""
    from tools.printing.spool import queue_defaults

    with _stub_queue("PageSize/Media Size: *A4 Letter Legal\n"
                     "Duplex/2-Sided Printing: *None DuplexNoTumble DuplexTumble\n"):
        assert queue_defaults("q") == {"paper": "A4", "duplex": False,
                                       "duplex_edge": "long"}

    with _stub_queue("PageSize/Media Size: A4 *Letter\n"
                     "Duplex/2-Sided Printing: None DuplexNoTumble *DuplexTumble\n"):
        assert queue_defaults("q") == {"paper": "Letter", "duplex": True,
                                       "duplex_edge": "short"}

    # A driverless queue says the same things in PWG/IPP vocabulary.
    with _stub_queue("media/Media Size: *iso_a4_210x297mm na_letter_8.5x11in\n"
                     "sides/2-Sided Printing: *one-sided two-sided-long-edge\n"):
        assert queue_defaults("q") == {"paper": "A4", "duplex": False,
                                       "duplex_edge": "long"}

    # A PPD variant is still that size, and a queue that says nothing is not
    # guessed at.
    with _stub_queue("PageSize/Media Size: *A4.Borderless A4\n"):
        assert queue_defaults("q") == {"paper": "A4"}
    with _stub_queue("ColorModel/Color Mode: *RGB Gray\n"):
        assert queue_defaults("q") == {}


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
            assert dlg.paper_combo.currentData() == "A4", "ignored the queue's paper"
            assert dlg.duplex_check.isChecked() is False, "ignored the queue's sides"

            dlg.paper_combo.setCurrentIndex(dlg.paper_combo.findData("A3"))
            dlg.duplex_check.setChecked(True)
            dlg.source_combo.setCurrentIndex(dlg.source_combo.findData("Tray2"))
            prefs.remember("office", dlg._current_settings())
            dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

            tab2, dlg2 = opened()
            assert dlg2.paper_combo.currentData() == "A3", "forgot the paper"
            assert dlg2.duplex_check.isChecked() is True, "forgot the sides"
            assert dlg2.source_combo.currentData() == "Tray2", "forgot the tray"
            assert prefs.last_printer() == "office"
            dlg2.deleteLater(); tab2.deleteLater(); _app.processEvents()
    finally:
        prefs.forget()


def test_a_remembered_tray_survives_being_stored_and_read_back():
    """The tray combo carried a (keyword, choice) tuple as its item data, and
    QComboBox.findData compares Python objects by identity — so a tuple rebuilt
    from parsed text never matched the one already in the combo, and the
    remembered tray silently did not come back. Literals hide it: they are
    interned, so a toy example works."""
    from PyQt6.QtWidgets import QComboBox

    parsed = "InputSlot Tray2".split()          # not interned, as from lpoptions
    tuples = QComboBox()
    tuples.addItem("Tray2", (parsed[0], parsed[1]))
    assert tuples.findData(("InputSlot", "Tray2")) == -1, \
        "tuple item data now matches by value — the workaround can be dropped"

    strings = QComboBox()
    strings.addItem("Tray2", parsed[1])
    assert strings.findData("Tray2") == 0, "string item data must match by value"


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


def _fake_qt_printer(page_size_id, duplex_mode):
    """Make QPrinterInfo report a printer, so the Qt branch of
    _on_printer_changed actually runs. Without one it is skipped entirely, and
    a test that does not stand one up cannot see Qt overwrite anything."""
    from contextlib import contextmanager
    import PyQt6.QtPrintSupport as QPS
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageSize

    class _Info:
        def isNull(self): return False
        def supportedPageSizes(self):
            return [QPageSize(QPageSize.PageSizeId.A4),
                    QPageSize(QPageSize.PageSizeId.Letter),
                    QPageSize(QPageSize.PageSizeId.A3)]
        def defaultPageSize(self): return QPageSize(page_size_id)
        def defaultDuplexMode(self): return duplex_mode
        def supportedColorModes(self): return [QPrinter.ColorMode.Color]

    @contextmanager
    def ctx():
        real = QPS.QPrinterInfo.printerInfo
        QPS.QPrinterInfo.printerInfo = staticmethod(lambda name: _Info())
        try:
            yield
        finally:
            QPS.QPrinterInfo.printerInfo = real
    return ctx()


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
                assert dlg.paper_combo.currentData() == "A4", (
                    f"open {attempt + 1}: Qt's Letter beat the queue's A4")
                assert dlg.duplex_check.isChecked() is False, (
                    f"open {attempt + 1}: Qt's duplex beat the queue's one-sided")
    finally:
        prefs.forget()
        for tab, dlg in opened:
            dlg.deleteLater(); tab.deleteLater()
        _app.processEvents()


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
