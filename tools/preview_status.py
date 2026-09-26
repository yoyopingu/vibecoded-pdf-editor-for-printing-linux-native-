"""The status strip used below document and tool previews."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
from tools.theme import _TV, _register_themed


class PreviewStatusBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewStatusBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(30)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(12, 0, 12, 0)
        self.row.setSpacing(20)
        self.row.addStretch()
        _register_themed(self)
        self._apply_theme()

    def add_label(self, text=""):
        label = QLabel(text)
        label.setObjectName("dimLabel")
        self.row.insertWidget(self.row.count() - 1, label)
        return label

    def add_widget(self, widget):
        self.row.insertWidget(self.row.count() - 1, widget)

    def _apply_theme(self):
        t = _TV
        self.setStyleSheet(
            f"QWidget#previewStatusBar{{background:{t['panel_bg']};"
            f"border-top:1px solid {t['vdim']};}}"
            f"QLabel{{color:{t['dim']};background:transparent;font-size:11px;}}")
