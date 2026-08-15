"""
The widget a rendered page is painted on.

Draws the pixmap, outlines the sheet, and owns the text selection: the character
boxes that come back with a render, the drag that picks a range out of them, and
the copy.
"""
from PyQt6.QtWidgets import QWidget, QApplication, QMenu, QSizePolicy
from PyQt6.QtCore import Qt, QRect, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QKeySequence, QBrush, QCursor
from tools.i18n import tr
from tools.theme import PAPER, _TV
from tools.viewer.rulers import GUIDE_COLOUR


class PdfPageCanvas(QWidget):
    """
    Zeigt eine PDF-Seite als Pixmap und ermöglicht
    Textauswahl per Maus sowie Kopieren mit Strg+C.
    """

    # A guide was dragged to a new place: axis, which one, where it ended up in
    # widget pixels. The view turns that back into a page coordinate.
    guide_moved = pyqtSignal(str, int, float)
    # Something was repainted, so the rulers need their scale again.
    repainted = pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap     = None
        self._chars      = []   # Liste von (char, x0, y0, x1, y1) in Widget-Koordinaten
        self._sel_start  = -1
        self._sel_end    = -1
        self._dragging   = False
        self._offset_x   = 0
        self._offset_y   = 0
        self._page_rect  = None   # sheet outline when the pixmap is a region
        # Guides, in this widget's pixels. The view owns them in page
        # coordinates and hands down where they land on screen — see
        # tools/viewer/rulers.py.
        self._guides_h   = []     # y positions
        self._guides_v   = []     # x positions
        self._preview    = None   # ("h"|"v", px) while one is being dragged out
        self._drag_guide = None   # ("h"|"v", index) while one is being moved

        self.setStyleSheet(f"background:{_TV['viewer_bg']};")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(QCursor(Qt.CursorShape.IBeamCursor))

    # ── guides ───────────────────────────────────────────────────────────────

    GUIDE_GRAB_PX = 4          # how near the pointer counts as "on the guide"

    def set_guides(self, horizontal, vertical):
        """Where the guides sit in widget pixels, from the view."""
        if horizontal != self._guides_h or vertical != self._guides_v:
            self._guides_h = list(horizontal)
            self._guides_v = list(vertical)
            self.update()

    def set_guide_preview(self, preview):
        """("h"|"v", px) while one is being dragged out of a ruler, or None."""
        if preview != self._preview:
            self._preview = preview
            self.update()

    def _guide_at(self, point):
        """The guide under `point` as ("h"|"v", index), or None."""
        for i, y in enumerate(self._guides_h):
            if abs(point.y() - y) <= self.GUIDE_GRAB_PX:
                return ("h", i)
        for i, x in enumerate(self._guides_v):
            if abs(point.x() - x) <= self.GUIDE_GRAB_PX:
                return ("v", i)
        return None

    def _paint_guides(self, p):
        pen = QPen(GUIDE_COLOUR, 1)
        p.setPen(pen)
        for y in self._guides_h:
            p.drawLine(0, int(y), self.width(), int(y))
        for x in self._guides_v:
            p.drawLine(int(x), 0, int(x), self.height())
        if self._preview is not None:
            axis, pos = self._preview
            p.setPen(QPen(GUIDE_COLOUR, 1, Qt.PenStyle.DashLine))
            if axis == "h":
                p.drawLine(0, int(pos), self.width(), int(pos))
            else:
                p.drawLine(int(pos), 0, int(pos), self.height())

    def set_page(self, pixmap, chars, offset_x, offset_y, page_rect=None):
        """
        pixmap   — gerendertes QPixmap
        chars    — [(char, x0, y0, x1, y1), …] in Widget-Pixelkoordinaten
        offset_x/y — wo die Pixmap innerhalb des Widgets platziert wird
        page_rect — (x, y, w, h) of the whole sheet in widget coords, when the
                    pixmap is only the visible part of a page too large to
                    render in one piece. The outline is drawn around this
                    instead of around the pixmap, which would otherwise put a
                    hairline through the middle of the page.
        """
        self._pixmap    = pixmap
        self._chars     = chars
        self._sel_start = -1
        self._sel_end   = -1
        self._offset_x  = offset_x
        self._offset_y  = offset_y
        self._page_rect = page_rect
        self.update()
        self.repainted.emit()

    def show_placeholder(self, x, y, w, h):
        """An empty sheet, while the page behind it is still rendering.

        Scrolling through a long document must not wait for anything: pages
        that have not been rendered yet stand in as blank paper of the right
        size, so the document keeps its shape and its scrollbar under you and
        the real page arrives underneath when it is ready.
        """
        self._pixmap    = None
        self._chars     = []
        self._sel_start = -1
        self._sel_end   = -1
        self._offset_x  = int(x)
        self._offset_y  = int(y)
        self._page_rect = (int(x), int(y), int(w), int(h))
        self.update()

    def clear(self):
        self._pixmap = None
        self._page_rect = None
        self._chars  = []
        self._sel_start = self._sel_end = -1
        self.update()

    # ── Maus-Events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            # Within a few pixels of a guide, the drag moves the guide. Only
            # then — anywhere else on the page this is still a text selection,
            # which is what the canvas is mostly for.
            grabbed = self._guide_at(e.position().toPoint())
            if grabbed is not None:
                self._drag_guide = grabbed
                return
            idx = self._char_at(e.position().toPoint())
            self._sel_start = idx
            self._sel_end   = idx
            self._dragging  = True
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag_guide is not None:
            axis, index = self._drag_guide
            point = e.position().toPoint()
            store = self._guides_h if axis == "h" else self._guides_v
            if 0 <= index < len(store):
                store[index] = point.y() if axis == "h" else point.x()
                self.update()
            return
        if not self._dragging:
            # Over a guide, say so: the cursor is the only hint that the line
            # can be picked up at all.
            on_guide = self._guide_at(e.position().toPoint())
            self.setCursor(QCursor(
                Qt.CursorShape.SplitVCursor if on_guide and on_guide[0] == "h"
                else Qt.CursorShape.SplitHCursor if on_guide
                else Qt.CursorShape.IBeamCursor))
        if self._dragging:
            idx = self._char_at(e.position().toPoint())
            self._sel_end = idx
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if self._drag_guide is not None:
                axis, index = self._drag_guide
                self._drag_guide = None
                store = self._guides_h if axis == "h" else self._guides_v
                if 0 <= index < len(store):
                    self.guide_moved.emit(axis, index, float(store[index]))
                return
            self._dragging = False

    def mouseDoubleClickEvent(self, e):
        """Doppelklick: ganzes Wort auswählen."""
        idx = self._char_at(e.position().toPoint())
        if idx < 0 or not self._chars:
            return
        text = "".join(c for c, *_ in self._chars)
        # Wort-Grenzen suchen
        start = idx
        while start > 0 and text[start-1] not in " \t\r\n":
            start -= 1
        end = idx
        while end < len(text)-1 and text[end+1] not in " \t\r\n":
            end += 1
        self._sel_start = start
        self._sel_end   = end
        self.update()

    # ── Tastatur ─────────────────────────────────────────────────────────────

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy):
            self._copy()
        elif e.matches(QKeySequence.StandardKey.SelectAll):
            if self._chars:
                self._sel_start = 0
                self._sel_end   = len(self._chars) - 1
                self.update()
        else:
            super().keyPressEvent(e)

    def _select_all(self):
        if self._chars:
            self._sel_start = 0
            self._sel_end   = len(self._chars) - 1
            self.update()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        sel  = self._selected_text()
        cp   = menu.addAction(tr("Kopieren"))
        cp.setEnabled(bool(sel))
        cp.triggered.connect(self._copy)
        sa = menu.addAction(tr("Alles auswählen"))
        sa.triggered.connect(self._select_all)
        menu.exec(e.globalPos())

    # ── Zeichnen ─────────────────────────────────────────────────────────────

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_TV['viewer_bg']))

        if not self._pixmap:
            # A page still rendering: draw the sheet it will be, so scrolling
            # past it looks like a document rather than a hole. Only when its
            # size is known — with nothing to go on there is nothing honest to
            # draw.
            if self._page_rect is not None:
                rx, ry, rw, rh = self._page_rect
                p.fillRect(int(rx), int(ry), int(rw), int(rh), QColor(PAPER))
                p.setPen(QPen(QColor(0, 0, 0, 45), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(int(rx), int(ry), int(rw) - 1, int(rh) - 1)
            self._paint_guides(p)
            p.end()
            return

        p.drawPixmap(self._offset_x, self._offset_y, self._pixmap)

        # Hairline around the page so it stays defined on a light background.
        # Solid black drew a hard halo around the sheet — on the dark backdrop
        # the white page needs no help separating, and the line only made the
        # edge look burnt.
        p.setPen(QPen(QColor(0, 0, 0, 45), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._page_rect is not None:
            rx, ry, rw, rh = self._page_rect
            p.drawRect(int(rx), int(ry), int(rw) - 1, int(rh) - 1)
        else:
            p.drawRect(self._offset_x, self._offset_y,
                       self._pixmap.width() - 1, self._pixmap.height() - 1)

        # Auswahl-Highlights
        if self._sel_start >= 0 and self._sel_end >= 0 and self._chars:
            lo = min(self._sel_start, self._sel_end)
            hi = max(self._sel_start, self._sel_end)
            sel_color = QColor(66, 135, 245, 80)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(sel_color))
            for i in range(lo, hi + 1):
                if i < len(self._chars):
                    _, x0, y0, x1, y1 = self._chars[i]
                    p.drawRect(QRect(
                        int(x0), int(y0),
                        max(1, int(x1 - x0)),
                        max(1, int(y1 - y0))
                    ))

        self._paint_guides(p)
        p.end()

    # ── Hilfsfunktionen ──────────────────────────────────────────────────────

    def _char_at(self, pt):
        """Gibt Index des Zeichens zurück das den Punkt enthält, sonst -1."""
        if not self._chars:
            return -1
        x, y = pt.x(), pt.y()
        # Exakter Treffer
        for i, (_, x0, y0, x1, y1) in enumerate(self._chars):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        # Nächstes Zeichen in der Nähe (für klicken zwischen Zeichen)
        best_i, best_d = -1, float("inf")
        for i, (_, x0, y0, x1, y1) in enumerate(self._chars):
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            d  = (cx - x)**2 + (cy - y)**2
            if d < best_d:
                best_d = d
                best_i = i
        return best_i if best_d < 40**2 else -1

    def _selected_text(self):
        if self._sel_start < 0 or self._sel_end < 0 or not self._chars:
            return ""
        lo = min(self._sel_start, self._sel_end)
        hi = max(self._sel_start, self._sel_end)
        return "".join(self._chars[i][0] for i in range(lo, min(hi+1, len(self._chars))))

    def _copy(self):
        text = self._selected_text()
        if text:
            QApplication.clipboard().setText(text)
