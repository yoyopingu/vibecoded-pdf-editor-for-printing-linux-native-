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

Whole pixels
------------
A page is a whole number of pixels across, decided by :func:`page_px_size`, and
:func:`snap_scale` is what the viewer uses so that the scale it lays out with is
the scale that can actually be rendered. Both exist because pdfium's
interruptible render (see ``tools.render.raster``) states the page's size in
integer device pixels rather than as a matrix, so a scale of "1.187 pixels per
point" is really "5527 pixels across". Snapping the two together up front is
what keeps a pan from being mistaken for a zoom: the viewer asks for the scale
it was given back, to the bit, and the already-rendered window still counts as
covering the viewport.
"""

import logging
import math

from tools.render.document_cache import _stat_key, open_page


def _revision(path):
    """Identity of the file's current contents — the same one the document
    cache keys on, so anything derived from a page is dropped when the file is
    rewritten underneath us (which the page manager does constantly)."""
    return _stat_key(path) or (path, None, None)

# How far beyond the viewport to render. Panning inside this costs no render at
# all, it is just a blit at a different offset.
REGION_MARGIN_PX = 192

_page_sizes: "dict" = {}
_PAGE_SIZES_MAX = 512


def _remember(cache, key, value, cap):
    """Store `value`, dropping the oldest entries once `cap` is reached.

    Not cache.clear(): a document with more pages than the cap would wipe
    everything it had just learned each time it filled up, so paging through a
    long file kept re-measuring pages it had already seen. Insertion order is
    enough to decide what to drop — these are filled in page order.
    """
    cache[key] = value
    while len(cache) > cap:
        cache.pop(next(iter(cache)))
    return value


def page_size_pt(path, page_index):
    """(width, height) of a page in points, unrotated. Cached — the viewer asks
    for this on every render and it never changes for a given file revision."""
    key = (_revision(path), page_index)
    size = _page_sizes.get(key)
    if size is None:
        with open_page(path, page_index) as page:
            size = (page.get_width(), page.get_height())
        _remember(_page_sizes, key, size, _PAGE_SIZES_MAX)
    return size


def cached_page_size_pt(path, page_index):
    """:func:`page_size_pt` if it has already been measured, else None.

    For the GUI thread, which must not be the one to find out: measuring means
    loading the page, which queues behind the render worker's lock and stalls
    the window for as long as that render takes.
    """
    return _page_sizes.get((_revision(path), page_index))


def page_px_size(page_w_pt, page_h_pt, scale, rotation=0):
    """Size of the whole page in whole pixels as it appears on screen, i.e.
    with the page manager's rotation applied.

    Whole pixels, not a float: this is the number handed to pdfium as the page's
    device size, and the same number the viewer lays out and scrolls against.
    They have to be the one number or the two drift apart by a fraction of a
    pixel and every pan looks like a change of scale.
    """
    w = max(1, int(round(page_w_pt * scale)))
    h = max(1, int(round(page_h_pt * scale)))
    return (h, w) if rotation % 180 == 90 else (w, h)


def snap_scale(page_w_pt, page_h_pt, scale):
    """The nearest scale to `scale` that puts a whole number of pixels across
    the page's long axis, so that asking for it twice renders the same grid.

    Snapping on the longer side keeps the relative error smallest; the other
    axis is within half a pixel over the whole page, which at these sizes is a
    part in ten thousand.
    """
    if scale <= 0 or page_w_pt <= 0 or page_h_pt <= 0:
        return scale
    longest = max(page_w_pt, page_h_pt)
    return max(1, int(round(longest * scale))) / longest


def render_region(path, page_index, scale, px0, py0, w, h, rotation=0,
                  should_cancel=None):
    """Render the displayed-space rectangle (px0, py0, w, h) at `scale`.

    Coordinates are pixels in the displayed page, whose full size is
    page_px_size(...). Returns a QImage exactly w x h, or None if the render
    failed or `should_cancel` asked for it to stop.
    """
    from tools.render.raster import render_window
    page_w_pt, page_h_pt = page_size_pt(path, page_index)
    page_px_w, page_px_h = page_px_size(page_w_pt, page_h_pt, scale, rotation)
    return render_window(path, page_index, page_px_w, page_px_h,
                         (int(px0), int(py0), max(1, int(w)), max(1, int(h))),
                         rotation=rotation, should_cancel=should_cancel)


# Character boxes at scale 1, per (path, page). They scale linearly, so the
# textpage is walked once per page instead of on every scroll — at deep zoom a
# region render happens for every pan, and that loop is pure Python.
_unit_chars: "dict" = {}
_UNIT_CHARS_MAX = 32


def _rotate_char_boxes(chars, rot, w, h):
    """Move character rectangles with the bitmap they were measured on.

    `w`/`h` are the *unrotated* image size in pixels; PIL's rotate(-rot,
    expand=True) turns the image clockwise by `rot`."""
    rot %= 360
    if rot == 0 or not chars:
        return chars
    out = []
    for ch, x0, y0, x1, y1 in chars:
        if rot == 90:      # (x, y) → (h - y, x)
            nx0, ny0, nx1, ny1 = h - y1, x0, h - y0, x1
        elif rot == 180:   # (x, y) → (w - x, h - y)
            nx0, ny0, nx1, ny1 = w - x1, h - y1, w - x0, h - y0
        elif rot == 270:   # (x, y) → (y, w - x)
            nx0, ny0, nx1, ny1 = y0, w - x1, y1, w - x0
        else:
            nx0, ny0, nx1, ny1 = x0, y0, x1, y1
        out.append((ch, min(nx0, nx1), min(ny0, ny1), max(nx0, nx1), max(ny0, ny1)))
    return out


def page_chars(path, page_index, scale, rotation=0):
    """Character boxes in displayed page-pixel space, relative to the page's
    top-left. Same shape as the boxes _PageRenderTask produces."""
    key = (_revision(path), page_index)
    unit = _unit_chars.get(key)
    if unit is None:
        unit = []
        try:
            with open_page(path, page_index) as page:
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
                        except Exception: pass   # the page closes with the context manager regardless
        except Exception:
            logging.debug("region: text extraction failed", exc_info=True)
            unit = []
        _remember(_unit_chars, key, unit, _UNIT_CHARS_MAX)

    scaled = [(ch, x0 * scale, y0 * scale, x1 * scale, y1 * scale)
              for ch, x0, y0, x1, y1 in unit]
    r = rotation % 360
    if r and scaled:
        # page_size_pt, not the page itself. Loading the page here to ask for
        # two numbers that never change cost a full FPDF_LoadPage on every pan
        # of a rotated page — 351 ms a step on the poster this was measured on,
        # for a width and a height already in a dict.
        page_w_pt, page_h_pt = page_size_pt(path, page_index)
        scaled = _rotate_char_boxes(scaled, r, page_w_pt * scale, page_h_pt * scale)
    return scaled


def _visible_span(page_px, avail, scroll):
    """The part of one axis of the page that is on screen, as whole pixels.

    Rounded outwards. The page's extent is a whole number of pixels (see
    :func:`page_px_size`); it is rounded up here as well because callers may
    still pass a float, and one pixel short of the page's last column shows as
    a hairline of background down the edge of the sheet.
    """
    extent = max(1, int(math.ceil(page_px)))
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
