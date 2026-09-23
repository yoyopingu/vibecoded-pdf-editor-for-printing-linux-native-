"""
The slim scrollbar shared by the three document views.
"""
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtWidgets import QScrollArea, QStyle, QStyleOptionSlider
from PyQt6.QtTest import QTest

from tests.support import FX, _app, _open_single_view, _settle
from tools.viewer.scrollbar import SLIM_W, SlimScrollBar


def test_scroll_arrows_do_not_overlap_the_draggable_thumb():
    for orientation in (Qt.Orientation.Vertical, Qt.Orientation.Horizontal):
        bar = SlimScrollBar(orientation)
        bar.resize(SLIM_W, 300) if orientation == Qt.Orientation.Vertical else bar.resize(300, SLIM_W)
        bar.setRange(0, 100)
        bar.setPageStep(20)
        bar.show()
        try:
            _app.processEvents()
            for value in (0, 100):
                bar.setValue(value)
                opt = QStyleOptionSlider()
                bar.initStyleOption(opt)
                rects = [bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar,
                         opt, part, bar) for part in
                         (QStyle.SubControl.SC_ScrollBarSubLine,
                          QStyle.SubControl.SC_ScrollBarAddLine,
                          QStyle.SubControl.SC_ScrollBarSlider)]
                sub, add, thumb = rects
                assert not sub.intersects(thumb) and not add.intersects(thumb)
                target = add if value == 0 else sub
                QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=target.center())
                assert bar.value() == (1 if value == 0 else 99)
            bar.setValue(0)
            opt = QStyleOptionSlider()
            bar.initStyleOption(opt)
            thumb = bar.style().subControlRect(QStyle.ComplexControl.CC_ScrollBar,
                     opt, QStyle.SubControl.SC_ScrollBarSlider, bar)
            QTest.mousePress(bar, Qt.MouseButton.LeftButton, pos=thumb.center())
            destination = (QPoint(thumb.center().x(), thumb.center().y() + 90)
                           if orientation == Qt.Orientation.Vertical else
                           QPoint(thumb.center().x() + 90, thumb.center().y()))
            QTest.mouseMove(bar, destination)
            QTest.mouseRelease(bar, Qt.MouseButton.LeftButton, pos=destination)
            assert bar.value() > 0, "the thumb cannot be grabbed and dragged"
        finally:
            bar.close()
            bar.deleteLater()
            _app.processEvents()


def _bar(area):
    bar = area.verticalScrollBar()
    assert isinstance(bar, SlimScrollBar), type(bar).__name__
    assert bar.width() == SLIM_W, bar.width()
    assert area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    assert bar.isVisible(), "the scrollbar is not showing"
    assert isinstance(area.horizontalScrollBar(), SlimScrollBar)
    return bar


def test_the_three_views_use_one_scrollbar():
    """Single page, page manager and merge scroll with the same 17 px bar.

    The single-page view used to have no bar: a zoomed page could only be
    wheeled. Dragging this one moves the page."""
    from tools.viewer.merge import MergeOrderWidget
    from tools.viewer.panel import PageViewerPanel

    vp, sv = _open_single_view(FX["normal"], 900, 700)
    try:
        assert isinstance(sv._vbar, SlimScrollBar)
        assert sv._vbar.width() == SLIM_W
        # A fitted page still has the bar. Hiding it until the user zoomed
        # is why opening a document showed nothing on the right.
        n = len(sv.model.order)
        assert sv._vbar.isVisible(), "the scrollbar is not on the page"
        assert n >= 2 and sv._vbar.maximum() >= sv._view.height(), \
            f"the bar does not span the document (max {sv._vbar.maximum()})"
        sv._vbar.setValue(sv._vbar.maximum())
        assert _settle(vp, lambda: sv._current == n - 1), \
            f"dragging to the end stayed on page {sv._current + 1}"
        sv._vbar.setValue(0)
        assert _settle(vp, lambda: sv._current == 0), "dragging back did not return to page 1"
        sv._zoom = 4.0
        sv._render()
        assert _settle(vp, lambda: sv._v_slot > sv._view.height() + 20), \
            "zoomed page did not lengthen the bar"
        sv._vbar.setValue(40)
        _app.processEvents()
        assert sv._current == 0 and abs(sv._scroll_y - 40) < 2, \
            f"page {sv._current} scroll {sv._scroll_y}"
    finally:
        vp.deleteLater(); _app.processEvents()

    host = PageViewerPanel(); host.resize(1000, 700); host.show()
    host.open_file(FX["normal"])
    _settle(host, lambda: host.tabs.count(), tries=100)
    host._toggle_manage()
    _settle(host, lambda: host.tabs.currentWidget().in_manage_mode(), tries=80)
    grid_scroll = host.tabs.currentWidget()._manage_widget
    assert isinstance(grid_scroll, QScrollArea)
    _bar(grid_scroll)
    host.deleteLater(); _app.processEvents()

    mw = MergeOrderWidget([FX["normal"], FX["single"], FX["framed"]])
    mw.resize(1000, 700); mw.show()
    _app.processEvents()
    _bar(mw._scroll)
    mw.deleteLater(); _app.processEvents()
    return "17 px bar on the single page, the page grid and the merge grid"
