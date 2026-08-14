"""
BasePanel v3
============
- Liest aktuelle PDF aus AppState (kein eigener Datei-Dialog nötig)
- Scrollbar damit Inhalte nie abgeschnitten werden
- Alles auf Main-Thread — keine Crashes
- Ergebnisse werden automatisch in neuem Tab geöffnet
"""
import os
import tempfile
import traceback
import uuid
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QListWidget, QListWidgetItem, QFrame,
    QAbstractItemView, QPlainTextEdit, QScrollArea, QApplication,
    QSizePolicy, QComboBox
)
from PyQt6.QtCore import Qt, QEvent
from tools.app_state import AppState, theme_color
from tools.i18n      import tr


_VIEW_SNAPSHOTS: dict = {}     # model signature -> flattened temp PDF
_SNAPSHOT_PATHS: set = set()   # the values above, for "is this path one of ours?"


def _remember_snapshot(sig, path):
    _VIEW_SNAPSHOTS[sig] = path
    _SNAPSHOT_PATHS.add(path)


def _forget_snapshot(sig):
    """Drop one snapshot and return the file it pointed at, or None."""
    path = _VIEW_SNAPSHOTS.pop(sig, None)
    if path is not None:
        _SNAPSHOT_PATHS.discard(path)
    return path


def _model_signature(model, base_path):
    """Everything about a PageModel that changes the document a tool should see."""
    return (base_path,
            tuple(model.order),
            tuple(sorted(model.src.items())),
            tuple(sorted((u, r) for u, r in model.rotations.items() if r)),
            tuple(sorted(model.foreign_src.items())))


def ensure_view_snapshot(base_path: str) -> str:
    """Path to a PDF that matches what "Seiten verwalten" is showing, writing
    one first if there is not one already.

    Named for the write, because there is one. The old name, displayed_pdf,
    read like an accessor and hid that calling it can put a file in the
    temp directory.

    Tools used to process the file on disk, but the page manager keeps the page
    order, the rotations, pages pulled in from other tabs and inserted blanks in
    an in-memory PageModel — the file only catches up when the user saves. A
    booklet built from a document whose pages had been reordered was therefore
    imposed from the *old* order, and "Leere Seite einfuegen" (which appends the
    blank to the end of the file and only records where it should appear) put
    that blank on the back of the cover.

    Returns `base_path` untouched when the model is a plain 1:1 view of the file
    — the common case, so nothing is written — and otherwise a temp PDF
    flattened into display order, cached per model state so repeated tool runs
    reuse it. Same page-for-page result as PdfTab.save_to().
    """
    model = getattr(AppState.get(), "page_model", None)
    if model is None or not getattr(model, "order", None) or not base_path:
        return base_path
    if base_path in _SNAPSHOT_PATHS:
        return base_path       # already flattened — never apply the model twice
    try:
        sig = _model_signature(model, base_path)
    except Exception:
        return base_path                       # not a model we understand
    cached = _VIEW_SNAPSHOTS.get(sig)
    if cached and os.path.isfile(cached):
        return cached
    try:
        from pypdf import PdfReader, PdfWriter
        n_file = len(PdfReader(base_path, strict=False).pages)
        # Identity view — the file already *is* what the user sees.
        if (not any(model.rotations.values()) and not model.foreign_src
                and [model.src.get(u) for u in model.order] == list(range(n_file))):
            return base_path
        readers = {}
        def _rdr(p):
            if p not in readers: readers[p] = PdfReader(p, strict=False)
            return readers[p]
        writer = PdfWriter()
        for uid in model.order:
            src_path, orig = model.page_source(uid, base_path)
            page = _rdr(src_path).pages[orig]
            rot  = model.get_rotation(uid)
            if rot: page.rotate(rot)
            writer.add_page(page)
        tmp_dir = os.path.join(tempfile.gettempdir(), "copyshop_view")
        os.makedirs(tmp_dir, exist_ok=True)
        out = os.path.join(tmp_dir, f"view_{uuid.uuid4().hex[:8]}.pdf")
        with open(out, "wb") as f:
            writer.write(f)
    except Exception:
        return base_path        # never block a tool because the snapshot failed
    # Drop the previous snapshot of the same file — one temp file per document,
    # not one per edit.
    for old_sig in [s for s in _VIEW_SNAPSHOTS if s[0] == base_path]:
        stale = _forget_snapshot(old_sig)
        try:
            os.remove(stale)
        except OSError:
            pass       # already gone, or the temp dir was cleared under us
    _remember_snapshot(sig, out)
    return out


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


class CurrentFileBar(QWidget):
    """
    Zeigt die aktuell geöffnete PDF an.
    Erscheint oben in jedem Tool.
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

        self.open_btn = QPushButton(tr("Andere Datei..."))
        self.open_btn.setObjectName("secondaryBtn")
        self.open_btn.setFixedHeight(26)
        self.open_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.open_btn.clicked.connect(self._pick_other)
        layout.addWidget(self.open_btn)

        # Update wenn sich die aktuelle PDF ändert
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

    def _pick_other(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("PDF öffnen"), "", tr("PDF Dateien (*.pdf)"))
        if path:
            AppState.get().open_pdf(path)


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


class LogBox(QPlainTextEdit):
    def __init__(self, placeholder="Log...", parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumHeight(110)
        self.setMinimumHeight(50)
        self.setPlaceholderText(placeholder)

    def log(self, msg: str, error=False):
        self.appendPlainText(("ERR  " if error else "OK   ") + msg)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def clear_log(self): self.clear()


class BasePanel(QWidget):
    TITLE    = "Tool"
    SUBTITLE = ""
    # Beschriftung des Ausführen-Buttons (pro Tool überschreibbar)
    RUN_LABEL = "  Ausführen"
    # Ob dieses Tool ein Ergebnis als neuen Tab öffnen soll
    OPENS_NEW_TAB = False

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup()

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Fixed header — title + subtitle never scroll away ─────────────
        hdr = QWidget()
        hl = QVBoxLayout(hdr)
        hl.setContentsMargins(14, 12, 14, 8)
        hl.setSpacing(3)
        title = QLabel(tr(self.TITLE))
        title.setWordWrap(True)
        tf = title.font(); tf.setPointSize(15); tf.setBold(True); title.setFont(tf)
        hl.addWidget(title)
        if self.SUBTITLE:
            sub = make_label(tr(self.SUBTITLE), dim=True)
            hl.addWidget(sub)
        outer.addWidget(hdr)
        outer.addWidget(make_separator())

        # ── Scrollable content ────────────────────────────────────────────
        scroll = ToolScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(14, 8, 14, 10)
        root.setSpacing(8)

        # Current file bar (shows which PDF is active)
        self.file_bar = CurrentFileBar()
        root.addWidget(self.file_bar)
        root.addWidget(make_separator())

        # Tool content
        self.build_ui(root)

        root.addStretch()
        root.addWidget(make_separator())

        # Log
        self.log = LogBox(tr("Log..."))
        root.addWidget(self.log)

        # Action row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.build_action_row(btn_row)
        root.addLayout(btn_row)

        scroll.setWidget(content)
        outer.addWidget(scroll)

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

        lay.addStretch()
        lay.addWidget(make_separator())

        self.log = LogBox(tr("Log..."))
        self.log.setMaximumHeight(80)   # compact in the sidebar
        lay.addWidget(self.log)

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

    # ── Override ──────────────────────────────────────────────────────────────

    def build_ui(self, layout: QVBoxLayout):
        pass

    def build_action_row(self, row: QHBoxLayout):
        row.addStretch()
        self.run_btn = QPushButton(tr(self.RUN_LABEL))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.setMinimumWidth(120)
        self.run_btn.clicked.connect(self._safe_run)
        row.addWidget(self.run_btn)

        from PyQt6.QtGui import QKeySequence, QShortcut
        sc = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        sc.activated.connect(self._enter_pressed)
        sc2 = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        sc2.activated.connect(self._enter_pressed)

    def _run_action(self) -> str:
        raise NotImplementedError

    def _enter_pressed(self):
        """Enter-Taste führt immer das Tool aus. (Checkboxen toggelt man mit der
        Leertaste — Enter eine Einstellung ändern zu lassen war verwirrend.)"""
        self._safe_run()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def current_pdf(self) -> str:
        """Pfad der PDF, die das Tool verarbeiten soll — die Seiten so, wie der
        Viewer sie zeigt (Reihenfolge, Drehung, eingefügte Seiten). Für den
        Original-Dateinamen (Titelzeile, Ausgabename) AppState.current_pdf."""
        return ensure_view_snapshot(AppState.get().current_pdf)

    def require_pdf(self) -> str:
        """Gibt aktuellen PDF-Pfad zurück, wirft Fehler wenn keine offen."""
        path = AppState.get().current_pdf
        if not path or not os.path.isfile(path):
            raise ValueError(tr(
                "Keine PDF geöffnet.\n"
                "Öffne zuerst eine PDF im Page Viewer (linke Seite)."))
        # Central guard: password-protected PDFs can't be processed and otherwise
        # surface as cryptic library errors (PasswordError / FileNotDecryptedError
        # / PdfiumError) in each tool. Give one clear message instead.
        try:
            from pypdf import PdfReader
            if PdfReader(path, strict=False).is_encrypted:
                raise ValueError(tr(
                    "Diese PDF ist passwortgeschützt.\n"
                    "Bitte zuerst entsperren (Passwort entfernen), dann erneut öffnen."))
        except ValueError:
            raise
        except Exception:
            pass   # unreadable for another reason — let the tool surface that itself
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
                self.log.log(msg or "Fertig.")
        except (ValueError, RuntimeError) as e:
            # Intentional, user-facing errors (no PDF, encrypted, bad input …):
            # show just the message, not a scary traceback.
            self.log.log(str(e), error=True)
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

        ``work_fn(report)`` executes on a worker thread (``report(msg)`` posts a
        progress line); it must touch only plain data, never Qt widgets. When it
        returns, ``on_done(result)`` runs back on the GUI thread; failures go to
        ``on_error(exc)`` (default: log the error). The run button is disabled
        for the duration (optionally relabelled to ``busy_label``) and re-enabled
        afterwards. Call this from _run_action and ``return None``.
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

        def _restore():
            self._async_running = False
            if hasattr(self, "run_btn"):
                self.run_btn.setEnabled(True)
                if prev_label is not None:
                    self.run_btn.setText(prev_label)

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
        from tools.jobs import submit
        self._async_job = submit(
            lambda job: work_fn(job.report),
            owner=self, name=type(self).__name__,
            on_progress=_progress, on_done=_done, on_error=_error,
            on_finished=_restore)

    def save_pdf(self, caption="PDF speichern als") -> str:
        src = AppState.get().current_pdf
        stem = os.path.splitext(os.path.basename(src))[0] if src else "output"
        slug = caption.lower()
        for ch in " äöüßàáâãèéêëìíîïòóôõùúûü./\\:()→":
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
