"""
MainWindow: the sidebar, and what it switches between — the viewer and the
tool panels.
"""
import sys, os
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QLabel, QFrame, QFileDialog, QMessageBox, QPushButton
from PyQt6.QtCore import Qt, pyqtSignal
from tools.i18n import tr, set_language
from tools.branding import APP_NAME, APP_TAGLINE, app_title, versioned
from tools.viewer.panel import PageViewerPanel
from tools.plugin_manager import PluginManagerPanel, discover_plugins
from tools.panels.colour_profile import ColourProfilePanel
from tools.panels.compress import CompressPanel
from tools.panels.forms import FormsPanel
from tools.panels.grayscale import GrayscalePanel
from tools.panels.layout_view import LayoutPanel
from tools.panels.ocr import OcrPanel
from tools.panels.page_numbers import PageNumbersPanel
from tools.panels.pdfx import PdfxPanel
from tools.panels.preflight import PreflightPanel
from tools.shell.settings import AppearanceDialog, GeneralDialog, PerformanceDialog, PrepressDialog
from tools.shell.style import apply_theme_globally
from tools.shell.titlebar import NavBtn, TitleBar


# The tool list, grouped by what each tool acts on. Layout is not in it:
# N-Up, Broschüre and Zuschneiden/Skalieren became stages of the Layout view
# (tools/panels/layout_view.py), which runs the same functions they do. Their
# panels still exist and still work — they are simply not a sidebar entry any
# more, because the view does all three together and previews the result.
#
# Bild ↔ PDF is likewise absent: an image opened becomes a PDF on the way in,
# and the other direction is an export.
TOOL_GROUPS = [
    ("Farbe", [
        ("Graustufen",              GrayscalePanel),
        ("Farbprofil / CMYK",       ColourProfilePanel),
    ]),
    ("Inhalt", [
        ("Seitenzahlen",            PageNumbersPanel),
        ("Formulare / Reduzieren",  FormsPanel),
        ("OCR — Texterkennung",     OcrPanel),
    ]),
    ("Ausgabe", [
        ("Druckvorstufenprüfung",   PreflightPanel),
        ("PDF/X-Export",            PdfxPanel),
        ("Komprimieren",            CompressPanel),
    ]),
]

# Flat, in sidebar order, for the stack indexes. Layout is index 1 — the first
# thing after the viewer — because it is a view, not a tool.
TOOLS = ([("Layout", LayoutPanel)]
         + [entry for _grp, entries in TOOL_GROUPS for entry in entries]
         + [("Plugin-Manager", PluginManagerPanel)])


VIEW_PREVIEW = 0
VIEW_PAGES   = 1
VIEW_LAYOUT  = 2


class ViewSwitch(QWidget):
    """Vorschau · Seiten verwalten · Layout, as one segmented control.

    Sized to the labels rather than to thirds: "Seiten verwalten" is three
    times the word "Layout", and equal thirds would spend the difference on
    padding around the short one.
    """
    picked = pyqtSignal(int)

    LABELS = ("Vorschau", "Seiten verwalten", "Layout")

    def __init__(self, parent=None):
        super().__init__(parent)
        # Two layouts, on purpose. The outer one holds the control off the
        # sidebar's edges; the inner one is the rectangle itself, so its border
        # encloses all three segments instead of each segment carrying its own.
        outer = QHBoxLayout(self)
        outer.setContentsMargins(6, 8, 6, 8)
        self._box = QWidget()
        self._box.setObjectName("viewSwitch")
        outer.addWidget(self._box)

        lay = QHBoxLayout(self._box)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._segs = []
        for i, label in enumerate(self.LABELS):
            b = QPushButton(tr(label))
            b.setObjectName("viewSeg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _c, x=i: self.picked.emit(x))
            # Sized to its own label, as the concept has it: equal thirds would
            # spend on "Layout" the width "Seiten verwalten" needs.
            lay.addWidget(b, b.fontMetrics().horizontalAdvance(b.text()))
            self._segs.append(b)
        self.set_current(VIEW_PREVIEW)

    def set_current(self, which):
        for i, b in enumerate(self._segs):
            b.setChecked(i == which)


class MainWindow(QMainWindow):
    def __init__(self, open_file=None, open_files=None):
        super().__init__()
        self.setWindowTitle(app_title())
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
        sidebar.setFixedWidth(224)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 6, 0, 0)
        sb.setSpacing(0)

        self._btns  = []
        self._stack = QStackedWidget()

        # ── The three views ──────────────────────────────────────────────────
        # Vorschau, Seiten verwalten and Layout are three ways of looking at the
        # same document, so they are one switch rather than three entries in a
        # list of tools — the tools change the document, the views show it.
        self._view_switch = ViewSwitch()
        self._view_switch.picked.connect(self._pick_view)
        sb.addWidget(self._view_switch)

        self.viewer = PageViewerPanel()
        self._stack.addWidget(self.viewer)
        self.viewer.tab_opened.connect(lambda: self._switch(0))
        self.viewer.switch_to_viewer   = lambda: self._switch(0)
        self.viewer.get_main_stack_idx = lambda: self._stack.currentIndex()
        self.viewer.restore_main_idx   = lambda idx: self._switch(idx)

        # Layout is stack index 1 and has no NavBtn: the switch above is how it
        # is reached. A placeholder keeps _btns aligned with the stack.
        #
        # Only its preview goes into the stack. Its staging column is a second
        # tenant of the same 224px sidebar slot that lists tools everywhere
        # else — mounted in _switch(), the way "Seiten verwalten" mounts
        # ManagePanel there. That is the one column the concept draws: same
        # width, same rhythm, a different job depending on the view.
        self._btns.append(None)          # index 0 — the viewer
        self._layout_panel = TOOLS[0][1]()
        self._stack.addWidget(self._layout_panel.preview_widget)
        self._btns.append(None)          # index 1 — Layout

        # ── The tools, grouped by what they act on ───────────────────────────
        # In their own container: the page manager and the merge preview each
        # bring a panel of their own, and two stacked lists of things to click
        # is one too many — but the view switch above must survive that, or the
        # way back out of those views disappears with the list.
        #
        # This column is a slot, not just the tool list: "Seiten verwalten"
        # mounts ManagePanel's operations here and Layout mounts its staging
        # sections here, in place of the tool list — one column, same width,
        # a different job depending on the view, exactly as the concept has it.
        self._sidebar_extra = None
        self._sidebar_slot = QWidget()
        self._sidebar_slot.setObjectName("sidebarSlot")
        slot_lay = QVBoxLayout(self._sidebar_slot)
        slot_lay.setContentsMargins(0, 0, 0, 0)
        slot_lay.setSpacing(0)

        self._tool_list = QWidget()
        self._tool_list.setObjectName("toolList")
        tl = QVBoxLayout(self._tool_list)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        idx = 2
        for group, entries in TOOL_GROUPS:
            gl = QLabel(tr(group).upper())
            gl.setObjectName("navGroup")
            gl.setContentsMargins(16, 13, 0, 3)
            tl.addWidget(gl)
            for label, PanelClass in entries:
                btn = NavBtn(tr(label))
                btn.clicked.connect(lambda c, x=idx: self._switch(x))
                tl.addWidget(btn); self._btns.append(btn)
                self._stack.addWidget(PanelClass())
                idx += 1

        sep0 = QFrame(); sep0.setObjectName("separator")
        sep0.setFrameShape(QFrame.Shape.HLine)
        tl.addWidget(sep0)
        pm_btn = NavBtn(tr("Plugin-Manager"))
        pm_btn.clicked.connect(lambda c, x=idx: self._switch(x))
        tl.addWidget(pm_btn); self._btns.append(pm_btn)
        self._stack.addWidget(PluginManagerPanel())
        slot_lay.addWidget(self._tool_list)
        sb.addWidget(self._sidebar_slot, 1)

        # Plugins
        plugins = discover_plugins()
        if plugins:
            sep = QFrame(); sep.setObjectName("separator")
            sep.setFrameShape(QFrame.Shape.HLine); tl.addWidget(sep)
            pl = QLabel(tr("PLUGINS")); pl.setObjectName("navGroup")
            pl.setContentsMargins(16, 13, 0, 3); tl.addWidget(pl)
            for plabel, PCls in plugins:
                idx += 1
                btn = NavBtn(plabel.strip())
                btn.clicked.connect(lambda c, x=idx: self._switch(x))
                tl.addWidget(btn); self._btns.append(btn)
                self._stack.addWidget(PCls())

        # The column is taller than the list, and the slack has to land
        # somewhere. Without this it went to the only children that can grow
        # vertically — the group headings — which took 103 px each for 33 px of
        # text and left every heading stranded 70 px above the tools under it.
        tl.addStretch(1)

        # ── Stand am unteren Rand ────────────────────────────────────────────
        # No addStretch() here: self._sidebar_slot above already carries the
        # stretch factor, so whatever is mounted in it — the tool list,
        # ManagePanel's operations, or Layout's staging sections — fills the
        # column down to this footer instead of leaving it pinned to its own
        # sizeHint with a gap of dead space underneath.

        # A version number told nobody anything — it belongs in the about box,
        # where somebody reporting a fault goes looking for it.
        beta = QLabel(tr("BETA"))
        beta.setObjectName("betaChip")
        beta.setContentsMargins(14, 0, 0, 10)
        sb.addWidget(beta)

        root.addWidget(sidebar)
        self._sidebar = sidebar   # keep reference for manage-mode hide/show

        # ── Hauptbereich ─────────────────────────────────────────────────────
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)
        main_col.addWidget(self._stack)

        wrapper = QWidget(); wrapper.setLayout(main_col)
        root.addWidget(wrapper, 1)

        # The status bar's preflight light offers a way through to the full
        # check; the viewer knows it wants the panel, not where the panel is.
        self.viewer.show_tool_panel = self._show_tool_panel

        # Wire the tool list's hide/show into the page viewer. The merge
        # preview brings its own sidebar and just needs the list out of the
        # way — no replacement content of its own — so it keeps this plain
        # pair rather than the mount/unmount below.
        self.viewer.hide_sidebar = lambda: self._tool_list.setVisible(False)
        self.viewer.show_sidebar = lambda: self._tool_list.setVisible(True)

        # "Seiten verwalten" mounts ManagePanel's operations into the same
        # slot Layout's staging sections use below — one column, swapped
        # content, per the concept.
        self.viewer.mount_sidebar_widget   = self._mount_sidebar_widget
        self.viewer.unmount_sidebar_widget = self._unmount_sidebar_widget
        self.viewer.sync_view_switch       = self._sync_view_switch

        self._switch(0)

    def _mount_sidebar_widget(self, widget):
        """Show `widget` in the tool column instead of the tool list — what
        "Seiten verwalten" does with ManagePanel's operations, and what
        _switch() below does with Layout's staging sections."""
        self._unmount_sidebar_widget()
        self._tool_list.setVisible(False)
        self._sidebar_extra = widget
        self._sidebar_slot.layout().addWidget(widget)
        widget.show()

    def _unmount_sidebar_widget(self):
        """Undo _mount_sidebar_widget(): detach whatever is mounted and bring
        the tool list back. Safe to call when nothing is mounted."""
        if self._sidebar_extra is not None:
            self._sidebar_slot.layout().removeWidget(self._sidebar_extra)
            self._sidebar_extra.setParent(None)
            self._sidebar_extra = None
        self._tool_list.setVisible(True)

    def _show_tool_panel(self, label):
        """Bring a tool panel forward by the name it carries in the sidebar."""
        for i, (name, _cls) in enumerate(TOOLS):
            if name == label:
                self._switch(i + 1)
                return

    def _switch(self, idx: int):
        # _btns carries a None where a stack page is reached by the view switch
        # rather than by a nav button (the viewer, and Layout).
        for i, btn in enumerate(self._btns):
            if btn is not None:
                btn.set_active(i == idx)
        self._stack.setCurrentIndex(idx)
        # Layout mounts its staging sections into the tool column; every other
        # page (the viewer included) gets the tool list back — "Seiten
        # verwalten" then mounts ManagePanel over that on its own, via the
        # viewer's enter/exit-manage calls.
        if idx == 1:
            self._mount_sidebar_widget(self._layout_panel.controls_widget)
        else:
            self._unmount_sidebar_widget()
        self._sync_view_switch()

    def _sync_view_switch(self):
        """Keep the segmented control showing where we actually are.

        The three views are not three stack pages: Seiten verwalten is a mode
        *inside* the viewer, so the switch has to read the viewer's state and
        not just the stack index."""
        idx = self._stack.currentIndex()
        if idx == 1:
            which = VIEW_LAYOUT
        elif idx == 0:
            tab = self.viewer._current()
            which = (VIEW_PAGES
                     if tab is not None and tab.in_manage_mode()
                     else VIEW_PREVIEW)
        else:
            which = -1          # a tool panel: none of the three is current
        self._view_switch.set_current(which)

    def _pick_view(self, which: int):
        """A click on the segmented control."""
        tab = self.viewer._current()
        in_manage = tab is not None and tab.in_manage_mode()
        if which == VIEW_LAYOUT:
            if in_manage:
                self.viewer._ensure_single_view()
            self._switch(1)
            return
        self._switch(0)
        if which == VIEW_PAGES and not in_manage:
            self.viewer._toggle_manage()
        elif which == VIEW_PREVIEW and in_manage:
            self.viewer._ensure_single_view()
        self._sync_view_switch()

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

    def _show_log_folder(self):
        """Open the folder holding the log and the native-crash file.

        The app is normally started from the desktop entry, so stderr is
        discarded and this is the only way to reach the record of a failure
        without knowing the XDG path by heart.
        """
        from tools.shell.crash_report import open_log_folder
        open_log_folder(self)

    def _show_about(self):
        QMessageBox.about(
            self,
            tr("Über {p0}").format(p0=APP_NAME),
            f"<b>{versioned()}</b><br><br>"
            + tr(APP_TAGLINE) + "<br><br>"
            + tr("Entwickelt mit Python · PyQt6 · pypdfium2 · pikepdf · pypdf") + "<br><br>"
            "<i>" + tr("open source") + "</i>"
        )

    def _set_language(self, lang: str):
        set_language(lang)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _open_appearance(self):
        dlg = AppearanceDialog(self)
        dlg.theme_changed.connect(self._apply_theme)
        dlg.scroll_changed.connect(self.viewer.set_continuous_scroll)
        dlg.exec()

    def _open_performance(self):
        PerformanceDialog(self).exec()

    def _open_prepress(self):
        PrepressDialog(self).exec()

    def _open_general(self):
        GeneralDialog(self).exec()

    def _apply_theme(self, theme: str):
        apply_theme_globally(theme)
