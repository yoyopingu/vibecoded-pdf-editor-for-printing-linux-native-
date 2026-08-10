"""
File classification and the conversion worker that turns images/office files
into PDFs.

Picking several files opens page_viewer.MergeOrderWidget directly as a tab (in
the same style as "Seiten verwalten"), where the choice between merging them and
opening them separately is made. Two dialog-shaped copies of that UI used to
live here — an ordering dialog and a modal open/merge chooser in front of it.
Both are gone: the chooser could be clicked faster than it could hand over, and
every extra click became another merge tab.
"""
import os, shutil, subprocess
from PyQt6.QtCore import QThread, pyqtSignal
from tools.i18n import tr

IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp"}
OFFICE_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
               ".odt", ".ods", ".odp", ".rtf", ".pages"}
PDF_EXT     = {".pdf"}

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
    # Deliberately NOT called "finished": that name belongs to QThread and
    # shadowing it hides the only signal that says the thread has actually
    # stopped — which is what a caller must wait for before dropping its last
    # reference, or Qt aborts the process with "destroyed while still running".
    progress  = pyqtSignal(int, str)
    converted = pyqtSignal(list)
    error     = pyqtSignal(int, str)

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
        self.converted.emit(results)
