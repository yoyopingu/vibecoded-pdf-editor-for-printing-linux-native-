"""
MultiOpenDialog + MergeOrderDialog
"""
import os, shutil, subprocess, tempfile
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QProgressBar, QWidget, QFrame,
    QApplication, QAbstractItemView, QSplitter
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
OFFICE_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
               ".odt", ".ods", ".odp", ".rtf", ".pages"}
PDF_EXT     = {".pdf"}

FILE_ICONS = {
    ".pdf":"📄",".jpg":"🖼",".jpeg":"🖼",".png":"🖼",
    ".tif":"🖼",".tiff":"🖼",".bmp":"🖼",".webp":"🖼",
    ".docx":"📝",".doc":"📝",".xlsx":"📊",".xls":"📊",
    ".pptx":"📊",".ppt":"📊",".odt":"📝",".ods":"📊",
    ".odp":"📊",".rtf":"📝",".pages":"📝"
}
FILE_KINDS = {
    ".pdf":"PDF",".jpg":"JPEG",".jpeg":"JPEG",".png":"PNG",
    ".tif":"TIFF",".tiff":"TIFF",".bmp":"BMP",".webp":"WebP",
    ".docx":"Word",".doc":"Word",".xlsx":"Excel",".xls":"Excel",
    ".pptx":"PowerPoint",".ppt":"PowerPoint",
    ".odt":"Writer",".ods":"Calc",".odp":"Impress",
    ".rtf":"RTF",".pages":"Pages"
}

def classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXT:     return "pdf"
    if ext in IMAGE_EXTS:  return "bild"
    if ext in OFFICE_EXTS: return "office"
    return None

def convert_to_pdf(path, out_dir):
    kind = classify(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    out  = os.path.join(out_dir, stem + ".pdf")
    if kind == "pdf":
        return path
    if kind == "bild":
        import img2pdf
        with open(out, "wb") as f:
            f.write(img2pdf.convert(path))
        return out
    if kind == "office":
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise RuntimeError("LibreOffice nicht gefunden.\nsudo pacman -S libreoffice-still")
        r = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, path],
            capture_output=True, text=True, timeout=120)
        expected = os.path.join(out_dir, stem + ".pdf")
        if os.path.isfile(expected):
            return expected
        for f in os.listdir(out_dir):
            if f.endswith(".pdf"):
                return os.path.join(out_dir, f)
        raise RuntimeError(f"Konvertierung fehlgeschlagen:\n{r.stderr.strip()[:300]}")
    raise RuntimeError(f"Nicht unterstuetzt: {os.path.basename(path)}")


class ConvertWorker(QThread):
    progress = pyqtSignal(int, str)
    finished = pyqtSignal(list)
    error    = pyqtSignal(int, str)

    def __init__(self, files, tmp_dir):
        super().__init__()
        self.files   = files
        self.tmp_dir = tmp_dir

    def run(self):
        results = []
        for i, path in enumerate(self.files):
            self.progress.emit(i, f"Verarbeite: {os.path.basename(path)}")
            try:
                results.append(convert_to_pdf(path, self.tmp_dir))
            except Exception as e:
                self.error.emit(i, str(e))
                results.append(None)
        self.finished.emit(results)


class MergeOrderDialog(QDialog):
    """Seitenverwaltungs-Stil: Dateien sortieren BEVOR konvertiert wird."""

    def __init__(self, file_paths, parent=None):
        super().__init__(parent)
        self._paths      = list(file_paths)
        self.final_paths = []
        self.setWindowTitle("Dateien zusammenfuehren")
        self.setMinimumSize(680, 540)
        self.setModal(True)
        # App-Stylesheet übernehmen
        from PyQt6.QtWidgets import QApplication as _QA
        app = _QA.instance()
        if app: self.setStyleSheet(app.styleSheet())
        self._build()

    def _sep(self):
        from tools.page_viewer import _TV
        f = QFrame(); f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"background:{_TV['border']};max-height:1px;border:none;margin:2px 0;")
        return f

    def _btn(self, text, fn):
        b = QPushButton(text); b.setObjectName("secondaryBtn")
        b.setMinimumHeight(28); b.clicked.connect(fn); return b

    def _section(self, layout, text):
        from tools.page_viewer import _TV
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color:{_TV['acc']};font-size:10px;font-weight:bold;"
            "letter-spacing:1px;background:transparent;")
        layout.addWidget(lbl)

    def _build(self):
        from tools.page_viewer import _TV
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)

        # ── Kopfzeile ────────────────────────────────────────────────
        hdr = QWidget(); hdr.setFixedHeight(52)
        hdr.setStyleSheet(
            f"background:{_TV['sidebar_bg']};border-bottom:1px solid {_TV['border']};")
        hl = QHBoxLayout(hdr); hl.setContentsMargins(14, 0, 14, 0)
        title_lbl = QLabel("Dateien zusammenfuehren")
        tf = title_lbl.font(); tf.setPointSize(13); tf.setBold(True); title_lbl.setFont(tf)
        title_lbl.setStyleSheet(f"color:{_TV['text']};background:transparent;")
        hl.addWidget(title_lbl); hl.addStretch()
        root.addWidget(hdr)

        # ── Splitter: Steuerung links, Liste rechts ───────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"QSplitter::handle{{background:{_TV['border']};width:2px;}}")

        # ── Linke Seite: Steuerung (wie ManagePanel) ──────────────────
        left_w = QWidget(); left_w.setFixedWidth(210)
        left_w.setStyleSheet(f"background:{_TV['sidebar_bg']};")
        from PyQt6.QtWidgets import QScrollArea
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea{{background:{_TV['sidebar_bg']};border:none;}}")
        content = QWidget(); content.setStyleSheet(f"background:{_TV['sidebar_bg']};")
        ll = QVBoxLayout(content); ll.setContentsMargins(10,10,10,10); ll.setSpacing(5)
        scroll.setWidget(content)
        outer_l = QVBoxLayout(left_w); outer_l.setContentsMargins(0,0,0,0); outer_l.setSpacing(0)
        outer_l.addWidget(scroll)

        # Info-Label
        self.sel_info = QLabel("Keine Auswahl")
        self.sel_info.setWordWrap(True)
        self.sel_info.setStyleSheet(f"color:{_TV['dim']};font-size:11px;padding:3px 0;background:transparent;")
        ll.addWidget(self.sel_info)
        ll.addWidget(self._sep())

        self._section(ll, "REIHENFOLGE")
        ll.addWidget(self._btn("▲  Hoch",    self._move_up))
        ll.addWidget(self._btn("▼  Runter",  self._move_down))
        ll.addWidget(self._sep())

        self._section(ll, "OPERATIONEN")
        ll.addWidget(self._btn("✕  Entfernen  (Entf)", self._remove))
        ll.addWidget(self._sep())

        self._section(ll, "DATEI-INFO")
        self.inf_name  = QLabel("—"); self.inf_name.setWordWrap(True)
        self.inf_name.setStyleSheet(f"color:{_TV['text']};font-size:11px;background:transparent;")
        ll.addWidget(self.inf_name)
        self.inf_type  = QLabel(""); self.inf_pages = QLabel(""); self.inf_size = QLabel("")
        for w in [self.inf_type, self.inf_pages, self.inf_size]:
            w.setStyleSheet(f"color:{_TV['dim']};font-size:10px;background:transparent;")
            ll.addWidget(w)
        ll.addWidget(self._sep())

        self.total_lbl = QLabel("")
        self.total_lbl.setWordWrap(True)
        self.total_lbl.setStyleSheet(f"color:{_TV['dim']};font-size:10px;background:transparent;")
        ll.addWidget(self.total_lbl)
        ll.addStretch()

        # Fusszeile links
        self.btn_go = QPushButton("  Zusammenfuehren")
        self.btn_go.setObjectName("actionBtn")
        self.btn_go.clicked.connect(self._accept)
        ll.addWidget(self.btn_go)
        bc = QPushButton("Abbrechen"); bc.setObjectName("secondaryBtn")
        bc.clicked.connect(self.reject); ll.addWidget(bc)
        splitter.addWidget(left_w)

        # ── Rechte Seite: Dateiliste (wie PageGrid) ───────────────────
        right_w = QWidget(); right_w.setStyleSheet(f"background:{_TV['bg']};")
        rl = QVBoxLayout(right_w); rl.setContentsMargins(6,6,6,6); rl.setSpacing(0)

        hdr2 = QLabel("Drag & Drop zum Umsortieren")
        hdr2.setObjectName("dimLabel")
        hdr2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(hdr2)

        self.lst = QListWidget()
        self.lst.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.lst.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.lst.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.lst.setStyleSheet(
            f"QListWidget{{background:{_TV['bg']};border:1px solid {_TV['border']};"
            "border-radius:4px;font-size:12px;outline:none;}"
            f"QListWidget::item{{padding:12px 16px;color:{_TV['text']};"
            f"border-bottom:1px solid {_TV['border']};min-height:32px;}}"
            f"QListWidget::item:selected{{background:{_TV['sel_bg']};color:#fff;}}"
            f"QListWidget::item:hover{{background:{_TV['hover']};}}")
        self.lst.model().rowsMoved.connect(self._on_reorder)
        self.lst.currentRowChanged.connect(self._on_select)
        rl.addWidget(self.lst, 1)
        splitter.addWidget(right_w)

        splitter.setSizes([210, 460])
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        from PyQt6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self).activated.connect(self._remove)

        self._rebuild()

    def _rebuild(self):
        self.lst.blockSignals(True)
        self.lst.clear()
        for i, path in enumerate(self._paths):
            ext  = os.path.splitext(path)[1].lower()
            icon = FILE_ICONS.get(ext, "📄")
            kind = FILE_KINDS.get(ext, ext.upper().lstrip("."))
            name = os.path.basename(path)
            item = QListWidgetItem(f"  {icon}  {i+1:2d}.  {name}   [{kind}]")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.lst.addItem(item)
        self.lst.blockSignals(False)
        self._update_total()

    def _on_reorder(self):
        new_paths = []
        for i in range(self.lst.count()):
            orig = self.lst.item(i).data(Qt.ItemDataRole.UserRole)
            new_paths.append(self._paths[orig])
        self._paths = new_paths
        self._rebuild()

    def _on_select(self, row):
        if row < 0 or row >= len(self._paths):
            self.inf_name.setText("—"); self.inf_type.setText("")
            self.inf_pages.setText(""); self.inf_size.setText(""); return
        path = self._paths[row]
        ext  = os.path.splitext(path)[1].lower()
        self.inf_name.setText(os.path.basename(path))
        self.inf_type.setText(f"Typ: {FILE_KINDS.get(ext, ext.upper().lstrip('.'))}")
        try:
            sz = os.path.getsize(path)
            self.inf_size.setText(f"Groesse: {sz/1024:.0f} KB")
        except Exception:
            self.inf_size.setText("")
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                self.inf_pages.setText(f"Seiten: {len(PdfReader(path).pages)}")
            except Exception:
                self.inf_pages.setText("Seiten: ?")
        else:
            self.inf_pages.setText("Seiten: nach Konvertierung bekannt")

    def _update_total(self):
        n = len(self._paths)
        n_other = sum(1 for p in self._paths
                      if os.path.splitext(p)[1].lower() != ".pdf")
        txt = f"{n} Datei(en)"
        if n_other:
            txt += f"  —  {n_other} werden noch konvertiert"
        self.total_lbl.setText(txt)
        self.btn_go.setText(f"  🔗  Konvertieren & zusammenfuehren  ({n})")

    def _move_up(self):
        r = self.lst.currentRow()
        if r <= 0: return
        self._paths[r-1], self._paths[r] = self._paths[r], self._paths[r-1]
        self._rebuild(); self.lst.setCurrentRow(r-1)

    def _move_down(self):
        r = self.lst.currentRow()
        if r < 0 or r >= len(self._paths)-1: return
        self._paths[r], self._paths[r+1] = self._paths[r+1], self._paths[r]
        self._rebuild(); self.lst.setCurrentRow(r+1)

    def _remove(self):
        rows = sorted({self.lst.row(i) for i in self.lst.selectedItems()}, reverse=True)
        for r in rows: self._paths.pop(r)
        self._rebuild()

    def _accept(self):
        if not self._paths: return
        self.final_paths = list(self._paths)
        self.accept()


class MultiOpenDialog(QDialog):
    result_single    = pyqtSignal(list)
    result_merged    = pyqtSignal(str)
    result_merge_tab = pyqtSignal(list)  # Dateilisten an Viewer-Tab übergeben

    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.files   = [f for f in files if os.path.isfile(f) and classify(f)]
        self.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
        self._worker = None
        self.setWindowTitle("Dateien oeffnen — CopyShop PDF Suite")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._build()

    def done(self, result_code):
        """Clean up temp directory on close."""
        try: shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception: pass
        super().done(result_code)

    def _build(self):
        from tools.page_viewer import _TV
        lay = QVBoxLayout(self)
        lay.setSpacing(12); lay.setContentsMargins(20,20,20,20)

        title = QLabel(f"{len(self.files)} Datei(en) ausgewaehlt")
        f = title.font(); f.setPointSize(13); f.setBold(True); title.setFont(f)
        lay.addWidget(title)

        sub = QLabel("Was soll mit den Dateien passieren?")
        sub.setStyleSheet(f"color:{_TV['dim']};"); lay.addWidget(sub)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{_TV['border']};max-height:1px;"); lay.addWidget(sep)

        self.lst = QListWidget(); self.lst.setMaximumHeight(200)
        self.lst.setStyleSheet(
            f"QListWidget{{background:{_TV['sidebar_bg']};border:1px solid {_TV['border']};border-radius:4px;}}"
            f"QListWidget::item{{padding:4px 8px;color:{_TV['text']};}}"
            f"QListWidget::item:alternate{{background:{_TV['hover']};}}")
        self.lst.setAlternatingRowColors(True)
        for fp in self.files:
            ext = os.path.splitext(fp)[1].lower()
            self.lst.addItem(QListWidgetItem(
                f"  {FILE_ICONS.get(ext,'📄')}  {os.path.basename(fp)}"))
        lay.addWidget(self.lst)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, max(1, len(self.files)))
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet(
            f"QProgressBar{{background:{_TV['sidebar_bg']};border-radius:4px;height:8px;}}"
            f"QProgressBar::chunk{{background:{_TV['acc']};border-radius:4px;}}")
        lay.addWidget(self.progress_bar)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{_TV['dim']};font-size:11px;")
        self.status_lbl.setVisible(False); lay.addWidget(self.status_lbl)

        br = QHBoxLayout(); br.setSpacing(8)
        self.btn_cancel = QPushButton("Abbrechen"); self.btn_cancel.setObjectName("secondaryBtn")
        self.btn_cancel.clicked.connect(self.reject); br.addWidget(self.btn_cancel); br.addStretch()

        self.btn_single = QPushButton("  Einzeln oeffnen"); self.btn_single.setObjectName("secondaryBtn")
        self.btn_single.setMinimumWidth(160); self.btn_single.clicked.connect(self._do_single)
        br.addWidget(self.btn_single)

        self.btn_merge = QPushButton("  Zusammenfuehren..."); self.btn_merge.setObjectName("actionBtn")
        self.btn_merge.setMinimumWidth(200); self.btn_merge.clicked.connect(self._do_merge)
        br.addWidget(self.btn_merge); lay.addLayout(br)

    def _set_busy(self, busy):
        self.btn_single.setEnabled(not busy); self.btn_merge.setEnabled(not busy)
        self.btn_cancel.setEnabled(not busy)
        self.progress_bar.setVisible(busy); self.status_lbl.setVisible(busy)
        QApplication.processEvents()

    def _on_progress(self, idx, text):
        self.progress_bar.setValue(idx+1); self.status_lbl.setText(text)

    def _on_error(self, idx, msg):
        self.status_lbl.setText(f"Fehler: {msg[:80]}")

    def _do_single(self):
        self._set_busy(True)
        self._worker = ConvertWorker(self.files, self.tmp_dir)
        self._worker.progress.connect(self._on_progress)
        self._worker.error.connect(self._on_error)
        def _done(pdfs):
            self._set_busy(False)
            valid = [p for p in pdfs if p]
            if valid: self.result_single.emit(valid); self.accept()
        self._worker.finished.connect(_done)
        self._worker.start()

    def _do_merge(self):
        # Dateiliste an Viewer-Tab übergeben — keine Konvertierung hier
        self.result_merge_tab.emit(list(self.files))
        self.accept()

