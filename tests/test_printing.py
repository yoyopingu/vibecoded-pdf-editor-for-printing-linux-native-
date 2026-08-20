"""
Printing.
"""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
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
                         "test-printer", 0, "A4", 0, null_progress())
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

    src = kw.pop("src", FX["normal"])
    scale_idx = kw.pop("scale_idx", 0)
    orient_idx = kw.pop("orient_idx", 0)
    from pypdf import PdfReader
    n_pages = len(PdfReader(src).pages)
    _open(src)
    subprocess.run = spy
    try:
        print_via_gs(src, PageModel(n_pages), [0], 1, "auto", True,
                     kw.pop("duplex"), kw.pop("edge", "long"), 0,
                     "test-printer", scale_idx, "A4", orient_idx,
                     null_progress(), **kw)
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


def _scaling_sent(**kw):
    """Just the options that decide whether the printer may resize the page."""
    return [o for o in _lp_options(duplex=False, **kw)
            if "scaling" in o or "fit" in o]


def test_fit_to_page_actually_asks_for_a_fit():
    """The bug this test exists for: "An Seite anpassen" did nothing.

    The job carried `fit-to-page` and, from an unrelated branch further down,
    `print-scaling=none` as well. CUPS settles that pair by not scaling —
    measured against pdftopdf, an A6 page on A4 media came back at 1.000
    instead of the 1.835 the fit called for — so the page printed at its own
    size and the option looked broken because it was.

    Nothing that cancels a fit may be sent alongside one.
    """
    sent = _scaling_sent(scale_idx=0)
    assert "print-scaling=none" not in sent, \
        f"the fit is cancelled by a no-scaling option: {sent}"
    assert any(o in sent for o in ("print-scaling=fit", "fit-to-page")), sent
    return f"fit asks for a fit: {sent}"


def test_shrink_never_asks_for_something_that_enlarges():
    """"Auf bedruckbaren Bereich verkleinern" promises it never enlarges.

    `fit-to-page` scales up as readily as down (measured: an A6 page came back
    at 1.835), so it is the wrong request for this mode however convenient it
    looks. `print-scaling=auto-fit` is the one that shrinks an oversized page
    and leaves a small one alone (measured: A3 -> 0.647, A6 -> 1.000).
    """
    sent = _scaling_sent(scale_idx=2)
    assert "fit-to-page" not in sent, f"shrink asked for an enlarging fit: {sent}"
    assert "print-scaling=auto-fit" in sent, sent
    return f"shrink asks only for a shrink: {sent}"


def test_a_fixed_size_forbids_the_printer_from_rescaling():
    """Feste Groesse means the size in the box, at every percentage.

    At anything but 100 % the content is scaled here and centred on the sheet,
    and the job used to carry no scaling option at all after that — leaving
    CUPS's own default (print-scaling=auto) to decide whether to size it
    again. Saying none is what makes the percentage mean what it says.
    """
    for pct in (100, 70, 150):
        sent = _scaling_sent(scale_idx=1, scale_pct=pct)
        assert sent == ["print-scaling=none"], f"{pct}%: {sent}"
    return "100 %, 70 % and 150 % all say print-scaling=none"


def test_every_scaling_mode_sends_exactly_one_instruction():
    """Structural, because the bug was two instructions that disagreed.

    Whatever the mode, and whether or not Ghostscript is installed, the job
    must carry one coherent answer to "may the printer resize this" — and the
    same answer either way, since Ghostscript's absence changes who does the
    work, not what the user asked for.
    """
    from tools.printing import spool
    real = spool.ghostscript_binary
    seen = {}
    try:
        for gs, tag in ((real, "gs"), (lambda: None, "no gs")):
            spool.ghostscript_binary = gs
            for idx in (0, 1, 2):
                sent = _scaling_sent(scale_idx=idx)
                assert sent, f"{tag}, mode {idx}: no scaling instruction at all"
                assert not ("print-scaling=none" in sent and len(sent) > 1), \
                    f"{tag}, mode {idx}: contradictory pair {sent}"
                assert len(sent) == len(set(sent)), f"{tag}, mode {idx}: {sent}"
                seen.setdefault(idx, sent)
                assert seen[idx] == sent, \
                    f"mode {idx} differs with and without Ghostscript: {seen[idx]} vs {sent}"
    finally:
        spool.ghostscript_binary = real
    return "one instruction per mode, the same with or without Ghostscript"


def test_a_landscape_page_tells_the_printer_it_is_landscape():
    """On "Automatisch", the media was turned to landscape for the job and CUPS
    was told nothing about it.

    Ghostscript duly produced a landscape sheet while lp still asked for plain
    `media=A4`, so the page landed unrotated on portrait media and lost its
    right-hand edge (measured against pdftopdf: MediaBox came back 595x842 with
    an identity matrix). Whatever orientation the media ended up in has to be
    named.
    """
    import os
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    land = os.path.join(_TMP, "print_landscape.pdf")
    if not os.path.exists(land):
        c = canvas.Canvas(land, pagesize=(A4[1], A4[0]))
        c.drawString(40, 100, "LANDSCAPE"); c.showPage(); c.save()

    def orient_for(src, orient_idx):
        opts = _lp_options(duplex=False, src=src, orient_idx=orient_idx)
        return [o for o in opts if o.startswith("orientation-requested")]

    assert orient_for(land, 0) == ["orientation-requested=4"], \
        "an auto-detected landscape page was not reported as landscape"
    assert orient_for(land, 2) == ["orientation-requested=4"]
    assert orient_for(land, 1) == ["orientation-requested=3"]
    assert orient_for(FX["normal"], 0) == ["orientation-requested=3"], \
        "a portrait document was reported as landscape"
    return "auto, portrait and landscape all name the orientation they used"


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


def test_a_printer_appearing_later_shows_up_without_a_restart():
    """The enumerated list was cached for the session and never asked again, so
    a printer connected while the app was running stayed invisible until a
    restart. Every open re-enumerates now; an unchanged answer must not disturb
    the combo, and a changed one must keep the current selection if it survives."""
    from tools.printing import dialog as D

    class _Combo:
        def __init__(self): self.items = []; self.idx = -1; self.enabled = True
        def clear(self): self.items = []; self.idx = -1
        def addItem(self, text, data=None): self.items.append((text, data))
        def count(self): return len(self.items)
        def setEnabled(self, v): self.enabled = v
        def blockSignals(self, v): pass
        def findData(self, d):
            return next((i for i, (_, x) in enumerate(self.items) if x == d), -1)
        def setCurrentIndex(self, i): self.idx = i
        def currentData(self):
            return self.items[self.idx][1] if 0 <= self.idx < len(self.items) else None
        class _Sig:
            def connect(self, *a): pass
            def disconnect(self, *a): pass
        currentIndexChanged = _Sig()

    class _Dlg:
        _apply_printer_list = D.PrintDialog._apply_printer_list
        _on_printers_enumerated = D.PrintDialog._on_printers_enumerated
        _settings_touched = False
        def __init__(self): self.printer_combo = _Combo(); self.changed = 0
        def _on_printer_changed(self): self.changed += 1

    old_cache = D._PRINTER_LIST_CACHE
    old_last = D.prefs.last_printer
    try:
        D.prefs.last_printer = lambda: ""
        D._PRINTER_LIST_CACHE = None

        dlg = _Dlg()
        dlg._apply_printer_list(["Officejet"], "Officejet")
        assert dlg.printer_combo.currentData() == "Officejet"
        assert dlg.changed == 1, "first population applies the printer's defaults"

        # Same answer arriving again: nothing is rebuilt, nothing re-applied.
        dlg._on_printers_enumerated(["Officejet"], "Officejet")
        assert dlg.changed == 1, "an unchanged list must not re-apply defaults"

        # A printer appears. It must show up, and the selection must survive.
        dlg._on_printers_enumerated(["Officejet", "Laser"], "Officejet")
        assert [d for _, d in dlg.printer_combo.items] == ["Officejet", "Laser"]
        assert dlg.printer_combo.currentData() == "Officejet", "selection was lost"
        assert dlg.changed == 1, "same printer still selected — defaults not re-applied"

        # The selected printer goes away: fall back rather than point at nothing.
        dlg._on_printers_enumerated(["Laser"], "Laser")
        assert dlg.printer_combo.currentData() == "Laser"
        assert dlg.changed == 2, "the printer really changed, so defaults apply"
    finally:
        D.prefs.last_printer = old_last
        D._PRINTER_LIST_CACHE = old_cache


def test_a_refresh_never_undoes_a_deliberate_choice():
    """A background re-enumeration can land after the operator has set the paper.
    If the selected printer has gone, the combo has to change — but the settings
    they chose must survive it, the same rule _apply_queue_info already follows."""
    from tools.printing import dialog as D

    class _Combo:
        def __init__(self): self.items = []; self.idx = -1
        def clear(self): self.items = []; self.idx = -1
        def addItem(self, text, data=None): self.items.append((text, data))
        def count(self): return len(self.items)
        def setEnabled(self, v): pass
        def blockSignals(self, v): pass
        def findData(self, d):
            return next((i for i, (_, x) in enumerate(self.items) if x == d), -1)
        def setCurrentIndex(self, i): self.idx = i
        def currentData(self):
            return self.items[self.idx][1] if 0 <= self.idx < len(self.items) else None
        class _Sig:
            def connect(self, *a): pass
            def disconnect(self, *a): pass
        currentIndexChanged = _Sig()

    class _Dlg:
        _apply_printer_list = D.PrintDialog._apply_printer_list
        _on_printers_enumerated = D.PrintDialog._on_printers_enumerated
        def __init__(self):
            self.printer_combo = _Combo(); self.changed = 0
            self._settings_touched = False
        def _on_printer_changed(self): self.changed += 1

    old_cache, old_last = D._PRINTER_LIST_CACHE, D.prefs.last_printer
    try:
        D.prefs.last_printer = lambda: ""
        D._PRINTER_LIST_CACHE = None
        dlg = _Dlg()
        dlg._apply_printer_list(["office"], "office")
        assert dlg.changed == 1

        dlg._settings_touched = True          # the operator picks A3, say
        dlg._on_printers_enumerated(["other"], "other")
        assert [d for _, d in dlg.printer_combo.items] == ["other"], \
            "the new list still has to be shown"
        assert dlg.changed == 1, "defaults were re-applied over a deliberate choice"
    finally:
        D.prefs.last_printer = old_last
        D._PRINTER_LIST_CACHE = old_cache


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

    # Measured on the framed fixture — a drawn rectangle — and not on a page
    # of text. The text fixture uses base-14 Helvetica, which is not embedded,
    # so pdfium substitutes whatever the system offers: stable within one
    # process and *not* stable between them. That made this test fail about
    # one run in four, with the 100 % span coming back as 62 px instead of 79
    # because a different substitute font is a different width. A rectangle
    # has the width it has.
    full = os.path.join(_TMP, "pct_100.pdf")
    half = os.path.join(_TMP, "pct_50.pdf")
    recenter_on_paper(FX["framed"], full, 595.276, 841.89, factor=1.0)
    recenter_on_paper(FX["framed"], half, 595.276, 841.89, factor=0.5)
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
