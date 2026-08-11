"""
The one place a PDF page becomes pixels.

Everything the viewer puts on screen — a thumbnail, a whole page, the window of
a page at deep zoom — goes through :func:`render_window`. It has three
properties that the two hand-rolled render paths it replaced did not:

**It can be stopped.** pdfium's progressive API (``FPDF_RenderPageBitmap_Start``
/ ``_Continue``) renders in slices with a callback that decides between them
whether to pause. Rendering one page of a poster-sized PDF takes 130-550 ms, and
a plain ``FPDF_RenderPageBitmap`` is 130-550 ms during which nothing can
interrupt it: the render thread is inside C, holding the process-wide pdfium
lock. Turning the page then meant waiting for a pre-render nobody wanted any
more to finish first. With a :data:`SLICE_MS` budget the same render is 25
slices and a cancel takes effect at the next one — measured at 5 ms against
131 ms, for no measurable loss of throughput.

**It does not copy the pixels four times.** ``bitmap.to_pil().convert("RGB")``,
``.tobytes()``, ``QImage(...).copy()`` was a colour conversion and three full
copies. pdfium's BGRA output is byte-for-byte ``QImage::Format_RGB32`` on a
little-endian machine, so the buffer is wrapped and copied once: 2.4 ms against
23.9 ms for a 1400x1000 window, and the gap grows with the bitmap.

**It rotates in pdfium.** ``FPDF_RenderPageBitmap_Start`` takes a quarter-turn
count, so a rotated page is rasterised turned instead of being rendered upright
and then turned by PIL — one less full-image pass (40 ms on a 4000x2830 render),
and no second bitmap alive at the same time.

Geometry
--------
Callers work in *displayed* pixel space: the page as it appears on screen, with
its rotation already applied, measuring ``page_px`` across. pdfium is told the
displayed page size and a negative offset, which is exactly the same statement,
so there is no coordinate mapping here to get wrong — see :func:`page_px_size`
in ``tools.render.region`` for how a scale becomes that size.
"""

import ctypes
import logging

from PyQt6.QtGui import QImage

from tools.render.document_cache import open_page

# How long a render slice may run before the pause callback is asked again.
# The cancel latency; also the granularity at which the GIL is handed back.
SLICE_MS = 8

# The callback type is created once: every ctypes.CFUNCTYPE(...) call builds a
# new type object, and pdfium is handed a pointer to an instance of it.
_PAUSE_CB = None


def _pause_type():
    global _PAUSE_CB
    if _PAUSE_CB is None:
        import pypdfium2.raw as pdfium_c
        _PAUSE_CB = ctypes.CFUNCTYPE(ctypes.c_int,
                                     ctypes.POINTER(pdfium_c.IFSDK_PAUSE))
    return _PAUSE_CB


def _quarter_turns(rotation):
    """pdfium's `rotate` argument: quarter turns clockwise, 0-3."""
    return int(round((rotation % 360) / 90.0)) % 4


def _image_from_bitmap(bitmap):
    """Wrap a BGRA pdfium bitmap as a QImage, copying once.

    Format_RGB32 is 0xffRRGGBB packed into a 32-bit int, which on a
    little-endian machine is the bytes B, G, R, X — pdfium's BGRA layout, with
    the alpha byte ignored rather than blended. The copy is what detaches the
    QImage from the bitmap's buffer, which is freed when the bitmap is.
    """
    return QImage(bitmap.buffer, bitmap.width, bitmap.height, bitmap.stride,
                  QImage.Format.Format_RGB32).copy()


def render_window(path, page_index, page_px_w, page_px_h, box=None,
                  rotation=0, should_cancel=None, slice_ms=SLICE_MS):
    """Rasterise part of a page.

    `page_px_w`/`page_px_h` are the size of the whole page in displayed pixels
    (rotation applied); `box` is ``(x, y, w, h)`` within that space, defaulting
    to the whole page. Returns a QImage exactly ``w`` x ``h``, or None if
    `should_cancel` asked to stop or the page could not be rendered.

    `should_cancel` is called between slices, at most every `slice_ms`
    milliseconds of rendering.
    """
    import time
    import pypdfium2 as pdfium
    import pypdfium2.raw as pdfium_c

    page_px_w = max(1, int(page_px_w))
    page_px_h = max(1, int(page_px_h))
    if box is None:
        box = (0, 0, page_px_w, page_px_h)
    x, y, w, h = (int(box[0]), int(box[1]), max(1, int(box[2])), max(1, int(box[3])))

    if should_cancel is not None and should_cancel():
        return None

    deadline = [0.0]

    def _need_pause(_p):
        return 1 if time.monotonic() >= deadline[0] else 0

    callback = _pause_type()(_need_pause)
    pause = pdfium_c.IFSDK_PAUSE(version=1, NeedToPauseNow=callback, user=None)

    try:
        with open_page(path, page_index) as page:
            bitmap = pdfium.PdfBitmap.new_native(
                w, h, pdfium_c.FPDFBitmap_BGRA, rev_byteorder=False)
            try:
                # White under the page: a PDF paints no background of its own,
                # and an uninitialised bitmap shows through wherever it doesn't.
                bitmap.fill_rect((255, 255, 255, 255), 0, 0, w, h)

                budget = max(1, int(slice_ms)) / 1000.0
                deadline[0] = time.monotonic() + budget
                status = pdfium_c.FPDF_RenderPageBitmap_Start(
                    bitmap, page, -x, -y, page_px_w, page_px_h,
                    _quarter_turns(rotation), pdfium_c.FPDF_ANNOT,
                    ctypes.byref(pause))
                try:
                    while status == pdfium_c.FPDF_RENDER_TOBECONTINUED:
                        if should_cancel is not None and should_cancel():
                            return None
                        deadline[0] = time.monotonic() + budget
                        status = pdfium_c.FPDF_RenderPage_Continue(
                            page, ctypes.byref(pause))
                finally:
                    # Required whether the render finished or was abandoned: it
                    # frees the progressive renderer hanging off the page. Skip
                    # it and the next render of that page renders nothing.
                    pdfium_c.FPDF_RenderPage_Close(page)

                if status == pdfium_c.FPDF_RENDER_FAILED:
                    return None
                # A render that got as far as finishing is returned even if the
                # caller has since lost interest: it costs one copy, and the
                # caller may still want to cache it. Only an abandoned one
                # comes back None.
                return _image_from_bitmap(bitmap)
            finally:
                try:
                    bitmap.close()
                except Exception:
                    logging.debug("raster: bitmap close failed", exc_info=True)
    except Exception:
        logging.exception("raster: rendering %s page %s failed", path, page_index)
        return None
