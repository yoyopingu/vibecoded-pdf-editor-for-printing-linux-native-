"""
One open document.

Owns the model, and switches between the single-page view and the page manager
over it. It is also the anchor of the viewer's one reference cycle: a tab builds
its children, and each child walks back up the parent chain to ask which tab
owns it, so the tab imports them at call time and they import the tab normally.
"""
import os, logging, tempfile
from PyQt6.QtWidgets import QVBoxLayout, QFrame, QScrollArea, QStackedWidget
from PyQt6.QtCore import pyqtSignal, QTimer
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


class PdfTab(PdfTabBase):
    changed = pyqtSignal()

    def __init__(self, pdf_path, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.model    = None
        self._setup()
        self._load()

    def _setup(self):
        self._stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._stack, 1)

        # Einzelansicht
        self.single = SinglePageView()
        self.single.page_changed.connect(self._on_page_changed)
        self._stack.addWidget(self.single)
        self._manage_widget = None
        self._manage_panel  = None

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
        grid_scroll.setWidget(grid)

        panel = ManagePanel(self.model, self.pdf_path, grid, parent=self, tab=self)
        panel.hide()
        panel.closed.connect(self._exit_manage)
        grid.order_changed.connect(self.changed.emit)
        grid.order_changed.connect(panel.update_info)
        grid.selection_changed.connect(panel.update_info)

        self._manage_panel = panel
        self._manage_widget = grid_scroll
        self._stack.addWidget(grid_scroll)

    def _enter_manage(self, on_exit=None):
        self._on_manage_exit = on_exit
        self._build_manage_once()
        self._manage_panel.show()
        self._stack.setCurrentWidget(self._manage_widget)

    def _exit_manage(self):
        self._stack.setCurrentWidget(self.single)
        # Restore sidebar / layout via callback
        cb = getattr(self, '_on_manage_exit', None)
        if cb:
            cb()
            self._on_manage_exit = None
        # Jump to the last selected page
        if self.model and self.model.selected:
            last_pos = max(
                pos for pos, uid in enumerate(self.model.order)
                if uid in self.model.selected)
            self.single._current = last_pos
        self.single.refresh()
        self.single._view.setFocus()
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
        return tr('Gespeichert: {p0} Seiten -> {p1}').format(p0=n, p1=out_path)
