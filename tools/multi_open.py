"""
File classification and shared conversion of images, Office documents and
PostScript/vector files into PDFs.

Picking several files opens tools.viewer.merge.MergeOrderWidget as a tab (in
the same style as "Seiten verwalten"), where the choice between merging them and
opening them separately is made. Two dialog-shaped copies of that UI used to
live here — an ordering dialog and a modal open/merge chooser in front of it.
Both are gone: the chooser could be clicked faster than it could hand over, and
every extra click became another merge tab.
"""
import os, shutil, subprocess
import tempfile
import img2pdf
from pathlib import Path
from tools.input_formats import ALL_EXTS, IMAGE_EXTS, OFFICE_EXTS, PDF_EXT, POSTSCRIPT_EXTS
from tools.ghostscript import require_ghostscript
from tools.jobs import Progress, null_progress
from tools.i18n import tr

# ── What this app can turn into a PDF ────────────────────────────────────────
# The single source of truth. These sets and the dialog filter below are used by
# every open path — the viewer's own button, the Datei menu, and the multi-file
# preview — so a format can never be accepted by one and hidden by another.
#
# Installed Pillow codecs and PDF-capable LibreOffice import filters determine
# the lists. PostScript/EPS goes through Ghostscript to preserve vector content.


def _pattern(exts):
    return " ".join("*" + e for e in sorted(exts))


def file_dialog_filter():
    """Filter string for every "open a file" dialog in the app."""
    return (tr("Alle unterstuetzten Dateien") + f" ({_pattern(ALL_EXTS)});;"
            + f"PDF ({_pattern(PDF_EXT)});;"
            + tr("Bilder") + f" ({_pattern(IMAGE_EXTS)});;"
            + tr("PostScript") + f" ({_pattern(POSTSCRIPT_EXTS)});;"
            + tr("Office & Text") + f" ({_pattern(OFFICE_EXTS)});;"
            + tr("Alle Dateien") + " (*)")


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXT:     return "pdf"
    if ext in IMAGE_EXTS:  return "bild"
    if ext in OFFICE_EXTS: return "office"
    if ext in POSTSCRIPT_EXTS: return "postscript"
    return None

def convert_to_pdf(path, out_dir, report=None):
    report = report or null_progress()
    report.check()
    kind = classify(path)
    if kind == "pdf":
        return path
    if Path(path).suffix.lower() == '.ai':
        with open(path, 'rb') as source:
            if source.read(1024).lstrip().startswith(b'%PDF-'):
                return path  # PDF-compatible Illustrator: preserve it directly.
    if kind is None:
        raise RuntimeError(tr("Nicht unterstuetzt: {p0}").format(p0=os.path.basename(path)))
    # Same-name inputs must not overwrite each other, and a stale PDF must not
    # disguise a failed conversion. Every input gets a fresh output directory.
    work_dir = os.path.abspath(tempfile.mkdtemp(prefix="input-", dir=out_dir))
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(work_dir, stem + ".pdf")
    try:
        if kind == "bild":
            data = images_to_pdf_bytes([path])
            report.check()
            with open(out, "wb") as f:
                f.write(data)
        elif kind == "postscript":
            command = [require_ghostscript(), '-dSAFER', '-dBATCH', '-dNOPAUSE',
                       '-sDEVICE=pdfwrite', '-dAutoRotatePages=/None',
                       '-sOutputFile=' + out]
            if Path(path).suffix.lower() != '.ps':
                command.append('-dEPSCrop')
            command += ['-f', os.path.abspath(path)]
            result = report.run(command, timeout=300)
            _check_conversion(result, out)
        else:
            soffice = shutil.which("soffice") or shutil.which("libreoffice")
            if not soffice:
                raise RuntimeError(tr('Benötigtes Programm nicht gefunden: {p0}').format(p0='soffice (LibreOffice)'))
            size_mb = os.path.getsize(path) / (1024 * 1024)
            timeout = max(120, int(size_mb * 30))
            profile = Path(work_dir, 'profile').as_uri()
            try:
                result = report.run(
                    [soffice, '-env:UserInstallation=' + profile, '--headless',
                     '--convert-to', 'pdf', '--outdir', work_dir, os.path.abspath(path)],
                    timeout=timeout)
            except subprocess.TimeoutExpired:
                raise RuntimeError(tr("LibreOffice-Timeout nach {p0}s ({p1} MB)").format(p0=timeout, p1=round(size_mb, 1)))
            _check_conversion(result, out)
            shutil.rmtree(os.path.join(work_dir, 'profile'), ignore_errors=True)
        report.check()
        return out
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _check_conversion(result, out):
    if result.returncode or not os.path.isfile(out) or not os.path.getsize(out):
        detail = result.stderr or result.stdout or b''
        if isinstance(detail, bytes):
            detail = detail.decode('utf-8', errors='replace')
        raise RuntimeError(tr('Konvertierung fehlgeschlagen:\n{p0}').format(p0=str(detail)[-1000:]))


def images_to_pdf_bytes(paths):
    """Wrap one or more image files in a single PDF, keeping JPEG data intact
    except when a mirrored EXIF orientation must be applied to its pixels.

    A JPEG is one picture. Phones store further images in the same file — an
    HDR gain map, a depth image, a stereo mate — and img2pdf turns each of
    those into its own page. The first picture is the one every other viewer
    shows, so that is the only page. TIFF and GIF keep every frame: those
    extra frames are pages, not a hidden attachment.

    img2pdf's first_frame_only flag covers a whole call. A JPEG therefore
    cannot share a call with a multi-page TIFF, or the TIFF would lose pages.
    Some cameras write Orientation=0 or another invalid value; img2pdf's
    strict default rejects the entire file, although its pixels are sound.
    """
    paths = list(paths)
    jpeg = [os.path.splitext(p)[1].lower() in (".jpg", ".jpeg", ".jpe", ".jfif", ".mpo") for p in paths]
    if any(jpeg) and not all(jpeg):
        return _concat_pdf_bytes(_one_image_pdf(p) for p in paths)
    if any(jpeg):
        if any(_jpeg_needs_flip(p) for p in paths):
            return _concat_pdf_bytes(_one_image_pdf(p) for p in paths)
        return img2pdf.convert(
            paths, first_frame_only=True, rotation=img2pdf.Rotation.ifvalid)
    if any(_numeric_image(p) for p in paths):
        return _concat_pdf_bytes(_one_image_pdf(p) for p in paths)
    try:
        return img2pdf.convert(paths)
    except (img2pdf.AlphaChannelError, img2pdf.UnsupportedColorspaceError):
        return _concat_pdf_bytes(_one_image_pdf(p) for p in paths)


def _jpeg_needs_flip(path):
    """The four mirrored EXIF orientations cannot be represented by /Rotate."""
    from PIL import Image
    try:
        with Image.open(path) as image:
            return image.getexif().get(274) in (2, 4, 5, 7)
    except Exception:
        return False   # let img2pdf report a genuinely unreadable image


def _one_image_pdf(path):
    if _numeric_image(path):
        return _decoded_image_pdf(path)
    if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".jpe", ".jfif", ".mpo"):
        if _jpeg_needs_flip(path):
            # img2pdf can ignore mirrored EXIF but cannot display it correctly
            # while embedding the original JPEG stream. Decode only this rare
            # case and use lossless PNG, so the page matches the photo viewer.
            import io
            from PIL import Image, ImageOps
            with Image.open(path) as image:
                corrected = ImageOps.exif_transpose(image)
                if corrected.mode not in ("1", "L", "RGB"):
                    corrected = corrected.convert("RGB")
                buf = io.BytesIO()
                corrected.save(buf, format="PNG",
                               icc_profile=image.info.get("icc_profile"))
            return img2pdf.convert(buf.getvalue())
        return img2pdf.convert(
            path, first_frame_only=True, rotation=img2pdf.Rotation.ifvalid)
    try:
        return img2pdf.convert(path)
    except (img2pdf.AlphaChannelError, img2pdf.UnsupportedColorspaceError):
        return _decoded_image_pdf(path)


def _numeric_image(path):
    if Path(path).suffix.lower() not in {'.fit', '.fits', '.pfm', '.im', '.tif', '.tiff'}:
        return False
    from PIL import Image, ImageSequence
    with Image.open(path) as image:
        return any(frame.mode in ('F', 'I') for frame in ImageSequence.Iterator(image))


def _decoded_image_pdf(path):
    # img2pdf cannot embed alpha from every Pillow format (ICNS is one), and
    # PDF has no floating-point image samples. Use lossless PNG for alpha;
    # numeric scientific images get the usual min/max display mapping.
    import io
    from PIL import Image, ImageOps, ImageSequence
    def frames():
        with Image.open(path) as image:
            for frame in ImageSequence.Iterator(image):
                corrected = ImageOps.exif_transpose(frame)
                if corrected.mode in ('F', 'I'):
                    low, high = corrected.getextrema()
                    if high > low:
                        corrected = corrected.point(lambda value: (value - low) * 255 / (high - low))
                    corrected = corrected.convert('L')
                elif corrected.mode not in ('1', 'L', 'LA', 'RGB', 'RGBA', 'P', 'I;16'):
                    corrected = corrected.convert('RGBA' if 'A' in corrected.getbands() else 'RGB')
                buf = io.BytesIO()
                options = {'icc_profile': image.info.get('icc_profile')}
                if image.info.get('dpi'):
                    options['dpi'] = image.info['dpi']
                corrected.save(buf, format='PNG', **options)
                yield img2pdf.convert(buf.getvalue())
    return _concat_pdf_bytes(frames())


def _concat_pdf_bytes(blobs):
    """Page-wise concatenation. Sources stay open until the result is saved:
    pikepdf copies a page lazily, and closing the source first drops it."""
    import contextlib
    import io
    import pikepdf
    with contextlib.ExitStack() as stack:
        merged = stack.enter_context(pikepdf.Pdf.new())
        for blob in blobs:
            src = stack.enter_context(pikepdf.open(io.BytesIO(blob)))
            for page in src.pages:
                merged.pages.append(page)
        buf = io.BytesIO()
        merged.save(buf)
        return buf.getvalue()


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
            pdfs.append(convert_to_pdf(path, out_dir, Progress(job) if job is not None else None))
        except Exception as e:
            failures.append((path, str(e)))
            pdfs.append(None)
    return pdfs, failures
