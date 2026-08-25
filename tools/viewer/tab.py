"""
One open document.

Owns the model, and switches between the single-page view and the page manager
over it. It is also the anchor of the viewer's one reference cycle: a tab builds
its children, and each child walks back up the parent chain to ask which tab
owns it, so the tab imports them at call time and they import the tab normally.
"""
import os, logging, tempfile
from PyQt6.QtWidgets import QHBoxLayout, QFrame, QScrollArea, QStackedWidget
from PyQt6.QtCore import pyqtSignal, QTimer, Qt
from tools.app_state import AppState
from tools.i18n import tr
from tools.pdf_access import is_locked
from tools.render.caches import _set_active
from tools.viewer.model import PageModel
from tools.printing.dialog import PrintDialog
from tools.viewer.manage import ManagePanel
from tools.viewer.page_grid import PageGrid
from tools.viewer.single_page import SinglePageView
from tools.viewer.tab_base import PdfTabBase


class _GridRail:
    """Drives the shared navigation rail while the page manager is showing.

    Same rail, other view: the thumb maps onto the grid's scroll range, the
    arrows step it, a picked page brings that card into view, and every grid
    scroll pushes the position back to the rail. Without this the page manager
    lost the scrollbar entirely — its own was a plain QScrollBar next to a rail
    that vanished with the preview.
    """

    def __init__(self, tab, grid, scroll):
        self.tab   = tab
        self.grid  = grid
        self.scroll = scroll

    def _bar(self):
        return self.scroll.verticalScrollBar()

    def rail_prev(self):
        self._nudge(-1)

    def rail_next(self):
        self._nudge(1)

    def _nudge(self, direction):
        bar = self._bar()
        step = max(60, int(self.scroll.viewport().height() * 0.85))
        bar.setValue(bar.value() + direction * step)

    def rail_wheel(self, dy_px):
        bar = self._bar()
        bar.setValue(bar.value() + int(dy_px))

    def rail_go_to(self, page):
        cards = self.grid.cards()
        if not cards:
            return
        i = max(0, min(int(page) - 1, len(cards) - 1))
        self._bar().setValue(max(0, int(cards[i].y()) - 8))

    def rail_drag_to(self, frac):
        bar = self._bar()
        bar.setValue(round(float(frac) * bar.maximum()))

    def rail_prompt_goto(self):
        n = self.tab.page_count()
        if n <= 0:
            return
        from PyQt6.QtWidgets import QInputDialog
        page, ok = QInputDialog.getInt(
            self.tab, tr("Gehe zu Seite"),
            tr('Seite (1 – {p0}):').format(p0=n),
            self.page(), 1, n)
        if ok:
            self.rail_go_to(page)

    def page(self):
        """The page the rail reports for the current scroll position.

        Derived from the scroll fraction rather than from which card sits
        under the viewport's middle: the grid lays cards out in rows of
        several, so a mid-line rule answers "the second row's leftmost card"
        even at the very top of the document. The cells are uniform, so the
        fraction maps onto page order directly — top is page 1, bottom is the
        last page."""
        cards = self.grid.cards()
        n = len(cards)
        if n <= 1:
            return max(1, n)
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        return max(1, min(n, int(round(frac * (n - 1))) + 1))

    def sync(self):
        """Scrollbar position → rail thumb and page number."""
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        single = self.tab.single
        single.nav_set_document(len(self.grid.cards()), self.page())
        single.nav_set_fraction(frac)


class PdfTab(PdfTabBase):
    changed = pyqtSignal()
    # Whether this document has edits that are not on disk. The document row
    # watches it for the tab's dot and for whether Speichern is live.
    dirty_changed = pyqtSignal(bool)

    # How many edits back a document can be taken. Each entry is a copy of the
    # page list and its bookkeeping — a few kilobytes for a long document, and
    # no page data at all.
    HISTORY_MAX = 50

    def __init__(self, pdf_path, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.model    = None
        # One undo stack per document, owned here rather than by the page
        # manager. It used to live in ManagePanel, which meant an edit was
        # undoable from the thumbnail sidebar and nowhere else — so a
        # Rückgängig anywhere else in the window had nothing to undo with, and
        # the panel had to exist for Strg+Z to mean anything at all.
        self._history    = []
        self._redo_stack = []
        self._dirty      = False
        self._saved_at   = None
        self._setup()
        self._load()

    def _setup(self):
        self._stack = QStackedWidget()
        # The navigation rail lives beside BOTH views rather than inside the
        # single-page one: the page manager shares it (see _enter_manage), so
        # switching layouts must not take the scrollbar away.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Einzelansicht
        self.single = SinglePageView()
        self.single.page_changed.connect(self._on_page_changed)
        self._stack.addWidget(self.single)
        self._manage_widget = None
        self._manage_panel  = None
        self._manage_rail   = None

        # The shared navigation rail. It stays stored here so the layout view
        # (Phase 4.4) can reparent it into its own rail host while it is
        # showing, and hand it back on exit — without ever building a second
        # one (see MainWindow._attach_layout_rail / _detach_layout_rail).
        self._nav_col = self.single.take_nav_rail()
        layout.addWidget(self._stack, 1)
        layout.addWidget(self._nav_col)

    def cancel_render_work(self):
        """Stop everything this tab has on the render queue.

        Closing a tab cancelled its tools/jobs.py work but nothing on _RenderQueue, so
        a pre-render for a document nobody is looking at any more went on holding
        the one render thread — and the pdfium lock with it — while the tab the
        user switched to waited behind it."""
        self.single.stop_background_work()

    def _load(self):
        """Load the document, or raise.

        This used to swallow every failure into a log line, so a corrupt or
        encrypted PDF opened a blank tab with model=None and no explanation —
        and every feature downstream then had to survive a tab with no model.
        Both construction sites report the exception instead."""
        from pypdf import PdfReader
        try:
            reader = PdfReader(self.pdf_path, strict=False)
            encrypted = reader.is_encrypted
            n = len(reader.pages)
        except Exception as e:
            logging.error(f"PdfTab._load: {e}")
            raise RuntimeError(tr('Die Datei ist beschaedigt oder keine gueltige PDF.\n{p0}')
                               .format(p0=e)) from e
        if encrypted and is_locked(self.pdf_path):
            # Only a file nothing can read without a password. A *restricted*
            # one — an owner password with no user password, which is most of
            # them — opens here as it does in every other viewer; the
            # restrictions it carries are about copying and printing, not about
            # looking at it. Turning those away was this application refusing
            # files that were never locked in the first place.
            raise RuntimeError(tr(
                "Diese PDF ist passwortgeschützt.\n"
                "Bitte zuerst entsperren (Passwort entfernen), dann erneut öffnen."))
        if n == 0:
            raise RuntimeError(tr("Das PDF enthaelt keine Seiten."))
        self.model = PageModel(n)
        self.single.load(self.pdf_path, self.model)
        AppState.get().page_model   = self.model
        AppState.get().current_page = 0
        _set_active(self.pdf_path, 0)

    def _on_page_changed(self, page_num):
        if self.model:
            AppState.get().current_page = page_num - 1
            _set_active(self.pdf_path, page_num - 1)
            # Slide the pre-render window to keep pages ahead warm
            QTimer.singleShot(200, self.single._prerender_all)

    def _build_manage_once(self):
        """Create PageGrid + ManagePanel once and cache them for the lifetime of the tab."""
        if self._manage_panel is not None:
            return  # already built
        grid = PageGrid(self.model, self.pdf_path)
        grid_scroll = QScrollArea()
        grid_scroll.setWidgetResizable(True)
        grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # The shared rail is this view's scrollbar; a second one beside it
        # would be a rival answer to the same question. Wheel scrolling still
        # reaches the grid directly.
        grid_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        grid_scroll.setWidget(grid)

        panel = ManagePanel(self.model, self.pdf_path, grid, parent=self, tab=self)
        panel.hide()
        panel.closed.connect(self._exit_manage)
        grid.order_changed.connect(self.changed.emit)
        grid.order_changed.connect(self._sync_rail_count)
        grid.order_changed.connect(panel.update_info)
        grid.selection_changed.connect(panel.update_info)

        self._manage_panel = panel
        self._manage_widget = grid_scroll
        self._manage_rail = _GridRail(self, grid, grid_scroll)
        self._stack.addWidget(grid_scroll)

    def _enter_manage(self, on_exit=None):
        self._on_manage_exit = on_exit
        self._build_manage_once()
        self._manage_panel.show()
        self._stack.setCurrentWidget(self._manage_widget)
        # The shared rail now drives the grid: same scrollbar, other view.
        self.single.rail_delegate = self._manage_rail
        # The thumb becomes a true scrollbar over the grid regardless of the
        # preview's page-flip mode.
        self.single.nav_scroll_mode(True)
        bar = self._manage_rail._bar()
        bar.valueChanged.connect(self._rail_sync_from_grid)
        # Start where the preview was: bring the current page's card into view.
        self._manage_rail.rail_go_to(self.single.current_page + 1)
        self._rail_sync_from_grid()

    def _rail_sync_from_grid(self, *_args):
        if self.in_manage_mode() and self._manage_rail is not None:
            self._manage_rail.sync()

    def _sync_rail_count(self):
        """Update the rail's total-page label and the status bar after add/delete."""
        if self.model is None:
            return
        n = len(self.model.order)
        self.single._tot_lbl.setText(str(n))
        self.single.publish_colour_counts()

    def _exit_manage(self):
        if self._manage_rail is not None:
            try:
                self._manage_rail._bar().valueChanged.disconnect(
                    self._rail_sync_from_grid)
            except TypeError:
                pass      # never connected, or already torn down
        self.single.rail_delegate = None
        self.single.nav_scroll_mode(self.single._continuous)
        self._stack.setCurrentWidget(self.single)
        # Restore sidebar / layout via callback
        cb = getattr(self, '_on_manage_exit', None)
        if cb:
            cb()
            self._on_manage_exit = None
        # Jump to the last selected page. go_to, not a bare _current write:
        # in continuous mode the render recomputes the page from the scroll
        # position, so writing _current was silently ignored there — and in
        # paged mode it kept the previous page's scroll offset.
        self.single.refresh()
        if self.model and self.model.selected:
            last_pos = max(
                pos for pos, uid in enumerate(self.model.order)
                if uid in self.model.selected)
            self.single.go_to(last_pos + 1)
        self.single._view.setFocus()
        self.changed.emit()

    # ── unsaved changes ──────────────────────────────────────────────────────

    def is_dirty(self):
        return self._dirty

    def saved_at(self):
        """When this document was last written, or None if it has not been."""
        return self._saved_at

    def mark_dirty(self, dirty=True):
        if self._dirty != bool(dirty):
            self._dirty = bool(dirty)
            self.dirty_changed.emit(self._dirty)

    # ── undo history ─────────────────────────────────────────────────────────

    def snapshot(self):
        """The document's shape right now: which pages it has, in what order,
        turned which way, and which file they are read from."""
        return (list(self.model.order),
                dict(self.model.rotations),
                dict(self.model.src),
                self.model._next_uid,
                dict(self.model.foreign_src),
                self.pdf_path)

    def push_history(self):
        """Record the current shape, before changing it. Every edit calls this
        first — the page manager's and the toolbar's alike."""
        if not self.model:
            return
        self._history.append(self.snapshot())
        self._redo_stack.clear()      # a new edit abandons the redo branch
        if len(self._history) > self.HISTORY_MAX:
            self._history.pop(0)
        self.mark_dirty(True)

    def can_undo(self):
        return bool(self._history)

    def can_redo(self):
        return bool(self._redo_stack)

    def undo(self):
        if not self._history:
            return False
        self._redo_stack.append(self.snapshot())
        if len(self._redo_stack) > self.HISTORY_MAX:
            self._redo_stack.pop(0)
        self._restore(self._history.pop())
        # Undoing back to the last saved state is not "clean": the file on disk
        # may itself be a rewritten temp (an inserted blank page swaps the
        # source file), so there is no cheap way to know. Saying "unsaved" when
        # the truth is unknown is the safe direction.
        self.mark_dirty(True)
        return True

    def redo(self):
        if not self._redo_stack:
            return False
        self._history.append(self.snapshot())
        if len(self._history) > self.HISTORY_MAX:
            self._history.pop(0)
        self._restore(self._redo_stack.pop())
        self.mark_dirty(True)
        return True

    def _restore(self, snap):
        order, rotations, src, next_uid, foreign_src, pdf_path = snap
        self.model.order       = order
        self.model.rotations   = rotations
        self.model.src         = src
        self.model._next_uid   = next_uid
        self.model.foreign_src = foreign_src
        self.model.selected.clear()
        if pdf_path and pdf_path != self.pdf_path:
            self.retarget(pdf_path)
        self.refresh_views()

    def retarget(self, path):
        """Point everything that resolves a page index at `path`.

        Four places cache it — the tab, the single view, the page manager and
        its grid — and an edit that rewrites the file (inserting a blank page,
        inserting another document) has to move all four together, or the views
        resolve new indexes against the old, shorter file."""
        self.pdf_path        = path
        self.single.pdf_path = path
        if self._manage_panel is not None:
            self._manage_panel.pdf_path = path
            self._manage_panel.grid.pdf_path = path

    def refresh_views(self):
        """Redraw whatever is on screen after the model changed."""
        if self._manage_panel is not None:
            self._manage_panel.grid._rebuild()
            self._manage_panel.grid.order_changed.emit()
        self.single.refresh()
        self.changed.emit()

    def _print(self):
        """Oeffnet den vollstaendigen Druckdialog."""
        if not self.model:
            return
        dlg = PrintDialog(self.pdf_path, self.model, self)
        dlg.exec()

    def page_count(self):
        return len(self.model.order) if self.model else 0

    def in_manage_mode(self):
        """True while this tab is showing the page manager rather than the
        single-page preview."""
        return bool(self._manage_widget is not None
                    and self._stack.currentWidget() is self._manage_widget)

    def selected_uids(self):
        """Selected pages in display order — empty when nothing is picked."""
        if not self.model: return []
        return [uid for uid in self.model.order if uid in self.model.selected]

    def save_to(self, out_path, uids=None):
        """Write the document as the page manager shows it. This is what Ctrl+S
        and Datei ▸ Speichern go through.

        `uids` limits the output to those pages, in display order — used by
        Ctrl+Shift+S when pages are picked in the page manager."""
        if not self.model: raise ValueError(tr("Keine PDF geladen."))
        from tools.render.caches import invalidate_revision
        from pypdf import PdfReader, PdfWriter
        readers = {}
        def _rdr(p):
            if p not in readers: readers[p] = PdfReader(p, strict=False)
            return readers[p]
        writer = PdfWriter()
        wanted = None if uids is None else set(uids)
        for uid in self.model.order:
            if wanted is not None and uid not in wanted: continue
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            reader = _rdr(src_path)
            if orig >= len(reader.pages): continue
            page = reader.pages[orig]
            rot  = self.model.get_rotation(uid)
            if rot: page.rotate(rot)
            writer.add_page(page)
        n = len(writer.pages)
        # Write beside the target and rename over it: saving in place is the
        # normal case here (Ctrl+S), and writing straight into the file we are
        # reading from leaves a truncated PDF behind if anything fails midway.
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf", dir=os.path.dirname(os.path.abspath(out_path)))
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                writer.write(f)
            os.replace(tmp_path, out_path)
        except Exception:
            try: os.unlink(tmp_path)
            except OSError: pass   # nothing written, or already removed
            raise
        invalidate_revision(out_path)
        # Only a save of the whole document settles it. Writing a subset is an
        # export — the tab still shows everything, and everything is still
        # unsaved.
        if uids is None:
            import time
            self._saved_at = time.time()
            self.mark_dirty(False)
        return tr('Gespeichert: {p0} Seiten -> {p1}').format(p0=n, p1=out_path)
