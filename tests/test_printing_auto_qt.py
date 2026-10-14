"""Exercise automatic physical orientation through Qt's real PDF backend."""
import os
from unittest.mock import patch

from pypdf import PdfReader
from PyQt6.QtGui import QPageLayout, QPageSize
from PyQt6.QtCore import QSizeF
from PyQt6.QtPrintSupport import QPrinter
import PyQt6.QtPrintSupport as QtPS

from tests.support import _TMP, _app
from tests.test_printing_orientation import _mixed_pages
from tools.viewer.tab import PdfTab
from tools.printing.dialog import PrintDialog
from tools.printing.spool import prerender_for_qt, FIT, FIXED, SHRINK


def test_qt_physical_auto_rotation_is_encoded_and_first_page_uses_right_axes():
    source = _mixed_pages()
    tab = PdfTab(source)
    dialog = PrintDialog(source, tab.model, tab)
    errors = []
    dialog._on_print_failed = errors.append
    dialog._finish = lambda *a: None
    real_printer = QPrinter
    try:
        for paper in ('Custom.420x297mm', ''):
            for scaling in (FIT, FIXED, SHRINK):
                rendered, skipped = prerender_for_qt(source, tab.model, [1, 0],
                    'auto', scaling, 0, paper, 72, 0, lambda *_: None, physical=True)
                if paper:
                    assert all(orient == QPageLayout.Orientation.Portrait
                               for _image, orient, _w, _h in rendered)
                    assert all(image.height >= image.width for image, *_ in rendered)
                    assert all((w, h) == (841, 1190) for _image, _orient, w, h in rendered)
                else:
                    assert [orient for _image, orient, _w, _h in rendered] == [
                        QPageLayout.Orientation.Landscape, QPageLayout.Orientation.Portrait]
                out = os.path.join(_TMP, f'physical-qt-{bool(paper)}-{scaling}.pdf')
                def to_file(*args, **kwargs):
                    printer = real_printer(*args, **kwargs)
                    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
                    printer.setOutputFileName(out)
                    # This is the device's existing stock, not the app's guess.
                    # With paper unspecified the app must leave it intact.
                    printer.setPageSize(QPageSize(QSizeF(600, 900), QPageSize.Unit.Point))
                    printer.setFullPage(True)
                    return printer
                for enum in ('PrinterMode', 'DuplexMode', 'ColorMode'):
                    setattr(to_file, enum, getattr(real_printer, enum))
                with patch.object(QtPS, 'QPrinter', to_file):
                    dialog._qt_send_to_printer(rendered, skipped, [1, 0], 1, 'auto',
                                              True, False, 'long', 'none', paper, 0, 72)
                assert not errors, errors
                sizes = [(float(p.mediabox.width), float(p.mediabox.height))
                         for p in PdfReader(out).pages]
                expected = [(841.89, 1190.55)] * 2 if paper else [(900, 600), (600, 900)]
                assert len(sizes) == 2
                for actual, wanted in zip(sizes, expected):
                    assert all(abs(a-b) < 1 for a, b in zip(actual, wanted)), (sizes, expected)
    finally:
        dialog.close(); tab.deleteLater(); _app.processEvents()
