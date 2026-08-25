"""
OCR — make a scanned PDF searchable: ocrmypdf where it is installed, and
tesseract's own PDF renderer where it is not.
"""
import os, subprocess, shutil
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from PyQt6.QtWidgets import QVBoxLayout, QComboBox, QGroupBox, QCheckBox
from tools.panels.base import BasePanel, make_label
from tools.i18n import get_language, tr
from tools.panels._shared import row
from tools.render.document_cache import open_document as _open_pdf


# ══════════════════════════════════════════════════════════════════════════════
# OCR
# ══════════════════════════════════════════════════════════════════════════════
def _run_ocr(src, out, lang, deskew, skip, report):
    """Run OCR on a worker thread (via BasePanel.run_async). Returns
    (out_path, summary); raises on failure."""
    if shutil.which("ocrmypdf"):
        report(tr("Starte ocrmypdf …"))
        cmd = ["ocrmypdf", "--language", lang, "--output-type", "pdfa"]
        if deskew: cmd.append("--deskew")
        if skip:   cmd.append("--skip-text")
        cmd += [src, out]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600)
        if r.returncode not in (0, 6):
            # A silent non-zero exit used to raise RuntimeError("") — an error
            # dialog with nothing in it.
            raise RuntimeError(r.stderr.strip() or r.stdout.strip()
                               or tr('ocrmypdf beendet mit Code {p0}').format(p0=r.returncode))
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(tr("OCR hat keine Ausgabedatei erzeugt."))
        return out, tr('OCR abgeschlossen ({p0})').format(p0=lang)

    if shutil.which("tesseract"):
        return _ocr_with_tesseract(src, out, lang, deskew, skip, report)

    raise RuntimeError(tr(
        "Kein OCR-Programm gefunden.\n"
        "Installation:  pip install ocrmypdf --break-system-packages\n"
        "           oder:  sudo pacman -S tesseract tesseract-data-deu"))


def tesseract_langs():
    """Language codes tesseract actually has data for. Empty when it cannot be
    asked — the caller then falls back to the built-in list."""
    if not shutil.which("tesseract"):
        return []
    try:
        r = subprocess.run(["tesseract", "--list-langs"],
                           capture_output=True, text=True, errors="replace", timeout=20)
    except Exception:
        return []
    langs = [l.strip() for l in (r.stdout or "").splitlines()[1:] if l.strip()]
    return [l for l in langs if l != "osd"]


def _page_has_text(pdf_path, index):
    """True when this page already carries extractable text."""
    try:
        with _pdfium_lock:
            doc = _open_pdf(pdf_path)
            try:
                return bool(doc[index].get_textpage().get_text_range().strip())
            finally:
                doc.close()
    except Exception:
        return False


def _ocr_with_tesseract(src, out, lang, deskew, skip, report):
    """OCR without ocrmypdf, using tesseract's own PDF renderer.

    This used to dump a .txt file and call it done, which is not what the panel
    offers — the point is a *searchable PDF*. Tesseract writes exactly that (the
    scan with an invisible text layer over it), one page at a time, so the pages
    are rendered, OCR'd and stitched back together here.
    """
    from pdf2image import convert_from_path
    from pypdf import PdfReader, PdfWriter
    import tempfile

    available = tesseract_langs()
    missing = [p for p in lang.split("+") if available and p not in available]
    if missing:
        raise RuntimeError(tr(
            "Sprachpaket fehlt: {p0}\nVerfügbar: {p1}\n"
            "Installation z.B.:  sudo pacman -S tesseract-data-{p0}"
        ).format(p0=", ".join(missing), p1=", ".join(available) or "—"))

    report(tr("Rendere Seiten …"))
    try:
        images = convert_from_path(src, dpi=300)
    except Exception as e:
        raise RuntimeError(tr(
            "Seiten konnten nicht gerendert werden (poppler installiert?):\n{p0}"
        ).format(p0=e))
    if not images:
        raise RuntimeError(tr("Das PDF enthält keine Seiten."))

    reader = PdfReader(src, strict=False)
    writer = PdfWriter()
    done = skipped = 0
    with tempfile.TemporaryDirectory(prefix="copyshop_ocr_") as tmp:
        for i, img in enumerate(images):
            report(tr('OCR Seite {p0} / {p1} …').format(p0=i + 1, p1=len(images)))
            if skip and _page_has_text(src, i):
                # Same intent as ocrmypdf --skip-text: leave pages that are
                # already searchable exactly as they are.
                if i < len(reader.pages):
                    writer.add_page(reader.pages[i])
                    skipped += 1
                    continue
            report.check()          # between pages: a half-OCR'd page is no page
            png  = os.path.join(tmp, f"p{i:04d}.png")
            base = os.path.join(tmp, f"o{i:04d}")
            img.save(png)
            cmd = ["tesseract", png, base, "-l", lang]
            if deskew:
                # Tesseract has no deskew, but PSM 1 runs orientation detection,
                # which is what actually helps a rotated scan.
                cmd += ["--psm", "1"]
            cmd.append("pdf")
            try:
                r = report.run(cmd, text=True, errors="replace", timeout=300)
            except subprocess.TimeoutExpired:
                raise RuntimeError(tr('Zeitüberschreitung bei Seite {p0}.').format(p0=i + 1))
            page_pdf = base + ".pdf"
            if r.returncode != 0 or not os.path.isfile(page_pdf):
                raise RuntimeError(tr('Seite {p0}: {p1}').format(
                    p0=i + 1, p1=(r.stderr or "").strip()[:300]
                    or tr('tesseract beendet mit Code {p0}').format(p0=r.returncode)))
            for page in PdfReader(page_pdf, strict=False).pages:
                writer.add_page(page)
            done += 1

        if not writer.pages:
            raise RuntimeError(tr("OCR hat keine Seiten erzeugt."))
        tmp_out = os.path.join(tmp, "merged.pdf")
        with open(tmp_out, "wb") as f:
            writer.write(f)
        shutil.move(tmp_out, out)

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(tr("OCR hat keine Ausgabedatei erzeugt."))
    note = tr('OCR abgeschlossen ({p0}) — {p1} Seite(n) erkannt').format(p0=lang, p1=done)
    if skipped:
        note += tr(', {p0} bereits mit Text übersprungen').format(p0=skipped)
    return out, note


class OcrPanel(BasePanel):
    TITLE         = "OCR -- Texterkennung"
    SUBTITLE      = "Gescannte PDFs durchsuchbar machen."

    # Pretty names for the codes tesseract reports; anything else is shown raw.
    #
    # Not run through tr(): these are a closed set of language names, not
    # sentences, and looking them up through tr()'s one flat namespace used
    # to mean "Deutsch" (a language name here) and "Deutsch" (the "switch to
    # German" menu item, in tools/i18n.py) were the same dict key. The second
    # definition silently won, so the menu item read "German" instead of
    # "Deutsch" in English mode. A (German, English) pair per code sidesteps
    # the shared namespace instead of relying on the key staying unique.
    _LANG_NAMES = {
        "deu": ("Deutsch", "German"), "eng": ("Englisch", "English"),
        "fra": ("Französisch", "French"), "spa": ("Spanisch", "Spanish"),
        "ita": ("Italienisch", "Italian"), "nld": ("Niederländisch", "Dutch"),
        "por": ("Portugiesisch", "Portuguese"), "afr": ("Afrikaans", "Afrikaans"),
        "tur": ("Türkisch", "Turkish"), "pol": ("Polnisch", "Polish"),
        "rus": ("Russisch", "Russian"),
    }

    def _lang_name(self, code):
        de, en = self._LANG_NAMES.get(code, (code, code))
        return en if get_language() == "en" else de

    def build_ui(self, layout):
        has_ocr  = shutil.which("ocrmypdf") is not None
        has_tess = shutil.which("tesseract") is not None
        langs    = tesseract_langs()
        if has_ocr:
            status = "✓  ocrmypdf"
        elif has_tess:
            # It works without ocrmypdf — say so instead of only showing a
            # missing-dependency cross, which read as "OCR is broken".
            status = tr("✓  tesseract  —  erzeugt durchsuchbare PDFs\n"
                        "○  ocrmypdf fehlt  →  optional, bringt echtes Deskew "
                        "und PDF/A:  pip install ocrmypdf --break-system-packages")
        else:
            status = tr("✗  Kein OCR-Programm  →  sudo pacman -S tesseract tesseract-data-deu")
        if has_tess:
            status += "\n" + tr('Sprachpakete: {p0}').format(p0=", ".join(langs) or "—")
        layout.addWidget(make_label(status, dim=True))

        ob = QGroupBox(tr("EINSTELLUNGEN")); ol = QVBoxLayout(ob)
        self.lang = QComboBox()
        # Only offer languages that are installed. The list was hard-coded and
        # included "fra", so picking it on a machine without that pack failed.
        for code in langs:
            name = self._lang_name(code)
            self.lang.addItem(f"{name} ({code})", code)
        if "deu" in langs and "eng" in langs:
            self.lang.addItem(tr("Deutsch + Englisch"), "deu+eng")
        if not langs:
            self.lang.addItem(tr("Deutsch (deu)"), "deu")
        idx = self.lang.findData("deu")
        if idx >= 0: self.lang.setCurrentIndex(idx)
        ol.addLayout(row(tr("Sprache:"), self.lang))
        self.deskew = QCheckBox(tr("Seiten begradigen")); self.deskew.setChecked(True); ol.addWidget(self.deskew)
        self.skip   = QCheckBox(tr("Seiten mit Text überspringen")); self.skip.setChecked(True); ol.addWidget(self.skip)
        layout.addWidget(ob)

        # Fortschrittsanzeige
        self.progress_lbl = make_label("", dim=True)
        layout.addWidget(self.progress_lbl)

    def _run_action(self):
        # Prep on the UI thread, then hand the work to the shared run_async().
        src  = self.require_pdf()
        out  = self.save_pdf("OCR-PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        lang   = self.lang.currentData()
        deskew = self.deskew.isChecked()
        skip   = self.skip.isChecked()
        self.progress_lbl.setText(tr("OCR läuft …"))
        self.run_async(
            lambda report: _run_ocr(src, out, lang, deskew, skip, report),
            on_done=self._ocr_done,
            on_error=self._ocr_failed,
            on_progress=self._ocr_progress,
        )
        return None

    def _ocr_progress(self, msg):
        self.progress_lbl.setText(msg)
        self.log.log(msg)

    def _ocr_done(self, result):
        out_path, summary = result
        self.progress_lbl.setText(tr("Fertig."))
        self.log.log(summary)
        if out_path.endswith(".txt"):
            self.log.log(tr('Text gespeichert: {p0}').format(p0=out_path))
        else:
            self.open_result(out_path, "OCR")

    def _ocr_failed(self, exc):
        self.progress_lbl.setText(tr("Fehler."))
        self.log.log(str(exc), error=True)
