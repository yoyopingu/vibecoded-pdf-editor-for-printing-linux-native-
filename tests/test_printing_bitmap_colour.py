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


def test_bitmap_print_does_not_inherit_a_false_grayscale_default():
    """Qt has reported GrayScale for colour CUPS queues. A colourful RGB
    raster must stay colourful in auto mode unless CUPS itself says mono."""
    import PyQt6.QtPrintSupport as QtPS
    from PyQt6.QtPrintSupport import QPrinter
    from PyQt6.QtGui import QPageLayout
    from PIL import Image
    from tools.printing import dialog as dialog_mod

    tab, dlg = _print_dialog(1, "bitmap_colour.pdf")
    real_qprinter = QtPS.QPrinter
    queue = "bitmap-colour-test"
    old_info = dialog_mod._QUEUE_INFO_CACHE.get(queue)
    rendered = [(Image.new("RGB", (300, 300), (240, 40, 30)),
                 QPageLayout.Orientation.Portrait, 300, 300)]
    output = {}
    failures = []
    dlg._on_print_failed = failures.append
    dlg._finish = lambda *args: None

    def to_file(*args, **kwargs):
        p = real_qprinter(*args, **kwargs)
        p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        p.setOutputFileName(output["path"])
        p.setColorMode(QPrinter.ColorMode.GrayScale)  # Qt's wrong queue guess
        return p

    for enum in ("PrinterMode", "DuplexMode", "ColorMode", "OutputFormat"):
        setattr(to_file, enum, getattr(real_qprinter, enum))

    def is_colour(defaults, name):
        output["path"] = os.path.join(_TMP, name)
        dialog_mod._QUEUE_INFO_CACHE[queue] = {"defaults": defaults}
        dlg._qt_send_to_printer(rendered, [], [0], 1, "auto", True,
                                False, "long", queue, "A4", 1, 150)
        doc = pdfium.PdfDocument(output["path"])
        image = doc[0].render(scale=0.3).to_pil().convert("RGB")
        doc.close()
        return any(r > g + 50 and r > b + 50
                   for r, g, b in image.get_flattened_data())

    QtPS.QPrinter = to_file
    try:
        assert is_colour({}, "bitmap_auto_colour.pdf"), \
            "bitmap auto inherited Qt's false grayscale default"
        assert not is_colour({"color_mode": "mono"}, "bitmap_auto_mono.pdf"), \
            "an actual monochrome queue default was ignored"
    finally:
        QtPS.QPrinter = real_qprinter
        if old_info is None:
            dialog_mod._QUEUE_INFO_CACHE.pop(queue, None)
        else:
            dialog_mod._QUEUE_INFO_CACHE[queue] = old_info
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    assert not failures, failures

