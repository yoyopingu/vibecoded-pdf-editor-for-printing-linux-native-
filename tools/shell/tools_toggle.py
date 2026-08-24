"""
The "Werkzeuge" toggle — the button that folds the tool list into the sidebar.

In "Seiten verwalten" and "Layout" the tool list lives behind this button: one
click appends the list to the bottom of the column's single scroll surface, a
second closes it again (docs/gui-concept.html: "Werkzeuge hinter einem
Umschalter … ein Klick hängt die Liste an das Ende der Seitenspalte an"). In
the preview the list is always open, so MainWindow/SidebarHost hide the toggle
there and in the merge preview entirely.

It is a checkable QPushButton: a grid glyph + "Werkzeuge" + a chevron that
rotates 180° while open. Its `toggled(bool)` signal is the native
QAbstractButton one; SidebarHost connects to it and reads `isChecked()`.
"""
from PyQt6.QtWidgets import QPushButton
from PyQt6.QtCore import Qt, QRect, QSize
from PyQt6.QtGui import QPainter
from tools.i18n import tr
from tools.shell.icons import icon
from tools.theme import _TV, _register_themed


class ToolsToggle(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("toolToggle")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setText(tr("Werkzeuge"))
        self._chev = None
        self._apply_theme()
        _register_themed(self)

    def _apply_theme(self):
        t = _TV
        # The grid glyph is accent-coloured, the chevron faint, exactly as the
        # concept draws them (.tooltoggle .ic:first-child / .chev). Rebuilt on
        # every theme switch because icon() resolves colour at call time.
        self.setIcon(icon("grid", colour=t["acc"], size=16))
        self.setIconSize(QSize(16, 16))
        self._chev = icon("chev", colour=t["vdim"], size=16)
        self.setStyleSheet(
            f"QPushButton#toolToggle{{"
            f"text-align:left; padding:4px 8px 4px 10px;"
            f"background:{t['btn_bg']}; color:{t['text']};"
            f"border:1px solid {t['btn_brd']}; border-radius:8px;"
            f"font-size:12px; min-height:30px;}}"
            f"QPushButton#toolToggle:hover{{border-color:{t['acc']};}}"
            f"QPushButton#toolToggle:checked{{background:{t['hover']};}}")

    def paintEvent(self, event):
        # Qt draws the icon + text left-aligned; the chevron is our own, riding
        # the right edge and turning 180° when the list is open.
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = 16
        x = self.width() - s - 12          # 12 px from the right edge
        y = (self.height() - s) // 2
        p.save()
        if self.isChecked():
            cx, cy = x + s / 2, y + s / 2
            p.translate(cx, cy)
            p.rotate(180)
            p.translate(-cx, -cy)
        self._chev.paint(p, QRect(x, y, s, s))
        p.restore()
        p.end()