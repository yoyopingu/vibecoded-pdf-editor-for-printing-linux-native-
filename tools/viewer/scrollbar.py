"""
The slim scrollbar on the right of a document view.

Acrobat's bar is a narrow column: a thumb you can grab, and a small step
button at each end. The single-page view had no bar at all — a zoomed page
could only be wheeled — and the page manager and the merge view each grew
whatever scrollbar the style sheet happened to leave them. One bar, 17 px,
on the right of all three.

The arrow buttons are drawn here because a stylesheet can reserve their
square but cannot draw the chevron without an image file. Anchor them to
the margin, not the content: otherwise Qt puts them on top of the thumb.
"""
from PyQt6.QtWidgets import QScrollArea, QScrollBar, QStyle, QStyleOptionSlider
from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QPainter, QPen, QColor, QPolygonF
from tools.theme import _TV, _register_themed


# The old 7-pixel painted thumb was easy to miss even inside its 15-pixel hit
# area. A little more visible width makes the grab target obvious.
SLIM_W = 17
_ARROW = 14


def slim_qss(t) -> str:
    track, thumb, hot, arrow_hot = (t["viewer_bg"], t["vdim"], t["dim"], t["panel_bg"])
    return (
        f"QScrollBar#slimScroll:vertical{{background:{track};width:{SLIM_W}px;"
        f"margin:{_ARROW}px 0 {_ARROW}px 0;border:none;}}"
        f"QScrollBar#slimScroll::handle:vertical{{background:{thumb};"
        f"min-height:32px;border:none;border-radius:4px;margin:2px 3px;}}"
        f"QScrollBar#slimScroll::handle:vertical:hover{{background:{hot};}}"
        f"QScrollBar#slimScroll::handle:vertical:pressed{{background:{hot};}}"
        f"QScrollBar#slimScroll::sub-line:vertical{{height:{_ARROW}px;"
        f"subcontrol-origin:margin;subcontrol-position:top;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::add-line:vertical{{height:{_ARROW}px;"
        f"subcontrol-origin:margin;subcontrol-position:bottom;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::sub-line:vertical:hover,"
        f"QScrollBar#slimScroll::add-line:vertical:hover{{background:{arrow_hot};}}"
        f"QScrollBar#slimScroll::add-page:vertical,"
        f"QScrollBar#slimScroll::sub-page:vertical{{background:none;}}"
        f"QScrollBar#slimScroll:horizontal{{background:{track};height:{SLIM_W}px;"
        f"margin:0 {_ARROW}px 0 {_ARROW}px;border:none;}}"
        f"QScrollBar#slimScroll::handle:horizontal{{background:{thumb};"
        f"min-width:32px;border:none;border-radius:4px;margin:3px 2px;}}"
        f"QScrollBar#slimScroll::handle:horizontal:hover{{background:{hot};}}"
        f"QScrollBar#slimScroll::handle:horizontal:pressed{{background:{hot};}}"
        f"QScrollBar#slimScroll::sub-line:horizontal{{width:{_ARROW}px;"
        f"subcontrol-origin:margin;subcontrol-position:left;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::add-line:horizontal{{width:{_ARROW}px;"
        f"subcontrol-origin:margin;subcontrol-position:right;background:{track};border:none;}}"
        f"QScrollBar#slimScroll::sub-line:horizontal:hover,"
        f"QScrollBar#slimScroll::add-line:horizontal:hover{{background:{arrow_hot};}}"
        f"QScrollBar#slimScroll::add-page:horizontal,"
        f"QScrollBar#slimScroll::sub-page:horizontal{{background:none;}}"
        f"QScrollBar#slimScroll::handle:disabled{{background:{t['border']};}}"
        "QScrollBar#slimScroll::up-arrow,QScrollBar#slimScroll::down-arrow,"
        "QScrollBar#slimScroll::left-arrow,QScrollBar#slimScroll::right-arrow"
        "{width:0;height:0;}"
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
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        style = self.style()
        cc = QStyle.ComplexControl.CC_ScrollBar
        vertical = self.orientation() == Qt.Orientation.Vertical
        sub = style.subControlRect(
            cc, opt, QStyle.SubControl.SC_ScrollBarSubLine, self)
        add = style.subControlRect(
            cc, opt, QStyle.SubControl.SC_ScrollBarAddLine, self)
        for rect, negative, enabled in ((sub, True, self.value() > self.minimum()),
                                         (add, False, self.value() < self.maximum())):
            colour = QColor(_TV["dim"] if enabled and self.isEnabled() else _TV["vdim"])
            self._chevron(p, rect, colour, point_negative=negative, vertical=vertical)
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
                QPointF(c.x(), tip_y),
                QPointF(c.x() + s, base_y),
            ])
        else:
            tip_x = c.x() - s if point_negative else c.x() + s
            base_x = c.x() + s * 0.6 if point_negative else c.x() - s * 0.6
            poly = QPolygonF([
                QPointF(base_x, c.y() - s),
                QPointF(tip_x, c.y()),
                QPointF(base_x, c.y() + s),
            ])
        p.setPen(QPen(colour, 1.2, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPolyline(poly)


def use_slim_scrollbars(area: QScrollArea) -> None:
    """Replace both bars of a scroll area. Qt deletes the ones it had.

    The vertical bar stays on screen even when the sheet already fits.
    A bar that appears only once you have overflowed is the one people
    report as missing.
    """
    area.setVerticalScrollBar(SlimScrollBar(Qt.Orientation.Vertical, area))
    area.setHorizontalScrollBar(SlimScrollBar(Qt.Orientation.Horizontal, area))
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
