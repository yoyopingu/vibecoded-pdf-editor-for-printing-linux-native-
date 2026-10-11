"""Exercise the dialog's real imposed PDF output, including form filtering."""
import os
import pypdfium2 as pdfium
from pypdf import PdfReader
from tests.support import _TMP, _app, _wait_for
from tests.printing_support import _form_pdf
from tools.render.document_cache import PDFIUM_LOCK
from tools.printing.dialog import PrintDialog, PDF_PRINTER
from tools.printing.content import FORM_FIELDS_ONLY
from tools.viewer.tab import PdfTab


def test_imposed_pdf_keeps_fields_and_sheet_orientation():
    src = _form_pdf('imposed_fields.pdf')
    tab = PdfTab(src)
    dlg = PrintDialog(src, tab.model, tab)
    completed, failed = [], []
    dlg._print_finished.disconnect()
    dlg._print_failed.disconnect()
    dlg._print_finished.connect(lambda *args: completed.append(args))
    dlg._print_failed.connect(failed.append)
    try:
        assert _wait_for(lambda: dlg.printer_combo.findData('none') < 0)
        dlg.printer_combo.setCurrentIndex(dlg.printer_combo.findData(PDF_PRINTER))
        paper_i = dlg.paper_combo.findData('A4')
        if paper_i < 0:
            dlg.paper_combo.addItem('A4', 'A4')
            paper_i = dlg.paper_combo.findData('A4')
        dlg.paper_combo.setCurrentIndex(paper_i)
        dlg.comments_combo.setCurrentIndex(dlg.comments_combo.findData(FORM_FIELDS_ONLY))
        # An explicit portrait setting must not turn a booklet spread back
        # into portrait during the downstream Print to PDF pass.
        dlg._set_orient_idx(1)
        for mode, faces in ((1, 1), (2, 1), (3, 2)):
            dlg._set_printing(False)
            dlg._handling_bar.setCurrentIndex(mode)
            dlg.poster_pct.setValue(100)
            dlg.poster_overlap.setValue(0)
            dlg.poster_cutmarks.setChecked(False)
            dlg.poster_labels.setChecked(False)
            dlg.nup_border.setChecked(False)
            dlg.nup_rotate.setChecked(False)
            dlg.booklet_rotate.setChecked(False)
            out = os.path.join(_TMP, f'imposed_fields_{mode}.pdf')
            dlg._ask_print_pdf_path = lambda: out
            before = len(completed)
            dlg._do_print()
            assert _wait_for(lambda: len(completed) > before or failed, timeout=20)
            assert not failed, failed
            with PdfReader(out) as reader:
                assert len(reader.pages) == faces
                box = reader.pages[0].mediabox
                assert (box.width > box.height) == (mode == 3)
            with PDFIUM_LOCK:
                with pdfium.PdfDocument(out) as doc:
                    page = doc[0]
                    try:
                        bm = page.render(scale=1.5)
                        image = bm.to_pil().convert('L')
                        bm.close()
                    finally:
                        page.close()
            assert sum(v < 200 for v in image.get_flattened_data()) > 200, (
                'form-field content erased after imposition', mode)
            dlg._close_progress()
    finally:
        dlg.close()
        tab.deleteLater()
        _app.processEvents()
