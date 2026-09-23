"""Exercise the navigation rail through the same input events as the user."""
from tests.support import FX, _app, _open_single_view, _settle
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from tools.shell.style import apply_theme_globally
from tools.theme import _TV


def test_page_entry_commits_on_enter_and_survives_background_render():
    vp, sv = _open_single_view(FX["normal"])
    try:
        nav = sv._navigation
        assert not nav.previous.isEnabled() and nav.next.isEnabled()
        QTest.mouseClick(nav.page, Qt.MouseButton.LeftButton)
        _app.processEvents()
        assert nav.page.selectedText() == "1"
        QTest.keyClicks(nav.page, "4")
        sv._render()
        assert nav.page.text() == "4", "a render overwrote the page being typed"
        assert sv._current == 0, "typing must not navigate before Enter"
        QTest.keyClick(nav.page, Qt.Key.Key_Return)
        assert sv._current == 3 and nav.page.text() == "4"
        assert sv._view.hasFocus(), "page shortcuts must work after Enter"

        QTest.mouseClick(nav.next, Qt.MouseButton.LeftButton)
        assert sv._current == 4 and not nav.next.isEnabled()
        QTest.mouseClick(nav.previous, Qt.MouseButton.LeftButton)
        assert sv._current == 3 and nav.next.isEnabled()

        for text, expected in (("999", 4), ("0", 0), ("", 0)):
            nav.page.setFocus()
            nav.page.setText(text)
            QTest.keyClick(nav.page, Qt.Key.Key_Return)
            assert sv._current == expected
            assert nav.page.text() == str(expected + 1)

        nav.page.setFocus()
        nav.page.setText("3")
        QTest.keyClick(nav.page, Qt.Key.Key_Escape)
        assert sv._current == 0 and nav.page.text() == "1"
        nav.page.setFocus()
        nav.page.setText("4")
        sv._view.setFocus()
        assert sv._current == 0 and nav.page.text() == "1"

        sv._vbar.setValue(sv._vbar.maximum())
        assert _settle(vp, lambda: sv._current == 4)
        assert nav.page.text() == "5" and not nav.next.isEnabled()
        # The count must follow unsaved page-manager edits too.
        sv.model.order.pop()
        sv.refresh()
        assert nav.page.text() == "4" and nav.total.text().endswith("4")
        assert not nav.next.isEnabled()
    finally:
        vp.close()
        vp.deleteLater()
        _app.processEvents()


def test_rail_zoom_fit_actual_size_and_rulers():
    vp, sv = _open_single_view(FX["single"])
    try:
        nav = sv._navigation
        assert not nav.previous.isEnabled() and not nav.next.isEnabled()
        QTest.mouseClick(nav.zoom_in, Qt.MouseButton.LeftButton)
        assert sv._zoom > 1
        QTest.mouseClick(nav.zoom_out, Qt.MouseButton.LeftButton)
        assert abs(sv._zoom - 1) < 0.001
        QTest.mouseClick(nav.actual, Qt.MouseButton.LeftButton)
        assert _settle(vp, lambda: sv._render_task is None and nav.zoom.text() == "100%")
        QTest.keyClick(sv._view, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        assert sv._zoom == 1 and sv._scroll_x == 0 and sv._scroll_y == 0
        QTest.mouseClick(nav.rulers, Qt.MouseButton.LeftButton)
        assert sv._rulers_on and sv._ruler_top.isVisible() and nav.rulers.isChecked()
        sv.toggle_rulers()
        assert not nav.rulers.isChecked() and not sv._ruler_top.isVisible()
    finally:
        vp.close()
        vp.deleteLater()
        _app.processEvents()


def test_rail_fits_small_view_and_theme_switch_preserves_navigation():
    vp, sv = _open_single_view(FX["normal"], 640, 500)
    old_theme = "dark" if _TV["viewer_bg"] == "#111827" else "light"
    try:
        sv.go_to(3)
        nav = sv._navigation
        for theme in ("light", "dark"):
            apply_theme_globally(theme)
            _app.processEvents()
            assert nav.page.text() == "3" and sv._current == 2
            for widget in (nav.rulers, nav.page, nav.previous, nav.next,
                           nav.zoom_in, nav.zoom_out, nav.actual):
                assert widget.isVisible() and nav.rect().contains(widget.geometry())
            assert nav.page.height() >= 26, "global input styles squeezed the page field"
        nav.set_document(12345, 99999)
        assert nav.page.width() >= nav.page.fontMetrics().horizontalAdvance("99999") + 10
        assert nav.width() >= nav.total.sizeHint().width() + 10
    finally:
        apply_theme_globally(old_theme)
        vp.close()
        vp.deleteLater()
        _app.processEvents()


def test_horizontal_scrollbar_resize_finishes_at_the_current_zoom():
    vp, sv = _open_single_view(FX["normal"])
    try:
        sv._zoom = 3
        sv._render()
        assert _settle(vp, lambda: sv._render_task is None and not sv._showing_provisional)
        assert sv._hbar.isVisible()
        # A portrait sheet is fitted by height here. Introducing the scrollbar
        # takes height away, even though the outer viewer widget never resizes.
        assert sv._last_pm.height() == (sv._view.height() - 16) * 3
        sv._zoom_fit()
        assert _settle(vp, lambda: sv._render_task is None and not sv._showing_provisional)
        assert not sv._hbar.isVisible()
        assert sv._last_pm.height() == sv._view.height() - 16
        sv._zoom = 12
        sv._render()
        assert _settle(vp, lambda: sv._region_task is None and not sv._showing_provisional)
        assert sv._region_img is not None
        vp.resize(vp.width() - 40, vp.height())
        assert _settle(vp, lambda: sv._region_task is None and not sv._showing_provisional)
    finally:
        vp.close()
        vp.deleteLater()
        _app.processEvents()


def test_ctrl_1_keeps_pdf_points_at_physical_size_across_monitors_and_windows():
    vp, sv = _open_single_view(FX["mixed"], 1000, 700)
    try:
        # QScreen's physical DPI is already expressed in the logical pixels
        # used by widget geometry. Model two monitors with different density.
        dpi = [90.0]
        sv._physical_dpi = lambda: dpi[0]
        QTest.keyClick(sv._view, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)

        def settled_at_true_size():
            if sv._render_task is not None or sv._region_task is not None:
                return False
            if sv._showing_provisional or sv._last_pm is None:
                return False
            expected = sv._page_w_pt * dpi[0] / 72.0
            return abs(sv._last_pm.width() - expected) <= 1

        assert _settle(vp, settled_at_true_size)
        assert sv._physical_size_mode and sv._zoom_lbl.text() == "100%"
        first_zoom = sv._zoom

        vp.resize(760, 540)
        assert _settle(vp, settled_at_true_size)
        assert sv._zoom != first_zoom, "fit-relative zoom did not follow the viewport"

        dpi[0] = 180.0
        sv._screen_changed(None)
        assert _settle(vp, settled_at_true_size)
        assert sv._zoom_lbl.text() == "100%"

        sv.next_page()
        assert _settle(vp, lambda: sv._current == 1 and settled_at_true_size())
        assert abs(sv._page_w_pt - 500) < 1
        QTest.keyClick(sv._view, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        assert not sv._physical_size_mode and sv._zoom == 1
    finally:
        vp.close()
        vp.deleteLater()
        _app.processEvents()
