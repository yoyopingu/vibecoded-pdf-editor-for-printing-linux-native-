"""
The tab host — the widget the main window puts on screen.

Opening files, closing tabs and everything that has to happen when one closes:
the render work it started is cancelled, its thumbnails and page renders are
dropped, and the parsed document behind them is released. A loaded page of a
large PDF is hundreds of megabytes, so a tab that is gone must not keep one.
"""
import os, shutil, atexit, logging
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTabWidget, QFileDialog, QApplication, QSplitter, QLineEdit
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QEvent
from PyQt6.QtGui import QKeySequence, QShortcut
from tools.app_state import AppState
from tools.i18n import tr
from tools.render.caches import _FullPageCache, _ThumbnailCache, _set_active
from tools.render.document_cache import release
from tools.viewer.merge import MergeOrderWidget
from tools.viewer.tab import PdfTab
from tools.theme import _TOP_BTN_W, _TV, _register_themed


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
                              Qt.Key.Key_Minus, Qt.Key.Key_0, Qt.Key.Key_1):
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
        self.hide_sidebar       = None   # lambda: sidebar.setVisible(False)
        self.show_sidebar       = None   # lambda: sidebar.setVisible(True)
        self._pre_manage_idx    = None   # gespeicherter Stack-Index vor Manage-Modus
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
                if self.hide_sidebar:
                    self.hide_sidebar()
                show_sb = self.show_sidebar
                def _on_exit():
                    self._exit_manage_layout()
                    if show_sb:
                        show_sb()
                try:
                    tab._enter_manage(on_exit=_on_exit)
                    self._enter_manage_layout(tab._manage_panel)
                    # Give the grid keyboard focus so arrow keys work immediately
                    if tab._manage_widget:
                        tab._manage_widget.setFocus()
                except Exception:
                    import traceback
                    logging.error(traceback.format_exc())
                    if show_sb:
                        show_sb()  # always restore sidebar on failure

    def _ensure_single_view(self):
        """Esc: immer zur Einzelansicht — Manage verlassen, Viewer zeigen, kein Zurück-Sprung."""
        if self.switch_to_viewer:
            self.switch_to_viewer()
        tab = self._current()
        if tab and tab._stack.currentWidget() is not tab.single:
            self._exit_manage_layout()
            tab._exit_manage()
            if self.show_sidebar:
                self.show_sidebar()
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Shortcuts
        # Ctrl+S / Ctrl+Shift+S belong to the Datei menu actions, which are
        # window-scoped and so fire from a tool panel too. Registering them here
        # as well made both ambiguous, and Qt then delivers neither.
        sc_print = QShortcut(QKeySequence("Ctrl+P"), self)
        sc_print.activated.connect(self._print_current)

        # ── Obere Toolbar ────────────────────────────────────────────────────
        self._top_bar = QWidget()
        self._top_bar.setObjectName("pvTopBar")
        self._top_bar.setFixedHeight(46)
        self._top_bar.setStyleSheet(
            f"QWidget#pvTopBar{{background:{_TV['sidebar_bg']};border-bottom:1px solid {_TV['border']};}}")
        top_bar = self._top_bar
        _register_themed(self)
        tbl = QHBoxLayout(top_bar)
        tbl.setContentsMargins(12, 0, 12, 0)
        tbl.setSpacing(8)

        # One primary action on the left, the document-scoped actions grouped on
        # the right in the *same* weight. "Öffnen" and "Seiten verwalten" used to
        # both be accent-filled, so the bar had two competing primaries pulling
        # at opposite ends with a third, differently-styled button beside one of
        # them.
        open_btn = QPushButton(tr("Öffnen..."))
        open_btn.setObjectName("actionBtn")
        open_btn.setMinimumWidth(_TOP_BTN_W)
        open_btn.clicked.connect(self._open)
        tbl.addWidget(open_btn)

        self._viewer_info = QLabel("")
        self._viewer_info.setObjectName("currentFileLabel")
        tbl.addWidget(self._viewer_info, 1)

        # The shortcut lives in the tooltip, not in the label: spelled out it made
        # this button twice the width of every other one in the app.
        self._manage_btn = QPushButton(tr("Seiten verwalten"))
        self._manage_btn.setObjectName("secondaryBtn")
        self._manage_btn.setToolTip(tr("Seiten verwalten") + "  (Strg+Umschalt+O)")
        self._manage_btn.setMinimumWidth(_TOP_BTN_W)
        self._manage_btn.setEnabled(False)
        self._manage_btn.clicked.connect(self._manage_current)
        tbl.addWidget(self._manage_btn)

        self._print_btn = QPushButton(tr("Drucken"))
        self._print_btn.setObjectName("secondaryBtn")
        self._print_btn.setToolTip(tr("Drucken") + "  (Strg+P)")
        self._print_btn.setMinimumWidth(_TOP_BTN_W)
        self._print_btn.setEnabled(False)
        self._print_btn.clicked.connect(self._print_current)
        tbl.addWidget(self._print_btn)

        layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Body: holds [ManagePanel (optional)] + [tabs]
        self._body = QWidget()
        self._body_layout = QHBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addWidget(self.tabs, 1)
        layout.addWidget(self._body, 1)

        self._manage_splitter_widget = None  # QSplitter shown in manage mode
        self._manage_tab             = None  # PdfTab whose panel is in the splitter
        self._toggle_in_progress     = False # reentrancy guard for _toggle_manage

        AppState.get().status_message.connect(self._on_status)

    def _apply_theme(self):
        self._top_bar.setStyleSheet(
            f"QWidget#pvTopBar{{background:{_TV['sidebar_bg']};border-bottom:1px solid {_TV['border']};}}")

    def _on_status(self, msg):
        pass  # Status wird in der Info-Leiste der SinglePageView angezeigt

    # ── Manage-Layout helpers ─────────────────────────────────────────────────

    def _enter_manage_layout(self, panel):
        """Place ManagePanel left of QTabWidget using a splitter."""
        if self._manage_splitter_widget:
            return  # already in manage layout

        panel.show()
        # Remember which tab owns this layout so _exit can detach correctly
        self._manage_tab = self._current()
        # Detach tabs from body, put into splitter alongside panel
        self._body_layout.removeWidget(self.tabs)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(panel)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 800])
        splitter.setStyleSheet(
            f"QSplitter::handle{{background:{_TV['splitter']};width:4px;}}")

        self._body_layout.addWidget(splitter, 1)
        self._manage_splitter_widget = splitter

    def _exit_manage_layout(self):
        """Restore QTabWidget to full body width, remove the splitter."""
        if not self._manage_splitter_widget:
            return
        # Detach panel back to its OWNING tab (not _current() which may have changed)
        tab = self._manage_tab
        if tab and tab._manage_panel:
            tab._manage_panel.setParent(tab)
            tab._manage_panel.hide()
        self._manage_tab = None
        # Detach tabs, then destroy the splitter
        self.tabs.setParent(None)
        self._body_layout.removeWidget(self._manage_splitter_widget)
        self._manage_splitter_widget.deleteLater()
        self._manage_splitter_widget = None
        self._body_layout.addWidget(self.tabs, 1)

    def _current(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, PdfTab) else None

    def _open(self, path=None):
        from PyQt6.QtWidgets import QMessageBox
        from tools.multi_open import (IMAGE_EXTS, OFFICE_EXTS, PDF_EXT,
                                      file_dialog_filter)
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Datei oeffnen"), "", file_dialog_filter())
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
                self, tr("Format nicht unterstuetzt"),
                tr('CopyShop kann "{p0}" nicht oeffnen.').format(p0=ext or "?"))
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
                tr("LibreOffice wird benoetigt um Office-Dateien zu oeffnen.\n"
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
        opened = ensure_openable(path, self)
        if opened is None:
            return              # cancelled at the password prompt
        path = opened
        # A damaged, encrypted or truncated PDF makes this raise. Unhandled in a
        # slot, PyQt takes the whole process down — so a single bad file killed
        # the app instead of reporting one failed open.
        try:
            tab = PdfTab(path)
        except Exception as e:
            logging.exception("open failed: %s", path)
            QMessageBox.critical(
                self, tr("Datei konnte nicht geoeffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            return
        name = os.path.basename(path)
        disp = name if len(name) <= 22 else name[:19] + "..."
        idx  = self.tabs.addTab(tab, f"  {disp}  ")
        self.tabs.setCurrentIndex(idx)
        AppState.get().open_pdf(path)
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
        from PyQt6.QtCore import QTimer

        def _focus():
            widget = self._current()
            if widget is not None and widget.isVisible():
                widget.single._view.setFocus()
        QTimer.singleShot(0, _focus)

    def open_file(self, path):
        tab = self._open(path)
        # Persist last opened file for the "reopen on startup" setting — but
        # only when it actually opened. Remembering a file that failed meant the
        # next start reopened it and failed again, every time.
        if tab is None:
            return
        try:
            from PyQt6.QtCore import QSettings
            QSettings("CopyShop", "PDFSuite").setValue("general/last_file", path)
        except Exception:
            logging.debug("could not record the last opened file", exc_info=True)

    def _open_result_tab(self, path, title):
        # Reached from AppState.result_ready, i.e. from a tool that just wrote a
        # file. If that file is unreadable this raises inside a slot and takes
        # the process with it — the tool's own error handling never sees it.
        try:
            tab = PdfTab(path)
        except Exception as e:
            logging.exception("result tab failed: %s", path)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(
                self, tr("Ergebnis konnte nicht geoeffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            return
        disp = title if len(title) <= 22 else title[:19] + "..."
        idx  = self.tabs.addTab(tab, f"  {disp}  ")
        self.tabs.setCurrentIndex(idx)
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
                from tools._base import discard_snapshots_for
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
        """Preview for several picked files, shown as a tab in the same style as
        the page manager: sort them, then either merge them into one document or
        open them as separate tabs."""
        import tempfile
        file_paths = [p for p in file_paths if os.path.isfile(p)]
        if not file_paths:
            return
        logging.debug(f"show_merge_tab: {len(file_paths)} Dateien: {file_paths}")

        # A repeat call with the same files is a double click or a re-sent open
        # request, not a second job. Raise the tab that is already open instead
        # of stacking an identical one behind it — that stack was how a fast
        # click ended up merging twice at once.
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, MergeOrderWidget) and w.source_paths == file_paths:
                self.tabs.setCurrentIndex(i)
                return

        widget = MergeOrderWidget(file_paths)   # records file_paths as source_paths
        # One conversion directory per tab. A single panel-wide one was wiped by
        # whichever merge tab was cancelled first, taking the output another tab
        # was still using with it.
        widget.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
        idx = self.tabs.addTab(widget, tr("  📂  Dateien oeffnen  "))
        self.tabs.setCurrentIndex(idx)
        self._manage_btn.setEnabled(False)
        self._print_btn.setEnabled(False)
        self._viewer_info.setText(tr("Dateien sortieren, zusammenfuehren oder einzeln oeffnen"))

        def _on_confirmed(paths):
            logging.debug(f"_on_confirmed empfangen: {len(paths)} Dateien")
            self._do_convert_and_merge(paths, widget)

        def _on_separately(paths):
            self._do_convert_and_open(paths, widget)

        def _on_cancelled():
            wi = self.tabs.indexOf(widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            self._update_toolbar()
            try: shutil.rmtree(widget.tmp_dir, ignore_errors=True)
            except Exception: pass   # ignore_errors already handles the file-level failures

        widget.merge_confirmed.connect(_on_confirmed)
        widget.open_separately.connect(_on_separately)
        widget.cancelled.connect(_on_cancelled)

    def _start_conversion(self, file_paths, merge_widget, on_done):
        """Convert the picked files to PDF in the merge tab's own temp dir and
        hand the results to `on_done(pdfs, failures)`. Shared by merge and
        open-separately.

        The conversion is a plain function on a pool job now. It used to be a
        QThread whose reference the panel had to keep alive by hand, in a set,
        until QThread.finished said the thread had really stopped — get that
        wrong and Qt aborts the process. tools/jobs.py owns the job instead, and
        it is tied to the merge tab so closing the tab stops it.
        """
        from tools.jobs import submit
        from tools.multi_open import convert_files
        self._viewer_info.setText(tr("Konvertiere Dateien..."))
        # Tab-Titel via Widget-Referenz setzen (sicher gegen Index-Shifts)
        wi = self.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.tabs.setTabText(wi, tr("  ⏳  Konvertiere...  "))

        # A file that cannot be converted is dropped from the result, so
        # convert_files hands back which ones and why — the user is told rather
        # than left with a document quietly missing pages.
        return submit(
            lambda job: convert_files(file_paths, merge_widget.tmp_dir, job),
            owner=merge_widget, name="convert-files",
            on_progress=self._viewer_info.setText,
            on_done=lambda result: on_done(result[0], result[1]))

    def _report_conversion_failures(self, failures):
        """Never let files vanish from a merge in silence."""
        if not failures:
            return
        from PyQt6.QtWidgets import QMessageBox
        detail = "\n".join(f"{os.path.basename(p)}  —  {m}" for p, m in failures)
        logging.error("conversion failed:\n%s", detail)
        AppState.get().status_message.emit(
            tr('{p0} Datei(en) konnten nicht konvertiert werden').format(p0=len(failures)))
        QMessageBox.warning(
            self, tr("Nicht konvertierte Dateien"),
            tr("Diese Dateien fehlen im Ergebnis:\n\n{p0}").format(p0=detail))

    def _conversion_failed(self, merge_widget, failures=()):
        """No file survived conversion — put the tab back the way it was so the
        user can change the list and try again instead of being stuck."""
        self._viewer_info.setText(tr("Fehler: Keine Dateien konvertiert"))
        wi = self.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.tabs.setTabText(wi, tr("  ✗  Fehler  "))
        merge_widget.set_busy(False)
        self._report_conversion_failures(failures)

    def _do_convert_and_merge(self, file_paths, merge_widget):
        """Konvertiert Dateien und fuegt sie zusammen."""
        logging.debug(f"_do_convert_and_merge: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            try:
                from pypdf import PdfWriter, PdfReader
                writer = PdfWriter()
                for path in valid:
                    for page in PdfReader(path, strict=False).pages:
                        writer.add_page(page)
                out = os.path.join(merge_widget.tmp_dir, "zusammengefuehrt.pdf")
                with open(out, "wb") as f:
                    writer.write(f)
            except Exception as e:
                logging.exception("merge failed")
                self._viewer_info.setText(tr('Fehler: {p0}').format(p0=e))
                merge_widget.set_busy(False)
                return
            wi = self.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            self._open_result_tab(out, tr("Zusammengefuehrt"))
            self._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _do_convert_and_open(self, file_paths, merge_widget):
        """"Einzeln oeffnen" — same conversion as the merge, but every file
        becomes its own tab instead of one combined document."""
        logging.debug(f"_do_convert_and_open: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            wi = self.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            failures = list(failures)
            for path in valid:
                try:
                    self._open(path)
                except Exception as e:
                    logging.exception("open failed: %s", path)
                    failures.append((path, str(e)))
            self._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _update_toolbar(self):
        tab = self._current()
        if tab and isinstance(tab, PdfTab):
            self._manage_btn.setEnabled(True)
            self._print_btn.setEnabled(True)
            self._viewer_info.setText(os.path.basename(tab.pdf_path))
        else:
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText("")

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
        tab.pdf_path        = path
        tab.single.pdf_path = path
        model.src         = {uid: i for i, uid in enumerate(model.order)}
        model.foreign_src = {}
        model.rotations   = {}
        if tab._manage_panel is not None:
            tab._manage_panel.pdf_path = path
            tab._manage_panel.grid.pdf_path = path
        idx  = self.tabs.indexOf(tab)
        name = os.path.basename(path)
        disp = name if len(name) <= 22 else name[:19] + "..."
        if idx >= 0:
            self.tabs.setTabText(idx, f"  {disp}  ")

    def _on_tab_changed(self, idx):
        # ── Always clean up manage layout when switching file tabs ────────────
        # The manage panel belongs to the outgoing tab; if it is still in the
        # splitter we must detach it now, before _current() changes meaning.
        if self._manage_splitter_widget and self._manage_tab is not None:
            old_tab = self._manage_tab   # tab that owns the panel
            # Exit manage mode on the old tab so its stack returns to single view
            if old_tab._stack.currentWidget() is not old_tab.single:
                old_tab._stack.setCurrentWidget(old_tab.single)
                old_tab._on_manage_exit = None  # discard stale callback
            try:
                self._exit_manage_layout()   # detach panel → old tab, destroy splitter
            finally:
                if self.show_sidebar:
                    self.show_sidebar()

        # ── Memory management: freeze outgoing tab, resume incoming tab ────────
        # Cancel pre-render tasks for ALL non-active tabs to stop them burning
        # cache slots that belong to the tab the user is actually looking at.
        active_widget = self.tabs.currentWidget()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, PdfTab) and tab is not active_widget:
                for t in list(tab.single._prerender_tasks):
                    t.cancel()
                tab.single._prerender_tasks.clear()
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
        if isinstance(w, PdfTab):
            _set_active(w.pdf_path, w.single._current)
            AppState.get().open_pdf(w.pdf_path)
            AppState.get().page_model   = w.model
            AppState.get().current_page = w.single._current
            self._manage_btn.setEnabled(True)
            self._print_btn.setEnabled(True)
            self._viewer_info.setText(os.path.basename(w.pdf_path))
            self.focus_page_view()
        elif isinstance(w, MergeOrderWidget):
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText(tr("Dateien sortieren, zusammenfuehren oder einzeln oeffnen"))
            w.setFocus()
        else:
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText("")
        self._sync_sidebar()

    def _sync_sidebar(self):
        """The merge preview brings its own sidebar. The app's tool nav sitting
        next to it made two stacked sidebars, the left one offering tools that
        do not apply to the view — so it steps aside for that tab, exactly as it
        does for the page manager."""
        w = self.tabs.currentWidget()
        if isinstance(w, MergeOrderWidget):
            if self.hide_sidebar: self.hide_sidebar()
        elif self._manage_splitter_widget is None:   # manage mode owns it there
            if self.show_sidebar: self.show_sidebar()
