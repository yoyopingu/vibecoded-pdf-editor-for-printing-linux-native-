"""
Checking that a colour conversion did not destroy the page.

Shared by Compress, Grayscale, Layers and ColourProfile.
Moved verbatim out of tools/all_tools.py; see tools/panels/__init__.py.
"""
import logging

from tools.page_viewer import _pdfium_lock
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

    Used by both colour conversions (greyscale and CMYK); `report` may be None.
    Returns {page_index: reason}."""
    bad = {}
    for n, i in enumerate(sorted(pages), 1):
        if report and n % 5 == 1:
            report(tr('Prüfe Seite {p0} / {p1} …{p2}').format(
                p0=n, p1=len(pages), p2=label))
        try:
            blacked, vanished = _conversion_damage(
                _page_luma(src, i), _page_luma(cand, i))
        except Exception:
            # Could not check it — treat as damaged rather than assume it is
            # fine. Silently shipping an unverified page is the whole problem.
            logging.exception("grayscale: verification of page %d failed", i + 1)
            bad[i] = "unverified"
            continue
        if blacked > _BLACKOUT_LIMIT:
            bad[i] = f"{blacked * 100:.1f}% schwarz"
        elif vanished > _BLACKOUT_LIMIT:
            bad[i] = f"{vanished * 100:.1f}% verschwunden"
    return bad
