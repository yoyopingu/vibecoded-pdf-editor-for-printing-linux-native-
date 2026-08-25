"""
MainWindow: the sidebar, and what it switches between — the viewer and the
tool panels.
"""
import sys, os
from PyQt6.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget, QLabel, QFrame, QFileDialog, QMessageBox, QPushButton, QScrollArea
from PyQt6.QtCore import Qt, pyqtSignal
from tools.i18n import tr, set_language, get_language
from tools.branding import APP_NAME, APP_TAGLINE, app_title, versioned
from tools.viewer.panel import PageViewerPanel
from tools.viewer.tab import PdfTab
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
from tools.shell.protokoll import ProtokollWindow, notify
from tools.shell.statusbar import StatusBar
from tools.shell.style import apply_theme_globally
from tools.shell.titlebar import NavBtn, TitleBar
from tools.shell.tools_toggle import ToolsToggle


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


class SidebarHost:
    """One sidebar slot, three tenants — one entry point, one scroll surface.

    The 224px tool column is a slot, not just a tool list: the list by default,
    ManagePanel's operations in manage mode, Layout's staging sections in the
    layout view, and nothing for the merge preview. Today those three behaviours
    arrived through three ad-hoc protocols — a hide/show pair, a mount/unmount
    pair, and a direct call from _switch — that all reached for the same widgets
    and left no room for the concept's Werkzeuge toggle. This class owns the
    slot and folds them into a single `mount(view, widget)`.

    `view` is the requesting view's token:
        "tool_list"  — the default / preview (the list, always open),
        "manage"     — ManagePanel's operations (widget mounted),
        "layout"     — the layout view's staging controls (widget mounted),
        "tool"       — a tool's settings panel (widget mounted, Phase 5),
        "merge"      — the merge preview (no replacement; the list steps aside).

    The slot is the concept's single scroll surface (`.toolscroll`): a
    Werkzeuge toggle (shown only in manage/layout) above one QScrollArea whose
    content holds either the list, the mounted widget, or the mounted widget
    with the list APPENDED below it — never a second, nested scroll of its own.
    """

    def __init__(self, slot, tool_list):
        self._slot = slot
        self._tool_list = tool_list
        self._extra = None
        self._view = None
        self._open = False
        self._guard = False

        lay = slot.layout()

        # The Werkzeuge toggle. It sits above the scroll surface and is visible
        # only in manage/layout — MainWindow's mount() calls decide that by view.
        self._toggle = ToolsToggle()
        self._toggle_box = QWidget()
        tbh = QHBoxLayout(self._toggle_box)
        tbh.setContentsMargins(6, 4, 6, 8)
        tbh.setSpacing(0)
        tbh.addWidget(self._toggle)
        self._toggle_box.setVisible(False)
        lay.addWidget(self._toggle_box)
        self._toggle.toggled.connect(self._on_toggle)

        # One scroll surface for the list and whatever is mounted beneath it.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content.setObjectName("toolscroll")
        clay = QVBoxLayout(self._content)
        clay.setContentsMargins(0, 0, 0, 8)
        clay.setSpacing(0)
        self._scroll.setWidget(self._content)
        lay.addWidget(self._scroll, 1)

        self._sync()

    def mount(self, view, widget=None):
        """Mount `view` (and its optional `widget`), resetting the toggle.

        A view change always resets the Werkzeuge toggle — the concept's
        `setView()` sets `toolsOpen = false`, so the list is never carried open
        across a switch."""
        self._view = view
        self._extra = widget
        self._set_open(False)
        self._sync()

    def unmount(self):
        """Back to the default: whatever is mounted is detached, the tool list
        returns."""
        self.mount("tool_list")

    def _on_toggle(self, checked):
        if self._guard:
            return
        self._open = checked
        self._sync()

    def _set_open(self, on):
        self._guard = True
        try:
            self._toggle.setChecked(on)
        finally:
            self._guard = False
        self._open = bool(on)

    def _sync(self):
        """Re-derive what the column shows from the current view and toggle.

        The content layout is rebuilt from scratch each time — it holds at most
        two widgets (the mounted content and, when the toggle is open, the tool
        list appended after it), so clearing and re-adding is the whole of it.
        """
        clay = self._content.layout()
        while clay.count():
            it = clay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()

        view = self._view
        self._toggle_box.setVisible(view in ("manage", "layout"))
        list_visible = (view in ("tool_list", "preview")
                        or (self._open and view in ("manage", "layout")))

        if view in ("manage", "layout", "tool") and self._extra is not None:
            clay.addWidget(self._extra)
            self._extra.show()
        if list_visible:
            clay.addWidget(self._tool_list)
            self._tool_list.show()


class MainWindow(QMainWindow):
    def __init__(self, open_file=None, open_files=None):
        super().__init__()
        self.setWindowTitle(app_title())
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1000, 640)
        self.resize(1280, 760)
        self._protokoll = None   # the Protokoll window, built on first open
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
        self._sidebar = sidebar
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
        self._layout_panel.set_single_source(
            lambda: self.viewer._current().single
            if self.viewer._current() else None)
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
        # Tools are no longer stack pages: picking one morphs the whole sidebar
        # into its settings panel, so the main stack keeps only the viewer (0)
        # and Layout (1). Each panel is instantiated once and reused, keyed by
        # the sidebar label.
        self._tool_panels = {}
        self._tool_btns = {}
        self._tool_return_ctx = None   # (stack idx, in_manage) a tool came from
        # Next free stack slot for any tool or plugin that keeps the old
        # full-page form as a stack page. The viewer is 0 and Layout is 1; the
        # counter is shared with the plugin loop below so the indices stay
        # contiguous and unique across both.
        idx = 2
        for group, entries in TOOL_GROUPS:
            gl = QLabel(tr(group).upper())
            gl.setObjectName("navGroup")
            gl.setContentsMargins(16, 13, 0, 3)
            tl.addWidget(gl)
            for label, PanelClass in entries:
                panel = PanelClass(self)
                btn = NavBtn(tr(label))
                if getattr(panel, "controls_widget", None) is not None:
                    btn.clicked.connect(lambda c, p=panel: self._mount_tool(p))
                    tl.addWidget(btn)
                    self._tool_panels[label] = panel
                    self._tool_btns[label] = btn
                    panel.back_requested.connect(self._back_to_tools)
                else:
                    # A tool without a controls_widget (e.g. Grayscale, a
                    # split-view tool) keeps the old full-page form as a stack
                    # page — don't break it.
                    btn.clicked.connect(lambda c, x=idx: self._switch(x))
                    tl.addWidget(btn); self._btns.append(btn)
                    self._stack.addWidget(panel)
                    idx += 1

        sep0 = QFrame(); sep0.setObjectName("separator")
        sep0.setFrameShape(QFrame.Shape.HLine)
        tl.addWidget(sep0)
        pm_panel = PluginManagerPanel(self)
        pm_btn = NavBtn(tr("Plugin-Manager"))
        pm_btn.clicked.connect(lambda c, p=pm_panel: self._mount_tool(p))
        tl.addWidget(pm_btn)
        self._tool_panels["Plugin-Manager"] = pm_panel
        self._tool_btns["Plugin-Manager"] = pm_btn
        pm_panel.back_requested.connect(self._back_to_tools)
        sb.addWidget(self._sidebar_slot, 1)
        self._sidebar_host = SidebarHost(self._sidebar_slot, self._tool_list)

        # Plugins
        plugins = discover_plugins()
        if plugins:
            sep = QFrame(); sep.setObjectName("separator")
            sep.setFrameShape(QFrame.Shape.HLine); tl.addWidget(sep)
            pl = QLabel(tr("PLUGINS")); pl.setObjectName("navGroup")
            pl.setContentsMargins(16, 13, 0, 3); tl.addWidget(pl)
            for plabel, PCls in plugins:
                pp = PCls(self)
                if getattr(pp, "controls_widget", None) is not None:
                    btn = NavBtn(plabel.strip())
                    btn.clicked.connect(lambda c, p=pp: self._mount_tool(p))
                    tl.addWidget(btn)
                    self._tool_btns[plabel.strip()] = btn
                    pp.back_requested.connect(self._back_to_tools)
                else:
                    # A plugin without a controls_widget keeps the old full-page
                    # form as a stack page — don't break it.
                    btn = NavBtn(plabel.strip())
                    btn.clicked.connect(lambda c, x=idx: self._switch(x))
                    tl.addWidget(btn); self._btns.append(btn)
                    self._stack.addWidget(pp)
                    idx += 1

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

        # Wire the shared sidebar slot into the page viewer. One entry point —
        # SidebarHost.mount(view, widget) — replaces the three protocols that
        # used to converge here (hide/show, mount/unmount, and _switch's own
        # direct call). "Seiten verwalten" mounts ManagePanel's operations,
        # Layout mounts its staging sections, and the merge preview just asks
        # the tool list to step aside.
        self.viewer.mount_sidebar = self._sidebar_host.mount
        self.viewer.sync_view_switch = self._sync_view_switch

        # Phase 3.1: the doc row (tabs + doc actions) belongs at the window
        # level, above (sidebar | body). The PageViewerPanel controller owns
        # the data + QTabBar + body (page area + empty state); MainWindow
        # only mounts doc_row in the central column between titlebar and the
        # sidebar|stacked row. The viewer itself sits in the stack as before;
        # its own body is rendered next to the sidebar.
        outer.insertWidget(1, self.viewer.doc_row)

        # ── Statusleiste (Fenster-Ebene, eine für alle Ansichten) ─────────────
        # Phase 2.4: the per-tab #infoBar is gone. One StatusBar under the body
        # carries the readings, the transient message and the ruler/zoom remote;
        # every view publishes on the bus and this bar reads it — it never
        # reaches down into a specific view. The zoom/ruler/preflight slots are
        # routed to the ACTIVE view of the ACTIVE tab.
        self._status_bar = StatusBar()
        self._status_bar.zoom_out_requested.connect(
            lambda: self._active_view_do("_zoom_out"))
        self._status_bar.zoom_in_requested.connect(
            lambda: self._active_view_do("_zoom_in"))
        self._status_bar.zoom_fit_requested.connect(
            lambda: self._active_view_do("_zoom_fit"))
        self._status_bar.ruler_toggled.connect(self._on_status_ruler_toggled)
        self._status_bar.preflight_requested.connect(
            lambda: self._active_view_do("_show_preflight"))
        # Phase 2.5: clicking the centre message opens the Protokoll window.
        self._status_bar.message_clicked.connect(self._show_protokoll)
        outer.addWidget(self._status_bar)

        # The viewer's transient status messages reach the bar through the bus:
        # notify() both logs them and publishes them for the bar to show.
        self.viewer.show_status = lambda msg: notify(msg)
        # Re-sync the bar's readings whenever the active tab or view changes.
        self.viewer.tabs.currentChanged.connect(self._resync_statusbar)
        # A tab switch while Layout is showing has to re-point the shared rail
        # (and its sheet column) at the newly active tab.
        self.viewer.tabs.currentChanged.connect(self._sync_layout_rail)

        self._switch(0)

    def _active_view_do(self, method):
        """Call `method` on the active tab's single-page view. The StatusBar is a
        remote control; the view owns the state."""
        tab = self.viewer._current()
        if tab is None:
            return
        fn = getattr(tab.single, method, None)
        if fn is not None:
            fn()

    def _on_status_ruler_toggled(self, on):
        tab = self.viewer._current()
        if tab is not None:
            tab.single._set_rulers_visible(bool(on))

    def _resync_statusbar(self, *_args):
        """The bar has no memory of its own: whoever is active now re-publishes
        its readings, and the default message follows the current view.

        Resolved through the tab bar's index rather than the page stack's
        currentWidget: this runs synchronously from the bar's currentChanged,
        before the proxy has made the new page current."""
        tab = self._active_tab()
        if tab is not None:
            tab.single.publish_status()
            self._status_bar.set_rulers_checked(tab.single._rulers_on)
        self._status_bar.set_default_message(self._view_default_message(tab))
        # A held tool result must not outlive the tool that produced it: the
        # view switch / back button is the tool exit, so the default returns
        # here even though set_default_message above honours the held flag.
        self._status_bar.clear_message()

    def _active_tab(self):
        idx = self.viewer.tabs.currentIndex()
        if 0 <= idx < self.viewer.tabs.count():
            w = self.viewer.tabs.widget(idx)
            if isinstance(w, PdfTab):
                return w
        return None

    def _view_default_message(self, tab=None):
        """What the centre message falls back to for the current view (concept
        `msgs`, docs/gui-concept.html). Empty means "nothing to say yet"."""
        if tab is None:
            tab = self._active_tab()
        idx = self._stack.currentIndex()
        if idx == 0:
            if tab is not None and tab.in_manage_mode():
                return tr("Seiten auswählen — Aktionen links, Entf zum Löschen.")
            return ""
        if idx == 1:
            return tr("Vorschau zeigt Zuschneiden + Anordnung — Ausführen wendet beide an.")
        return ""

    def _show_tool_panel(self, label):
        """Bring a tool panel forward by the name it carries in the sidebar."""
        panel = self._tool_panels.get(label)
        if panel is not None:
            self._mount_tool(panel)

    def _mount_tool(self, panel):
        """Morph the whole sidebar into `panel`'s settings, keeping the main
        area on the view it was picked from — the viewer preview, the manage
        grid, or the layout sheets — never jumping it to the preview.

        The view a tool is picked from is remembered so the "‹ Werkzeuge" back
        button can hand the main area back to exactly that view."""
        stack_idx = self._stack.currentIndex()
        tab = self.viewer._current()
        in_manage = bool(tab is not None and tab.in_manage_mode())
        # A tool is only ever picked from a main-area view (preview/manage on
        # stack 0, layout on stack 1). If the stack is somewhere else — e.g. a
        # grayscale full-page form, whose sidebar still offers the other tools —
        # fall back to the viewer preview rather than mis-remembering it.
        if stack_idx not in (0, 1):
            stack_idx, in_manage = 0, False
            self._switch(0)
        self._tool_return_ctx = (stack_idx, in_manage)
        # Only the sidebar morphs; the main stack keeps showing whatever view
        # it was already in.
        self._sidebar_host.mount("tool", panel.controls_widget)
        for label, p in self._tool_panels.items():
            if p is panel:
                btn = self._tool_btns.get(label)
                if btn is not None:
                    btn.set_active(True)
                break
        # A tool's settings column is wider than the 224px preview sidebar
        # (its controls were built for a full-width panel). Widen the sidebar
        # to fit the tool so its fields are not clipped, and give it back to
        # the narrow list when the tool is dismissed.
        want = panel.controls_widget.sizeHint().width() + 28
        self._sidebar.setFixedWidth(max(224, min(want, 420)))
        self._sync_view_switch()

    def _back_to_tools(self):
        """The "‹ Werkzeuge" back button: unmount the tool and return to the
        view it was picked from — the viewer preview, the manage grid, or the
        layout sheets — restoring that view's sidebar content in the process."""
        for btn in self._tool_btns.values():
            btn.set_active(False)
        self._sidebar.setFixedWidth(224)
        ctx = self._tool_return_ctx
        self._tool_return_ctx = None
        stack_idx, in_manage = ctx if ctx is not None else (0, False)
        if stack_idx == 1:
            # Came from Layout: re-enter it (mounts its staging controls and
            # re-points the shared rail), leaving the main area on the sheets.
            self._switch(1)
            return
        if in_manage:
            # Came from the manage grid (stack 0, tab in manage mode): keep the
            # main area on the grid and put ManagePanel's operations back in the
            # sidebar — not the tool list.
            tab = self.viewer._current()
            if tab is not None and tab.in_manage_mode() and \
                    tab._manage_panel is not None:
                self._sidebar_host.mount("manage", tab._manage_panel)
                self._sync_view_switch()
                return
        # Default (and fallback when nothing was remembered): the preview list.
        self._switch(0)

    def _switch(self, idx: int):
        # _btns carries a None where a stack page is reached by the view switch
        # rather than by a nav button (the viewer, and Layout). Tools are not
        # stack pages — their buttons are highlighted by _mount_tool, so every
        # stack switch clears them.
        for btn in self._tool_btns.values():
            btn.set_active(False)
        for i, btn in enumerate(self._btns):
            if btn is not None:
                btn.set_active(i == idx)
        self._stack.setCurrentIndex(idx)
        # Layout mounts its staging sections into the tool column; every other
        # page (the viewer included) gets the tool list back — "Seiten
        # verwalten" then mounts ManagePanel over that on its own, via the
        # viewer's enter/exit-manage calls.
        if idx == 1:
            self._sidebar_host.mount("layout", self._layout_panel.controls_widget)
            # The shared navigation rail is a child of the active tab's body,
            # which is NOT visible in the layout view — reparent it into the
            # layout's rail host, and hand the rail back when we leave.
            self._sync_layout_rail()
        else:
            self._unmount_layout_rail()
            self._sidebar_host.mount("tool_list")
        self._sync_view_switch()

    def _sync_layout_rail(self, *_args):
        """Keep the shared rail + sheet column pointed at the active tab while
        Layout is showing. Called when entering Layout and on every tab switch."""
        if self._stack.currentIndex() != 1:
            return
        cur = self.viewer._current()
        if cur is self._layout_rail_tab():
            return
        self._detach_layout_rail(self._layout_rail_tab())
        self._attach_layout_rail(cur)

    def _layout_rail_tab(self):
        return getattr(self, "_layout_rail_tab_ref", None)

    def _attach_layout_rail(self, tab):
        lp = self._layout_panel
        self._layout_rail_tab_ref = tab
        # Clear any column left in the host by a tab that closed while mounted
        # (its SinglePageView is gone, so its nav column cannot be returned to it).
        host_lay = lp.rail_host.layout()
        while host_lay.count():
            it = host_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        if tab is None or getattr(tab, "_nav_col", None) is None:
            lp.rail_host.setVisible(False)
            lp.sheet_rail.single = None
            lp._sheetwrap.rebuild()
            return
        lp.sheet_rail.single = tab.single
        host_lay.addWidget(tab._nav_col)
        lp.rail_host.setVisible(True)
        tab.single.rail_delegate = lp.sheet_rail
        tab.single.nav_scroll_mode(True)
        self._layout_bar_conn = lp._sheetwrap.verticalScrollBar().valueChanged.connect(
            lp.sheet_rail.sync)
        lp.sheet_rail.sync()
        lp.sheet_rail.rail_go_to(tab.single.current_page + 1)
        lp._sheetwrap.rebuild()

    def _detach_layout_rail(self, tab):
        """Hand the rail back to `tab`'s own layout, and the preview's rail
        delegate back to the single-page view."""
        if tab is None:
            return
        lp = self._layout_panel
        try:
            lp._sheetwrap.verticalScrollBar().valueChanged.disconnect(
                self._layout_bar_conn)
        except (TypeError, AttributeError):
            pass
        try:
            tab.single.rail_delegate = None
            tab.single.nav_scroll_mode(tab.single._continuous)
        except RuntimeError:
            return    # tab already closed while Layout was showing
        if getattr(tab, "_nav_col", None) is not None:
            try:
                tab._nav_col.setParent(tab)
                tab.layout().addWidget(tab._nav_col)
            except RuntimeError:
                pass

    def _unmount_layout_rail(self):
        """Leaving the Layout view entirely: detach whatever tab is mounted."""
        self._detach_layout_rail(self._layout_rail_tab())
        self._layout_rail_tab_ref = None
        self._layout_panel.rail_host.setVisible(False)

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
        self._resync_statusbar()

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

    def _show_protokoll(self):
        """Open the Protokoll window — the session's log of every message.

        One window, built lazily and reused. Both entry points land here: the
        Hilfe ▸ "Logs anzeigen" menu item and a click on the status bar's
        centre line (decision 2).
        """
        if self._protokoll is None:
            self._protokoll = ProtokollWindow(self)
        self._protokoll.refresh()
        self._protokoll.show()
        self._protokoll.raise_()
        self._protokoll.activateWindow()

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
        if lang == get_language():
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr("Sprache ändern"))
        box.setText(tr("Die Änderung der Sprache wird erst nach einem Neustart wirksam."))
        box.setInformativeText(tr("App jetzt neu starten?"))
        btn_now = box.addButton(tr("Jetzt neu starten"), QMessageBox.ButtonRole.AcceptRole)
        btn_later = box.addButton(tr("Beim nächsten Start"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(btn_now)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_now:
            set_language(lang)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        elif clicked is btn_later:
            set_language(lang)
        else:
            # Cancel: the menu checkmark was flipped by the triggered action,
            # so put it back to the language that is actually persisted.
            pass
        self._title_bar.refresh_language_checks()

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
