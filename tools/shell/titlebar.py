"""
The frameless window's own chrome — the menu bar, the drag-to-move, and
the window buttons.
"""
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt, QPoint, QSize
from PyQt6.QtGui import QKeySequence, QAction
from tools.app_state import theme_color
from tools.i18n import tr, get_language
from tools.branding import APP_NAME, app_title
from tools.shell.icons import icon


class NavBtn(QPushButton):
    def __init__(self, text, viewer=False, parent=None, icon_name=None):
        super().__init__(text, parent)
        if icon_name:
            self.setIcon(icon(icon_name, colour=theme_color("DIM"), size=16))
            self.setIconSize(QSize(18, 18))
        self.setObjectName("viewerBtn" if viewer else "navBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("active", "false")

    def set_active(self, active: bool):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self); self.style().polish(self)


class TitleBar(QWidget):
    """Frameless custom title bar: drag area + menu bar + window controls."""

    def __init__(self, window: "MainWindow", parent=None):
        super().__init__(parent)
        self._win = window
        self._drag_pos = None
        self.setObjectName("titleBar")
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(0)

        # App title
        title = QLabel(app_title())
        title.setObjectName("titleBarLabel")
        layout.addWidget(title)

        layout.addSpacing(16)

        # Menu bar embedded in title bar
        self.menu_bar = self._build_menu()
        layout.addWidget(self.menu_bar)

        layout.addStretch()

        # The theme toggle — the concept's .tb-btn.theme, before the window
        # buttons (gui-concept.html:648-651). It flips the app between the dark
        # and light themes, persisted in AppSettings, and its icon shows what
        # clicking it will switch TO (sun → light, moon → dark).
        self._theme_btn = QPushButton()
        self._theme_btn.setObjectName("themeBtn")
        self._theme_btn.setToolTip(tr("Thema wechseln"))
        self._theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_btn.setFixedSize(42, 42)
        self._theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self._theme_btn)

        # Window controls
        self._win_btns = []
        for name, tip, slot in [
            ("min",   "Minimieren",    window.showMinimized),
            ("max",   "Maximieren",    self._toggle_max),
            ("close", "Schließen",     window.close),
        ]:
            btn = QPushButton()
            btn.setObjectName("titleBarBtn")
            btn.setToolTip(tr(tip))
            btn.setFixedSize(42, 42)
            btn.clicked.connect(slot)
            layout.addWidget(btn)
            self._win_btns.append((btn, name))
        self._sync_theme_icons()

    def _toggle_theme(self):
        """Flip the persisted theme and push it everywhere (stylesheets, viewer
        palette, every registered panel). The toggle's own icon follows."""
        from tools.shell.settings import AppSettings
        s = AppSettings.get()
        new = "light" if s.theme() == "dark" else "dark"
        s.set_theme(new)
        self._win._apply_theme(new)

    def _sync_theme_icons(self):
        """Re-draw every drawn icon in the title bar for the live theme."""
        from tools.shell.settings import AppSettings
        cur = AppSettings.get().theme()
        # Dark chrome shows the sun (click for light), light shows the moon.
        self._theme_btn.setIcon(icon(
            "sun" if cur == "dark" else "moon", colour=theme_color("DIM"), size=16))
        self._theme_btn.setIconSize(QSize(16, 16))
        for btn, name in self._win_btns:
            btn.setIcon(icon(name, colour=theme_color("DIM"), size=16))
            btn.setIconSize(QSize(16, 16))

    def _build_menu(self):
        from PyQt6.QtWidgets import QMenuBar
        mb = QMenuBar(self)
        mb.setObjectName("titleMenuBar")

        # Datei
        menu_file = mb.addMenu(tr("Datei"))
        act_open = QAction(tr("Datei öffnen…"), self)
        act_open.setShortcut(QKeySequence("Ctrl+O"))
        act_open.triggered.connect(self._win._open_multi_dialog)
        menu_file.addAction(act_open)
        act_multi = QAction(tr("Mehrere Dateien öffnen…"), self)
        act_multi.triggered.connect(self._win._open_multi_dialog)
        menu_file.addAction(act_multi)
        menu_file.addSeparator()
        # Saving belongs to the document, not to the page manager — it used to
        # live only in that sidebar, so it was unreachable from the normal view
        # even though Strg+S was already wired up there.
        # Resolved when triggered, not now: the title bar is built before
        # MainWindow creates .viewer.
        act_save = QAction(tr("Speichern"), self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(lambda: self._win.viewer._save_current())
        menu_file.addAction(act_save)
        act_save_as = QAction(tr("Speichern unter…"), self)
        act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_save_as.triggered.connect(lambda: self._win.viewer._save_as_current())
        menu_file.addAction(act_save_as)
        menu_file.addSeparator()
        act_quit = QAction(tr("Beenden"), self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self._win.close)
        menu_file.addAction(act_quit)

        # Einstellungen
        menu_settings = mb.addMenu(tr("Einstellungen"))
        act_appear = QAction(tr("Darstellung…"), self)
        act_appear.triggered.connect(self._win._open_appearance)
        menu_settings.addAction(act_appear)
        act_perf = QAction(tr("Leistung…"), self)
        act_perf.setShortcut(QKeySequence("Ctrl+,"))
        act_perf.triggered.connect(self._win._open_performance)
        menu_settings.addAction(act_perf)
        act_prepress = QAction(tr("Druckvorstufe…"), self)
        act_prepress.triggered.connect(self._win._open_prepress)
        menu_settings.addAction(act_prepress)
        act_general = QAction(tr("Allgemein…"), self)
        act_general.triggered.connect(self._win._open_general)
        menu_settings.addAction(act_general)

        # Sprache submenu
        menu_lang = menu_settings.addMenu(tr("Sprache"))
        self._act_de = QAction(tr("Deutsch"), self)
        self._act_de.setCheckable(True)
        self._act_de.setChecked(get_language() == "de")
        self._act_de.triggered.connect(lambda: self._win._set_language("de"))
        menu_lang.addAction(self._act_de)
        self._act_en = QAction(tr("English"), self)
        self._act_en.setCheckable(True)
        self._act_en.setChecked(get_language() == "en")
        self._act_en.triggered.connect(lambda: self._win._set_language("en"))
        menu_lang.addAction(self._act_en)

        # Hilfe
        menu_help = mb.addMenu(tr("Hilfe"))
        act_log = QAction(tr("Logs anzeigen"), self)
        act_log.triggered.connect(self._win._show_protokoll)
        menu_help.addAction(act_log)
        menu_help.addSeparator()
        act_about = QAction(tr("Über {p0}").format(p0=APP_NAME), self)
        act_about.triggered.connect(self._win._show_about)
        menu_help.addAction(act_about)

        return mb

    def refresh_language_checks(self):
        """Sync the language menu checkmarks with the persisted language."""
        lang = get_language()
        self._act_de.setChecked(lang == "de")
        self._act_en.setChecked(lang == "en")

    def _toggle_max(self):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            if self._win.isMaximized():
                ratio = e.globalPosition().x() / max(1, self._win.width())
                self._win.showNormal()
                self._drag_pos = QPoint(int(self._win.width() * ratio), self.height() // 2)
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()
