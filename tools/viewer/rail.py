"""
The navigation rail: the page number, the tick track with its thumb, and the
column widget that holds the rail once it has been handed to another layout.

Two modes. Paged (`picked`, a page number): the thumb snaps to the page being
shown and a drag picks pages. Continuous (`position_dragged`, a 0..1 fraction
of the track): the thumb mirrors the exact scroll position and a drag scrolls
to the fraction under the pointer.
"""
from PyQt6.QtCore import QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QLabel, QSizePolicy, QWidget

from tools.theme import FIND, _TV


class _PageField(QLabel):
    """The page number at the foot of the rail. A label you can click.

    A QLineEdit here would be 40 px of permanent input box for something read a
    hundred times and typed into once; a label that answers a click with the
    go-to prompt costs nothing and says the same thing."""
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("1", parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class _PageTrack(QWidget):
    """The rail's track: one tick per page, and a thumb marking where you are.

    Drawn rather than assembled from a QScrollBar because the two things that
    make it useful are not a scroll bar's business — a tick per page, so the
    length of the document is visible rather than implied, and later the marks
    that say where a search found something.

    The thumb sits *inside* the track and is squared off, 11 px in a 13 px
    groove: it is the same object as the rail with a section filled in, not a
    capsule laid on top of it.

    Two modes. Paged (`picked`, a page number): the thumb snaps to the page
    being shown and a drag picks pages. Continuous (`position_dragged`, a 0..1
    fraction of the track): the thumb mirrors the exact scroll position
    and a drag scrolls to the fraction under the pointer — a drag in a paged
    rail jumps a page at a time, which in a document that flows reads as the
    rail moving in chunks.
    """
    picked = pyqtSignal(int)              # a page, 1-based
    position_dragged = pyqtSignal(float)  # 0..1 of the track, continuous mode

    GROOVE   = 13
    THUMB_IN = 1                      # inset each side, so 11 px of thumb
    MIN_THUMB = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self._n       = 0
        self._page    = 1
        self._span    = 1             # pages visible at once; 1 until slice 4
        self._hits    = []            # pages a search found something on
        self._drag    = False
        self._scroll_mode = False     # continuous: thumb tracks the scroll
        self._scroll_frac = None      # 0..1 while _scroll_mode
        self.setFixedWidth(self.GROOVE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Expanding)

    def set_document(self, n_pages, page=1):
        n_pages = max(0, int(n_pages))
        if (n_pages, page) == (self._n, self._page):
            return
        self._n    = n_pages
        self._page = max(1, min(int(page), max(1, n_pages)))
        self.update()

    def set_scroll_mode(self, on):
        """Continuous mode: the thumb follows the scroll position, not the page."""
        on = bool(on)
        if on == self._scroll_mode:
            return
        self._scroll_mode = on
        if not on:
            self._scroll_frac = None
        self.update()

    def set_scroll_position(self, frac):
        """Where the viewport is along the strip, as a 0..1 fraction."""
        frac = None if frac is None else min(1.0, max(0.0, float(frac)))
        if self._scroll_mode and frac != self._scroll_frac:
            self._scroll_frac = frac
            self.update()

    def set_hits(self, pages):
        """Which pages a search found something on, so the rail can say where
        the answers are before you scroll to them."""
        pages = sorted(set(pages))
        if pages != self._hits:
            self._hits = pages
            self.update()

    def _thumb_rect(self):
        h = self.height()
        if self._n <= 0:
            return None
        frac = min(1.0, self._span / self._n)
        th   = max(self.MIN_THUMB, int(h * frac))
        # Where the thumb's *top* goes, so the last page puts it flush with the
        # bottom rather than half off the end.
        travel = max(0, h - th)
        if self._scroll_mode and self._scroll_frac is not None:
            pos = self._scroll_frac
        else:
            pos = 0 if self._n <= 1 else (self._page - 1) / (self._n - 1)
        return QRect(self.THUMB_IN, int(travel * pos),
                     self.GROOVE - 2 * self.THUMB_IN, th)

    def _frac_at(self, y):
        rect = self._thumb_rect()
        th = rect.height() if rect else self.MIN_THUMB
        travel = max(1, self.height() - th)
        return min(1.0, max(0.0, (y - th / 2) / travel))

    def _page_at(self, y):
        pos = self._frac_at(y)
        return int(round(pos * (self._n - 1))) + 1 if self._n > 1 else 1

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_TV['track']))
        p.drawRoundedRect(0, 0, self.GROOVE, self.height(), 3, 3)

        # One tick per page — but only while they are far enough apart to read
        # as ticks. On a 900-page document they would be a solid bar.
        if self._n > 1:
            step = self.height() / self._n
            if step >= 4:
                p.setPen(QPen(QColor(_TV['border']), 1))
                for i in range(1, self._n):
                    y = int(i * step)
                    p.drawLine(2, y, self.GROOVE - 3, y)

        # Search hits, before the thumb so the thumb is never hidden by one.
        if self._hits and self._n > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(*FIND["current"]))
            h = self.height()
            for page in self._hits:
                y = int((page - 1) / max(1, self._n - 1) * max(0, h - 3))
                p.drawRect(1, y, self.GROOVE - 2, 2)

        rect = self._thumb_rect()
        if rect is not None:
            p.setPen(Qt.PenStyle.NoPen)
            # A neutral grey thumb, never the blue accent: the rail is the
            # grid's scrollbar, and a blue thumb read as a selection rather
            # than a scroll control (Batch F). vdim is the palette's muted grey.
            p.setBrush(QColor(_TV['vdim']))
            p.drawRoundedRect(rect, 2, 2)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._n > 0:
            self._drag = True
            if self._scroll_mode:
                self.position_dragged.emit(self._frac_at(e.position().y()))
            else:
                self.picked.emit(self._page_at(e.position().y()))

    def mouseMoveEvent(self, e):
        if self._drag and self._n > 0:
            if self._scroll_mode:
                self.position_dragged.emit(self._frac_at(e.position().y()))
            else:
                self.picked.emit(self._page_at(e.position().y()))

    def mouseReleaseEvent(self, _e):
        self._drag = False


class _NavRailColumn(QWidget):
    """Holds the navigation rail once it has been handed to another layout.

    The tab puts the rail beside both views so the page manager can share it
    (see PdfTab). Sitting inside the view's layout, the wheel over the rail
    reached the view through event propagation; from out here it has to be
    forwarded — to the view itself in preview mode, or to whatever the rail's
    delegate is driving (the grid) in manage mode.
    """

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self._view = view

    def wheelEvent(self, e):
        view = self._view
        handler = view._rail_handler()
        if handler is view:
            view.wheelEvent(e)
            e.accept()
            return
        ang = e.angleDelta().y()
        pix = e.pixelDelta().y()
        detent = abs(ang) >= 120
        if pix and not detent:
            handler.rail_wheel(-float(pix))
        elif ang:
            handler.rail_wheel(-view.SCROLL_NOTCH_PX * (ang / 120.0))
        e.accept()
