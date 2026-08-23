"""
Finding text in the open document.

One function does the work: :func:`find_all` walks the pages in display order
and reports every occurrence of a string as a page position and a list of
rectangles. It is meant to run on a job (see tools/jobs.py) — reading a page's
text means walking its content stream, which is the cost the colour-space scan
pays too, and not something to do on the GUI thread.

Rectangles come back in **points from the sheet's top-left corner**, the same
space the ruler guides live in, so the view converts them with the one
conversion it already has (``_sheet_on_screen``). They are therefore correct at
any zoom and any scroll position without ever being recomputed.

Character boxes come from :func:`tools.render.region.page_chars`, which the
viewer already fills for text selection — so a search reuses work the document
has usually done already, and a second search over the same file costs nothing
but the string comparison.

Case folding is done per character rather than on the whole string. ``str.lower``
is not length-preserving for every character in Unicode ('İ' lowers to two), and
the match indexes have to keep pointing at the boxes they came from — so the
lowered text is built alongside a map back into the box list.
"""
import logging
from collections import namedtuple

from tools.render.region import page_chars


# One occurrence: where it is in the document, and the rectangles to draw over
# it. `boxes` are (x0, y0, x1, y1) in points from the top-left of the sheet as
# displayed, one per line the match spans.
Hit = namedtuple("Hit", "page start end boxes")

# Two character boxes belong to the same line when their vertical extents
# overlap by at least this much of the shorter one. Lines of a proportional
# font are never pixel-aligned, and a match that wraps must not be drawn as one
# rectangle across the whole paragraph.
_LINE_OVERLAP = 0.5


def _lowered(chars):
    """(text, index_map) — the characters lowered, and where each resulting
    character came from in `chars`."""
    parts, index_map = [], []
    for i, entry in enumerate(chars):
        low = entry[0].lower()
        parts.append(low)
        index_map.extend([i] * len(low))
    return "".join(parts), index_map


def merge_boxes(boxes):
    """Character rectangles → one rectangle per line.

    A highlight per character leaves a comb of separate marks with the letter
    spacing showing through; one rectangle around the lot turns a match that
    wraps into a block over the whole paragraph. Grouping by line is what both
    mistakes have in common.
    """
    out = []
    for x0, y0, x1, y1 in boxes:
        if out:
            px0, py0, px1, py1 = out[-1]
            overlap = min(y1, py1) - max(y0, py0)
            shorter = min(y1 - y0, py1 - py0)
            if shorter > 0 and overlap >= shorter * _LINE_OVERLAP:
                out[-1] = (min(px0, x0), min(py0, y0),
                           max(px1, x1), max(py1, y1))
                continue
        out.append((x0, y0, x1, y1))
    return out


def search_page(src_path, orig, rotation, needle_low):
    """Every occurrence of `needle_low` (already lowered) on one page, as
    (start, end, boxes)."""
    try:
        chars = page_chars(src_path, orig, 1.0, rotation)
    except Exception:
        logging.debug("search: could not read page %s of %s",
                      orig, src_path, exc_info=True)
        return []
    if not chars or not needle_low:
        return []
    text, index_map = _lowered(chars)
    found, at = [], text.find(needle_low)
    while at >= 0:
        end = at + len(needle_low)
        lo, hi = index_map[at], index_map[end - 1]
        boxes = merge_boxes([tuple(c[1:]) for c in chars[lo:hi + 1]])
        found.append((lo, hi, boxes))
        # Overlapping occurrences are still occurrences — "aa" appears twice in
        # "aaa" — so the next search starts one character on, not past the match.
        at = text.find(needle_low, at + 1)
    return found


def find_all(pdf_path, model, needle, should_stop=None, progress=None):
    """Every occurrence of `needle` in the document, in display order.

    `model` is the PageModel, so this searches the document as the page manager
    currently has it — reordered, with pages deleted and pages pulled in from
    other files — rather than the file as it sits on disk.
    """
    needle_low = (needle or "").lower()
    if not needle_low.strip():
        return []
    hits = []
    order = list(model.order)
    total = len(order)
    for pos, uid in enumerate(order):
        if should_stop is not None and should_stop():
            break
        if progress is not None and pos % 8 == 0:
            progress(pos, total, len(hits))
        try:
            src_path, orig = model.page_source(uid, pdf_path)
            rotation = model.get_rotation(uid)
        except Exception:
            logging.debug("search: page %s has no source", pos, exc_info=True)
            continue
        for start, end, boxes in search_page(src_path, orig, rotation,
                                             needle_low):
            hits.append(Hit(pos, start, end, boxes))
    return hits


def first_at_or_after(hits, page_pos):
    """Index of the first hit on `page_pos` or later, wrapping to 0.

    A search started on page 40 of a 90-page document should land on the next
    thing ahead of the reader, not send them back to page 1.
    """
    for i, hit in enumerate(hits):
        if hit.page >= page_pos:
            return i
    return 0 if hits else -1
