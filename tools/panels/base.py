"""
BasePanel v3
============
- Liest aktuelle PDF aus AppState (kein eigener Datei-Dialog noetig)
- Scrollbar damit Inhalte nie abgeschnitten werden
- Alles auf Main-Thread — keine Crashes
- Ergebnisse werden automatisch in neuem Tab geöffnet
"""
import logging
import os
import tempfile
import traceback
import uuid
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QListWidget, QListWidgetItem, QFrame,
    QAbstractItemView, QScrollArea, QApplication,
    QSizePolicy, QComboBox
)
from PyQt6.QtCore import Qt, QEvent, pyqtSignal
from tools.app_state import AppState, theme_color
from tools.i18n      import tr
from tools.pdf_access import is_locked
from tools.shell.protokoll import LogAdapter
from tools.theme      import _TV
from tools.snapshots  import ensure_view_snapshot


class ToolScrollArea(QScrollArea):
    """QScrollArea that never auto-scrolls when a child widget receives focus.

    Qt's default QScrollArea installs an event filter on every child widget
    and calls ensureWidgetVisible() on FocusIn events.  This causes section
    labels that sit just above a button to be half-cropped at the top of the
    viewport the moment that button gets focus.  Intercepting FocusIn before
    the base-class eventFilter runs prevents that behaviour entirely.
    """
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            return False   # don't let focus trigger ensureWidgetVisible
        return super().eventFilter(obj, event)


def make_label(text: str, dim=False, bold=False) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    if dim:
        lbl.setObjectName("dimLabel")
    if bold:
        f = lbl.font(); f.setBold(True); lbl.setFont(f)
    return lbl


def make_separator() -> QFrame:
    sep = QFrame()
    sep.setObjectName("separator")
    sep.setFrameShape(QFrame.Shape.HLine)
    return sep


def _describe_os_error(exc):
    """A file-system failure in the words of someone trying to save a file.

    The path matters more than the errno: "cannot write here" is only useful
    if it says where here is.
    """
    path = getattr(exc, "filename", None) or ""
    reason = getattr(exc, "strerror", None) or str(exc)
    if path:
        return tr('Die Datei konnte nicht geschrieben werden:\n{p0}\n\n{p1}').format(
            p0=path, p1=reason)
    return tr('Die Datei konnte nicht geschrieben werden:\n{p0}').format(p0=reason)


class CurrentFileBar(QWidget):
    """Which PDF the tool will work on. Read-only, at the top of every tool.

    It used to carry an "Andere Datei..." button that called
    AppState.open_pdf() directly. That was a second way into the application
    that bypassed the first: it swapped the file under the tool without opening
    a tab, so the page manager's model still described the document that was
    there before — and every tool reads its pages through that model. Files are
    opened in the viewer, which is the only place that builds the model to go
    with them.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("currentFileBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        icon = QLabel(tr("PDF:"))
        icon.setObjectName("dimLabel")
        icon.setFixedWidth(30)
        layout.addWidget(icon)

        self.file_label = QLabel(tr("Keine Datei geöffnet — öffne zuerst eine PDF im Page Viewer"))
        self.file_label.setObjectName("currentFileLabel")
        self.file_label.setWordWrap(False)
        # Don't let the (long) filename/placeholder dictate the width of the bar —
        # otherwise it forces narrow tool sidebars far wider than they should be.
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.file_label, 1)

        # Update wenn sich die aktuelle PDF aendert
        AppState.get().pdf_changed.connect(self._update)
        self._update(AppState.get().current_pdf)

    def _update(self, path: str):
        if path and os.path.isfile(path):
            self.file_label.setText(os.path.basename(path))
            self.file_label.setObjectName("currentFileLabel")
        else:
            self.file_label.setText(tr("Keine Datei geöffnet — öffne zuerst eine PDF im Page Viewer"))
            self.file_label.setObjectName("dimLabel")
        self.file_label.setStyleSheet("")  # let QSS handle colour
        self.file_label.style().unpolish(self.file_label)
        self.file_label.style().polish(self.file_label)

class FileDropList(QListWidget):
    def __init__(self, extensions=("*.pdf",), parent=None):
        super().__init__(parent)
        self.extensions = [e.replace("*", "").lower() for e in extensions]
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(100)

    def dragEnterEvent(self, e):
        # Internes Umsortieren
        if e.source() is self:
            e.acceptProposedAction()
        # Externe Dateien vom Dateimanager
        elif e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is self:
            e.acceptProposedAction()
        elif e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        # Internes Drag & Drop — Qt übernimmt das Umsortieren
        if e.source() is self:
            super().dropEvent(e)
            return
        # Externe Dateien hinzufügen
        for url in e.mimeData().urls():
            path = url.toLocalFile()
            ext  = os.path.splitext(path)[1].lower()
            if any(ext == x for x in self.extensions):
                self._add_one(path)
        e.acceptProposedAction()

    def _add_one(self, path):
        item = QListWidgetItem("  " + os.path.basename(path))
        item.setData(Qt.ItemDataRole.UserRole, path)
        self.addItem(item)

    def add_files(self, paths):
        for p in paths: self._add_one(p)

    def get_paths(self):
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]

    def remove_selected(self):
        for item in self.selectedItems(): self.takeItem(self.row(item))

    def move_up(self):
        r = self.currentRow()
        if r > 0:
            item = self.takeItem(r); self.insertItem(r-1, item); self.setCurrentRow(r-1)

    def move_down(self):
        r = self.currentRow()
        if r < self.count()-1:
            item = self.takeItem(r); self.insertItem(r+1, item); self.setCurrentRow(r+1)


class BasePanel(QWidget):
    TITLE    = "Tool"
    SUBTITLE = ""
    # Beschriftung des Ausführen-Buttons (pro Tool überschreibbar)
    RUN_LABEL = "  Ausführen"

    # Emitted when the operator clicks the "‹ Werkzeuge" back button; the
    # window listens and returns the sidebar to the tool list.
    back_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup()

    def _setup(self):
        self.controls_widget = self.build_controls_widget()
        self.preview_widget = None

    def build_controls_widget(self) -> QWidget:
        """The settings panel a tool morphs the whole sidebar into when it is
        picked (Phase 5). Top to bottom: a "‹ Werkzeuge" back button, the tool
        title, the current-file bar, the tool's own fields, and the action row.
        No nested scroll — the SidebarHost's toolscroll is the one and only
        scroll surface. Sets the attributes the helpers rely on (file_bar,
        log, run_btn, stop_btn, back_btn)."""
        content = QWidget(self)
        lay = QVBoxLayout(content)
        lay.setContentsMargins(10, 8, 12, 10)
        lay.setSpacing(8)

        self.back_btn = QPushButton(tr("‹ Werkzeuge"))
        self.back_btn.setObjectName("secondaryBtn")
        self.back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_requested.emit)
        lay.addWidget(self.back_btn)

        title = QLabel(tr(self.TITLE))
        tf = title.font(); tf.setPointSize(13); tf.setBold(True); title.setFont(tf)
        lay.addWidget(title)
        lay.addWidget(make_separator())

        self.file_bar = CurrentFileBar()
        lay.addWidget(self.file_bar)
        lay.addWidget(make_separator())

        self.build_ui(lay)

        # Let combo boxes shrink with the sidebar instead of demanding their
        # widest item's full width (which used to blow the sidebar past its cap
        # and clip the right-hand side of the controls). A combo's horizontal
        # size policy is Fixed by default, so it also needs to be told to
        # expand into the field row — otherwise it stays at its sizeHint and
        # clips long item texts at the widget edge.
        for combo in content.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                combo.sizePolicy().verticalPolicy())

        lay.addStretch()
        lay.addWidget(make_separator())

        self.log = LogAdapter()

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self.build_action_row(btn_row)
        lay.addLayout(btn_row)

        return content

    def build_tool_sidebar(self) -> QWidget:
        """Standardized left sidebar for split-view tools (Crop, Grayscale,
        N-Up). Every "sliding" sidebar is built here so they stay identical:
        a scrollable PANEL-coloured column with title, the current-file bar, the
        tool's own controls (build_ui), the log, and the action row. Sets
        self.file_bar / self.log / self.run_btn and returns the widget to drop
        into the splitter. Kept vertically compact so the whole sidebar fits
        without scrolling on typical screens.
        """
        panel = QWidget()
        panel.setObjectName("toolLeftPanel")
        panel.setStyleSheet(f"QWidget#toolLeftPanel{{background:{theme_color('PANEL')};}}")
        self._tool_left_w = panel   # exposed for panels that re-theme dynamically

        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 10); lay.setSpacing(6)

        title = QLabel(tr(self.TITLE))
        tf = title.font(); tf.setPointSize(13); tf.setBold(True); title.setFont(tf)
        lay.addWidget(title)
        lay.addWidget(make_separator())

        self.file_bar = CurrentFileBar()
        lay.addWidget(self.file_bar)
        lay.addWidget(make_separator())

        self.build_ui(lay)

        # Let combo boxes shrink with the sidebar instead of demanding their
        # widest item's full width (which used to blow the sidebar past its cap
        # and clip the right-hand side of the controls).
        for combo in panel.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                combo.sizePolicy().verticalPolicy())

        lay.addStretch()
        lay.addWidget(make_separator())

        self.log = LogAdapter()

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self.build_action_row(btn_row)
        lay.addLayout(btn_row)

        scroll = ToolScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # AsNeeded is a safety net: if a tool's controls are still wider than the
        # sidebar, they stay reachable via a scrollbar instead of being clipped.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(f"QScrollArea{{background:{theme_color('PANEL')};border:none;}}")
        scroll.setWidget(panel)
        scroll.setMinimumWidth(340); scroll.setMaximumWidth(480)
        return scroll

    def build_tool_right_panel(self) -> QWidget:
        """The right half of a split-view tool (Crop, N-Up, Grayscale): a
        themed, empty container to build the preview into. Companion to
        build_tool_sidebar() above for the left half — three panels used to
        build this by hand, six identical lines each, and because those lines
        set the object name apply_split_view_theme() below styles, the two had
        drifted out of step in one of them before this was factored out.

        Returns the empty widget; add the tool's own preview into its layout
        (already a zero-margin QVBoxLayout). Sets self._tool_right_w, which
        apply_split_view_theme() reads.
        """
        right_w = QWidget()
        right_w.setObjectName("toolRightPanel")
        self._tool_right_w = right_w
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(0, 0, 0, 0)
        right_l.setSpacing(0)
        return right_w

    def apply_split_view_theme(self):
        """The two stylesheet lines every split-view tool's _apply_theme()
        needs for build_tool_sidebar()'s and build_tool_right_panel()'s
        widgets. Call this first and add whatever else the tool's own
        right-pane content needs (a preview's background, a splitter handle,
        a status bar) after it — this only knows about the two containers."""
        t = _TV
        self._tool_left_w.setStyleSheet(
            f"QWidget#toolLeftPanel{{background:{t['panel_bg']};}}")
        self._tool_right_w.setStyleSheet(
            f"QWidget#toolRightPanel{{background:{t['viewer_bg']};}}")

    # ── Override ──────────────────────────────────────────────────────────────

    def build_ui(self, layout: QVBoxLayout):
        pass

    def add_stop_button(self, row: QHBoxLayout):
        """Put the Stop button in `row`. Hidden until something is running.

        Separate from build_action_row because two panels build their own
        action row — one to rename the button, one because its actions live
        inside its group boxes — and a tool that can start work it cannot stop
        is the thing this exists to prevent. Overriding build_action_row
        without calling this is caught by a test.
        """
        self.stop_btn = QPushButton(tr("Stopp"))
        self.stop_btn.setObjectName("secondaryBtn")
        self.stop_btn.setMinimumWidth(90)
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.stop_running)
        row.addWidget(self.stop_btn)
        return self.stop_btn

    def build_action_row(self, row: QHBoxLayout):
        row.addStretch()
        # A tool that has taken a wrong turn — the wrong file, the wrong
        # settings, or simply longer than the person at the counter has — used
        # to leave no way out but killing the window, which loses every other
        # tab with it.
        self.add_stop_button(row)

        self.run_btn = QPushButton(tr(self.RUN_LABEL))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.setMinimumWidth(120)
        self.run_btn.clicked.connect(self._safe_run)
        row.addWidget(self.run_btn)

        from PyQt6.QtGui import QKeySequence, QShortcut
        sc = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc.activated.connect(self._enter_pressed)
        sc2 = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        sc2.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc2.activated.connect(self._enter_pressed)

    def _run_action(self) -> str:
        raise NotImplementedError

    def _enter_pressed(self):
        """Enter-Taste fuehrt immer das Tool aus. (Checkboxen toggelt man mit der
        Leertaste — Enter eine Einstellung aendern zu lassen war verwirrend.)"""
        self._safe_run()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def current_pdf(self) -> str:
        """Pfad der PDF, die das Tool verarbeiten soll — die Seiten so, wie der
        Viewer sie zeigt (Reihenfolge, Drehung, eingefügte Seiten). Für den
        Original-Dateinamen (Titelzeile, Ausgabename) AppState.current_pdf.

        Call this, not AppState.get().current_pdf, from anywhere that measures
        or previews a page — the page manager's rotations and reordering are
        only reflected here. Reading AppState's path directly measures and
        previews the file as it is on disk, so a rotated page came out in its
        original orientation."""
        return ensure_view_snapshot(AppState.get().current_pdf)

    def require_pdf(self) -> str:
        """Gibt aktuellen PDF-Pfad zurück, wirft Fehler wenn keine offen."""
        path = AppState.get().current_pdf
        if not path:
            raise ValueError(tr(
                "Keine PDF geöffnet.\n"
                "Öffne zuerst eine PDF im Page Viewer (linke Seite)."))
        if not os.path.isfile(path):
            # A document *is* open — its file has gone from under it. Saying
            # "no PDF open" here sent the operator to open the file they
            # already had open, which is the one thing that cannot work; a
            # stick pulled out, a share dropped or a file moved by someone
            # else all arrived as that same wrong sentence.
            raise ValueError(tr(
                "Die Datei ist nicht mehr auffindbar:\n{p0}\n\n"
                "Sie wurde verschoben, umbenannt oder gelöscht — oder das "
                "Laufwerk ist nicht mehr verbunden. Die Seitenansicht zeigt "
                "noch den letzten Stand; zum Weiterarbeiten die Datei wieder "
                "verfügbar machen und neu öffnen."
            ).format(p0=path))
        # Central guard: a locked PDF cannot be processed and otherwise surfaces
        # as a cryptic library error (PasswordError / FileNotDecryptedError /
        # PdfiumError) in each tool. Give one clear message instead.
        #
        # Locked, not merely encrypted. This used to test is_encrypted, which is
        # true of every restricted file as well — an owner password with no user
        # password — and those read perfectly. The tools were refusing documents
        # that pikepdf and pdfium open without being asked for anything.
        if is_locked(path):
            raise ValueError(tr(
                "Diese PDF ist passwortgeschützt.\n"
                "Bitte zuerst entsperren (Passwort entfernen), dann erneut öffnen."))
        # And a document with no pages in it, which every tool then failed at in
        # its own way — "Accessing nonexistent PDF page number" from one,
        # "Failed to load document (PDFium: Success)" from another.
        try:
            import pikepdf
            with pikepdf.open(path) as _pdf:
                empty = len(_pdf.pages) == 0
        except Exception:
            empty = False      # unreadable for another reason: let the tool say so
        if empty:
            raise ValueError(tr("Das PDF enthält keine Seiten."))
        # Only now flatten the page manager's view: the guards above have to run
        # on the real file (an encrypted one can't be copied page by page).
        return ensure_view_snapshot(path)

    def open_result(self, path: str, title: str = ""):
        """Öffnet ein Ergebnis in einem neuen Tab."""
        AppState.get().open_result(path, title)

    def _safe_run(self):
        self.log.clear_log()
        if hasattr(self, "run_btn"):
            self.run_btn.setEnabled(False)
        QApplication.processEvents()
        try:
            msg = self._run_action()
            if msg is not None:
                self.log.log(msg or "Fertig.", hold=True)
        except (ValueError, RuntimeError) as e:
            # Intentional, user-facing errors (no PDF, encrypted, bad input …):
            # show just the message, not a scary traceback.
            self.log.log(str(e), error=True)
        except OSError as e:
            # A read-only stick, a full disk, a folder that is no longer there.
            # None of those is a bug in this application and none of them is
            # helped by a traceback — but eight of the thirteen tools printed
            # one, because "cannot write the file" arrived here as an
            # unexpected exception like any other.
            self.log.log(_describe_os_error(e), error=True)
            logging.debug("tool could not write its output", exc_info=True)
        except Exception as e:
            # Unexpected error = likely a bug: keep the traceback for diagnosis.
            self.log.log(str(e), error=True)
            self.log.log(traceback.format_exc(), error=True)
        finally:
            # Skip re-enabling if the action manages the button itself (e.g. async OCR)
            if hasattr(self, "run_btn") and not getattr(self, "_async_running", False):
                self.run_btn.setEnabled(True)

    def run_async(self, work_fn, on_done, *, on_error=None,
                  on_progress=None, busy_label=None):
        """Run heavy work off the UI thread so the window stays responsive.

        ``work_fn(report)`` executes on a worker thread. ``report`` is a
        :class:`tools.jobs.Progress`: call it to post a progress line, and use
        ``report.check()`` / ``report.run(cmd)`` so the work can be stopped.
        It must touch only plain data, never Qt widgets. When it returns,
        ``on_done(result)`` runs back on the GUI thread; failures go to
        ``on_error(exc)`` (default: log the error). The run button is disabled
        for the duration (optionally relabelled to ``busy_label``), a Stop
        button appears beside it, and both are restored afterwards. Call this
        from _run_action and ``return None``.
        """
        job = getattr(self, "_async_job", None)
        if job is not None and not job.is_finished:
            raise RuntimeError(tr("Vorgang läuft bereits — bitte warten."))

        self._async_running = True
        prev_label = None
        if hasattr(self, "run_btn"):
            self.run_btn.setEnabled(False)
            if busy_label:
                prev_label = self.run_btn.text()
                self.run_btn.setText(tr(busy_label))
        if hasattr(self, "stop_btn"):
            self.stop_btn.setEnabled(True)
            self.stop_btn.setVisible(True)

        def _restore():
            self._async_running = False
            if hasattr(self, "run_btn"):
                self.run_btn.setEnabled(True)
                if prev_label is not None:
                    self.run_btn.setText(prev_label)
            if hasattr(self, "stop_btn"):
                self.stop_btn.setVisible(False)
            # Confirmation belongs here, not on the error path: a cancelled job
            # deliberately emits no error — stopping is not a failure — so the
            # only place that reliably runs after a stop is `finished`. Without
            # this the log ended on "Wird abgebrochen …" and never said it had.
            job = getattr(self, "_async_job", None)
            if job is not None and job.cancelled:
                self.log.log(tr("Abgebrochen — es wurde nichts geschrieben."))

        def _progress(msg):
            (on_progress or self.log.log)(msg)

        def _done(result):
            _restore()
            on_done(result)

        def _error(exc):
            _restore()
            if on_error:
                on_error(exc)
            else:
                self.log.log(str(exc), error=True)

        # A pool job, not a QThread. The panel keeps a handle so it can tell
        # whether work is still running and cancel it, but the job's lifetime no
        # longer depends on that reference: tools/jobs.py owns it until run()
        # has returned. _restore is also hung off `finished`, so a cancelled job
        # still gives the button back.
        from tools.jobs import Progress, submit
        self._async_job = submit(
            lambda job: work_fn(Progress(job)),
            owner=self, name=type(self).__name__,
            on_progress=_progress, on_done=_done, on_error=_error,
            on_finished=_restore)

    def stop_running(self):
        """Stop whatever this panel started. Safe to call when nothing is."""
        job = getattr(self, "_async_job", None)
        if job is None or job.is_finished:
            return False
        job.cancel()
        if hasattr(self, "stop_btn"):
            # Left visible but dead: the work stops at its next checkpoint,
            # which on a Ghostscript run is immediate and on a page loop is
            # the end of the current page. A button that vanished here would
            # read as "already stopped" while the log was still moving.
            self.stop_btn.setEnabled(False)
        self.log.log(tr("Wird abgebrochen …"))
        return True

    def save_pdf(self, caption="PDF speichern als") -> str:
        src = AppState.get().current_pdf
        stem = os.path.splitext(os.path.basename(src))[0] if src else "output"
        slug = caption.lower()
        for ch in " aeoeuessaaaaeeeeiiiioooouuuu./\\:()→":
            slug = slug.replace(ch, "_")
        slug = slug.strip("_")[:30]
        tmp_dir = os.path.join(tempfile.gettempdir(), "copyshop_output")
        os.makedirs(tmp_dir, exist_ok=True)
        return os.path.join(tmp_dir, f"{stem}_{slug}_{uuid.uuid4().hex[:6]}.pdf")

    def save_dir(self) -> str:
        src = AppState.get().current_pdf
        stem = os.path.splitext(os.path.basename(src))[0] if src else "output"
        tmp_dir = os.path.join(tempfile.gettempdir(), "copyshop_output", f"{stem}_{uuid.uuid4().hex[:6]}")
        os.makedirs(tmp_dir, exist_ok=True)
        return tmp_dir

    def pick_pdf(self, caption="PDF öffnen") -> str:
        path, _ = QFileDialog.getOpenFileName(self, tr(caption), "", tr("PDF Dateien (*.pdf)"))
        return path or ""

    def pick_pdfs(self) -> list:
        paths, _ = QFileDialog.getOpenFileNames(self, tr("PDFs öffnen"), "", tr("PDF Dateien (*.pdf)"))
        return paths

    def pick_images(self) -> list:
        # Same image list the rest of the app accepts, so this dialog cannot
        # hide a format the converter handles (it was missing *.gif).
        from tools.multi_open import IMAGE_EXTS
        pattern = " ".join("*" + e for e in sorted(IMAGE_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Bilder öffnen"), "", tr("Bilder") + f" ({pattern})")
        return paths
