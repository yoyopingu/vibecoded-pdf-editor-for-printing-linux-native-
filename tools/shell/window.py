"""
MainWindow: the sidebar, and what it switches between — the viewer and the
tool panels.
"""
import sys, os
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QLabel, QFrame, QFileDialog, QMessageBox
from PyQt6.QtCore import Qt
from tools.i18n import tr, set_language
from tools.viewer.panel import PageViewerPanel
from tools.plugin_manager import PluginManagerPanel, discover_plugins
from tools.panels.colour_profile import ColourProfilePanel
from tools.panels.compress import CompressPanel
from tools.panels.crop_resize import CropResizePanel
from tools.panels.forms import FormsPanel
from tools.panels.grayscale import GrayscalePanel
from tools.panels.img_pdf import ImgPdfPanel
from tools.panels.impose import ImposePanel
from tools.panels.layers import LayersPanel
from tools.panels.nup import NUpPanel
from tools.panels.ocr import OcrPanel
from tools.panels.page_numbers import PageNumbersPanel
from tools.panels.preflight import PreflightPanel
from tools.shell.settings import AppearanceDialog, GeneralDialog, PerformanceDialog
from tools.shell.style import apply_theme_globally
from tools.shell.titlebar import NavBtn, TitleBar


TOOLS = [
    ("N-Up Layout",               NUpPanel),
    ("Broschüre / Ausschießen",   ImposePanel),
    ("Komprimieren",              CompressPanel),
    ("Zuschneiden / Skalieren",   CropResizePanel),
    ("Seitenzahlen",              PageNumbersPanel),
    ("Bild ↔ PDF",                ImgPdfPanel),
    ("Graustufen",                GrayscalePanel),
    ("Formulare / Reduzieren",    FormsPanel),
    ("OCR — Texterkennung",       OcrPanel),
    ("Druckvorstufenprüfung",     PreflightPanel),
    ("Ebenen (OCG)",              LayersPanel),
    ("Farbprofil / CMYK",         ColourProfilePanel),
    ("Plugin-Manager",            PluginManagerPanel),
]


class MainWindow(QMainWindow):
    def __init__(self, open_file=None, open_files=None):
        super().__init__()
        self.setWindowTitle(tr("CopyShop PDF Suite"))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1000, 640)
        self.resize(1280, 760)
        self._build()
        # The startup file is opened here, synchronously, on purpose. Deferring
        # it into the event loop spends the loop's first turn blocked on the
        # open — and a launch that forwards a file in exactly that window has
        # its connection dropped, so that file never arrives. Better to finish
        # opening before the loop starts serving anything.
        if open_file:
            self._switch(0)
            self.viewer.open_file(open_file)
        elif open_files:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(100, lambda: self._open_multi(open_files))

    def open_paths(self, paths, activation_token=""):
        """Open files in THIS window — one file becomes a tab, several go through
        the merge preview. Called when another launch forwards its files to the
        running instance (see _listen_for_open_requests).

        Raises the window exactly once, carrying the launcher's activation
        token. It used to be raised twice — once by the receiver with the token
        and again here without one — and that second, unauthenticated request
        landing right behind the good one is what left the window merely
        blinking in the task bar instead of coming forward."""
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            self._raise_to_front(activation_token)
            return
        self._raise_to_front(activation_token)
        # Let the raise reach the compositor before anything slow runs. Opening
        # a non-PDF shells out to LibreOffice for seconds on this very thread,
        # and a frozen window cannot come forward.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._open_forwarded(paths))

    def _open_forwarded(self, paths):
        if len(paths) == 1:
            self._switch(0)
            self.viewer.open_file(paths[0])
        else:
            self._open_multi(paths)

    def _raise_to_front(self, activation_token=""):
        """Bring this window forward when another launch hands us its files.

        On Wayland a compositor ignores raise_()/activateWindow() from a process
        the user did not just interact with — that is focus-stealing prevention,
        and it is why the file opened silently in the background. The launching
        process is given an XDG activation token by the file manager; it hands
        that token over with the paths, and Qt's Wayland plugin consumes it from
        the environment on requestActivate(). X11 ignores the token and the
        plain raise still works there."""
        if activation_token:
            os.environ["XDG_ACTIVATION_TOKEN"] = activation_token
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        handle = self.windowHandle()
        if handle is not None:
            handle.requestActivate()
        # If the compositor still refuses, at least mark the task bar entry so
        # the window is not simply lost.
        QApplication.alert(self)
        os.environ.pop("XDG_ACTIVATION_TOKEN", None)

    def _open_multi(self, files):
        """Several files at once go straight to the merge preview, which offers
        both "merge" and "open separately".

        There used to be a modal chooser in front of it. Clicking its merge
        button faster than the preview could be built queued the click again
        and stacked up one merge tab per click, and confirming a second merge
        while the first was still converting destroyed a running QThread —
        an instant abort. The preview is the only step now."""
        from tools.multi_open import classify
        files = [f for f in files if os.path.isfile(f) and classify(f)]
        if not files:
            return
        self._switch(0)
        if len(files) == 1:
            self.viewer.open_file(files[0])
        else:
            self.viewer.show_merge_tab(files)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Custom Title Bar ──────────────────────────────────────────────────
        self._title_bar = TitleBar(self)
        outer.addWidget(self._title_bar)

        # ── Body (sidebar + main) ─────────────────────────────────────────────
        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addWidget(body, 1)

        # ── Seitenleiste ─────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 6, 0, 0)
        sb.setSpacing(0)

        self._btns  = []
        self._stack = QStackedWidget()

        # Page Viewer Button (hervorgehoben)
        vb = NavBtn(tr("Seiten-Viewer"), viewer=True)
        vb.clicked.connect(lambda: self._switch(0))
        sb.addWidget(vb); self._btns.append(vb)

        self.viewer = PageViewerPanel()
        self._stack.addWidget(self.viewer)
        self.viewer.tab_opened.connect(lambda: self._switch(0))
        self.viewer.switch_to_viewer   = lambda: self._switch(0)
        self.viewer.get_main_stack_idx = lambda: self._stack.currentIndex()
        self.viewer.restore_main_idx   = lambda idx: self._switch(idx)

        # Tools
        tl = QLabel("  " + tr("WERKZEUGE"))
        tl.setObjectName("sectionLabel")
        tl.setContentsMargins(14, 8, 0, 2)
        sb.addWidget(tl)

        for i, (label, PanelClass) in enumerate(TOOLS):
            btn = NavBtn(tr(label))
            idx = i + 1
            btn.clicked.connect(lambda c, x=idx: self._switch(x))
            sb.addWidget(btn); self._btns.append(btn)
            self._stack.addWidget(PanelClass())

        # Plugins
        plugins = discover_plugins()
        if plugins:
            sep = QFrame(); sep.setObjectName("separator")
            sep.setFrameShape(QFrame.Shape.HLine); sb.addWidget(sep)
            pl = QLabel("  " + tr("PLUGINS")); pl.setObjectName("sectionLabel")
            pl.setContentsMargins(14, 10, 0, 4); sb.addWidget(pl)
            base = len(TOOLS) + 1
            for pi, (plabel, PCls) in enumerate(plugins):
                btn = NavBtn(plabel.strip())
                idx = base + pi
                btn.clicked.connect(lambda c, x=idx: self._switch(x))
                sb.addWidget(btn); self._btns.append(btn)
                self._stack.addWidget(PCls())

        # ── Version am unteren Rand ───────────────────────────────────────────
        sb.addStretch()

        ver = QLabel(tr("v3.0  —  open source"))
        ver.setObjectName("dimLabel"); ver.setContentsMargins(14, 0, 0, 8)
        sb.addWidget(ver)

        root.addWidget(sidebar)
        self._sidebar = sidebar   # keep reference for manage-mode hide/show

        # ── Hauptbereich ─────────────────────────────────────────────────────
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)
        main_col.addWidget(self._stack)

        wrapper = QWidget(); wrapper.setLayout(main_col)
        root.addWidget(wrapper, 1)

        # Wire sidebar hide/show into the page viewer
        self.viewer.hide_sidebar = lambda: self._sidebar.setVisible(False)
        self.viewer.show_sidebar = lambda: self._sidebar.setVisible(True)

        self._switch(0)

    def _switch(self, idx: int):
        for i, btn in enumerate(self._btns):
            btn.set_active(i == idx)
        self._stack.setCurrentIndex(idx)

    def _open_dialog(self):
        # Every open path offers the same formats. This dialog used to filter to
        # *.pdf even though the viewer converts images and Office documents on
        # open, so the app hid files it was perfectly able to handle.
        from tools.multi_open import file_dialog_filter
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Datei öffnen"), "", file_dialog_filter())
        if path:
            self._switch(0)
            self.viewer.open_file(path)

    def _open_multi_dialog(self):
        from tools.multi_open import file_dialog_filter
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Mehrere Dateien öffnen"), "", file_dialog_filter())
        if paths:
            if len(paths) == 1:
                self._switch(0)
                self.viewer.open_file(paths[0])
            else:
                self._open_multi(paths)

    def _show_about(self):
        QMessageBox.about(
            self,
            tr("Über CopyShop PDF Suite"),
            "<b>CopyShop PDF Suite v3</b><br><br>"
            + tr("Professionelles PDF-Werkzeug für Copyshops und Druckvorstufe.") + "<br><br>"
            + tr("Entwickelt mit Python · PyQt6 · pypdfium2 · pikepdf · pypdf") + "<br><br>"
            "<i>" + tr("open source") + "</i>"
        )

    def _set_language(self, lang: str):
        set_language(lang)
        import os
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _open_appearance(self):
        dlg = AppearanceDialog(self)
        dlg.theme_changed.connect(self._apply_theme)
        dlg.exec()

    def _open_performance(self):
        PerformanceDialog(self).exec()

    def _open_general(self):
        GeneralDialog(self).exec()

    def _apply_theme(self, theme: str):
        apply_theme_globally(theme)
