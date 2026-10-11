"""Booklet previews show the exported faces without rasterising an imposed PDF."""
import os
from unittest.mock import patch
import pypdfium2 as pdfium
from PyQt6.QtGui import QColor, QImage
from tests.support import FX, _TMP, _app, _open, _sync_async, _wait_for
from tools.app_state import AppState
from tools.i18n import tr
from tools.panels.impose import ImposePanel, _booklet_page_sizes
from tools.render.caches import _FullPageCache, _ThumbnailCache
from tools.render.document_cache import PDFIUM_LOCK
from tools.render.images import _render_image


def test_booklet_preview_navigation_and_padding():
    state = _open(FX['normal'])  # Five source pages occupy four sheet faces.
    panel = ImposePanel()
    try:
        panel._page_image = lambda *args: None
        for side, expected in enumerate(([0], [1], [2], [3, 4])):
            panel._sheet_index = side
            shown = []
            with patch('tools.panels.impose.paint_page_number',
                       side_effect=lambda _p, _r, page: shown.append(page)):
                preview, _ = panel._render_preview(600, 500, 1.0)
            assert preview is not None
            assert shown == expected, (side, shown)
            assert panel._sheet_bar.maximum() == 3
            assert panel._pane.navigation.total.text() == tr('von {p0}').format(p0=4)
        panel._sheet_bar.setValue(0)
        panel._step_sheet(1)
        assert panel._sheet_index == 1
        assert panel._sheet_status.text() == tr('Blatt {p0} / {p1} · {p2}').format(
            p0=1, p1=2, p2=tr('Rückseite'))
        assert state.current_page == 0, 'sheet navigation turned the source page'
        panel._step_sheet(1)
        assert panel._sheet_index == 2
        panel._step_sheet(10)
        assert panel._sheet_index == 3
    finally:
        panel.close()
        panel.deleteLater()


def test_booklet_preview_matches_exported_front_and_back():
    path = FX['booklet32']
    _open(path)
    panel = ImposePanel()
    output = os.path.join(_TMP, 'booklet-preview-output.pdf')
    try:
        panel.save_pdf = lambda *a: output
        panel.open_result = lambda *a: None
        _sync_async(panel)
        panel._run_action()
        # Real source pixels in the shared cache, not a render of the imposed
        # file: this is also the path used when the viewer has drawn them first.
        for page in (31, 0, 1, 30, 17, 14, 15, 16):
            image = _render_image(path, page, 800)
            _ThumbnailCache.put((path, page, 0, 800), image)
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument(output)
            try:
                for side in (0, 1, 14, 15):
                    panel._sheet_index = side
                    preview, info = panel._render_preview(600, 500, 1.0)
                    assert 'rendert' not in info
                    rendered_page = doc[side]
                    try:
                        actual = rendered_page.render(scale=.5).to_pil().convert('RGB')
                    finally:
                        rendered_page.close()
                    shown = preview.toImage()
                    for fx in (.25, .75):
                        actual_rgb = actual.getpixel((int(actual.width * fx), actual.height // 2))
                        preview_rgb = shown.pixelColor(int(shown.width() * fx), shown.height() // 2)
                        assert max(abs(a - b) for a, b in zip(
                            actual_rgb, preview_rgb.getRgb()[:3])) <= 3, side
            finally:
                doc.close()
    finally:
        panel.close()
        panel.deleteLater()


def test_booklet_preview_reuses_both_screen_caches():
    path = FX['normal']
    _open(path)
    panel = ImposePanel()
    image = QImage(800, 1000, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    try:
        for cache in ('full', 'thumbnail'):
            _FullPageCache.invalidate(path)
            _ThumbnailCache.invalidate(path)
            if cache == 'full':
                _FullPageCache.put(path, 0, 0, 800, 1000, (image, 595, 842, 1.0, []))
            else:
                _ThumbnailCache.put((path, 0, 0, 800), image)
            with patch('tools.panels.impose._render_queue.submit',
                       side_effect=AssertionError('cached page was rendered again')):
                result = panel._page_image((path, 0, 0), (595, 842), 400)
            assert result is not None and result.pixelColor(0, 0) == QColor('red')
    finally:
        _FullPageCache.invalidate(path)
        _ThumbnailCache.invalidate(path)
        panel.close()
        panel.deleteLater()


def test_booklet_preview_follows_model_without_snapshot():
    state = _open(FX['normal'])
    model = state.page_model
    model.order.reverse()
    model.rotations[model.order[0]] = 90
    panel = ImposePanel()
    try:
        with patch('tools._base.ensure_view_snapshot',
                   side_effect=AssertionError('preview flattened the document')):
            sources = panel._preview_sources()
            assert sources[0] == (FX['normal'], 4, 90)
            sizes = _booklet_page_sizes(sources)
            assert sizes[0][0] > sizes[0][1]
            panel._page_image = lambda *a: None
            preview, _ = panel._render_preview(600, 500, 1.0)
            assert preview is not None
            assert panel._sizes == sizes
        panel.out_fmt.set_format(tr('Benutzerdefiniert (mm)'))
        panel.out_fmt.set_custom_size(100, 100)
        panel.norm_check.setChecked(False)
        preview, problem = panel._render_preview(600, 500, 1.0)
        assert preview is None and problem, 'preview hid the full-size fitting error'
        panel.norm_check.setChecked(True)
        preview, _ = panel._render_preview(600, 500, 1.0)
        assert preview is not None
    finally:
        panel.close()
        panel.deleteLater()


def test_booklet_preview_loads_geometry_asynchronously_and_cancels_hidden_renders():
    _open(FX['normal'])
    panel = ImposePanel()
    try:
        panel.resize(1000, 700)
        panel.show()
        assert _wait_for(lambda: bool(panel._sizes) and panel._preview.pixmap() is not None)
        assert panel._metadata_key is not None
        assert _wait_for(lambda: 'rendert' not in panel._preview_info.text())
        panel.hide()
        _app.processEvents()
        assert not panel._tasks
        AppState.get().current_pdf = FX['single']
        assert panel._sheet_index == 0 and not panel._sizes
    finally:
        panel.close()
        panel.deleteLater()
