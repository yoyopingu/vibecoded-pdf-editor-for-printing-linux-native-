"""Mouse crop coordinates must describe the page that the operator sees."""
import os

import pikepdf
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from tests.support import FX, _TMP, _app, _open, _wait_for
from tools.panels.crop_resize import CropResizePanel
from tools.render.images import _render_image
from tools.shell.style import apply_theme_globally


def _ready(panel):
    panel._pane.refresh()
    assert _wait_for(lambda: panel._crop_geometry is not None), 'source preview never appeared'
    rect, pw, ph, *_ = panel._crop_geometry
    pm = panel._preview.pixmap()
    rect = rect.translated((panel._preview.width() - pm.width()) // 2,
                           (panel._preview.height() - pm.height()) // 2)
    return rect, pw, ph


def _point(rect, x, y):
    return QPoint(round(rect.left() + x * rect.width()),
                  round(rect.top() + y * rect.height()))


def _drag(panel, start, end):
    before = panel._preview.grab().toImage()
    QTest.mousePress(panel._preview, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(panel._preview, end)
    assert panel._crop_band.isVisible(), 'no crop rectangle while dragging'
    assert panel._preview.grab().toImage() != before, 'selection is invisible on the page'
    QTest.mouseRelease(panel._preview, Qt.MouseButton.LeftButton, pos=end)


def test_draw_crop_matches_rotated_offset_page_and_exported_content():
    path = os.path.join(_TMP, 'mouse-crop-source.pdf')
    c = canvas.Canvas(path, pagesize=(480, 360))
    # Asymmetric colours detect both wrong edges and accidental content scaling.
    c.setFillColorRGB(1, 0, 0); c.rect(0, 0, 480, 360, fill=1, stroke=0)
    c.setFillColorRGB(0, 1, 0); c.rect(60, 40, 180, 120, fill=1, stroke=0)
    c.setFillColorRGB(0, 0, 1); c.rect(240, 160, 180, 120, fill=1, stroke=0)
    c.showPage(); c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].CropBox = [60, 40, 420, 280]
        pdf.pages[0].Rotate = 90
        pdf.save(path)
    for extra_rotation in (0, 90, 180, 270):
        state = _open(path)
        state.page_model.rotate_selected(extra_rotation)
        panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
        try:
            # A drawn rectangle supersedes format fitting and equal margins.
            panel.scale_check.setChecked(True)
            panel.sync_cb.setChecked(True)
            panel.draw_crop.setChecked(True)
            rect, pw, ph = _ready(panel)
            original = _render_image(path, 0, 480, extra_rotation)
            start, end = _point(rect, .2, .15), _point(rect, .7, .65)
            _drag(panel, end, start)  # drawing backwards must work too
            assert not panel.draw_crop.isChecked()
            assert not panel.scale_check.isChecked() and not panel.sync_cb.isChecked()
            assert not panel._crop_band.isVisible()
            top, bottom, left, right = panel._margins_mm()
            assert abs(left * 2.8346456693 - pw * .2) < 1
            assert abs(top * 2.8346456693 - ph * .15) < 1
            out = os.path.join(_TMP, f'mouse-crop-{extra_rotation}.pdf')
            panel.save_pdf = lambda *a, **k: out
            panel.open_result = lambda *a, **k: None
            panel._run_action()
            page = PdfReader(out).pages[0]
            assert abs(float(page.mediabox.width) - pw * .5) < 2
            assert abs(float(page.mediabox.height) - ph * .5) < 2
            assert page.rotation == 0
            result = _render_image(out, 0, 240)
            # Interior colour samples must match the drawn source window.
            for x, y in ((.15, .2), (.8, .2), (.15, .8), (.8, .8)):
                expected = original.pixelColor(round((.2 + .5*x) * (original.width()-1)),
                                               round((.15 + .5*y) * (original.height()-1)))
                actual = result.pixelColor(round(x*(result.width()-1)), round(y*(result.height()-1)))
                assert actual == expected, (extra_rotation, x, y, actual.name(), expected.name())
            assert bottom > 0 and right > 0
        finally:
            panel.close(); panel.deleteLater(); _app.processEvents()


def test_draw_crop_clamps_to_page_and_escape_or_click_preserves_fields():
    for theme in ('light', 'dark'):
        apply_theme_globally(theme)
        _open(FX['normal'])
        panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
        try:
            panel.cl2.setValue(10)
            panel.draw_crop.setChecked(True)
            panel._pane._set_zoom(1.5 if theme == 'dark' else 1.0)
            rect, pw, ph = _ready(panel)
            values = panel._margins_mm()
            point = _point(rect, .3, .2)
            QTest.mouseClick(panel._preview, Qt.MouseButton.LeftButton, pos=point)
            assert panel._margins_mm() == values and panel.draw_crop.isChecked()
            QTest.mousePress(panel._preview, Qt.MouseButton.LeftButton, pos=point)
            QTest.mouseMove(panel._preview, _point(rect, .8, .7))
            QTest.keyClick(panel._preview, Qt.Key.Key_Escape)
            assert not panel.draw_crop.isChecked() and not panel._crop_band.isVisible()
            assert panel._margins_mm() == values
            panel.draw_crop.setChecked(True)
            rect, pw, ph = _ready(panel)
            _drag(panel, _point(rect, .25, .25), _point(rect, 1.2, 1.2))
            top, bottom, left, right = panel._margins_mm()
            assert bottom == right == 0
            assert abs(left * 2.8346456693 - pw * .25) < 1
            assert abs(top * 2.8346456693 - ph * .25) < 1
        finally:
            panel.close(); panel.deleteLater(); _app.processEvents()
    apply_theme_globally('dark')


def test_page_change_during_drag_cannot_crop_the_new_page():
    _open(FX['normal'])
    panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
    try:
        panel.draw_crop.setChecked(True)
        rect, *_ = _ready(panel)
        QTest.mousePress(panel._preview, Qt.MouseButton.LeftButton, pos=_point(rect, .2, .2))
        panel._page_bar.setValue(1)
        _ready(panel)
        QTest.mouseRelease(panel._preview, Qt.MouseButton.LeftButton, pos=_point(rect, .8, .8))
        assert panel._margins_mm() == (0, 0, 0, 0)
        rect, *_ = _ready(panel)
        QTest.mousePress(panel._preview, Qt.MouseButton.LeftButton, pos=_point(rect, .2, .2))
        panel._pane._set_zoom(1.3)
        _ready(panel)
        QTest.mouseRelease(panel._preview, Qt.MouseButton.LeftButton, pos=_point(rect, .8, .8))
        assert panel._margins_mm() == (0, 0, 0, 0), 'zoom changed the crop coordinate map'
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()


def test_draw_crop_works_before_page_content_has_rendered():
    from unittest.mock import patch
    _open(FX['normal'])
    panel = CropResizePanel(); panel.resize(1100, 800)
    try:
        with patch('tools.panels.crop_resize.cached_screen_page', return_value=(None, False)), \
             patch.object(panel, '_request_preview_image'):
            panel.show()
            panel.draw_crop.setChecked(True)
            rect, pw, ph = _ready(panel)
            assert 'rendert' in panel._preview_info.text()
            _drag(panel, _point(rect, .1, .2), _point(rect, .9, .8))
            top, bottom, left, right = panel._margins_mm()
            assert abs(left * 2.8346456693 - pw * .1) < 1
            assert abs(right * 2.8346456693 - pw * .1) < 1
            assert abs(top * 2.8346456693 - ph * .2) < 1
            assert abs(bottom * 2.8346456693 - ph * .2) < 1
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()


def test_crop_rectangles_undo_redo_as_whole_operations():
    _open(FX['normal'])
    panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
    panel.activateWindow()
    try:
        # The percentage is a derived readout; establish page geometry before
        # taking the initial settings snapshot, rather than capturing 100% cold.
        panel.draw_crop.setChecked(True)
        _ready(panel)
        panel.draw_crop.setChecked(False)
        panel.cl2.setValue(7)
        panel.sync_cb.setChecked(True)
        panel.scale_check.setChecked(True)
        before = panel._crop_settings()
        for start, end in (((.1, .2), (.9, .8)), ((.3, .15), (.7, .6))):
            panel.draw_crop.setChecked(True)
            rect, *_ = _ready(panel)
            _drag(panel, _point(rect, *start), _point(rect, *end))
            if panel._crop_undo.count() == 1:
                first = panel._crop_settings()
        second = panel._crop_settings()
        assert panel._crop_undo.count() == 2, 'one rectangle must be one command'
        _app.processEvents()
        QTest.keyClick(panel._preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_settings() == first, (panel._crop_settings(), first)
        _app.processEvents()
        QTest.keyClick(panel._preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_settings() == before, (panel._crop_settings(), before)
        QTest.keyClick(panel._preview, Qt.Key.Key_Z,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert panel._crop_settings() == first, (panel._crop_settings(), first)
        QTest.keyClick(panel._preview, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_settings() == second
        _app.processEvents()
        QTest.keyClick(panel._preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        panel.draw_crop.setChecked(True)
        rect, *_ = _ready(panel)
        _drag(panel, _point(rect, .05, .1), _point(rect, .8, .9))
        assert not panel._crop_undo.canRedo(), 'a new rectangle must replace the redo branch'
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()


def test_crop_undo_restores_format_fitting_and_clears_on_document_change():
    _open(FX['normal'])
    panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
    panel.activateWindow()
    try:
        panel.draw_crop.setChecked(True)
        _ready(panel)
        panel.draw_crop.setChecked(False)
        panel.fmt.set_format('A5  (148x210mm)')
        panel.scale_check.setChecked(True)
        assert _wait_for(lambda: panel._pending_target is None)
        before = panel._crop_settings()
        panel.draw_crop.setChecked(True)
        rect, *_ = _ready(panel)
        _drag(panel, _point(rect, .2, .2), _point(rect, .8, .8))
        _app.processEvents()
        QTest.keyClick(panel._preview, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_settings() == before, (panel._crop_settings(), before)
        out = os.path.join(_TMP, 'undo-crop-format.pdf')
        panel.save_pdf = lambda *a, **k: out
        panel.open_result = lambda *a, **k: None
        panel._run_action()
        page = PdfReader(out).pages[0]
        assert abs(float(page.mediabox.width) / 2.8346456693 - 148) < .1
        assert abs(float(page.mediabox.height) / 2.8346456693 - 210) < .1
        _open(FX['single'])
        assert panel._crop_undo.count() == 0, 'undo must not cross documents'
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()


def test_crop_undo_respects_text_editors_and_hidden_tools():
    from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLineEdit
    _open(FX['normal'])
    window = QWidget(); layout = QVBoxLayout(window)
    panel = CropResizePanel(); layout.addWidget(panel)
    other = QLineEdit(); layout.addWidget(other)
    window.resize(1100, 850); window.show(); window.activateWindow()
    try:
        panel.draw_crop.setChecked(True)
        rect, *_ = _ready(panel)
        _drag(panel, _point(rect, .2, .2), _point(rect, .8, .8))
        index = panel._crop_undo.index()
        edit = panel.cl2.lineEdit(); edit.setFocus(); edit.selectAll()
        QTest.keyClicks(edit, '123')
        assert edit.isUndoAvailable()
        QTest.keyClick(edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_undo.index() == index, 'text undo was stolen by crop undo'
        panel.hide(); other.setFocus()
        QTest.keyClick(other, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert panel._crop_undo.index() == index, 'hidden tool intercepted undo'
    finally:
        window.close(); window.deleteLater(); _app.processEvents()


def test_crop_drag_reports_live_output_size_without_pdf_work():
    from unittest.mock import patch
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent
    from tools.i18n import tr
    from tools.panels._shared import MM_TO_PT

    _open(FX['framed_rot90'])
    panel = CropResizePanel(); panel.resize(1100, 800); panel.show()
    try:
        panel.draw_crop.setChecked(True)
        panel._pane._set_zoom(1.3)
        rect, pw, ph = _ready(panel)
        margins = panel._margins_mm()
        start = _point(rect, .8, .75)
        QTest.mousePress(panel._preview, Qt.MouseButton.LeftButton, pos=start)
        with patch.object(panel._pane, 'refresh') as refresh, \
             patch('tools.panels.crop_resize.page_size_pt') as dimensions, \
             patch.object(panel, '_request_preview_image') as render:
            for x, y in ((.2, .25), (-.2, 1.2)):
                end = _point(rect, x, y)
                event = QMouseEvent(QEvent.Type.MouseMove, QPointF(end), QPointF(end),
                                    Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier)
                _app.sendEvent(panel._preview, event)
                clipped = panel._crop_point(QPointF(end), rect)
                width = abs(start.x() - clipped.x()) * pw / rect.width() / MM_TO_PT
                height = abs(start.y() - clipped.y()) * ph / rect.height() / MM_TO_PT
                assert panel._preview_info.text() == tr('Ausgabe: {p0:.1f} × {p1:.1f} mm').format(
                    p0=width, p1=height)
            refresh.assert_not_called()
            dimensions.assert_not_called()
            render.assert_not_called()
        assert panel._margins_mm() == margins and panel._crop_undo.count() == 0
        # A normal refresh (e.g. sharper cached pixels arriving) must preserve
        # the live readout; Escape must then recover the ordinary status.
        panel._pane.refresh()
        assert panel._preview_info.text() == panel._crop_live_info
        ordinary_status = panel._crop_status_before
        with patch.object(panel._pane, 'refresh'):
            QTest.keyClick(panel._preview, Qt.Key.Key_Escape)
        assert panel._preview_info.text() == ordinary_status
        assert panel._crop_live_info is None
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()
