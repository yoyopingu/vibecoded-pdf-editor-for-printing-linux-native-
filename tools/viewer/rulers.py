"""
Rulers down the edges of the page view, and the guides you pull out of them.

What Acrobat does, which is what this copies:

  * Ctrl+R shows and hides them.
  * The numbering starts at the top-left corner of the *page*, not of the
    window — so a reading is a distance into the sheet, which is the number
    anyone measuring a margin actually wants.
  * Dragging down from the top ruler leaves a horizontal guide; dragging right
    from the left ruler leaves a vertical one. Double-clicking a ruler drops a
    guide where you clicked.
  * A guide can be dragged to a new place, and dragged back onto its ruler to
    get rid of it. Right-clicking a ruler clears the guides on this page or in
    the whole document.

Millimetres, because this is a print shop: every paper size, margin and bleed
in the rest of the application is in millimetres and a ruler that disagreed
with them would be one more conversion to do in your head.

The guides themselves are kept by the view in page coordinates, not in pixels
— see SinglePageView._guides. A guide 20 mm into the sheet stays 20 mm into
the sheet through a zoom, a scroll and a page turn, which is the whole point of
setting one.
"""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QFont
from PyQt6.QtWidgets import QMenu, QWidget

from tools.i18n import tr
from tools.theme import _TV

RULER_THICKNESS = 22          # px; enough for a two-digit label and a tick
GUIDE_COLOUR = QColor(0, 170, 230)


class RulerBar(QWidget):
    """One ruler. `horizontal` picks the top bar or the left one.

    It knows nothing about pages: the view hands it where the sheet starts on
    screen and how many pixels a millimetre is, and it draws from that.
    """

    # axis ("h" or "v") and where it sits, in the page canvas's pixels.
    guide_previewed = pyqtSignal(str, float)
    guide_dropped   = pyqtSignal(str, float)
    clear_requested = pyqtSignal(bool)    # True = every page, False = this one

    def __init__(self, horizontal=True, parent=None):
        super().__init__(parent)
        self._horizontal = horizontal
        self._origin_px  = 0.0    # where the page's edge sits, in view pixels
        self._px_per_mm  = 0.0    # 0 = nothing measured yet, draw an empty bar
        self._cursor_px  = None   # follows the pointer over the page
        self._dragging   = False
        if horizontal:
            self.setFixedHeight(RULER_THICKNESS)
            self.setCursor(Qt.CursorShape.SplitVCursor)
        else:
            self.setFixedWidth(RULER_THICKNESS)
            self.setCursor(Qt.CursorShape.SplitHCursor)
        self.setMouseTracking(True)

    # ── what the view tells it ───────────────────────────────────────────────

    def set_scale(self, origin_px, px_per_mm):
        if (abs(origin_px - self._origin_px) > 0.01
                or abs(px_per_mm - self._px_per_mm) > 1e-6):
            self._origin_px = float(origin_px)
            self._px_per_mm = float(px_per_mm)
            self.update()

    def set_cursor_px(self, px):
        """Mark where the pointer is over the page, or None to clear it."""
        if px != self._cursor_px:
            self._cursor_px = px
            self.update()

    # ── ticks ────────────────────────────────────────────────────────────────

    def _step_mm(self):
        """A tick spacing that stays legible at any zoom: the finest step from
        the ladder below that still leaves ~6 px between ticks."""
        if self._px_per_mm <= 0:
            return None
        for step in (1, 2, 5, 10, 20, 50, 100, 200, 500):
            if step * self._px_per_mm >= 6:
                return step
        return 500

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_TV['panel_bg']))
        edge = QColor(_TV['border'])
        p.setPen(QPen(edge, 1))
        if self._horizontal:
            p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        else:
            p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())

        step = self._step_mm()
        if step is None:
            p.end()
            return

        font = QFont(); font.setPointSize(7)
        p.setFont(font)
        length = self.width() if self._horizontal else self.height()
        ink = QColor(_TV['dim'])
        text = QColor(_TV['text'])

        # Whole steps either side of the page's origin, so 0 lands on the sheet
        # corner and negatives run off to its left and above it.
        first = int(((0 - self._origin_px) / self._px_per_mm) // step) * step
        last  = int(((length - self._origin_px) / self._px_per_mm) // step + 1) * step
        labelled = step * (5 if step * self._px_per_mm < 34 else 1)
        for mm in range(first, last + 1, step):
            pos = self._origin_px + mm * self._px_per_mm
            if pos < -2 or pos > length + 2:
                continue
            major = (mm % labelled == 0)
            size = RULER_THICKNESS - (7 if major else 4)
            p.setPen(QPen(ink, 1))
            if self._horizontal:
                p.drawLine(int(pos), RULER_THICKNESS - size, int(pos), RULER_THICKNESS - 1)
            else:
                p.drawLine(RULER_THICKNESS - size, int(pos), RULER_THICKNESS - 1, int(pos))
            if major:
                p.setPen(QPen(text, 1))
                label = str(mm)
                if self._horizontal:
                    p.drawText(int(pos) + 2, 9, label)
                else:
                    # Down the bar rather than across it: a vertical ruler 22 px
                    # wide has no room for "150" written the usual way.
                    p.save()
                    p.translate(9, int(pos) - 2)
                    p.rotate(-90)
                    p.drawText(0, 0, label)
                    p.restore()

        if self._cursor_px is not None:
            p.setPen(QPen(QColor(_TV['acc']), 1))
            c = int(self._cursor_px)
            if self._horizontal:
                p.drawLine(c, 0, c, RULER_THICKNESS)
            else:
                p.drawLine(0, c, RULER_THICKNESS, c)
        p.end()

    # ── pulling a guide out ──────────────────────────────────────────────────
    #
    # Which number matters is the opposite of the obvious one. Dragging *down*
    # from the top ruler leaves a horizontal guide, and where that guide lands
    # is how far down you dragged — the coordinate across the ruler, not along
    # it. Both are reported in the page canvas's own pixels, so the view can
    # place them without knowing which bar they came from.

    def _across(self, event):
        """How far past the ruler the pointer is, in canvas pixels."""
        pos = event.position()
        return float((pos.y() if self._horizontal else pos.x()) - RULER_THICKNESS)

    def _along(self, event):
        """How far along the ruler the pointer is, in canvas pixels."""
        pos = event.position()
        return float(pos.x() if self._horizontal else pos.y())

    def _drag_axis(self):
        """Dragging off the top ruler makes a horizontal guide, and vice versa."""
        return "h" if self._horizontal else "v"

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self.guide_previewed.emit(self._drag_axis(), self._across(e))

    def mouseMoveEvent(self, e):
        if self._dragging:
            self.guide_previewed.emit(self._drag_axis(), self._across(e))

    def mouseReleaseEvent(self, e):
        if self._dragging and e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.guide_dropped.emit(self._drag_axis(), self._across(e))

    def mouseDoubleClickEvent(self, e):
        # Acrobat drops a guide where you double-click a ruler — on the other
        # axis from the one dragging gives you, because there is no drag to
        # take a direction from. Double-clicking the left ruler at 40 mm gives
        # a horizontal line at 40 mm, which is what the number under the
        # pointer says.
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            axis = "v" if self._horizontal else "h"
            self.guide_dropped.emit(axis, self._along(e))

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        here = menu.addAction(tr("Hilfslinien dieser Seite entfernen"))
        everywhere = menu.addAction(tr("Alle Hilfslinien entfernen"))
        chosen = menu.exec(e.globalPos())
        if chosen is here:
            self.clear_requested.emit(False)
        elif chosen is everywhere:
            self.clear_requested.emit(True)
