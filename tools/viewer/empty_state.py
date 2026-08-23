"""
The empty window: what the canvas shows before any document is open.

A drop target, the two ways to start (one file, or several to merge), and up
to four recently opened files with a thumbnail, a page count and a date —
enough to tell two versions of the same job apart, which is the actual
problem in a copy shop. Shown in place of the tab strip's canvas rather than
as a dialog, so opening a file fills this window instead of rearranging it.
"""
import datetime
import os

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                             QWidget)

from tools.i18n import get_language, tr
from tools.multi_open import classify
from tools.shell.style import app_icon
from tools.theme import PAPER, _TV, _register_themed

MAX_RECENT = 4

_MONTHS = {
    "de": ["JAN", "FEB", "MÄR", "APR", "MAI", "JUN",
           "JUL", "AUG", "SEP", "OKT", "NOV", "DEZ"],
    "en": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
}


def _page_glyph() -> QPixmap:
    """A generic document thumbnail — a sheet with a header bar and two lines
    of body, the same abstraction the concept draws. Not a real page render:
    four of these cost nothing to show and never go stale."""
    pm = QPixmap(60, 78)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor("#c8ccd2"), 1))
    p.setBrush(QColor(PAPER))
    p.drawRoundedRect(1, 1, 58, 76, 3, 3)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#c2cad6"))
    p.drawRect(9, 11, 42, 6)
    p.setBrush(QColor("#dde3ea"))
    for y in (25, 33, 41, 49):
        p.drawRect(9, y, 42, 3)
    p.end()
    return pm


def _page_count(path: str):
    if os.path.splitext(path)[1].lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader
        return len(PdfReader(path, strict=False).pages)
    except Exception:
        return None


def _short_date(path: str) -> str:
    try:
        d = datetime.date.fromtimestamp(os.path.getmtime(path))
    except OSError:
        return ""
    today = datetime.date.today()
    if d == today:
        return tr("HEUTE")
    if d == today - datetime.timedelta(days=1):
        return tr("GESTERN")
    months = _MONTHS.get(get_language(), _MONTHS["de"])
    return f"{d.day}. {months[d.month - 1]}"


def _recent_meta(path: str) -> str:
    n = _page_count(path)
    date = _short_date(path)
    if n is None:
        return date
    pages = tr('{p0} S').format(p0=n)
    return f"{pages} · {date}" if date else pages


class _RecentCard(QWidget):
    """One "Zuletzt geöffnet" entry. Clicking it opens the file, the same as
    picking it from a file dialog would."""
    clicked = pyqtSignal()

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.setObjectName("recentCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(112)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(5)

        thumb = QLabel()
        thumb.setPixmap(_page_glyph())
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(thumb)

        name = QLabel(os.path.basename(path))
        name.setObjectName("recentName")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setWordWrap(True)
        lay.addWidget(name)

        meta = QLabel(_recent_meta(path))
        meta.setObjectName("dimLabel")
        meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(meta)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class EmptyStateWidget(QWidget):
    """Fills the canvas while no tab is open. See PageViewerPanel._sync_empty_state."""
    open_requested  = pyqtSignal()          # "Datei öffnen…" clicked
    merge_requested = pyqtSignal()          # "Mehrere zusammenführen…" clicked
    file_chosen     = pyqtSignal(str)       # a recent-file card was clicked
    files_dropped   = pyqtSignal(list)      # one or more files dragged in

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setAcceptDrops(True)
        self._build()
        _register_themed(self)
        self._apply_theme()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.addStretch(2)

        col = QWidget()
        col.setFixedWidth(400)
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(7)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(app_icon().pixmap(QSize(44, 44)))
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(icon_lbl)

        self._headline = QLabel(tr("PDF, Bild oder Office-Datei hierher ziehen"))
        self._headline.setObjectName("emptyHeadline")
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._headline.setWordWrap(True)
        cl.addWidget(self._headline)

        sub = QLabel(tr("Bilder und Office-Dokumente werden beim Öffnen umgewandelt."))
        sub.setObjectName("dimLabel")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        cl.addWidget(sub)

        cta = QHBoxLayout()
        cta.setSpacing(8)
        open_btn = QPushButton(tr("Datei öffnen…"))
        open_btn.setObjectName("actionBtn")
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.clicked.connect(self.open_requested.emit)
        merge_btn = QPushButton(tr("Mehrere zusammenführen…"))
        merge_btn.setObjectName("secondaryBtn")
        merge_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        merge_btn.clicked.connect(self.merge_requested.emit)
        cta.addStretch()
        cta.addWidget(open_btn)
        cta.addWidget(merge_btn)
        cta.addStretch()
        cl.addLayout(cta)

        row_outer = QHBoxLayout()
        row_outer.addStretch()
        row_outer.addWidget(col)
        row_outer.addStretch()
        outer.addLayout(row_outer)
        outer.addSpacing(22)

        self._recent_hd = QLabel(tr("Zuletzt geöffnet"))
        self._recent_hd.setObjectName("sectionLabel")
        self._recent_hd.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._recent_hd.setVisible(False)
        outer.addWidget(self._recent_hd)

        self._recent_row = QHBoxLayout()
        self._recent_row.setSpacing(14)
        self._recent_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addLayout(self._recent_row)

        outer.addStretch(3)
        self._cards = []

    def set_recent(self, paths):
        """Show up to MAX_RECENT of `paths` (newest first) as clickable cards."""
        for c in self._cards:
            self._recent_row.removeWidget(c)
            c.setParent(None)
            c.deleteLater()
        self._cards = []
        paths = [p for p in paths if os.path.isfile(p)][:MAX_RECENT]
        self._recent_hd.setVisible(bool(paths))
        for p in paths:
            card = _RecentCard(p)
            card.clicked.connect(lambda p=p: self.file_chosen.emit(p))
            self._recent_row.addWidget(card)
            self._cards.append(card)
        self._apply_theme()   # new cards start unstyled otherwise

    def _apply_theme(self):
        t = _TV
        self._headline.setStyleSheet(
            f"color:{t['text']};font-size:14px;font-weight:600;background:transparent;")
        for c in self._cards:
            c.setStyleSheet(
                f"QWidget#recentCard{{border:1px solid {t['border']};"
                f"border-radius:8px;background:{t['card_bg']};}}"
                f"QWidget#recentCard:hover{{border-color:{t['acc']};}}")
            name = c.findChild(QLabel, "recentName")
            if name is not None:
                name.setStyleSheet(f"color:{t['text']};font-size:10.5px;background:transparent;")

    # ── drag & drop ──────────────────────────────────────────────────────────

    def _local_files(self, mime):
        if not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and classify(u.toLocalFile()) is not None]

    def dragEnterEvent(self, e):
        if self._local_files(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if self._local_files(e.mimeData()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        files = self._local_files(e.mimeData())
        if files:
            self.files_dropped.emit(files)
            e.acceptProposedAction()
        else:
            e.ignore()
