"""Compare composed preview pixels with the vector sheets that will print."""
import os
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainter, QPixmap
from tests.support import _TMP, _app, _wait_for
from tools.jobs import cancel_owner
from tools.render.document_cache import PDFIUM_LOCK
from tools.printing.preview import _PrintPreview
from tools.printing.handling import build_nup_pdf, build_booklet_pdf, build_poster_pdf
from tools.printing.spool import write_subset_pdf
from tools.viewer.model import PageModel


def _source():
    path = os.path.join(_TMP, 'imposition_colours.pdf')
    c = canvas.Canvas(path)
    for i, colour in enumerate(((1, 0, 0), (0, 1, 0), (0, 0, 1),
                                (1, 0, 1), (0, 1, 1), (1, .5, 0),
                                (.4, .4, .4), (.5, 0, .5))):
        w, h = (180, 260) if i % 2 == 0 else (260, 180)
        c.setPageSize((w, h))
        c.setFillColorRGB(*colour)
        c.rect(0, 0, w, h, stroke=0, fill=1)
        c.setFillColorRGB(0, 0, 0)
        c.rect(0, h * .7, w * .3, h * .3, stroke=0, fill=1)
        c.showPage()
    c.save()
    return path


def _compare(pv, output):
    layouts = pv._sheets()
    with PDFIUM_LOCK:
        with pdfium.PdfDocument(output) as doc:
            assert len(doc) == pv._nav_count()
    for i, layout in enumerate(layouts):
        if i:
            pv._next_page()
        visible = {pv._pages[slot.page] for slot in layout.slots}
        assert _wait_for(lambda: visible <= pv._images.keys()), 'sources never rendered'
        w, h = layout.size
        pm = QPixmap(round(w), round(h))
        pm.fill()
        painter = QPainter(pm)
        try:
            pv._draw_handling(painter, QRectF(0, 0, w, h))
        finally:
            painter.end()
        preview = pm.toImage()
        with PDFIUM_LOCK:
            with pdfium.PdfDocument(output) as doc:
                page = doc[i]
                try:
                    bm = page.render(scale=1)
                    actual = bm.to_pil().convert('RGB')
                    bm.close()
                finally:
                    page.close()
        for fx in (.07, .19, .37, .53, .71, .89):
            for fy in (.09, .23, .41, .59, .77, .93):
                x, y = int(w * fx), int(h * fy)
                a = preview.pixelColor(x, y)
                got, want = (a.red(), a.green(), a.blue()), actual.getpixel((x, y))
                assert max(abs(a - b) for a, b in zip(got, want)) < 20, (
                    pv._handling, i, (x, y), got, want)


def test_preview_matches_printed_sheets_for_all_imposition_modes():
    src = _source()
    model = PageModel(8)
    model.move(0, 8)
    model.selected = {model.order[1]}
    model.rotate_selected(90)
    pv = _PrintPreview(src, model)
    pv.show()
    try:
        assert _wait_for(lambda: len(pv._sizes) == 8)
        paper = (200, 300)
        scenarios = [
            ('nup', list(range(8)), {'count': 6, 'order': 'v', 'nup_rotate': True}),
            ('nup', list(reversed(range(8))), {'count': 6, 'order': 'vr', 'nup_rotate': False}),
            ('booklet', list(range(5)), {'side': 'both', 'booklet_rotate': True}),
            ('booklet', list(range(8)), {'side': 'back', 'bind': 'right',
                                       'sheet_from': 2, 'sheet_to': 2, 'booklet_rotate': False}),
            ('poster', [0, 1], {'tile_pct': 137, 'overlap_mm': 0}),
            ('poster', [0, 1], {'tile_pct': 200, 'overlap_mm': 10}),
        ]
        for mode, pages, opts in scenarios:
            pv.set_pages(pages)
            pv.update_settings(0, 'Custom.200x300', 1, 0, handling=mode, handling_opts=opts)
            pv._current = 0
            pv._render_page()
            subset = os.path.join(_TMP, 'imposition_subset.pdf')
            write_subset_pdf(src, model, pages, subset)
            out = os.path.join(_TMP, 'imposition_' + mode + '.pdf')
            if mode == 'nup':
                build_nup_pdf(subset, None, paper, opts['count'], opts['order'],
                              False, opts['nup_rotate'], out)
            elif mode == 'booklet':
                build_booklet_pdf(subset, None, paper, opts.get('bind', 'left'),
                                  opts.get('side', 'both'), opts.get('sheet_from', 1),
                                  opts.get('sheet_to', 0), opts['booklet_rotate'], out)
            else:
                build_poster_pdf(subset, None, paper, opts['tile_pct'],
                                 opts['overlap_mm'], False, False, out)
            _compare(pv, out)
            assert len(pv._images) <= 16
    finally:
        cancel_owner(pv)
        pv.close()
        pv.deleteLater()
        _app.processEvents()
