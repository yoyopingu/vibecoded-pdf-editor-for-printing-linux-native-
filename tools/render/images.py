"""
Images, moved verbatim out of tools/page_viewer.py.
"""
import io
from PyQt6.QtGui import QPixmap, QImage, QColor
from tools.render.raster import render_window
from tools.render.region import page_px_size, page_size_pt


# Ceiling on either pixel dimension of one full-page render. A page is rendered
# as a single bitmap, so without this a deep zoom asks pdfium for a gigapixel
# image and the app stalls or runs out of memory.
#
# It is no longer a cap on quality. Past it the viewer stops rendering the page
# in one piece and renders only the window on screen instead (see
# tools/render/region.py and SinglePageView._show_region), at the exact scale,
# however far in the user has zoomed. So this is the point where the strategy
# changes, not a ceiling: below it a whole-page render is simpler and can be
# cached and pre-rendered for page flipping; above it, it would be gigabytes.
MAX_RENDER_PX = 4000


_SCALE_EPS = 0.01


def _good_enough(cached_scale, wanted_scale):
    """May a render made at `cached_scale` stand as the final image at
    `wanted_scale`, or must the page be rendered again?

    Only when it does not have to be enlarged. Shrinking a render made at a
    higher scale is supersampling — the result is as sharp as a fresh render and
    often smoother, because it averages several rendered pixels into each screen
    pixel. Enlarging is the opposite: Qt has to invent the pixels, and that is
    the blur this rule exists to keep off the screen.

    So zooming out reuses what is already rendered and costs nothing, while
    zooming in past what has been rendered triggers a real render. An earlier
    rule reused anything within 1.45x, which meant enlarging by up to 45% and
    left the page visibly soft; the rule after that re-rendered for any change
    at all, including shrinking, and made zooming out on a complex page as
    expensive as zooming in for no gain in what you see.
    """
    if cached_scale <= 0 or wanted_scale <= 0:
        return False
    return cached_scale >= wanted_scale * (1.0 - _SCALE_EPS)


def pil_to_qpixmap(pil) -> QPixmap:
    """Convert a PIL image to a QPixmap via an in-memory PNG. GUI thread only
    (QPixmap must not be created off the main thread)."""
    buf = io.BytesIO()
    pil.save(buf, "PNG")
    buf.seek(0)
    return QPixmap.fromImage(QImage.fromData(buf.read()))


def _render_image(pdf_path, page_index, width, rotation=0, should_cancel=None):
    """Render a whole PDF page to a QImage `width` pixels across (before
    rotation). Safe to call from background threads.

    Returns a placeholder rather than None when the page cannot be rendered:
    callers put the result straight into a label. A cancelled render is the one
    case that comes back None, because nobody is waiting for it.
    """
    try:
        from tools.render.raster import render_window
        from tools.render.region import page_px_size, page_size_pt
        w_pt, h_pt = page_size_pt(pdf_path, page_index)
        scale = width / w_pt
        px_w, px_h = page_px_size(w_pt, h_pt, scale, rotation)
        img = render_window(pdf_path, page_index, px_w, px_h,
                            rotation=rotation, should_cancel=should_cancel)
        if img is not None:
            return img
        if should_cancel is not None and should_cancel():
            return None
        raise RuntimeError("render failed")
    except Exception:
        img = QImage(max(1, int(width)), max(1, int(width * 1.414)),
                     QImage.Format.Format_RGB32)
        img.fill(QColor("#2a3a5a"))
        return img


def render_page(pdf_path, page_index, width):
    return QPixmap.fromImage(_render_image(pdf_path, page_index, width))


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
