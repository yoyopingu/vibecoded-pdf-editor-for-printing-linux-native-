"""
The tab host — the widget the main window puts on screen.

Opening files, closing tabs and everything that has to happen when one closes:
the render work it started is cancelled, its thumbnails and page renders are
dropped, and the parsed document behind them is released. A loaded page of a
large PDF is hundreds of megabytes, so a tab that is gone must not keep one.
"""
import os, shutil, atexit, logging
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                             QTabBar, QStackedWidget, QFileDialog, QToolButton,
                             QApplication, QLineEdit, QMenu)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QEvent, QTimer, QSize, QPoint
from PyQt6.QtGui import QKeySequence, QShortcut, QCursor
from tools.app_state import AppState, theme_color
from tools.branding import APP_NAME
from tools.i18n import tr
from tools.render.caches import _FullPageCache, _ThumbnailCache, _set_active
from tools.shell.icons import icon
from tools.render.document_cache import release
from tools.viewer.empty_state import EmptyStateWidget
from tools.viewer.merge import MergeOrderWidget
from tools.viewer.find import FindBar
from tools.viewer.open_flow import MergeFlow
from tools.viewer.tab import PdfTab
from tools.theme import _register_themed


class _ElidedLabel(QLabel):
    """A label that shortens its own text rather than pushing the row wider.

    It shares the tab bar with everything else in the corner, so it is the part
    that has to give way — and a message cut off mid-glyph reads as a fault
    where an ellipsis reads as a message too long to show."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, max(0, self.width() - 2)))


class _TabHost(QObject):
    """QTabWidget surface that's actually a QTabBar + QStackedWidget.

    The doc row (concept .docbar) is the QTabBar at the top of the window — not
    the bottom of a QTabWidget that owns the body — so the tab pages live in a
    separate QStackedWidget beside the sidebar. This proxy mirrors the small
    surface every caller reaches (`panel.tabs.X` for count/widget/currentIndex/
    addTab/removeTab/setTabText/...) so the structural lift is invisible to
    open_flow, find, _ViewerKeyFilter and every test that drives `vp.tabs`."""

    currentChanged = pyqtSignal(int)

    def __init__(self, bar, pages, parent=None):
        super().__init__(parent)
        self._bar = bar
        self._pages = pages
        bar.currentChanged.connect(self._on_current_changed)
        # The per-tab close cross is a real QToolButton (styling the
        # QTabBar::close-button subcontrol makes Qt render nothing). It is shown
        # on the active tab and on hover; this filter keeps that in step with the
        # pointer. The bar is only ever filtered once; each button gets the same
        # filter so a pointer sitting on the button itself still counts as
        # hovering its tab.
        bar.installEventFilter(self)
        bar.currentChanged.connect(self._refresh_close_visibility)

    def _on_current_changed(self, idx):
        self.currentChanged.emit(idx)
        if 0 <= idx < self._pages.count():
            self._pages.setCurrentIndex(idx)
        self._refresh_close_visibility()

    def eventFilter(self, obj, event):
        if obj is self._bar or isinstance(obj, QToolButton):
            if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave,
                                QEvent.Type.MouseMove):
                self._refresh_close_visibility()
        return super().eventFilter(obj, event)

    def _hovered_tab(self):
        """The tab under the pointer, -1 when none. Works whether the pointer
        rests on the bar or on a close button (a child, still inside the bar's
        rect), so tabAt resolves it either way."""
        pos = self._bar.mapFromGlobal(QCursor.pos())
        if not self._bar.rect().contains(pos):
            return -1
        return self._bar.tabAt(pos)

    def _refresh_close_visibility(self):
        """Close cross: always on the active tab, on hover elsewhere."""
        cur = self._bar.currentIndex()
        hover = self._hovered_tab()
        for i in range(self._bar.count()):
            b = self._bar.tabButton(i, QTabBar.ButtonPosition.RightSide)
            if b is not None:
                b.setVisible(i == cur or i == hover)

    def _mk_close_button(self):
        """A 16 px close cross for one tab. It closes whichever tab it currently
        sits on (tabAt at click time), so it stays correct through reorders and
        removals without any index bookkeeping."""
        btn = QToolButton(self._bar)
        btn.setObjectName("tabCloseBtn")
        btn.setIcon(icon("close", colour="#8892a4", size=14))
        btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(18, 18)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda: self._bar.tabCloseRequested.emit(
            self._bar.tabAt(btn.mapTo(self._bar, QPoint(0, 0)))))
        btn.installEventFilter(self)
        return btn

    # surface -----------------------------------------------------------------

    def count(self): return self._bar.count()
    def currentIndex(self): return self._bar.currentIndex()
    def setCurrentIndex(self, i): self._bar.setCurrentIndex(i)
    def currentWidget(self): return self._pages.currentWidget()
    def widget(self, i): return self._pages.widget(i)
    def indexOf(self, w): return self._pages.indexOf(w)
    def addTab(self, w, label):
        self._pages.addWidget(w)
        idx = self._bar.addTab(label)
        # Make the new widget current in the same stance the caller would
        # expect of QTabWidget: the tab they just added is the active page.
        self._pages.setCurrentIndex(idx)
        self._bar.setTabData(idx, w)
        self._bar.setTabButton(idx, QTabBar.ButtonPosition.RightSide,
                               self._mk_close_button())
        self._refresh_close_visibility()
        return idx
    def removeTab(self, i):
        w = self._pages.widget(i)
        # Pages first: `_on_tab_changed` runs synchronously from currentChanged
        # below, and `_sync_sidebar` looks at `_pages.currentWidget()`. If the
        # bar removes the tab first, the page is still in the stack and
        # _on_tab_changed misreads it as the current merge/PdfTab.
        if i < self._pages.count():
            self._pages.removeWidget(w)
        self._bar.removeTab(i)
        self._refresh_close_visibility()
    def setTabText(self, i, t): self._bar.setTabText(i, t)
    def tabText(self, i): return self._bar.tabText(i)
    def setMovable(self, b): self._bar.setMovable(b)
    def setTabsClosable(self, b): self._bar.setTabsClosable(b)
    def setExpanding(self, b): self._bar.setExpanding(b)
    def tabCloseRequested(self): return self._bar.tabCloseRequested
    def tabBar(self): return self._bar
    def isVisible(self): return self._bar.isVisible()
    def setVisible(self, b): self._bar.setVisible(b)


class _ViewerKeyFilter(QObject):
    """
    Globaler QApplication-Event-Filter.
    Ctrl+Shift+O → Viewer einblenden + Einzelansicht ↔ Seiten verwalten
    Escape       → Viewer einblenden + immer zur Einzelansicht
    Tab wird NICHT abgefangen → Standard-Fokus-Traversal der Widgets.
    """
    def __init__(self, viewer_panel):
        super().__init__(viewer_panel)
        self._vp = viewer_panel

    def eventFilter(self, obj, event):
        # Stand down entirely while a modal dialog (print dialog, settings, file
        # picker, message box) is open, so its own widgets get Tab/Escape/zoom
        # keys for normal focus traversal instead of us hijacking them for the
        # background viewer.
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        # ShortcutOverride: Qt fragt Widget ob es die Taste übernehmen will.
        # event.accept() tells Qt "send this as KeyPress, not as shortcut".
        # We MUST return False here so Qt still sees the accepted state and
        # dispatches the subsequent KeyPress — returning True would eat the
        # ShortcutOverride entirely and no KeyPress would follow.
        if t == QEvent.Type.ShortcutOverride:
            k = event.key()
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            # Escape is claimed (exits the manage view). Tab is intentionally
            # NOT claimed anymore so it performs normal focus traversal between
            # input fields — the manage view moved to Ctrl+Shift+O.
            if k == Qt.Key.Key_Escape:
                focused = QApplication.focusWidget()
                if not isinstance(focused, QLineEdit):
                    event.accept()
                    return False
            # Qt reports Ctrl+Shift+Tab as Key_Backtab with Ctrl modifier
            if ctrl and k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_W,
                              Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                              Qt.Key.Key_Minus, Qt.Key.Key_0, Qt.Key.Key_1,
                              Qt.Key.Key_F):
                event.accept()
                return False
            if k == Qt.Key.Key_F3:
                event.accept()
                return False
            return False

        if t != QEvent.Type.KeyPress:
            return False

        k    = event.key()
        mods = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # Ctrl+Shift+O → toggle the pages overview (Manage view). Handled BEFORE
        # the text-field guard so it works globally, even while a field is
        # focused. This replaces the old plain-Tab binding, which hijacked Tab
        # everywhere and made normal field-to-field focus traversal impossible.
        if ctrl and shift and k == Qt.Key.Key_O:
            try:
                self._vp._toggle_manage()
            except Exception as exc:
                logging.error(f"_toggle_manage: {exc}", exc_info=True)
            return True

        # Strg+F opens the find field and F3 walks the results — both from
        # anywhere, including from inside the field itself, which is why they
        # are handled before the text-field guard below.
        if ctrl and k == Qt.Key.Key_F:
            try:
                self._vp.toggle_find()
            except Exception as exc:
                logging.error(f"toggle_find: {exc}", exc_info=True)
            return True
        if k == Qt.Key.Key_F3:
            try:
                self._vp._step_find(-1 if shift else +1)
            except Exception as exc:
                logging.error(f"_step_find: {exc}", exc_info=True)
            return True
        # Escape closes the find field before it does anything else: the field
        # has the keyboard at that moment, and this is the one thing a person
        # expects Escape to do while typing in a search box.
        if k == Qt.Key.Key_Escape and self._vp._find.box.isVisible():
            self._vp.set_find_visible(False)
            self._vp.focus_page_view()
            return True

        # Nie abfangen wenn ein Textfeld fokussiert ist
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return False

        # Ctrl+W → close current tab (like browser / Acrobat)
        if ctrl and k == Qt.Key.Key_W:
            try:
                idx = self._vp.tabs.currentIndex()
                if idx >= 0:
                    self._vp._close_tab(idx)
            except Exception as exc:
                logging.error(f"_close_tab: {exc}", exc_info=True)
            return True

        # Ctrl+Tab → forward, Ctrl+Shift+Tab (reported as Ctrl+Backtab) → backward
        if ctrl and k == Qt.Key.Key_Tab:
            try:
                self._vp._cycle_tab(forward=True)
            except Exception as exc:
                logging.error(f"_cycle_tab: {exc}", exc_info=True)
            return True
        if ctrl and k == Qt.Key.Key_Backtab:
            try:
                self._vp._cycle_tab(forward=False)
            except Exception as exc:
                logging.error(f"_cycle_tab: {exc}", exc_info=True)
            return True

        # ── Zoom shortcuts — work regardless of which widget has focus ──────
        # Ctrl+0=fit/reset, Ctrl+1=actual size, Ctrl++/= zoom in, Ctrl+- zoom out
        if ctrl and k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                          Qt.Key.Key_Minus, Qt.Key.Key_0, Qt.Key.Key_1):
            tab = self._vp._current()
            if tab:
                in_manage = (tab._stack.currentWidget() is not tab.single)
                if in_manage and tab._manage_panel:
                    # Zoom the thumbnail grid
                    grid = tab._manage_panel.grid
                    try:
                        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal): grid.zoom_in()
                        elif k == Qt.Key.Key_Minus:                   grid.zoom_out()
                        elif k == Qt.Key.Key_0:                       grid.zoom_reset()
                    except Exception as exc:
                        logging.error(f"manage zoom shortcut: {exc}", exc_info=True)
                else:
                    sv = tab.single
                    try:
                        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal): sv._zoom_in()
                        elif k == Qt.Key.Key_Minus:                   sv._zoom_out()
                        elif k == Qt.Key.Key_0:                       sv._zoom_fit()
                        elif k == Qt.Key.Key_1:                       sv._zoom_actual_size()
                    except Exception as exc:
                        logging.error(f"zoom shortcut: {exc}", exc_info=True)
            return True

        if k == Qt.Key.Key_Escape:
            try:
                self._vp._ensure_single_view()
            except Exception as exc:
                logging.error(f"_ensure_single_view: {exc}", exc_info=True)
            return True
        return False


class PageViewerPanel(QWidget):
    tab_opened = pyqtSignal()  # neuer Tab geöffnet
    tabs_changed = pyqtSignal()  # Tab hinzugefügt/entfernt

    def __init__(self, parent=None):
        super().__init__(parent)
        # Callbacks die MainWindow setzt:
        self.switch_to_viewer   = None   # lambda: main._switch(0)
        self.get_main_stack_idx = None   # lambda: main._stack.currentIndex()
        self.restore_main_idx   = None   # lambda idx: main._switch(idx)
        self.mount_sidebar   = None   # SidebarHost.mount(view, widget=None)
        self.sync_view_switch = None  # lambda: reread which of the 3 views is current
        self.show_status            = None   # lambda msg: window-level status bar
        self.open_multi_dialog = None  # lambda: main._open_multi_dialog() — the "+" open
        self.set_title_document = None  # lambda name: main._title_bar.set_document_name(name)
        self._pre_manage_idx    = None   # gespeicherter Stack-Index vor Manage-Modus
        self._pf_job      = None
        self._setup_ui()
        AppState.get().result_ready.connect(self._open_result_tab)
        # Globaler Tab/Escape-Filter
        self._key_filter = _ViewerKeyFilter(self)
        QApplication.instance().installEventFilter(self._key_filter)
        self.destroyed.connect(
            lambda: QApplication.instance().removeEventFilter(self._key_filter))

    def _toggle_manage(self):
        """
        Tab: Einzelansicht ↔ Seiten verwalten umschalten.
        Beim Hineingehen: aktuellen Stack-Index merken.
        Beim Herausgehen: zum gespeicherten Stack-Index zurückkehren.
        """
        if self._toggle_in_progress:
            return   # block re-entrant calls from rapid key presses
        self._toggle_in_progress = True
        try:
            self._toggle_manage_impl()
        finally:
            self._toggle_in_progress = False

    def _toggle_manage_impl(self):
        tab = self._current()
        # Wenn wir noch nicht im Viewer sind, müssen wir erst prüfen ob wir
        # gerade schon im Manage-Modus sind (Stack-Index 0 aber Manage aktiv)
        currently_in_manage = (tab is not None and
                               tab._stack.currentWidget() is not tab.single)

        if currently_in_manage:
            # → Manage verlassen und zurück zur vorherigen Ansicht
            self._exit_manage_layout()
            tab._exit_manage()
            if self._pre_manage_idx is not None and self.restore_main_idx:
                self.restore_main_idx(self._pre_manage_idx)
            self._pre_manage_idx = None
        else:
            # → Manage betreten: aktuellen Stack-Index speichern, Viewer zeigen
            if self.get_main_stack_idx:
                self._pre_manage_idx = self.get_main_stack_idx()
            if self.switch_to_viewer:
                self.switch_to_viewer()
            tab = self._current()   # nach switch_to_viewer neu holen
            if tab:
                def _on_exit():
                    self._exit_manage_layout()
                    if self.sync_view_switch:
                        self.sync_view_switch()
                try:
                    tab._enter_manage(on_exit=_on_exit)
                    self._enter_manage_layout(tab._manage_panel)
                    # Give the grid keyboard focus so arrow keys work immediately
                    if tab._manage_widget:
                        tab._manage_widget.setFocus()
                except Exception:
                    import traceback
                    logging.error(traceback.format_exc())
                    self._exit_manage_layout()  # always restore the tool column on failure
        # Reached by the Ctrl+Shift+O shortcut as well as the view switch —
        # the switch itself only resyncs when a click on it is what triggered
        # this, so it has to happen here too, or entering/leaving manage mode
        # from the keyboard leaves "Seiten verwalten" unhighlighted.
        if self.sync_view_switch:
            self.sync_view_switch()

    def _ensure_single_view(self):
        """Esc: immer zur Einzelansicht — Manage verlassen, Viewer zeigen, kein Zurück-Sprung."""
        if self._merge_widget is not None:
            self._exit_merge_preview()   # Esc exits the merge view (decision 3)
        if self.switch_to_viewer:
            self.switch_to_viewer()
        tab = self._current()
        if tab and tab._stack.currentWidget() is not tab.single:
            self._exit_manage_layout()
            tab._exit_manage()
        if self.sync_view_switch:
            self.sync_view_switch()
        self._pre_manage_idx = None   # Esc bricht den Rücksprung-Pfad ab

    def _cycle_tab(self, forward=True):
        """Ctrl+Tab / Ctrl+Shift+Tab: zum nächsten / vorherigen Tab wechseln."""
        n = self.tabs.count()
        if n < 2:
            return
        cur = self.tabs.currentIndex()
        nxt = (cur + (1 if forward else -1)) % n
        self.tabs.setCurrentIndex(nxt)

    def _setup_ui(self):
        # The panel owns its body (page area + empty state). It does NOT own
        # layout placement of its doc_row; MainWindow mounts that in the
        # window-level column above (sidebar | this_panel). Tests that drive
        # PageViewerPanel directly still resize this widget to see the body,
        # which is what they need to verify empty-state behaviour.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Shortcuts
        # Ctrl+S / Ctrl+Shift+S belong to the Datei menu actions, which are
        # window-scoped and so fire from a tool panel too. Registering them here
        # as well made both ambiguous, and Qt then delivers neither.
        sc_print = QShortcut(QKeySequence("Ctrl+P"), self)
        sc_print.activated.connect(self._print_current)

        _register_themed(self)

        # ── Dokumentleiste ───────────────────────────────────────────────────
        # Phase 3.1: the doc row is a window-level strip above (sidebar | stack).
        # The bar (QTabBar) lives in `doc_row`; the rendered tab pages live in
        # `_pages` (a QStackedWidget) inside the body row, beside the sidebar.
        # Every caller reaches `panel.tabs.X` — the _TabHost proxy mirrors the
        # QTabWidget surface they used to touch.
        self._bar = QTabBar()
        self._bar.setMovable(True)
        self._bar.setTabsClosable(True)
        self._bar.setExpanding(False)
        self._bar.setDrawBase(False)
        # The bar stays hidden until a tab is opened; the empty state occupies
        # the body instead. Phase 3.1 mirrors the pre-3.1 contract
        # (`vp.tabs.isVisible()` was False before any open).
        self._bar.setVisible(False)
        self._bar.tabCloseRequested.connect(self._close_tab)
        # The bar's currentChanged connects again below — *after* the proxy
        # _TabHost is built — so the proxy handler runs FIRST and updates
        # `_pages.currentWidget(...)` before `_on_tab_changed` reads it.

        self._pages = QStackedWidget()
        self._pages.setVisible(False)
        # When the proxy writes `_pages.setCurrentIndex` from inside the bar's
        # currentChanged slot, the stack emits its OWN currentChanged — and
        # that fires `_pages.stackChanged` after the bar's slot has already
        # read `_pages.currentWidget()` and called `_sync_sidebar`. The view
        # is then stale until Qt's event loop pumps. Connect the bar's
        # currentChanged through `_pages.setCurrentWidgetAfter()` so the
        # stack's currentWidget reflects the bar's intent by the time
        # `_on_tab_changed` runs.
        self.tabs = _TabHost(self._bar, self._pages, parent=self)
        # Connect AFTER the proxy so the page area is current by the time
        # `_on_tab_changed` reads it (it then runs `_sync_sidebar` against
        # the right widget).
        self._bar.currentChanged.connect(self._on_tab_changed)

        # Doc row host. The window's top-level layout adds this ABOVE the body.
        # Its size is fixed so the titlebar/docbar/body/statusstack rhythm holds
        # whether a tab is open or not.
        self.doc_row = QWidget()
        self.doc_row.setObjectName("docRow")
        dr_lay = QHBoxLayout(self.doc_row)
        dr_lay.setContentsMargins(0, 0, 0, 0)
        dr_lay.setSpacing(0)
        # The tab bar takes its natural width (not stretched): the "+" must sit
        # immediately to the right of the rightmost tab (concept .newtab), and a
        # stretch keeps the doc actions at the far end of the same row.
        dr_lay.addWidget(self._bar, 0)
        # The "+" — multi-select open (Ctrl+O). It belongs beside the last tab,
        # not at the far right with find/save/print (concept .newtab, gui-concept
        # #942). Built here, outside _build_doc_actions, so it hugs the tab bar.
        self._new_btn = QPushButton()
        self._new_btn.setObjectName("newtabBtn")
        self._new_btn.setIcon(icon("plus", colour=theme_color("DIM"), size=16))
        self._new_btn.setIconSize(QSize(16, 16))
        self._new_btn.setFixedSize(28, 28)
        self._new_btn.setToolTip(tr("Neuer Tab"))
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.clicked.connect(self._open_multi_dialog)
        dr_lay.addWidget(self._new_btn)
        dr_lay.addStretch(1)
        # The doc actions sit at the right end of the same row, exactly where
        # they lived in the cornerWidget of the old QTabWidget.
        self._doc_actions = self._build_doc_actions()
        dr_lay.addWidget(self._doc_actions, 0)

        # Keep the doc row in this widget's layout by default. MainWindow will
        # reparent it via setParent(), but tests that drive PageViewerPanel
        # directly get a working tab strip without needing MainWindow.
        layout.addWidget(self.doc_row, 0)

        # Body row: the tab pages, beside the sidebar.
        self._body = QWidget()
        self._body_layout = QHBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addWidget(self._pages, 1)

        # Before the first file, the page area is an empty sliver saying
        # nothing — fills it with a drop target, the two ways to start, and
        # up to four recently opened files. Swapped for the pages rather than
        # layered under them.
        self._empty_state = EmptyStateWidget()
        self._empty_state.open_requested.connect(self._open_via_dialog)
        self._empty_state.merge_requested.connect(self._merge_via_dialog)
        self._empty_state.file_chosen.connect(self.open_file)
        self._empty_state.files_dropped.connect(self._open_dropped)
        self._body_layout.addWidget(self._empty_state, 1)

        # Body is part of the panel's own layout so a direct test/sizing of
        # PageViewerPanel shows it. MainWindow places it BESIDE the sidebar
        # in the body row of the central column.
        layout.addWidget(self._body, 1)
        self._sync_empty_state()

        self._merge = MergeFlow(self)
        self._manage_tab         = None   # PdfTab whose panel is mounted
        self._toggle_in_progress = False  # reentrancy guard for _toggle_manage
        # The one active merge batch (decision 3: one at a time). Not a doc-bar
        # tab — the merge is the fourth view, its pane in the sidebar and its
        # FileGrid in the main area, so this widget is owned here rather than by
        # the tab strip. None when no merge view is showing.
        self._merge_widget   = None
        self._merge_rail_tab = None   # PdfTab whose nav rail is in the merge host
        self._merge_leaving  = False  # suppress the doc-tab auto-exit mid-inset
        # Retired merge views, kept alive so the shared render thread never
        # emits into a torn-down grid (see _exit_merge_preview). Cleared only
        # when the panel goes away.
        self._merge_retired = []

        # The preflight light re-checks a little after the document settles.
        self._pf_timer = QTimer(self)
        self._pf_timer.setSingleShot(True)
        self._pf_timer.timeout.connect(self._run_preflight)

        AppState.get().status_message.connect(self._on_status)
        # Settle the row before anything is open, or every action looks
        # available on an empty window.
        self._update_toolbar()

    # ── the document row ─────────────────────────────────────────────────────

    def _build_doc_actions(self):
        """What rides in the tab bar's right-hand corner: open, find, and the
        three things you do to the document that is open."""
        w = QWidget()
        w.setObjectName("docActions")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 0, 10, 0)
        lay.setSpacing(6)

        def act(text, tip, slot, shortcut="", icon=False, obj="docBtn"):
            b = QPushButton(text)
            b.setObjectName("docIconBtn" if icon else obj)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip + (f"  ({shortcut})" if shortcut else ""))
            if icon:
                b.setFixedSize(30, 30)
            else:
                b.setMinimumHeight(30)
            if slot is not None:
                b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        # Transient messages — a conversion running, a merge failing. They used
        # to have a whole label's width of the old bar to themselves and were
        # blank almost always; here they are given room only while they have
        # something to say. Slice 2 moves them to the status bar.
        self._viewer_info = _ElidedLabel()
        self._viewer_info.setObjectName("currentFileLabel")
        self._viewer_info.setMaximumWidth(260)
        lay.addWidget(self._viewer_info)

        self._find = FindBar(self, act)
        # The find box sits in the doc row too, between the search icon and
        # Bearbeiten, so it expands inline rather than swallowing a separate
        # row. Phase 3.1: the doc row is no longer a cornerWidget of a single
        # QTabWidget — we add the find box directly to its layout.
        lay.addWidget(self._find.box)

        self._edit_btn = act("", tr("Bearbeiten"), None, icon=True)
        self._edit_btn.setIcon(icon("edit", colour=theme_color("DIM"), size=16))
        self._edit_btn.setIconSize(QSize(16, 16))
        menu = QMenu(self._edit_btn)
        self._act_undo = menu.addAction(tr("Rückgängig"))
        self._act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self._act_undo.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._act_undo.triggered.connect(self._undo)
        self._act_redo = menu.addAction(tr("Wiederholen"))
        self._act_redo.setShortcut(QKeySequence("Ctrl+Y"))
        self._act_redo.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._act_redo.triggered.connect(self._redo)
        menu.addSeparator()
        a = menu.addAction(tr("Kopieren"))
        a.triggered.connect(self._copy_selection)
        a = menu.addAction(tr("Alles auswählen"))
        a.triggered.connect(self._select_all_text)
        self._edit_btn.setMenu(menu)

        self._save_btn = act(tr("Speichern"), tr("Am Originalpfad speichern"),
                             self._save_current, "Strg+S")
        self._print_btn = act(tr("Drucken"), tr("Drucken"),
                              self._print_current, "Strg+P")

        # Kept under its old name: the page manager is reached from the sidebar's
        # view switch now, but _update_toolbar and the merge paths still speak of
        # a manage button, and the shortcut still works.
        self._manage_btn = self._edit_btn
        return w

    def _apply_theme(self):
        # The row is styled from the application stylesheet, which
        # apply_theme_globally replaces before this runs — Qt re-polishes for a
        # new sheet by itself.
        self._find.retheme()
        # The Bearbeiten glyph is stroked in the live theme's DIM, so it has to
        # be re-drawn on a theme switch like every other drawn icon.
        self._edit_btn.setIcon(icon("edit", colour=theme_color("DIM"), size=16))
        # The "+" next to the tabs is drawn the same way and would otherwise
        # keep the colour of the theme the app started in.
        self._new_btn.setIcon(icon("plus", colour=theme_color("DIM"), size=16))

    def _on_status(self, msg):
        """Whatever the app has to say goes through the window's status bar
        (a `show_status` callback MainWindow injects). With no document there is
        no page to describe, so the doc row's own label carries it — a conversion
        running before any tab exists still has to be able to say so."""
        tab = self._current()
        if self.show_status is not None:
            self.show_status(msg or "")
        if tab is not None:
            self._viewer_info.setText("")
        else:
            self._viewer_info.setText(msg or "")

    # ── edit actions, aimed at the tab that is open ──────────────────────────

    def _undo(self):
        tab = self._current()
        if tab is None:
            return
        AppState.get().status_message.emit(
            tr("Rückgängig.") if tab.undo() else tr("Nichts zum Rückgängig."))
        self._update_toolbar()

    def _redo(self):
        tab = self._current()
        if tab is None:
            return
        AppState.get().status_message.emit(
            tr("Wiederholt.") if tab.redo() else tr("Nichts zum Wiederholen."))
        self._update_toolbar()

    def _copy_selection(self):
        self._on_canvas("_copy")

    def _select_all_text(self):
        self._on_canvas("_select_all")

    def _on_canvas(self, method):
        tab = self._current()
        if tab is None:
            return
        fn = getattr(tab.single._view, method, None)
        if fn is not None:
            fn()

    def _tab_label(self, tab, name):
        """A tab's text: the file, shortened, with a dot while it has unsaved
        edits. The dot is the answer to "have I saved this" without having to
        look anywhere else — and it is why the row can tell whether Speichern
        should be live."""
        disp = name if len(name) <= 22 else name[:19] + "..."
        dot = "\u25cf  " if (isinstance(tab, PdfTab) and tab.is_dirty()) else ""
        return f"  {dot}{disp}  "

    def _sync_tab_label(self, tab):
        if not isinstance(tab, PdfTab):
            return
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.setTabText(idx, self._tab_label(
                tab, os.path.basename(tab.pdf_path)))

    def _wire_tab(self, tab):
        """Let a newly opened document drive the row."""
        tab.dirty_changed.connect(lambda _d, t=tab: self._on_tab_dirty(t))
        tab.changed.connect(self._update_toolbar)
        tab.changed.connect(self._schedule_preflight)
        # The panel is reached from the sidebar; the view only knows it wants it.
        tab.single.open_preflight_panel = self._open_preflight_panel
        # A document opened after the setting was changed still has to honour it.
        from tools.shell.settings import AppSettings
        tab.single.set_continuous(AppSettings.get().continuous_scroll())

    def _on_tab_dirty(self, tab):
        self._sync_tab_label(tab)
        if tab is self._current():
            self._update_toolbar()

    # ── the preflight light ──────────────────────────────────────────────────

    def _schedule_preflight(self):
        """Ask again shortly. Debounced because the trigger is "the document
        changed", and dragging a page across a grid emits that per drop —
        checking on each one would queue a job per edit for an answer only the
        last one is about."""
        self._pf_timer.start(700)

    def _run_preflight(self):
        tab = self._current()
        if tab is None or not tab.model:
            return
        if self._pf_job is not None:
            self._pf_job.cancel()
            self._pf_job = None
        tab.single.set_preflight("running")

        from tools.jobs import submit
        from tools.panels.preflight import ambient_check
        # The flattened view, not the file on disk: the light has to describe
        # the document as the page manager currently has it, which is what a
        # print or an export would produce.
        try:
            from tools.snapshots import ensure_view_snapshot
            src = ensure_view_snapshot(tab.pdf_path)
        except Exception:
            src = tab.pdf_path

        self._pf_job = submit(
            lambda job: ambient_check(src),
            owner=tab, name="preflight-light",
            on_done=lambda res, t=tab: self._preflight_done(t, res),
            on_error=lambda _e, t=tab: t.single.set_preflight("unknown"))

    def _preflight_done(self, tab, result):
        self._pf_job = None
        if tab is not self._current():
            return
        issues, _oks = result
        tab.single.set_preflight("warn" if issues else "ok", issues)

    # ── find ─────────────────────────────────────────────────────────────────

    def set_continuous_scroll(self, on):
        """Darstellung changed the page layout: every open document follows."""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PdfTab):
                w.single.set_continuous(on)

    def toggle_find(self):
        self._find.toggle()

    def set_find_visible(self, visible):
        self._find.set_visible(visible)

    def _step_find(self, direction):
        self._find.step(direction)

    def _clear_find(self):
        self._find.clear()

    def _open_preflight_panel(self):
        """Switch to the Druckvorstufenprüfung panel, if the window has one."""
        opener = getattr(self, "show_tool_panel", None)
        if opener is not None:
            opener("Druckvorstufenprüfung")

    def _close_current_tab(self):
        idx = self.tabs.currentIndex()
        if idx >= 0:
            self._close_tab(idx)

    # ── Manage-Layout helpers ─────────────────────────────────────────────────

    def _enter_manage_layout(self, panel):
        """Mount ManagePanel's operations into the app's 224px tool column —
        the same slot the tool list and Layout's staging sections use, so the
        column stays at one width and one place regardless of which of the
        three fills it."""
        if self._manage_tab is not None:
            return  # already in manage layout

        panel.show()
        # Remember which tab owns this layout so _exit can detach correctly
        self._manage_tab = self._current()
        if self.mount_sidebar:
            self.mount_sidebar("manage", panel)

    def _exit_manage_layout(self):
        """Detach ManagePanel from the tool column and give it back."""
        if self._manage_tab is None:
            return
        # Detach panel back to its OWNING tab (not _current() which may have changed)
        tab = self._manage_tab
        if tab and tab._manage_panel:
            tab._manage_panel.setParent(tab)
            tab._manage_panel.hide()
        self._manage_tab = None
        if self.mount_sidebar:
            self.mount_sidebar("tool_list")

    def _current(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, PdfTab) else None

    def _open(self, path=None):
        from PyQt6.QtWidgets import QMessageBox
        from tools.multi_open import (IMAGE_EXTS, OFFICE_EXTS, PDF_EXT,
                                      file_dialog_filter)
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Datei öffnen"), "", file_dialog_filter())
        if not path: return

        # Everything below assumes a readable file. Opening one that vanished
        # between being picked and being read (a stale "recent file", a removed
        # USB stick) used to fall through to the PDF parser and raise inside a
        # slot, which aborts the process rather than showing anything.
        if not os.path.isfile(path):
            QMessageBox.warning(self, tr("Datei nicht gefunden"),
                                tr('Die Datei existiert nicht mehr:\n{p0}').format(p0=path))
            return

        ext = os.path.splitext(path)[1].lower()
        if ext not in PDF_EXT | IMAGE_EXTS | OFFICE_EXTS:
            QMessageBox.warning(
                self, tr("Format nicht unterstützt"),
                tr('{p0} kann "{p1}" nicht öffnen.').format(
                    p0=APP_NAME, p1=ext or "?"))
            return

        if ext in IMAGE_EXTS:
            try:
                import img2pdf, tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
                with open(tmp, "wb") as f:
                    f.write(img2pdf.convert(path))
                path = tmp
            except Exception as e:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, tr("Bild-Konvertierung fehlgeschlagen"), str(e))
                return

        elif ext in OFFICE_EXTS:
            # LibreOffice takes seconds to start, and this runs on the GUI
            # thread. Say what is happening instead of just freezing.
            self._viewer_info.setText(
                tr('Konvertiere {p0} …').format(p0=os.path.basename(path)))
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            QApplication.processEvents()
            try:
                return self._open_office(path)
            finally:
                QApplication.restoreOverrideCursor()
                self._update_toolbar()

        return self._add_pdf_tab(path)

    def _open_office(self, path):
        """Convert an Office/text/vector document via LibreOffice, then open it."""
        from PyQt6.QtWidgets import QMessageBox
        import subprocess, tempfile
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            QMessageBox.warning(self, tr("LibreOffice fehlt"),
                tr("LibreOffice wird benötigt um Office-Dateien zu öffnen.\n"
                   "Installation: sudo pacman -S libreoffice-still"))
            return None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
            atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
            stem = os.path.splitext(os.path.basename(path))[0]
            r = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", tmp_dir, path],
                capture_output=True, text=True, errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            QMessageBox.warning(self, tr("Konvertierung fehlgeschlagen"),
                tr("LibreOffice hat nicht innerhalb von 120 Sekunden geantwortet."))
            return None
        except Exception as e:
            QMessageBox.warning(self, tr("Office-Konvertierung fehlgeschlagen"), str(e))
            return None
        converted = os.path.join(tmp_dir, stem + ".pdf")
        if not os.path.isfile(converted):
            # LibreOffice benennt manchmal anders — suche erste PDF
            pdfs = [f for f in os.listdir(tmp_dir) if f.endswith(".pdf")]
            if not pdfs:
                QMessageBox.warning(self, tr("Konvertierung fehlgeschlagen"),
                                    (r.stderr or "").strip()[:300]
                                    or tr("LibreOffice hat keine PDF erzeugt."))
                return None
            converted = os.path.join(tmp_dir, pdfs[0])
        return self._add_pdf_tab(converted)

    def _add_pdf_tab(self, path):
        from PyQt6.QtWidgets import QMessageBox
        from tools.pdf_access import ensure_openable
        # A file that genuinely needs a password gets asked about, once, with an
        # explanation — and is worked on as a decrypted copy from here on. A
        # restricted one, which is most of what people call password-protected,
        # needs nothing and is not asked about.
        #
        # The password prompt and every early-return below run BEFORE
        # `_reveal_tabs()`: a cancel or a failure here must leave the empty
        # state exactly where it was, not a blank page area with the count still
        # at zero.
        opened = ensure_openable(path, self)
        if opened is None:
            return              # cancelled at the password prompt
        path = opened
        self._reveal_tabs()
        # A damaged, encrypted or truncated PDF makes this raise. Unhandled in a
        # slot, PyQt takes the whole process down — so a single bad file killed
        # the app instead of reporting one failed open.
        try:
            tab = PdfTab(path)
        except Exception as e:
            logging.exception("open failed: %s", path)
            QMessageBox.critical(
                self, tr("Datei konnte nicht geöffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            self._sync_empty_state()   # restore the empty window (count is 0)
            return
        self._wire_tab(tab)
        idx  = self.tabs.addTab(tab, self._tab_label(tab, os.path.basename(path)))
        self.tabs.setCurrentIndex(idx)
        AppState.get().open_pdf(path)
        self._update_toolbar()
        self.focus_page_view()
        self.tab_opened.emit()
        self.tabs_changed.emit()
        return tab

    def focus_page_view(self):
        """Give the page view the keyboard, once it is actually on screen.

        Two reasons this is not simply setFocus() here. addTab on an empty
        tab bar makes the new tab current by itself, so setCurrentIndex is a
        no-op and _on_tab_changed — which is where focus was being set — never
        runs for the first file opened. And even when it does run, the widget
        is not visible yet at that moment, and Qt drops focus set on a widget
        that cannot take it.

        The result was that after opening a file the arrow keys did nothing
        until the preview had been clicked, because the keys were still going
        to whatever was focused before — usually the Öffnen button.
        """
        def _focus():
            widget = self._current()
            if widget is not None and widget.isVisible():
                widget.single._view.setFocus()
        QTimer.singleShot(0, _focus)

    def open_file(self, path):
        tab = self._open(path)
        # Persist last opened file for the "reopen on startup" setting, and add
        # it to "Zuletzt geöffnet" in the empty window — but only when it
        # actually opened. Remembering a file that failed meant the next start
        # reopened it and failed again, every time.
        if tab is None:
            return
        try:
            from PyQt6.QtCore import QSettings
            QSettings("CopyShop", "PDFSuite").setValue("general/last_file", path)
        except Exception:
            logging.debug("could not record the last opened file", exc_info=True)
        try:
            from tools.shell.settings import AppSettings
            AppSettings.get().add_recent_file(path)
        except Exception:
            logging.debug("could not record the recent file", exc_info=True)

    # ── the empty window ──────────────────────────────────────────────────────

    def _open_via_dialog(self):
        """"Datei öffnen…" in the empty window — the same picker Strg+O opens,
        going through open_file() so the choice is recorded like any other."""
        from tools.multi_open import file_dialog_filter
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Datei öffnen"), "", file_dialog_filter())
        if path:
            self.open_file(path)

    def _merge_via_dialog(self):
        """"Mehrere zusammenführen…" in the empty window."""
        from tools.multi_open import file_dialog_filter
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Mehrere Dateien öffnen"), "", file_dialog_filter())
        self._open_dropped(paths)

    def _open_multi_dialog(self):
        """"+" in the doc row (and its Ctrl+O): a MULTI-select picker, not the
        single-file one — one file opens directly, several go to the same
        merge-or-separate preview a drag of several does. Routed through the
        window's handler when present, because it owns the window/viewer switch;
        a bare panel (a test) opens the same picker itself."""
        if self.open_multi_dialog is not None:
            self.open_multi_dialog()
            return
        from tools.multi_open import file_dialog_filter
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Mehrere Dateien öffnen"), "", file_dialog_filter())
        self._open_dropped(paths)

    def _open_dropped(self, paths):
        """One or more files dragged onto the empty window: one opens, several
        go to the same merge-or-separate preview a multi-select open does."""
        paths = [p for p in paths if p]
        if not paths:
            return
        if len(paths) == 1:
            self.open_file(paths[0])
        else:
            self.show_merge_tab(paths)

    def _reveal_tabs(self):
        """Make the page area + tab strip visible before a new tab's content
        is built.

        Must run before PdfTab(...)/MergeOrderWidget(...) construction, not
        only afterwards from _sync_empty_state() reacting to currentChanged:
        a page view built while an ancestor is still hidden skips its first
        render, and nothing later asks it to try again — the render stays
        armed on a page nobody is looking at until the next zoom or turn.

        Phase 3.1: tabs live in their own doc_row. Both the bar and the page
        area swap with the empty state."""
        if not self._pages.isVisible():
            self._empty_state.setVisible(False)
            self._pages.setVisible(True)
            self._bar.setVisible(True)

    def _sync_empty_state(self):
        """Swap the page area for the empty state, or back, whenever the count
        of open tabs crosses zero in either direction. The reverse direction
        (pages → empty) is always safe here; the forward one goes through
        _reveal_tabs() instead, called before the new tab exists rather than
        after — see there for why the order matters."""
        empty = self.tabs.count() == 0
        if empty:
            self._pages.setVisible(False)
            self._bar.setVisible(False)
            self._empty_state.setVisible(True)
            from tools.shell.settings import AppSettings
            self._empty_state.set_recent(AppSettings.get().recent_files())
        else:
            self._reveal_tabs()

    def _open_result_tab(self, path, title):
        # Reached from AppState.result_ready, i.e. from a tool that just wrote a
        # file. If that file is unreadable this raises inside a slot and takes
        # the process with it — the tool's own error handling never sees it.
        self._reveal_tabs()
        try:
            tab = PdfTab(path)
        except Exception as e:
            logging.exception("result tab failed: %s", path)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(
                self, tr("Ergebnis konnte nicht geöffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            self._sync_empty_state()   # restore the empty window (count is 0)
            return
        self._wire_tab(tab)
        idx  = self.tabs.addTab(tab, self._tab_label(tab, title))
        self.tabs.setCurrentIndex(idx)
        self._update_toolbar()
        self.tab_opened.emit()
        self.tabs_changed.emit()

    def _close_tab(self, idx):
        w = self.tabs.widget(idx)
        # Whatever this tab (or a dialog it owns) put on the pool stops here.
        try:
            from tools.jobs import cancel_owner
            cancel_owner(w)
            for child in w.findChildren(QWidget):
                cancel_owner(child)
        except Exception:
            logging.debug("closing a tab: cancelling its jobs failed", exc_info=True)
        if isinstance(w, PdfTab):
            w.cancel_render_work()
            _ThumbnailCache.evict_tab(w.pdf_path)
            _FullPageCache.evict_tab(w.pdf_path)
            # …and the parsed document behind them. A loaded page of a
            # large PDF is hundreds of megabytes; holding one for a tab
            # that is gone is the largest single thing this app can leak.
            try:
                if w.pdf_path and os.path.isfile(w.pdf_path):
                    release(w.pdf_path)
            except Exception:
                logging.exception("close: releasing the cached document failed")
            # And the flattened copy the tools were reading. It is a whole
            # document in the temp directory; nothing used to remove it, so a
            # counter that opens a hundred files a day left a hundred copies
            # of customer work behind it.
            try:
                from tools.snapshots import discard_snapshots_for
                discard_snapshots_for(w.pdf_path)
            except Exception:
                logging.debug("close: removing the view snapshot failed",
                              exc_info=True)
            # If this tab was showing a locked document, its open file is the
            # decrypted copy. That one does not wait for the quit.
            try:
                from tools.pdf_access import discard_unlocked_copy
                discard_unlocked_copy(w.pdf_path)
            except Exception:
                logging.debug("close: removing the decrypted copy failed",
                              exc_info=True)
        elif isinstance(w, MergeOrderWidget):
            # Closing the preview discards its conversions — unless one is still
            # running, in which case the worker is still writing in there.
            if w.tmp_dir and not w._busy:
                try: shutil.rmtree(w.tmp_dir, ignore_errors=True)
                except Exception: pass   # as above — the directory is temporary either way
        self.tabs.removeTab(idx)
        self.tabs_changed.emit()

    def get_tab_names(self):
        """Gibt Liste von (idx, name, is_current) für alle PDF-Tabs zurück."""
        result = []
        current = self.tabs.currentIndex()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PdfTab):
                name = self.tabs.tabText(i).strip()
                result.append((i, name, i == current))
        return result

    def switch_to_tab(self, idx):
        self.tabs.setCurrentIndex(idx)

    def _manage_current(self):
        # Reuse _toggle_manage so the button works as a proper toggle,
        # exiting manage mode when clicked while already inside it.
        self._toggle_manage()

    def show_merge_tab(self, file_paths):
        self._merge.show_merge_tab(file_paths)

    # ── the merge view (decision 3: a main-area view, one batch at a time) ──

    def _enter_merge(self, widget):
        """Bring the merge view up: its FileGrid becomes the main-area content
        (mounted into `_pages` as a non-tab page — the real document tabs stay
        awake in the doc bar), its pane mounts into the main sidebar via the
        SidebarHost, and the shared nav rail (if a tab is open to share one)
        is reparented into the merge's rail host."""
        if self._merge_widget is widget:
            # Re-entering the current batch: just re-point the main area.
            self._reveal_tabs()
            self._pages.setCurrentWidget(widget.preview_widget)
            widget._ensure_filter()
            return
        self._merge_widget = widget
        self._reveal_tabs()
        self._pages.addWidget(widget.preview_widget)
        self._pages.setCurrentWidget(widget.preview_widget)
        if self.mount_sidebar:
            self.mount_sidebar("merge", widget.controls_widget)
        self._enter_merge_rail(widget)
        widget._ensure_filter()
        self._update_toolbar()
        self._viewer_info.setText(
            tr("Dateien sortieren, zusammenführen oder einzeln öffnen"))
        if self.sync_view_switch:
            self.sync_view_switch()

    def _enter_merge_rail(self, widget):
        """Reparent the active tab's shared nav rail into the merge view's rail
        host and point it at the FileGrid (like Layout does with its sheets)."""
        # The current *document* is the bar's current tab. `_current()` would
        # return None here: the merge preview is what `_pages` is showing.
        tab = None
        idx = self._bar.currentIndex()
        if 0 <= idx < self.tabs.count():
            w = self.tabs.widget(idx)
            if isinstance(w, PdfTab):
                tab = w
        self._merge_rail_tab = tab
        widget._file_rail.single = tab.single if tab is not None else None
        host_lay = widget.rail_host.layout()
        while host_lay.count():
            it = host_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
        if tab is None or getattr(tab, "_nav_col", None) is None:
            widget.rail_host.setVisible(False)
            widget._file_rail.sync()
            return
        host_lay.addWidget(tab._nav_col)
        widget.rail_host.setVisible(True)
        tab.single.rail_delegate = widget._file_rail
        tab.single.nav_scroll_mode(True)
        self._merge_bar_conn = widget._scroll.verticalScrollBar().valueChanged.connect(
            widget._file_rail.sync)
        widget._file_rail.sync()

    def _exit_merge_preview(self):
        """Leave the merge view: unmount its pane and rail, drop the grid from
        the main area, and clean up the merge's temp dir. Follows the new exit
        paths — cancel / Esc / Zusammenführen / Einzeln öffnen, or picking one
        of the still-visible document tabs."""
        widget = self._merge_widget
        if widget is None:
            return
        self._merge_widget = None
        self._exit_merge_rail(widget)
        try:
            from tools.jobs import cancel_owner
            cancel_owner(widget)
            for child in widget.findChildren(QWidget):
                cancel_owner(child)
        except Exception:
            logging.debug("exiting merge: cancelling its jobs failed", exc_info=True)
        i = self._pages.indexOf(widget.preview_widget)
        if i >= 0:
            self._pages.removeWidget(widget.preview_widget)
        widget._cleanup_filter()
        # The merge's FileGrid renders thumbnails on the shared pdfium render
        # thread. Tearing the widget down here races that thread — a render
        # finishing a moment later would emit into a half-deleted grid and
        # corrupt the heap. So the widget is *retired*, not destroyed: its
        # pending renders are cancelled, it is handed its preview back, and it
        # is kept referenced until the panel itself goes away (the render
        # thread drains, then the whole session is torn down by
        # QApplication.aboutToQuit / os._exit). One batch at a time, so at most
        # a handful of small merge views accumulate.
        try:
            widget._grid.cancel_render_work()
        except Exception:
            logging.debug("exiting merge: cancelling thumbnails failed", exc_info=True)
        widget.preview_widget.setParent(widget._splitter)
        self._merge_retired.append(widget)
        # The merge temp dir moves with the exit path (open_flow.py:63–69).
        if widget.tmp_dir:
            try: shutil.rmtree(widget.tmp_dir, ignore_errors=True)
            except Exception: pass
        # Restore the sidebar: the manage operations if a tab is in manage mode,
        # otherwise the tool list.
        if self.mount_sidebar:
            if (self._manage_tab is not None
                    and self._manage_tab._manage_panel is not None):
                self.mount_sidebar("manage", self._manage_tab._manage_panel)
            else:
                self.mount_sidebar("tool_list")
        # Point the main area back at the previously current document (or the
        # empty state when none is open).
        if self.tabs.count() > 0 and self._bar.currentIndex() >= 0:
            self._pages.setCurrentIndex(self._bar.currentIndex())
        self._sync_empty_state()
        self._update_toolbar()
        if self.sync_view_switch:
            self.sync_view_switch()

    def _exit_merge_rail(self, widget):
        """Hand the shared rail back to its owning tab, and the rail delegate
        back to the single-page view."""
        tab = self._merge_rail_tab
        self._merge_rail_tab = None
        if tab is None:
            return
        try:
            widget._scroll.verticalScrollBar().valueChanged.disconnect(
                self._merge_bar_conn)
        except (TypeError, AttributeError):
            pass
        try:
            tab.single.rail_delegate = None
            tab.single.nav_scroll_mode(tab.single._continuous)
        except RuntimeError:
            return    # tab already closed while the merge was showing
        widget._file_rail.single = None
        if getattr(tab, "_nav_col", None) is not None:
            try:
                tab._nav_col.setParent(tab)
                tab.layout().addWidget(tab._nav_col)
            except RuntimeError:
                pass
        widget.rail_host.setVisible(False)

    def _update_toolbar(self):
        """Bring the document row in line with the tab that is open.

        Called on every tab change and after every edit: what is available
        depends on there being a document, and on whether it has a history and
        unsaved changes."""
        tab = self._current()
        has_doc = isinstance(tab, PdfTab)
        for w in (self._edit_btn, self._print_btn, self._find.open_btn):
            w.setEnabled(has_doc)
        if not has_doc:
            self.set_find_visible(False)
        # Speichern is disabled only when there is no document at all. A doc
        # that happens to have nothing new to save still shows an enabled,
        # clickable button — the audit read a greyed-out Save as "can't save
        # this file" (an inverted hierarchy next to the always-live Drucken).
        self._save_btn.setEnabled(has_doc)
        self._act_undo.setEnabled(bool(has_doc and tab.can_undo()))
        self._act_redo.setEnabled(bool(has_doc and tab.can_redo()))
        self._sync_tab_label(tab)
        # The title bar wordmark follows the current document ("Folio — file").
        if self.set_title_document is not None:
            self.set_title_document(
                os.path.basename(tab.pdf_path) if has_doc else "")

    def _print_current(self):
        tab = self._current()
        if tab: tab._print()

    def _save_current(self):
        """Ctrl+S — aktuellen Tab am Originalpfad speichern."""
        tab = self._current()
        if not tab: return
        try:
            tab.save_to(tab.pdf_path)
            AppState.get().status_message.emit(
                tr('Gespeichert: {p0}').format(p0=os.path.basename(tab.pdf_path)))
        except Exception as e:
            AppState.get().status_message.emit(f"Speicherfehler: {e}")

    def _save_as_current(self):
        """Ctrl+Shift+S — save under a new name.

        With pages picked in the page manager this saves *those pages only*; it
        used to write the whole document and ignore the selection entirely. The
        selection is honoured only while the manager is actually on screen, so a
        selection left behind from an earlier visit cannot silently truncate an
        ordinary Save As."""
        tab = self._current()
        if not tab: return
        uids     = tab.selected_uids() if tab.in_manage_mode() else []
        subset   = bool(uids) and len(uids) < len(tab.model.order)
        stem, ext = os.path.splitext(tab.pdf_path)
        suggested = f"{stem}_auswahl{ext or '.pdf'}" if subset else tab.pdf_path
        title = tr('Auswahl speichern als ({p0} Seiten)').format(p0=len(uids)) \
                if subset else tr("Speichern als")
        path, _ = QFileDialog.getSaveFileName(
            self, title, suggested, tr("PDF Dateien (*.pdf)"))
        if not path: return
        try:
            tab.save_to(path, uids=uids if subset else None)
            name = os.path.basename(path)
            if subset:
                # An export of part of the document — the tab still shows the
                # whole thing, so it keeps its own file.
                AppState.get().status_message.emit(
                    tr('{p0} Seite(n) gespeichert als: {p1}').format(p0=len(uids), p1=name))
                return
            self._retarget_tab(tab, path)
            AppState.get().open_pdf(path)
            AppState.get().status_message.emit(tr('Gespeichert als: {p0}').format(p0=name))
        except Exception as e:
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except Exception:
                pass
            AppState.get().status_message.emit(f"Speicherfehler: {e}")

    def _retarget_tab(self, tab, path):
        """Point a tab at the file just written for it.

        The model must be re-based, not just re-pathed. save_to() writes the
        pages in display order with rotations baked in, so in the new file page
        i *is* order position i — while src/foreign_src still held indexes into
        the old sources and rotations still asked for a turn already applied.
        Re-pathing alone made a reordered or rotated document show the wrong
        pages after Save As."""
        model = tab.model
        tab.retarget(path)          # tab, single view, manage panel and its grid
        model.src         = {uid: i for i, uid in enumerate(model.order)}
        model.foreign_src = {}
        model.rotations   = {}
        self._sync_tab_label(tab)

    def _on_tab_changed(self, idx):
        # Covers every path that adds or removes a tab: currentChanged fires
        # on the transition to/from -1, whichever call site caused it.
        self._sync_empty_state()

        # ── A doc tab picked while the merge view is up exits it ─────────────
        # The merge is a main-area view, not a tab, so it has no entry in the
        # bar. Clicking one of the document tabs that stay visible (decision 3)
        # leaves the merge view and returns to that document. (During
        # open-separately the _merge_leaving guard keeps the temp dir alive
        # until every converted file has been read.)
        if (self._merge_widget is not None
                and not getattr(self, "_merge_leaving", False)):
            self._exit_merge_preview()

        # ── Always clean up manage layout when switching file tabs ────────────
        # The manage panel belongs to the outgoing tab; if it is still mounted
        # in the tool column we must detach it now, before _current() changes
        # meaning.
        if self._manage_tab is not None:
            old_tab = self._manage_tab   # tab that owns the panel
            # Exit manage mode on the old tab so its stack returns to single view
            if old_tab._stack.currentWidget() is not old_tab.single:
                old_tab._stack.setCurrentWidget(old_tab.single)
                old_tab._on_manage_exit = None  # discard stale callback
                # _exit_manage() also hands the shared rail back to the single
                # view; switching the stack directly above skipped that, so the
                # outgoing tab was left driving its (now hidden) page-manager
                # grid, and the nav rail went dead when the user returned.
                old_tab._restore_rail_after_manage()
            self._exit_manage_layout()   # detach panel → old tab, unmount

        # ── Memory management: freeze outgoing tab, resume incoming tab ────────
        # Cancel pre-render tasks for ALL non-active tabs to stop them burning
        # cache slots that belong to the tab the user is actually looking at.
        active_widget = self.tabs.currentWidget()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, PdfTab) and tab is not active_widget:
                tab.single.cancel_prerenders()
                # Its rendered pages stay. They used to be thrown away here,
                # every page but the one it was showing, on the reasoning that
                # "it can re-render quickly when the user switches back" — which
                # is true of a text page and false of the ones this application
                # exists for: seconds each, and a document's worth of them.
                #
                # That was written when the cache held six entries and evicting
                # another tab was the only way to make room. It is bounded by
                # memory now, and _priority_evict already drops other tabs first
                # when room is actually needed. Throwing the work away before
                # anything asks for the space just means doing it twice.

        w = active_widget
        # A search belongs to the document it was run over: carrying its hits
        # across a tab switch would mark positions in one file from the text of
        # another.
        self.set_find_visible(False)
        if isinstance(w, PdfTab):
            self._schedule_preflight()
            _set_active(w.pdf_path, w.single.current_page)
            AppState.get().open_pdf(w.pdf_path)
            AppState.get().page_model   = w.model
            AppState.get().current_page = w.single.current_page
            self._update_toolbar()
            self.focus_page_view()
        elif isinstance(w, MergeOrderWidget):
            self._update_toolbar()
            self._viewer_info.setText(tr("Dateien sortieren, zusammenführen oder einzeln öffnen"))
            w.setFocus()
        else:
            self._update_toolbar()
            self._viewer_info.setText("")
        self._sync_sidebar()

    def _sync_sidebar(self):
        """The merge preview brings its own sidebar. The app's tool nav sitting
        next to it made two stacked sidebars, the left one offering tools that
        do not apply to the view — so it steps aside for the merge view, exactly
        as it does for the page manager. The merge pane is mounted (or removed)
        by the merge enter/exit paths, so here we only have to avoid clobbering
        it while a doc tab is current."""
        if self._merge_widget is not None:
            return
        w = self.tabs.currentWidget()
        if self._manage_tab is None:   # manage mode owns it there
            if self.mount_sidebar: self.mount_sidebar("tool_list")
