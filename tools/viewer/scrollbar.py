"""
The slim scrollbar on the right of a document view.

Acrobat's bar is a narrow column: a thumb you can grab, and a small step
button at each end. The single-page view had no bar at all — a zoomed page
could only be wheeled — and the page manager and the merge view each grew
whatever scrollbar the style sheet happened to leave them. One bar, 15 px,
on the right of all three.

The arrow buttons are drawn here because a stylesheet can reserve their
square but cannot draw the chevron without an image file.
"""
from PyQt6.QtWidgets import QScrollArea, QScrollBar, QStyle, QStyleOptionSlider
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPainter, QPen, QColor, QPolygonF
from tools.theme import _TV, _register_themed


# Wide enough to grab, narrow enough that it is a scrollbar and not a panel.
SLIM_W = 15
_ARROW = 14


def _neutral(dark):
    """Track, thumb, thumb-under-the-pointer, chevron. Neutral on purpose:
    the viewer's own blue reads as another sidebar, which is what this
    replaced."""
    if dark:
        return "#1b2433", "#5c6b82", "#7d8da3", "#d5dde8", "#243044"
    return "#e6ebf2", "#aeb8c6", "#8b97a8", "#3d4a5c", "#d5dce6"


def _is_dark(bg):
    c = bg.lstrip("#")
    if len(c) < 6:
        return False
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return r + g + b < 384


def slim_qss(t) -> str:
    track, thumb, hot, _mark, arrow_hot = _neutral(_is_dark(t["viewer_bg"]))
    return (
        f"QScrollBar#slimScroll:vertical{{background:{track};width:{SLIM_W}px;"
        f"margin:{_ARROW}px 0 {_ARROW}px 0;border:none;}}"
        f"QScrollBar#slimScroll::handle:vertical{{background:{thumb};"
        f"min-height:28px;border-radius:3px;margin:2px;}}"
        f"QScrollBar#slimScroll::handle:vertical:hover{{background:{hot};}}"
        f"QScrollBar#slimScroll::sub-line:vertical{{height:{_ARROW}px;"
        f"subcontrol-position:top;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::add-line:vertical{{height:{_ARROW}px;"
        f"subcontrol-position:bottom;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::sub-line:vertical:hover,"
        f"QScrollBar#slimScroll::add-line:vertical:hover{{background:{arrow_hot};}}"
        f"QScrollBar#slimScroll::add-page:vertical,"
        f"QScrollBar#slimScroll::sub-page:vertical{{background:none;}}"
        f"QScrollBar#slimScroll:horizontal{{background:{track};height:{SLIM_W}px;"
        f"margin:0 {_ARROW}px 0 {_ARROW}px;border:none;}}"
        f"QScrollBar#slimScroll::handle:horizontal{{background:{thumb};"
        f"min-width:28px;border-radius:3px;margin:2px;}}"
        f"QScrollBar#slimScroll::handle:horizontal:hover{{background:{hot};}}"
        f"QScrollBar#slimScroll::sub-line:horizontal{{width:{_ARROW}px;"
        f"subcontrol-position:left;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::add-line:horizontal{{width:{_ARROW}px;"
        f"subcontrol-position:right;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::sub-line:horizontal:hover,"
        f"QScrollBar#slimScroll::add-line:horizontal:hover{{background:{arrow_hot};}}"
        f"QScrollBar#slimScroll::add-page:horizontal,"
        f"QScrollBar#slimScroll::sub-page:horizontal{{background:none;}}"
    )


class SlimScrollBar(QScrollBar):
    """Acrobat's bar. Drop it in anywhere a document scrolls."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setObjectName("slimScroll")
        if orientation == Qt.Orientation.Vertical:
            self.setFixedWidth(SLIM_W)
        else:
            self.setFixedHeight(SLIM_W)
        _register_themed(self)
        self._apply_theme()

    def _apply_theme(self):
        self.setStyleSheet(slim_qss(_TV))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # The stylesheet reserves the two squares. It cannot draw the
        # chevron, so the mark is painted on top of whatever it left there.
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _track, _thumb, _hot, mark, _ah = _neutral(_is_dark(_TV["viewer_bg"]))
        colour = QColor(mark)
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        cc = QStyle.ComplexControl.CC_ScrollBar
        vertical = self.orientation() == Qt.Orientation.Vertical
        sub = style.subControlRect(
            cc, opt, QStyle.SubControl.SC_ScrollBarSubLine, self)
        add = style.subControlRect(
            cc, opt, QStyle.SubControl.SC_ScrollBarAddLine, self)
        self._chevron(p, sub, colour, point_negative=True, vertical=vertical)
        self._chevron(p, add, colour, point_negative=False, vertical=vertical)
        p.end()

    @staticmethod
    def _chevron(p, rect, colour, *, point_negative, vertical):
        if rect.width() < 4 or rect.height() < 4:
            return
        c = rect.center()
        s = 3.2
        if vertical:
            tip_y = c.y() - s if point_negative else c.y() + s
            base_y = c.y() + s * 0.6 if point_negative else c.y() - s * 0.6
            poly = QPolygonF([
                QPointF(c.x() - s, base_y),
                QPointF(c.x() + s, base_y),
                QPointF(c.x(), tip_y),
            ])
        else:
            tip_x = c.x() - s if point_negative else c.x() + s
            base_x = c.x() + s * 0.6 if point_negative else c.x() - s * 0.6
            poly = QPolygonF([
                QPointF(base_x, c.y() - s),
                QPointF(base_x, c.y() + s),
                QPointF(tip_x, c.y()),
            ])
        p.setPen(QPen(colour, 1.0))
        p.setBrush(colour)
        p.drawPolygon(poly)


def use_slim_scrollbars(area: QScrollArea) -> None:
    """Replace both bars of a scroll area. Qt deletes the ones it had."""
    area.setVerticalScrollBar(SlimScrollBar(Qt.Orientation.Vertical, area))
    area.setHorizontalScrollBar(SlimScrollBar(Qt.Orientation.Horizontal, area))
