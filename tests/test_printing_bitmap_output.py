"""Printing: bitmap behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_the_qt_print_path_actually_runs():
    """It had `QPrinter.PageSize.A4` in it, which is PyQt5 — PyQt6 moved those
    ids to QPageSize.PageSizeId. Every print through this function raised
    AttributeError before it drew anything.

    Nothing caught it because nothing reached it: the function was the fallback
    for a failed Ghostscript run, so on a working machine it never executed.
    "Als Bitmap drucken" routes here deliberately, which is how it surfaced.

    Runs the real function against a QPrinter aimed at a PDF file, so the whole
    body executes — the page size, the duplex and colour calls, and the painter
    loop — rather than only the one line that was wrong.
    """
    import PyQt6.QtPrintSupport as QtPS
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageLayout
    from PIL import Image

    tab, dlg = _print_dialog(n_pages=2, name="qtpath.pdf")
    out = os.path.join(_TMP, "qt_path_out.pdf")
    real_qprinter = QtPS.QPrinter

    def _to_file(*a, **k):
        """A QPrinter aimed at a file, so this runs with no hardware.

        A factory rather than a subclass: a Python class deriving from a Qt
        one leaves an object whose C++ half is freed before the Python half at
        interpreter shutdown, which is the teardown segfault tools/app.py
        already carries a comment about. Nothing here needs to override
        anything — only to hand back a printer pointed somewhere harmless.
        """
        p = real_qprinter(*a, **k)
        p.setOutputFormat(real_qprinter.OutputFormat.PdfFormat)
        p.setOutputFileName(out)
        return p

    # The code under test reads its enums off the same name it constructs.
    for _enum in ("PrinterMode", "DuplexMode", "ColorMode", "OutputFormat"):
        setattr(_to_file, _enum, getattr(real_qprinter, _enum))

    failures = []
    dlg._on_print_failed = lambda msg: failures.append(msg)
    finished = []
    dlg._finish = lambda *a: finished.append(a)

    rendered = [(Image.new("RGB", (600, 850), "white"),
                 QPageLayout.Orientation.Portrait, 600, 850) for _ in range(2)]
    QtPS.QPrinter = _to_file
    try:
        dlg._qt_send_to_printer(rendered, [], [0, 1], 1, "auto", True,
                                False, "long", "none", "A4", 0)
    finally:
        QtPS.QPrinter = real_qprinter
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

    assert not failures, f"the Qt print path failed: {failures}"
    assert finished, "the job never reported itself finished"
    assert os.path.exists(out) and os.path.getsize(out) > 0, \
        "the Qt path produced no output at all"
    assert len(PdfReader(out).pages) == 2, \
        f"expected 2 pages, got {len(PdfReader(out).pages)}"
    return "the Qt/bitmap path runs end to end and spools both pages"


