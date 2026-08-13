"""
Printing.
"""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _open, _page_labels


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
