"""
Checking that a colour conversion did not destroy the page.

Shared by Compress, Grayscale, Layers and ColourProfile.
"""
import logging

from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.i18n       import tr


_VERIFY_SCALE   = 0.30    # ~180 px across an A4 page — enough to see a blackout
_BLACKOUT_LIMIT = 0.004   # 0.4 % of the page turning solid black is already wrong


def _page_luma(path, index, scale=_VERIFY_SCALE):
    """Greyscale render of one page, for comparing before against after."""
    import pypdfium2 as pdfium
    with _pdfium_lock:
        doc = pdfium.PdfDocument(path)
        try:
            return doc[index].render(scale=scale).to_pil().convert("L")
        finally:
            doc.close()


def _conversion_damage(ref_l, got_l):
    """(blacked_out, vanished) as fractions of the page.

    `blacked_out` is the share of pixels that were clearly light in the original
    and came back solid black; `vanished` is the reverse — content that was dark
    and is now blank paper. Both are compared against a *greyscale* render of the
    original, so a legitimate colour→grey conversion scores ~0: only gross
    damage registers, not the small colorimetric differences between
    Ghostscript's conversion and Pillow's."""
    from PIL import ImageChops
    if got_l.size != ref_l.size:
        got_l = got_l.resize(ref_l.size)
    light = ref_l.point(lambda v: 255 if v > 160 else 0)
    dark  = ref_l.point(lambda v: 255 if v < 90  else 0)
    now_black = got_l.point(lambda v: 255 if v < 50  else 0)
    now_blank = got_l.point(lambda v: 255 if v > 230 else 0)
    total = ref_l.size[0] * ref_l.size[1] or 1
    hit = lambda a, b: ImageChops.darker(a, b).histogram()[255] / total
    return hit(light, now_black), hit(dark, now_blank)


def _verify_pages_intact(src, cand, pages, report, label=""):
    """Which of `pages` came out damaged in `cand` compared with `src`.

    Ghostscript reports success and exits 0 while blacking out a transparency
    group or a soft-masked image — the failure this exists to catch. It is
    silent, it is invisible until the job is printed, and it is not something a
    return code will ever tell us about, so every converted page is looked at.

    Both documents are parsed once and held open for the walk. Going through
    _page_luma instead reopened both files for every page — 2N parses for N
    pages, and parsing is the expensive half on exactly the heavy documents
    this runs on. The lock is taken per page rather than around the whole
    loop, so a long verification does not lock the viewer out of rendering for
    its whole duration.

    Used by compress, greyscale, CMYK and PDF/X; `report` may be None.
    Returns {page_index: reason}."""
    import pypdfium2 as pdfium
    bad = {}
    wanted = sorted(pages)
    with _pdfium_lock:
        src_doc = pdfium.PdfDocument(src)
        cand_doc = pdfium.PdfDocument(cand)
    try:
        for n, i in enumerate(wanted, 1):
            # Between pages, never inside one: a page is the unit of work here
            # and half a comparison tells nobody anything.
            if report is not None and getattr(report, "cancelled", False):
                report.check()
            if report and n % 5 == 1:
                report(tr('Prüfe Seite {p0} / {p1} …{p2}').format(
                    p0=n, p1=len(wanted), p2=label))
            try:
                with _pdfium_lock:
                    ref = src_doc[i].render(scale=_VERIFY_SCALE).to_pil().convert("L")
                    got = cand_doc[i].render(scale=_VERIFY_SCALE).to_pil().convert("L")
                blacked, vanished = _conversion_damage(ref, got)
            except Exception:
                # Could not check it — treat as damaged rather than assume it
                # is fine. Silently shipping an unverified page is the whole
                # problem.
                logging.exception("verification of page %d failed", i + 1)
                bad[i] = "unverified"
                continue
            if blacked > _BLACKOUT_LIMIT:
                bad[i] = f"{blacked * 100:.1f}% schwarz"
            elif vanished > _BLACKOUT_LIMIT:
                bad[i] = f"{vanished * 100:.1f}% verschwunden"
    finally:
        with _pdfium_lock:
            src_doc.close()
            cand_doc.close()
    return bad
