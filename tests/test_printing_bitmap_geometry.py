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


def test_the_bitmap_print_fills_the_sheet_whatever_dpi_was_chosen():
    """Choosing a resolution must change the detail, not the size.

    The pages are rasterised for a device of the chosen dpi and then drawn a
    dot to a pixel. That agreed by accident while the number came from the
    printer itself; "Als Bitmap" is the first thing to choose a different one,
    and 300 dpi of pixels drawn on a 1200 dpi device came out at a quarter
    size in the corner of the sheet — smaller the lower the resolution asked
    for, which is the opposite of what picking one is for.
    """
    import PyQt6.QtPrintSupport as QtPS
    from PyQt6.QtPrintSupport import QPrinter
    from PIL import ImageOps
    from tools.printing.spool import prerender_for_qt

    tab, dlg = _print_dialog(n_pages=1, name="bmp_geom.pdf")
    real_qprinter = QtPS.QPrinter
    dlg._on_print_failed = lambda m: (_ for _ in ()).throw(
        AssertionError(f"the print path failed: {m}"))
    dlg._finish = lambda *a: None

    def coverage(dpi):
        out = os.path.join(_TMP, f"bmp_geom_{dpi}.pdf")

        def to_file(*a, **k):
            p = real_qprinter(*a, **k)
            p.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            p.setOutputFileName(out)
            return p
        for _enum in ("PrinterMode", "DuplexMode", "ColorMode", "OutputFormat"):
            setattr(to_file, _enum, getattr(real_qprinter, _enum))

        rendered, skipped = prerender_for_qt(
            tab.pdf_path, tab.model, [0], "auto", 0, 0, "A4", dpi, 0.0,
            null_progress())
        QtPS.QPrinter = to_file
        try:
            dlg._qt_send_to_printer(rendered, skipped, [0], 1, "auto", True,
                                    False, "long", "none", "A4", 0, dpi)
        finally:
            QtPS.QPrinter = real_qprinter
        doc = pdfium.PdfDocument(out)
        pil = doc[0].render(scale=1, fill_color=(255, 255, 255, 255)) \
                    .to_pil().convert("L")
        doc.close()
        box = ImageOps.invert(pil).getbbox()
        assert box, f"{dpi} dpi printed a blank sheet"
        return (box[2] - box[0]) / pil.width

    try:
        got = {dpi: coverage(dpi) for dpi in (150, 300, 600)}
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()

    widest, narrowest = max(got.values()), min(got.values())
    assert widest - narrowest < 0.03, \
        f"the chosen dpi changed how big the page printed: {got}"

    # And it has to come out the size the page is, not a stamp in the corner.
    # Measured against the source rather than a number picked here, so the
    # fixture can change its mind about how much ink it draws.
    doc = pdfium.PdfDocument(tab.pdf_path)
    pil = doc[0].render(scale=1, fill_color=(255, 255, 255, 255)) \
                .to_pil().convert("L")
    doc.close()
    box = ImageOps.invert(pil).getbbox()
    expected = (box[2] - box[0]) / pil.width
    assert abs(narrowest - expected) < 0.03, (
        f"printed at {100 * narrowest:.0f}% of the sheet, "
        f"but the page itself is {100 * expected:.0f}%: {got}")
    return ("same size at 150/300/600 dpi, and the size the page really is "
            f"({100 * expected:.0f}% of the sheet wide)")
