"""
The geometry of continuous mode: where every page sits in the document strip.

Pure layout math, no widgets. SinglePageView owns the scroll and the render
requests; this module answers where a page is, which page a viewport is
mostly looking at, and what zoom a sheet must be rendered at.
"""
from tools.render.caches import _FullPageCache
from tools.render.region import cached_page_size_pt, page_px_size, page_size_pt


def page_size_pts(model, pdf_path, uid, fallback):
    """(w_pt, h_pt) of a page as displayed, or a safe A4 fallback."""
    try:
        src_path, orig = model.page_source(uid, pdf_path)
        rot  = model.get_rotation(uid)
        size = cached_page_size_pt(src_path, orig)
        w_pt, h_pt = size if size else page_size_pt(src_path, orig)
        if rot % 180 == 90:
            w_pt, h_pt = h_pt, w_pt
        return w_pt, h_pt
    except Exception:
        import logging
        logging.debug("strip: page has no size", exc_info=True)
        return fallback


def resolve_ref(model, pdf_path, current, fallback):
    """The page the continuous fit is measured against, as (w_pt, h_pt).

    Zoom 1.0 shows this page whole — that is what "fit" means everywhere else
    in the view, and what a reader expects the strip to start at. Every other
    sheet is laid out at the same pixels-per-point, so a differently-sized
    page is not rescaled as it scrolls past and the strip never changes shape
    under the pointer.
    """
    if model is None or not model.order:
        return fallback
    pos = max(0, min(current, len(model.order) - 1))
    return page_size_pts(model, pdf_path, model.order[pos], fallback)


def build(model, pdf_path, scale, gap_px, fallback):
    """(strip, strip_h): every page's place in the strip, in display pixels.

    The sizes are a box lookup per page and cached — measured at 27 ms cold
    for a 500-page document and 0.2 ms warm — so the strip costs nothing to
    keep accurate on a mixed-size document, which is the case this app exists
    for and the one a uniform-height guess gets wrong.

    Half a gutter of breathing room above the first sheet and below the last.
    At Ctrl+0 fit this is exactly what centring works out to — the fitted page
    is viewport − gutter tall, so half the gutter is the room on each side.
    """
    pad = float(gap_px) / 2.0
    strip, y = [], pad
    for pos, uid in enumerate(model.order):
        w_pt, h_pt = page_size_pts(model, pdf_path, uid, fallback)
        w_px, h_px = page_px_size(w_pt, h_pt, scale, 0)
        strip.append((pos, y, w_px, h_px))
        y += h_px + gap_px
    height = y - gap_px + pad if strip else 0.0
    return strip, height


def top_of(strip, pos):
    for p, y, _w, _h in strip:
        if p == pos:
            return y
    return 0.0


def page_at(strip, y, viewport_h):
    """The page the viewport is mostly looking at."""
    best = 0
    vh = float(viewport_h)
    for pos, top, _w, h in strip:
        if top + h * 0.5 <= y + vh * 0.5:
            best = pos
        else:
            break
    return best


def cache_entry(src_path, orig, rot, avail_w, avail_h, w_px):
    """Best full-page image for a strip sheet, or None.

    Prefers the viewport-bucketed cache entry; falls back to any render of
    the page at least as wide as the sheet, so a page already drawn in
    single-page mode is not blank while the strip re-renders it."""
    cached = _FullPageCache.get(src_path, orig, rot, avail_w, avail_h)
    if cached is not None:
        return cached
    img = _FullPageCache.get_at_least(src_path, orig, rot, max(1, int(w_px)))
    if img is None:
        return None
    pw, ph = _FullPageCache.get_dims(src_path, orig, rot)
    return (img, pw or 0.0, ph or 0.0, 0.0, [])


def render_zoom(display_scale, avail_w, avail_h, pad,
                src_path, orig, rot, w_px, h_px, fallback_zoom):
    """Zoom argument for _PageRenderTask so its fit-page scale matches
    the continuous fit-width display scale for this sheet."""
    pw, ph = 0.0, 0.0
    size = cached_page_size_pt(src_path, orig)
    if size is not None:
        pw, ph = size
        if rot % 180 == 90:
            pw, ph = ph, pw
    if pw <= 0 or ph <= 0:
        # Infer from the sheet we already laid out.
        if display_scale > 0 and w_px > 0:
            pw = w_px / display_scale
            ph = h_px / display_scale if h_px > 0 else pw
        else:
            return max(fallback_zoom, 1.0)
    fit_page = min((avail_w - pad) / pw, (avail_h - pad) / ph)
    if fit_page <= 0:
        return max(fallback_zoom, 1.0)
    return max(0.05, display_scale / fit_page)
