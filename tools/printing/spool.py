"""
Sending a document to paper.

Ghostscript and lp where they exist, Qt where they do not. Kept apart from the
dialog because none of it is about widgets: it takes a path, a page model and
the settings that were chosen, and it is the part that has to be right when the
copy shop is billing for the output.

_gs_blacked_out is here for the same reason. Ghostscript reports success while
turning a transparency group solid black, so what comes back is compared against
what went in before any of it reaches a printer.
"""
import logging
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock


def _gs_blacked_out(before, after, budget=60):
    """Page indexes that went from paper-white to near-black during Ghostscript's
    colour conversion for printing.

    Deliberately coarser than the page-by-page check the Grayscale and CMYK tools
    use: this step also scales, fits and re-centres, so content legitimately
    moves and a per-pixel comparison would flag healthy pages. Comparing mean
    brightness is immune to that and still catches the case that matters — a page
    that was mostly white coming back mostly black. Large documents are sampled,
    because this runs before every print and must not add noticeable delay.

    Returns [] when nothing is wrong and None if it could not be checked."""
    import pypdfium2 as pdfium
    try:
        with _pdfium_lock:
            a = pdfium.PdfDocument(before); b = pdfium.PdfDocument(after)
            try:
                n = min(len(a), len(b))
                if not n:
                    return None
                step = max(1, n // budget)
                bad = []
                for i in range(0, n, step):
                    pa = a[i].render(scale=0.12).to_pil().convert("L")
                    pb = b[i].render(scale=0.12).to_pil().convert("L")
                    ma = sum(pa.get_flattened_data()) / (pa.size[0] * pa.size[1] or 1)
                    mb = sum(pb.get_flattened_data()) / (pb.size[0] * pb.size[1] or 1)
                    if ma > 200 and mb < 80:
                        bad.append(i)
                return bad
            finally:
                a.close(); b.close()
    except Exception:
        logging.exception("print: could not verify the colour conversion")
        return None
