"""Shared printing fixtures for the process-isolated behavior modules."""
import os
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from tools.jobs import null_progress
from tests.support import FX, _TMP, _open


def _print_tab(n_pages=10, name="print_src.pdf"):
    from tools.viewer.tab import PdfTab
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 90); c.drawCentredString(300, 400, f"P{i+1}"); c.showPage()
    c.save()
    return PdfTab(src)


def _print_dialog(n_pages=10, name="print_src.pdf"):
    from tools.printing.dialog import PrintDialog
    tab = _print_tab(n_pages, name)
    return tab, PrintDialog(tab.pdf_path, tab.model, tab)


def _form_pdf(name, value="Firma Muster GmbH", regenerate=True):
    """A one-page AcroForm with a single text field, optionally filled."""
    blank = os.path.join(_TMP, "blank_" + name)
    c = canvas.Canvas(blank, pagesize=A4)
    c.setFont("Helvetica", 12)
    c.drawString(60, 780, "Auftrag")
    c.acroForm.textfield(name="kunde", x=60, y=700, width=300, height=24,
                         borderWidth=1, fontSize=12)
    c.showPage(); c.save()
    if value is None:
        return blank
    from pypdf import PdfWriter
    out = os.path.join(_TMP, name)
    w = PdfWriter(clone_from=blank)
    w.update_page_form_field_values(
        w.pages[0], {"kunde": value}, auto_regenerate=regenerate)
    with open(out, "wb") as f:
        w.write(f)
    return out


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
    paper_key  = kw.pop("paper_key", "A4")
    n_pages = len(PdfReader(src).pages)
    _open(src)
    subprocess.run = spy
    try:
        print_via_gs(src, PageModel(n_pages), [0], 1, "auto", True,
                     kw.pop("duplex", False), kw.pop("edge", "long"),
                     "test-printer", scale_idx, paper_key, orient_idx,
                     null_progress(), **kw)
    finally:
        subprocess.run = real_run
    lp = [c for c in captured if c and c[0] == "lp"]
    assert lp, "the job never reached lp"
    return [lp[-1][i + 1] for i, x in enumerate(lp[-1]) if x == "-o"]


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

