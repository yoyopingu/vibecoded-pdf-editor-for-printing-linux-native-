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
                         [0], 1, mode, False, False, "long",
                         "test-printer", 0, "A4", 0, null_progress())
        finally:
            subprocess.run = real
        got = [o for o in captured["opts"]
               if "color" in o.lower() or o.startswith("ColorModel")]
        assert got == want, f"{mode}: sent {got}, expected {want}"
        assert saturation(captured["file"]) == src_sat, \
            f"{mode}: the spooled file lost its colour — it cannot be recovered"
    return "auto / color / mono, colour intact"

