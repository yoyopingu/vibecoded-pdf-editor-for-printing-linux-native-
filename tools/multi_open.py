"""
MultiOpenDialog — asks whether picked files should be opened separately or
merged, plus the conversion worker that turns images/office files into PDFs.

The merge ordering UI lives in page_viewer.MergeOrderWidget (shown as a tab, in
the same style as "Seiten verwalten"). A second, dialog-shaped copy of it used
to live here and had drifted out of sync; it was unreachable and is gone.
"""
import os, shutil, subprocess, tempfile
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QProgressBar, QFrame, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from tools.i18n import tr

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
            raise RuntimeError(tr("LibreOffice nicht gefunden.\nsudo pacman -S libreoffice-still"))
        r = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, path],
            capture_output=True, text=True, errors="replace", timeout=120)
        expected = os.path.join(out_dir, stem + ".pdf")
        if os.path.isfile(expected):
            return expected
        for f in os.listdir(out_dir):
            if f.endswith(".pdf"):
                return os.path.join(out_dir, f)
        raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=r.stderr.strip()[:300]))
    raise RuntimeError(tr("Nicht unterstuetzt: {p0}").format(p0=os.path.basename(path)))


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
            self.progress.emit(i, tr("Verarbeite: {p0}").format(p0=os.path.basename(path)))
            try:
                results.append(convert_to_pdf(path, self.tmp_dir))
            except Exception as e:
                self.error.emit(i, str(e))
                results.append(None)
        self.finished.emit(results)


class MultiOpenDialog(QDialog):
    result_single    = pyqtSignal(list)
    result_merged    = pyqtSignal(str)
    result_merge_tab = pyqtSignal(list)  # Dateilisten an Viewer-Tab übergeben

    def __init__(self, files, parent=None):
        super().__init__(parent)
        self.files   = [f for f in files if os.path.isfile(f) and classify(f)]
        self.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
        self._worker = None
        self.setWindowTitle(tr("Dateien oeffnen — CopyShop PDF Suite"))
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

        title = QLabel(tr('{p0} Datei(en) ausgewaehlt').format(p0=len(self.files)))
        f = title.font(); f.setPointSize(13); f.setBold(True); title.setFont(f)
        lay.addWidget(title)

        sub = QLabel(tr("Was soll mit den Dateien passieren?"))
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
        self.btn_cancel = QPushButton(tr("Abbrechen")); self.btn_cancel.setObjectName("secondaryBtn")
        self.btn_cancel.clicked.connect(self.reject); br.addWidget(self.btn_cancel); br.addStretch()

        self.btn_single = QPushButton(tr("  Einzeln oeffnen")); self.btn_single.setObjectName("secondaryBtn")
        self.btn_single.setMinimumWidth(160); self.btn_single.clicked.connect(self._do_single)
        br.addWidget(self.btn_single)

        self.btn_merge = QPushButton(tr("  Zusammenfuehren...")); self.btn_merge.setObjectName("actionBtn")
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
        self.status_lbl.setText(tr('Fehler: {p0}').format(p0=msg[:80]))

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

