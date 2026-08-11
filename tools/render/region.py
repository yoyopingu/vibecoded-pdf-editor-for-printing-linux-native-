"""
Render an arbitrary rectangle of a page, at an arbitrary scale.

The viewer used to render each page as one bitmap, which is why it carried a
4000px ceiling: an A4 page at 40x zoom is 23812x33676 pixels — 800 megapixels,
3.2 GB. The cap kept that from happening and cost all detail past roughly 5x in
exchange.

Only the part of the page on screen is worth rendering, and that is a constant
number of pixels however far in the user has zoomed. Measured on an A4 text
page, a 1384x1144 window costs 31ms at 5x and 8ms at 80x — it gets *cheaper*
with zoom, because less of the document falls inside the window.

Why one window and not a grid of tiles
--------------------------------------
Tiles were tried first. pdfium rasterises a glyph that straddles a clip edge
slightly differently from the same glyph rendered whole, so a tile grid leaves
faint seams through text: composited tiles differed from a single render on
0.14% of pixels, every one of them within 2px of a tile boundary, by up to 130
levels. A guard band fixes it only if the band is wider than the largest glyph,
which is unbounded.

A single window has no internal boundary, so the question does not arise. And a
glyph clipped by the window edge is *not* dropped — a 300pt glyph rendered
through a 300x300 window with no margin at all comes out identical to the same
area of a full-page render (max channel difference 1). Only the outermost pixel
or two of the window can differ, and the window is drawn with a margin beyond
the viewport, so those pixels are off screen.
"""

import logging
import math

from PyQt6.QtGui import QImage

from tools.render.document_cache import _stat_key, page_document


def _revision(path):
    """Identity of the file's current contents — the same one the document
    cache keys on, so anything derived from a page is dropped when the file is
    rewritten underneath us (which the page manager does constantly)."""
    return _stat_key(path) or (path, None, None)

# How far beyond the viewport to render. Panning inside this costs no render at
# all, it is just a blit at a different offset.
REGION_MARGIN_PX = 192


_page_sizes: "dict" = {}


def page_size_pt(path, page_index):
    """(width, height) of a page in points, unrotated. Cached — the viewer asks
    for this on every render and it never changes for a given file revision."""
    key = (_revision(path), page_index)
    size = _page_sizes.get(key)
    if size is None:
        with page_document(path) as doc:
            page = doc[page_index]
            try:
                size = (page.get_width(), page.get_height())
            finally:
                page.close()
        if len(_page_sizes) > 256:
            _page_sizes.clear()
        _page_sizes[key] = size
    return size


def displayed_page_px(page_w_pt, page_h_pt, scale, rotation):
    """Size of the whole page in pixels as it appears on screen, i.e. after the
    page manager's rotation has been applied."""
    w, h = page_w_pt * scale, page_h_pt * scale
    return (h, w) if rotation % 180 == 90 else (w, h)


def _unrotated_rect(px0, py0, w, h, rotation, page_px_w, page_px_h):
    """Map a rectangle in displayed pixel space back to the unrotated page.

    (page_px_w, page_px_h) is the *unrotated* page size in pixels. Rotation is
    the quarter-turn clockwise that the rendered bitmap gets afterwards, so this
    is its inverse; see _rotate_char_boxes in page_viewer for the same mapping
    applied to text boxes.
    """
    r = rotation % 360
    if r == 90:      # displayed (x, y) came from unrotated (y, page_px_h - x)
        return py0, page_px_h - (px0 + w), h, w
    if r == 180:
        return page_px_w - (px0 + w), page_px_h - (py0 + h), w, h
    if r == 270:     # displayed (x, y) came from unrotated (page_px_w - y, x)
        return page_px_w - (py0 + h), px0, h, w
    return px0, py0, w, h


def render_region(path, page_index, scale, px0, py0, w, h, rotation=0):
    """Render the displayed-space rectangle (px0, py0, w, h) at `scale`.

    Coordinates are pixels in the displayed page, whose full size is
    displayed_page_px(...). Returns a QImage exactly w x h, or None on failure.
    """
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    w = max(1, int(w)); h = max(1, int(h))
    px0 = int(px0); py0 = int(py0)
    with page_document(path) as doc:
        page = doc[page_index]
        try:
            page_px_w = page.get_width()  * scale
            page_px_h = page.get_height() * scale
            ux, uy, uw, uh = _unrotated_rect(px0, py0, w, h, rotation,
                                             page_px_w, page_px_h)
            uw = max(1, int(round(uw))); uh = max(1, int(round(uh)))
            bitmap = pdfium.PdfBitmap.new_native(
                uw, uh, pdfium_c.FPDFBitmap_BGRA, rev_byteorder=False)
            bitmap.fill_rect((255, 255, 255, 255), 0, 0, uw, uh)
            # Integer translation, so every window samples the same pixel grid:
            # two windows of the same page at the same scale always agree where
            # they overlap.
            matrix = pdfium_c.FS_MATRIX(scale, 0, 0, scale,
                                        -float(int(round(ux))), -float(int(round(uy))))
            clip = pdfium_c.FS_RECTF(0, 0, uw, uh)
            pdfium_c.FPDF_RenderPageBitmapWithMatrix(
                bitmap, page, matrix, clip, pdfium_c.FPDF_ANNOT)
            pil = bitmap.to_pil().convert("RGB")
        finally:
            page.close()

    if rotation % 360:
        pil = pil.rotate(-(rotation % 360), expand=True)
    if pil.size != (w, h):
        # Only ever off by a pixel from the rounding above.
        pil = pil.crop((0, 0, w, h)) if (pil.size[0] >= w and pil.size[1] >= h) \
              else pil.resize((w, h))
    raw = pil.tobytes()
    return QImage(raw, pil.width, pil.height,
                  pil.width * 3, QImage.Format.Format_RGB888).copy()


# Character boxes at scale 1, per (path, page). They scale linearly, so the
# textpage is walked once per page instead of on every scroll — at deep zoom a
# region render happens for every pan, and that loop is pure Python.
_unit_chars: "dict" = {}
_UNIT_CHARS_MAX = 32


def page_chars(path, page_index, scale, rotation=0):
    """Character boxes in displayed page-pixel space, relative to the page's
    top-left. Same shape as the boxes _PageRenderTask produces."""
    key = (_revision(path), page_index)
    unit = _unit_chars.get(key)
    if unit is None:
        unit = []
        try:
            with page_document(path) as doc:
                page = doc[page_index]
                textpage = None
                try:
                    h_pt = page.get_height()
                    textpage = page.get_textpage()
                    for i in range(textpage.count_chars()):
                        ch = textpage.get_text_range(i, 1)
                        if not ch:
                            continue
                        r = textpage.get_charbox(i, loose=False)
                        unit.append((ch, r[0], h_pt - r[3], r[2], h_pt - r[1]))
                finally:
                    if textpage is not None:
                        try: textpage.close()
                        except Exception: pass
                    page.close()
        except Exception:
            logging.debug("region: text extraction failed", exc_info=True)
            unit = []
        if len(_unit_chars) >= _UNIT_CHARS_MAX:
            _unit_chars.pop(next(iter(_unit_chars)), None)
        _unit_chars[key] = unit

    scaled = [(ch, x0 * scale, y0 * scale, x1 * scale, y1 * scale)
              for ch, x0, y0, x1, y1 in unit]
    r = rotation % 360
    if r and scaled:
        from tools.page_viewer import _rotate_char_boxes
        with page_document(path) as doc:
            page = doc[page_index]
            try:
                w = page.get_width() * scale
                h = page.get_height() * scale
            finally:
                page.close()
        scaled = _rotate_char_boxes(scaled, r, w, h)
    return scaled


def _visible_span(page_px, avail, scroll):
    """The part of one axis of the page that is on screen, as whole pixels.

    Rounded outwards, and the page's own extent rounded up: the page is a
    fractional number of pixels wide, and truncating that left the window one
    pixel short of the page's last column at the far corner — a hairline of
    background down the edge of the sheet.
    """
    extent = int(math.ceil(page_px))
    if page_px <= avail:
        return 0, extent, extent
    lo = max(0, int(math.floor(scroll)))
    hi = min(extent, int(math.ceil(scroll + avail)))
    return lo, hi, extent


def region_for_viewport(page_px_w, page_px_h, avail_w, avail_h,
                        scroll_x, scroll_y, margin=REGION_MARGIN_PX):
    """The rectangle worth rendering for the current viewport: what is visible,
    grown by `margin` and clipped to the page."""
    lo_x, hi_x, ext_x = _visible_span(page_px_w, avail_w, scroll_x)
    lo_y, hi_y, ext_y = _visible_span(page_px_h, avail_h, scroll_y)
    x0 = max(0, lo_x - margin); x1 = min(ext_x, hi_x + margin)
    y0 = max(0, lo_y - margin); y1 = min(ext_y, hi_y + margin)
    return x0, y0, max(1, x1 - x0), max(1, y1 - y0)


def covers(region, page_px_w, page_px_h, avail_w, avail_h, scroll_x, scroll_y):
    """Does an already-rendered region still cover everything on screen?"""
    if region is None:
        return False
    rx, ry, rw, rh = region
    lo_x, hi_x, _ = _visible_span(page_px_w, avail_w, scroll_x)
    lo_y, hi_y, _ = _visible_span(page_px_h, avail_h, scroll_y)
    return (rx <= lo_x and ry <= lo_y
            and rx + rw >= hi_x and ry + rh >= hi_y)
