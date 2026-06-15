"""
BasePanel v3
============
- Liest aktuelle PDF aus AppState (kein eigener Datei-Dialog nötig)
- Scrollbar damit Inhalte nie abgeschnitten werden
- Alles auf Main-Thread — keine Crashes
- Ergebnisse werden automatisch in neuem Tab geöffnet
"""
import os
import traceback
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QListWidget, QListWidgetItem, QFrame,
    QAbstractItemView, QPlainTextEdit, QScrollArea, QApplication,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QFont
from tools.app_state import AppState
from tools.i18n      import tr


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
            self, "PDF öffnen", "", "PDF Dateien (*.pdf)")
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

    # ── Override ──────────────────────────────────────────────────────────────

    def build_ui(self, layout: QVBoxLayout):
        pass

    def build_action_row(self, row: QHBoxLayout):
        row.addStretch()
        self.run_btn = QPushButton(tr("  Ausführen"))
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
        """Enter-Taste: Checkbox togglen wenn fokussiert, sonst Tool ausführen."""
        from PyQt6.QtWidgets import QCheckBox
        fw = QApplication.focusWidget()
        if fw and fw is not self and self.isAncestorOf(fw):
            if isinstance(fw, QCheckBox):
                fw.setChecked(not fw.isChecked())
                return
        self._safe_run()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def current_pdf(self) -> str:
        """Gibt den Pfad der aktuell geöffneten PDF zurück."""
        return AppState.get().current_pdf

    def require_pdf(self) -> str:
        """Gibt aktuellen PDF-Pfad zurück, wirft Fehler wenn keine offen."""
        path = self.current_pdf()
        if not path or not os.path.isfile(path):
            raise ValueError(
                "Keine PDF geöffnet.\n"
                "Öffne zuerst eine PDF im Page Viewer (linke Seite).")
        return path

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
        except Exception as e:
            self.log.log(str(e), error=True)
            self.log.log(traceback.format_exc(), error=True)
        finally:
            # Skip re-enabling if the action manages the button itself (e.g. async OCR)
            if hasattr(self, "run_btn") and not getattr(self, "_async_running", False):
                self.run_btn.setEnabled(True)

    def save_pdf(self, caption="PDF speichern als") -> str:
        import tempfile, uuid
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
        import tempfile, uuid
        src = AppState.get().current_pdf
        stem = os.path.splitext(os.path.basename(src))[0] if src else "output"
        tmp_dir = os.path.join(tempfile.gettempdir(), "copyshop_output", f"{stem}_{uuid.uuid4().hex[:6]}")
        os.makedirs(tmp_dir, exist_ok=True)
        return tmp_dir

    def pick_pdf(self, caption="PDF öffnen") -> str:
        path, _ = QFileDialog.getOpenFileName(self, caption, "", "PDF Dateien (*.pdf)")
        return path or ""

    def pick_pdfs(self) -> list:
        paths, _ = QFileDialog.getOpenFileNames(self, "PDFs öffnen", "", "PDF Dateien (*.pdf)")
        return paths

    def pick_images(self) -> list:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Bilder öffnen", "",
            "Bilder (*.png *.jpg *.jpeg *.tiff *.tif *.bmp *.webp)")
        return paths
