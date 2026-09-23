"""Document navigation beside the scroll track, within reach at any zoom.

Keep page navigation separate from pixel scrolling: the track must remain
draggable even when a fitted page needs no panning. Icons are painted with
Qt so their strokes follow the palette and remain sharp at any display scale.
"""
from PyQt6.QtCore import Qt, QPointF, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QIntValidator, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QLabel, QLineEdit, QToolButton, QVBoxLayout, QWidget

from tools.i18n import tr
from tools.theme import _TV, _register_themed


class PageNumberEdit(QLineEdit):
    page_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = 0
        self._select_timer = QTimer(self)
        self._select_timer.setSingleShot(True)
        self._select_timer.timeout.connect(self.selectAll)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setValidator(QIntValidator(1, 2147483647, self))
        self.setAccessibleName(tr("Aktuelle Seite"))
        self.setToolTip(tr("Seitenzahl eingeben und mit Enter bestätigen"))
        self.setFixedHeight(28)

    def set_page(self, page):
        self._page = page
        # Background renders must not overwrite a number being typed.
        if not self.hasFocus():
            self.setText(str(page))

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Qt places the caret after FocusIn. Select after that click has
        # finished, while subsequent clicks can still position the caret.
        self._select_timer.start(0)

    def focusOutEvent(self, event):
        self._select_timer.stop()
        self.setText(str(self._page))
        super().focusOutEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            page = int(self.text()) if self.text() else self._page
            self.page_requested.emit(page)
            self.clearFocus()
            self.setText(str(self._page))
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.setText(str(self._page))
            self.clearFocus()
            event.accept()
        else:
            super().keyPressEvent(event)


class NavigationButton(QToolButton):
    def __init__(self, glyph, label, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self.setAccessibleName(label)
        self.setToolTip(label)
        self.setFixedSize(32, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        colour = _TV["text"] if self.isEnabled() else _TV["vdim"]
        if self.isChecked():
            colour = _TV["acc"]
        p.setPen(QPen(QColor(colour), 1.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        glyph = self._glyph
        if glyph in ("up", "down"):
            direction = -1 if glyph == "up" else 1
            p.drawPolyline(QPolygonF([QPointF(-5, -2 * direction),
                                      QPointF(0, 3 * direction),
                                      QPointF(5, -2 * direction)]))
        elif glyph in ("plus", "minus"):
            p.drawLine(QPointF(-6, 0), QPointF(6, 0))
            if glyph == "plus":
                p.drawLine(QPointF(0, -6), QPointF(0, 6))
        elif glyph == "ruler":
            p.drawRoundedRect(QRectF(-8, -5, 16, 10), 1, 1)
            for x in (-4, 0, 4):
                p.drawLine(QPointF(x, -5), QPointF(x, 0 if x == 0 else -2))
        elif glyph == "actual":
            font = p.font()
            font.setPixelSize(11)
            p.setFont(font)
            p.drawText(QRectF(-16, -12, 32, 24), Qt.AlignmentFlag.AlignCenter, "1:1")
        p.end()


class DocumentNavigation(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("documentNavigation")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 10, 5, 10)
        layout.setSpacing(3)

        self.rulers = NavigationButton("ruler", tr("Lineale und Hilfslinien"))
        self.rulers.setCheckable(True)
        layout.addWidget(self.rulers, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

        self.previous = NavigationButton("up", tr("Vorherige Seite"))
        self.next = NavigationButton("down", tr("Nächste Seite"))
        self.page = PageNumberEdit()
        self.total = QLabel()
        self.total.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.total.setObjectName("navigationTotal")
        for widget in (self.previous, self.page, self.total, self.next):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addSpacing(12)

        self.zoom_in = NavigationButton("plus", tr("Ansicht vergrößern"))
        self.zoom_out = NavigationButton("minus", tr("Ansicht verkleinern"))
        self.actual = NavigationButton("actual", tr("Originalgröße (100 %)"))
        self.zoom = QLabel("100%")
        self.zoom.setObjectName("navigationZoom")
        self.zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for widget in (self.zoom_in, self.zoom, self.zoom_out, self.actual):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        self.set_document(0, 0)
        _register_themed(self)
        self._apply_theme()

    def set_document(self, page, total):
        self.page.set_page(page)
        self.total.setText(tr("von {p0}").format(p0=total))
        self.previous.setEnabled(page > 1)
        self.next.setEnabled(0 < page < total)
        self.page.setEnabled(total > 0)
        for widget in (self.zoom_in, self.zoom_out, self.actual, self.rulers):
            widget.setEnabled(total > 0)
        # Large manuals still need a readable page count; a fixed two-digit
        # field clips both the current page and the total once they reach 1000.
        width = max(34, self.page.fontMetrics().horizontalAdvance(str(total)) + 14)
        self.page.setFixedWidth(width)
        self.setFixedWidth(max(46, width + 10, self.total.sizeHint().width() + 10))

    def _apply_theme(self):
        t = _TV
        self.setStyleSheet(
            f"QWidget#documentNavigation{{background:{t['panel_bg']};"
            f"border-left:1px solid {t['border']};}}"
            f"QToolButton{{background:transparent;border:1px solid transparent;"
            f"border-radius:5px;padding:0;}}"
            f"QToolButton:hover{{background:{t['card_bg']};}}"
            f"QToolButton:pressed,QToolButton:checked{{background:{t['sel_bg']};}}"
            f"QToolButton:focus{{border-color:{t['acc']};}}"
            f"QLineEdit{{background:{t['input_bg']};color:{t['text']};"
            f"border:1px solid {t['input_brd']};border-radius:4px;"
            f"padding:0;font-size:12px;min-height:26px;}}"
            f"QLineEdit:focus{{border:1px solid {t['acc']};padding:0;}}"
            f"QLabel{{background:transparent;color:{t['dim']};font-size:10px;}}"
            f"QLabel#navigationZoom{{color:{t['text']};}}")
        self.update()
