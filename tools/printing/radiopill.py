"""
The print dialog's radiopill: a checkable pill button with a dot indicator.

Styled to the concept's `.radiopill` (docs/gui-concept.html) — a rounded pill
with a small circle on the left that turns accent when the pill is selected,
and an accent border over a soft accent ground.

It is a QToolButton, not a QRadioButton, so exclusivity is NOT handled by
Qt's autoExclusive siblings. The dialog puts its pills into a QButtonGroup
(with IDs where the checkedId() logic needs them) — the same mechanism the
scale group always used, so `_scale_index()` / `_sync_scale_pct()` are
untouched. The public surface QAbstractButton promises — setChecked,
isChecked, toggled — all work, which is what the tests rely on.

Self-painted from _TV (the live viewer palette) so it follows the theme at
paint time, the same way the rest of the dialog's panels do.
"""
from PyQt6.QtCore import Qt, QRectF, QSize
from PyQt6.QtGui import QPainter, QPen, QColor
from PyQt6.QtWidgets import QToolButton
from tools.theme import _TV


class RadioPill(QToolButton):
    """A checkable pill button with a dot indicator and a text label."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoExclusive(False)     # exclusivity is the QButtonGroup's job
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(30)
        self._pad_x = 12                 # horizontal padding either side
        self._dot = 13                   # dot diameter, px

    # ── Geometry ────────────────────────────────────────────────────────────
    def sizeHint(self):
        w = self.fontMetrics().horizontalAdvance(self.text())
        w += self._pad_x * 2 + self._dot + 8
        return QSize(max(44, w), 30)

    def minimumSizeHint(self):
        return self.sizeHint()

    # ── Painting ────────────────────────────────────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        acc  = _TV["acc"]
        line = _TV["line_strong"]
        text = _TV["text"]
        dim  = _TV["dim"]
        on   = self.isChecked()

        r = self.rect().adjusted(1, 1, -1, -1)
        radius = r.height() / 2.0

        # The pill body: accent-soft ground + accent border when checked, a
        # neutral outline otherwise.
        if on:
            soft = QColor(acc); soft.setAlpha(40)
            p.setBrush(soft)
        else:
            p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(acc if on else line), 1))
        p.drawRoundedRect(r, radius, radius)

        # The dot indicator on the left.
        cy = self.height() / 2.0
        dot_left = self._pad_x
        dot_r = QRectF(dot_left, cy - self._dot / 2.0, self._dot, self._dot)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(acc if on else line), 1.5))
        p.drawEllipse(dot_r)
        if on:                                # a filled centre marks the choice
            inner = self._dot * 0.46
            p.setBrush(QColor(acc))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(dot_left + (self._dot - inner) / 2.0,
                                 cy - inner / 2.0, inner, inner))

        # The label, accent text when selected.
        fm = self.fontMetrics()
        baseline = (self.height() + fm.ascent() - fm.descent()) / 2.0
        p.setPen(QColor(text if on else dim))
        p.drawText(int(dot_left + self._dot + 8), int(baseline), self.text())

        p.end()
