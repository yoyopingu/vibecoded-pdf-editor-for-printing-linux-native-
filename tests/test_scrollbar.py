"""
The slim scrollbar shared by the three document views.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QScrollArea

from tests.support import FX, _app, _open_single_view, _settle
from tools.viewer.scrollbar import SLIM_W, SlimScrollBar


def _bar(area):
    bar = area.verticalScrollBar()
    assert isinstance(bar, SlimScrollBar), type(bar).__name__
    assert bar.width() == SLIM_W, bar.width()
    assert area.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
    assert bar.isVisible(), "the scrollbar is not showing"
    assert isinstance(area.horizontalScrollBar(), SlimScrollBar)
    return bar


def test_the_three_views_use_one_scrollbar():
    """Single page, page manager and merge scroll with the same 15 px bar.

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
    return "15 px bar on the single page, the page grid and the merge grid"
