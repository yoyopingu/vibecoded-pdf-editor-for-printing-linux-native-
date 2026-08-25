"""
File classification and the conversion worker that turns images/office files
into PDFs.

Picking several files opens tools.viewer.merge.MergeOrderWidget as a tab (in
the same style as "Seiten verwalten"), where the choice between merging them and
opening them separately is made. Two dialog-shaped copies of that UI used to
live here — an ordering dialog and a modal open/merge chooser in front of it.
Both are gone: the chooser could be clicked faster than it could hand over, and
every extra click became another merge tab.
"""
import os, shutil, subprocess
from tools.i18n import tr

# ── What this app can turn into a PDF ────────────────────────────────────────
# The single source of truth. These sets and the dialog filter below are used by
# every open path — the viewer's own button, the Datei menu, and the multi-file
# preview — so a format can never be accepted by one and hidden by another.
#
# Images go through img2pdf (verified here: png incl. alpha and 16-bit, jpg,
# tif, bmp, webp, gif). Everything in OFFICE_EXTS goes through LibreOffice,
# which also handles plain text, csv, html and svg — those were convertible all
# along but were missing from the lists, so the app refused files it could open.
IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp", ".webp", ".gif"}
OFFICE_EXTS = {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
               ".odt", ".ods", ".odp", ".odg", ".rtf", ".pages",
               ".txt", ".csv", ".html", ".htm", ".svg"}
PDF_EXT     = {".pdf"}

ALL_EXTS = PDF_EXT | IMAGE_EXTS | OFFICE_EXTS


def _pattern(exts):
    return " ".join("*" + e for e in sorted(exts))


def file_dialog_filter():
    """Filter string for every "open a file" dialog in the app."""
    return (tr("Alle unterstützten Dateien") + f" ({_pattern(ALL_EXTS)});;"
            + f"PDF ({_pattern(PDF_EXT)});;"
            + tr("Bilder") + f" ({_pattern(IMAGE_EXTS)});;"
            + tr("Office & Text") + f" ({_pattern(OFFICE_EXTS)});;"
            + tr("Alle Dateien") + " (*)")


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
        size_mb = os.path.getsize(path) / (1024 * 1024)
        timeout = max(120, int(size_mb * 30))
        try:
            r = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, path],
                capture_output=True, text=True, errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(tr("LibreOffice-Timeout nach {p0}s ({p1} MB)").format(p0=timeout, p1=round(size_mb, 1)))
        expected = os.path.join(out_dir, stem + ".pdf")
        if os.path.isfile(expected):
            return expected
        for f in os.listdir(out_dir):
            if f.endswith(".pdf"):
                return os.path.join(out_dir, f)
        raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=r.stderr.strip()[:300]))
    raise RuntimeError(tr("Nicht unterstützt: {p0}").format(p0=os.path.basename(path)))


def convert_files(paths, out_dir, job=None):
    """Convert each path to PDF in `out_dir`. Returns (pdfs, failures).

    `pdfs` has one entry per input — the converted path, or None where it
    failed — so callers can line results up with what they asked for. `failures`
    is [(path, message)], because a file silently missing from a merge is worse
    than one that says why.

    Runs on a tools.jobs pool job; pass it as `job` for progress and to be able
    to stop between files. This was a QThread subclass whose lifetime the caller
    had to guarantee by hand; a plain function has no lifetime to get wrong.
    """
    pdfs, failures = [], []
    for path in paths:
        if job is not None and job.cancelled:
            break
        if job is not None:
            job.report(tr("Verarbeite: {p0}").format(p0=os.path.basename(path)))
        try:
            pdfs.append(convert_to_pdf(path, out_dir))
        except Exception as e:
            failures.append((path, str(e)))
            pdfs.append(None)
    return pdfs, failures
