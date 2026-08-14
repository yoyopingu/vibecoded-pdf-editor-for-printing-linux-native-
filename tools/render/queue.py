"""
One thread, and what it renders next.

Everything the viewer wants drawn is submitted here as a task with a priority:

    0  the page on screen — the user is watching
    1  thumbnails that are visible
    2  pre-renders of pages they might turn to

One worker, not a pool, because pdfium is serialised process-wide anyway (see
tools/render/document_cache.py) — and because this queue can do two things a
QThreadPool cannot: preempt the task that is *running* when a page turn arrives,
and drop every queued pre-render in one pass. The reasoning is written out at
_RenderQueue itself.

The tasks are here too: a thumbnail, a whole page, and the window of a page at a
zoom too deep to render whole. All three check for cancellation while they run,
so a render nobody wants any more stops in single-digit milliseconds rather than
holding the thread and the pdfium lock until it finishes.
"""
import atexit, threading, heapq, logging
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QImage
from tools.render.caches import _FullPageCache, _ThumbnailCache
from tools.render.document_cache import close_all
from tools.render.images import MAX_RENDER_PX, _SCALE_EPS, _good_enough, _render_image
from tools.render.raster import render_window
from tools.render.region import page_chars, page_px_size, page_size_pt, render_region, snap_scale


def _thumb_render_width(width, cap=2400):
    """Round a wanted thumbnail width up onto a coarse ladder.

    Thumbnail widths are derived from the window's, so they change by a pixel
    at a time as it is dragged — and every distinct width is a separate cache
    key. Rounding up to a step means a resize lands on a width already rendered
    and gets shrunk to fit (see _ThumbnailCache.get_at_least) instead of walking
    the page's drawing again, which on a heavy page is over a second per card.
    Up, never down, so the cached image is never stretched.
    """
    step = 128
    return min(int(cap), max(step, -(-int(width) // step) * step))


class _ThumbSignals(QObject):
    ready = pyqtSignal(int, int, QImage)   # generation, card_idx, image


class _ThumbTask:
    """Renders one thumbnail, submitted to _RenderQueue."""
    def __init__(self, gen, cidx, path, pidx, rot, render_w, signals):
        self._gen, self._cidx   = gen, cidx
        self._path, self._pidx  = path, pidx
        self._rot, self._w      = rot, render_w
        self._signals           = signals
        self._active            = True

    def cancel(self): self._active = False

    def run(self):
        if not self._active:
            return
        key = (self._path, self._pidx, self._rot, self._w)
        img = _ThumbnailCache.get(key)
        if img is None:
            # Anything already rendered at this width or wider is a better
            # source than pdfium: shrinking it is supersampling, and rendering
            # again costs a full walk of the page's drawing whatever size the
            # result is. See _ThumbnailCache.get_at_least.
            bigger = (_ThumbnailCache.get_at_least(self._path, self._pidx,
                                                   self._rot, self._w)
                      or _FullPageCache.get_at_least(self._path, self._pidx,
                                                     self._rot, self._w))
            if bigger is not None:
                img = bigger.scaledToWidth(
                    self._w, Qt.TransformationMode.SmoothTransformation)
            else:
                img = _render_image(self._path, self._pidx, self._w, self._rot,
                                    should_cancel=lambda: not self._active)
            if img is None:
                return                    # cancelled mid-render
            if self._active:
                _ThumbnailCache.put(key, img)
        if self._active and self._signals is not None:
            self._signals.ready.emit(self._gen, self._cidx, img)
        self._active = False   # mark as done so _maybe_schedule can prune it


class _RenderQueue:
    def __init__(self):
        self._heap    = []
        self._seq     = 0
        self._lock    = threading.Lock()
        self._cond    = threading.Condition(self._lock)
        self._running = None
        self._stop    = False
        self._thread  = threading.Thread(target=self._loop, daemon=True,
                                         name="CopyShop-Render")
        self._thread.start()

    def submit(self, task, priority: int = 1):
        with self._cond:
            if self._stop:
                return              # shutting down — never start new work
            self._seq += 1
            task._priority = priority
            heapq.heappush(self._heap, (priority, self._seq, task))
            # P0 (active page) preempts any lower-priority running task so the
            # user doesn't wait behind thumbnails or pre-renders.
            if priority == 0 and self._running is not None:
                if getattr(self._running, '_priority', 0) > 0:
                    self._running.cancel()
            self._cond.notify()

    def cancel_queued(self, min_priority: int):
        """Cancel all not-yet-started tasks at >= min_priority and remove them from the queue."""
        with self._cond:
            self._heap = [(p, s, t) for p, s, t in self._heap
                          if getattr(t, '_priority', 99) < min_priority]

    def shutdown(self, timeout: float = 2.0):
        """Stop the worker and drop every pending task. Idempotent."""
        with self._cond:
            self._stop = True
            for _, _, task in self._heap:
                try: task.cancel()
                except Exception: pass   # cancel is best-effort; a task that will not stop still gets dropped
            self._heap.clear()
            if self._running is not None:
                try: self._running.cancel()
                except Exception: pass   # same: the queue is being cleared either way
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self):
        while True:
            with self._cond:
                while not self._heap and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                _, _, task = heapq.heappop(self._heap)
                self._running = task          # set while lock is held
            try:
                task.run()
            except Exception:
                logging.exception("RenderQueue task error")
            finally:
                with self._cond:
                    self._running = None      # clear while lock is held


_render_queue = _RenderQueue()


def shutdown_render_queue(timeout: float = 2.0):
    """Stop the background render thread before the widget tree is torn down.

    The worker emits Qt signals (``_ThumbSignals`` / ``_PageSignals``) at the
    end of every task. main() now deletes the whole window on the way out, so
    the worker has to be stopped *first* — otherwise a render finishing a
    moment later would emit into half-deleted receivers.

    Wired to QApplication.aboutToQuit, which runs before app.exec() returns,
    and registered with atexit as a backstop for anything that exits without an
    event loop (tests, scripts). Joining is what makes it deterministic: the
    thread is a daemon, so without it shutdown ordering is left to chance.
    """
    try:
        _render_queue.shutdown(timeout)
    except Exception:
        pass          # never let shutdown noise mask the real exit path
    try:
        # Pool jobs first: they emit into widgets, and main() deletes the window
        # right after this. cancel_all waits for the pool to drain.
        # int() matters: waitForDone takes milliseconds as an int, and a float
        # raised a TypeError that the except below swallowed — shutdown quietly
        # cancelled nothing at all until a test went looking.
        from tools.jobs import cancel_all
        cancel_all(int(timeout * 1000) if timeout else 2000)
    except Exception:
        logging.exception("shutdown: cancelling background jobs failed")
    try:
        # Only after the workers have stopped: closing a document that a render
        # is still inside would be exactly the thing the handle locks prevent.
        close_all()
    except Exception:
        logging.exception("shutdown: closing cached documents failed")


# The backstop for anything that exits without an event loop — tests, scripts.
# main() also wires this to QApplication.aboutToQuit, which runs first and is
# what makes shutdown deterministic; this only has to catch the rest. It used
# to sit in tools/page_viewer.py, a long way from the function it registers.
atexit.register(shutdown_render_queue)


class _PageSignals(QObject):
    ready = pyqtSignal(int, QImage, int, int, float, float, float, list, bool)


class _RegionSignals(QObject):
    ready = pyqtSignal(int, QImage, int, int, float, list)


class _RegionRenderTask:
    """Renders the part of a page that is on screen, at the exact zoom.

    Used instead of _PageRenderTask once the whole page at this zoom would be a
    bigger bitmap than MAX_RENDER_PX allows. Cost and memory are set by the size
    of the window, not by the zoom, which is what lets the zoom go as deep as
    the user likes.
    """
    def __init__(self, gen, path, orig, rot, scale, rect, signals):
        self._gen    = gen
        self._path   = path
        self._orig   = orig
        self._rot    = rot
        self._scale  = scale
        self._rect   = rect          # (px0, py0, w, h) in displayed page pixels
        self._sig    = signals
        self._active = True

    def cancel(self): self._active = False

    def run(self):
        if not self._active:
            return
        try:
            px0, py0, w, h = self._rect
            # The cancel is checked between render slices, so a window this
            # task no longer wants is abandoned in single-digit milliseconds
            # rather than after the whole thing has been drawn.
            img = render_region(self._path, self._orig, self._scale,
                                px0, py0, w, h, self._rot,
                                should_cancel=lambda: not self._active)
            if img is None or not self._active:
                return
            try:
                chars = page_chars(self._path, self._orig, self._scale, self._rot)
            except Exception:
                chars = []
            if self._sig is not None and self._active:
                self._sig.ready.emit(self._gen, img, int(px0), int(py0),
                                     self._scale, chars)
        except Exception:
            logging.exception("region render failed")
        finally:
            self._active = False


def _target_scale(avail_w, avail_h, zoom, page_w_pt, page_h_pt, fallback=1.0):
    """Pixels per point a page should be rendered at to fill `avail_w` x
    `avail_h` at `zoom`, with MAX_RENDER_PX applied.

    One function, called from both the GUI thread (deciding whether the cached
    render is good enough) and the render thread (deciding what to render). The
    two used to compute it separately, and any disagreement between them — a
    snapped scale against an unsnapped one, say — is a page that re-renders on
    every repaint, because the render never lands on the scale that was asked
    for.

    Snapped, because the only scales that can be rendered are the ones that put
    a whole number of pixels across the page; see snap_scale in
    tools/render/region.py.
    """
    if page_w_pt <= 0 or page_h_pt <= 0:
        return fallback
    pad = 16
    fit = min((avail_w - pad) / page_w_pt, (avail_h - pad) / page_h_pt)
    return snap_scale(page_w_pt, page_h_pt,
                      min(fit * zoom,
                          MAX_RENDER_PX / page_w_pt,
                          MAX_RENDER_PX / page_h_pt))


class _PageRenderTask:
    """Renders the full single-page view, submitted to _RenderQueue.

    When signals=None the task runs in pre-render mode: renders and stores
    the result in _FullPageCache but emits nothing, warming the cache.
    """
    def __init__(self, gen, path, orig, rot, avail_w, avail_h, zoom,
                 signals=None, stand_in_shown=False):
        self._gen    = gen
        self._path   = path
        self._orig   = orig
        self._rot    = rot
        self._aw     = avail_w
        self._ah     = avail_h
        self._zoom   = zoom
        self._sig    = signals   # None → pre-render (cache-only) mode
        self._active = True
        # The caller already put the scaled cache entry on screen and is only
        # waiting for the real render. Emitting the same stand-in again from
        # here would repeat a smooth scale of a multi-megapixel image on the
        # render thread — and delay the render the caller is waiting for by
        # exactly as long as that takes.
        self._stand_in_shown = stand_in_shown

    def cancel(self): self._active = False

    def target_scale(self, page_w_pt, page_h_pt, fallback=1.0):
        """Pixels per point this page should be rendered at, cap included."""
        return _target_scale(self._aw, self._ah, self._zoom,
                             page_w_pt, page_h_pt, fallback)

    def _emit(self, img, pw, ph, scale, chars, provisional):
        if self._sig is None or not self._active:
            return
        off_x = int(max(0.0, (self._aw - img.width())  / 2.0))
        off_y = int(max(0.0, (self._ah - img.height()) / 2.0))
        self._sig.ready.emit(self._gen, img, off_x, off_y,
                             pw, ph, scale, chars, provisional)

    def run(self):
        if not self._active:
            return

        # ── Anything cached is a stand-in, never the answer ────────────────────
        # Qt-scaling a render made at a different zoom is interpolation, and the
        # user must not be left looking at interpolated pixels. So a cached image
        # is emitted immediately — flagged provisional — purely so the view has
        # something sharp-ish to show, and then this falls through and renders
        # the page properly at the scale actually being asked for.
        #
        # There used to be a threshold here: anything within 1.45x of the cached
        # scale was declared "good enough" and no real render ever happened. That
        # band is exactly where the blurring lived.
        cached = _FullPageCache.get(self._path, self._orig, self._rot,
                                    self._aw, self._ah)
        if cached is not None:
            img, pw, ph, cached_scale, raw_chars = cached
            if self._sig is None:
                return           # pre-render mode: the cache is already warm
            wanted = self.target_scale(pw, ph, cached_scale)
            ratio  = wanted / cached_scale if cached_scale > 0 else 1.0
            if _good_enough(cached_scale, wanted):
                # Rendered at this scale or finer: shrinking it is as sharp as
                # rendering again, so this is the final image, not a stand-in.
                if abs(ratio - 1.0) > _SCALE_EPS:
                    img = img.scaled(max(1, int(img.width()  * ratio)),
                                     max(1, int(img.height() * ratio)),
                                     Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                    raw_chars = [(ch, x*ratio, y*ratio, x2*ratio, y2*ratio)
                                 for ch, x, y, x2, y2 in raw_chars]
                self._emit(img, pw, ph, wanted, raw_chars, False)
                return
            if not self._stand_in_shown:
                self._emit(
                    img.scaled(max(1, int(img.width()  * ratio)),
                               max(1, int(img.height() * ratio)),
                               Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation),
                    pw, ph, wanted,
                    [(ch, x*ratio, y*ratio, x2*ratio, y2*ratio)
                     for ch, x, y, x2, y2 in raw_chars],
                    True)
            # …and on to the real render.

        try:

            raw_w_pt, raw_h_pt = page_size_pt(self._path, self._orig)
            # The bitmap comes out of pdfium already turned by self._rot, so a
            # quarter turn swaps the page's width and height. Everything
            # downstream — the fit, the zoom percentage, the "Masse" readout —
            # has to see the page as it ends up on screen, not as it sits in the
            # file. It used to be told the original dimensions, so a page turned
            # 90° was fitted into a portrait box and still measured as portrait.
            quarter = self._rot % 180 == 90
            page_w_pt, page_h_pt = ((raw_h_pt, raw_w_pt) if quarter
                                    else (raw_w_pt, raw_h_pt))

            # Exactly the scale the fast-path above asked for, so the result
            # lands on the cache entry it was compared against instead of a hair
            # off it, which would re-render forever.
            scale = self.target_scale(page_w_pt, page_h_pt)
            px_w, px_h = page_px_size(raw_w_pt, raw_h_pt, scale, self._rot)

            img = render_window(self._path, self._orig, px_w, px_h,
                                rotation=self._rot,
                                should_cancel=lambda: not self._active)
            if img is None or not self._active:
                return

            # Text boxes come from the shared cache, which walks the textpage
            # once per page and scales the result afterwards. Doing it here, per
            # render, was 62% of the cost of rendering a dense page — 215ms of
            # Python looping over 15,000 characters to recompute coordinates
            # that only needed multiplying.
            raw_chars = page_chars(self._path, self._orig, scale, self._rot)

            # A display render is the exact one for the current zoom, so it
            # takes the slot; a pre-render only fills an empty one.
            _FullPageCache.put(self._path, self._orig, self._rot,
                               self._aw, self._ah,
                               (img, page_w_pt, page_h_pt, scale, raw_chars),
                               force=self._sig is not None)

            # The real thing, at the scale asked for: not provisional.
            self._emit(img, page_w_pt, page_h_pt, scale, raw_chars, False)
        except Exception:
            if self._sig is not None and self._active:
                img = _render_image(self._path, self._orig,
                                    max(1, self._aw - 16), self._rot)
                self._sig.ready.emit(self._gen, img, 0, 0, 0.0, 0.0, 1.0, [], False)


_prerender_enabled = True   # can be toggled via apply_performance_settings()


def prerender_enabled():
    """Whether speculative pre-rendering is on.

    A function, not the flag itself: apply_performance_settings rebinds
    _prerender_enabled, and a caller in another module that had imported the
    name would go on reading the value it was bound to at import time. That
    was one module when both lived together, and is two now.
    """
    return _prerender_enabled


def apply_performance_settings(prerender=True, render_threads=1,
                                thumb_threads=1, cache_size=300,
                                full_page_cache=6):
    """Apply performance settings at runtime (called from PerformanceDialog).
    render_threads / thumb_threads are ignored — rendering uses a single
    priority queue thread so all jobs are serialised without lock contention.
    """
    global _prerender_enabled
    _prerender_enabled = bool(prerender)
    _ThumbnailCache.MAX = max(50, int(cache_size))
    _FullPageCache.MAX  = max(2, int(full_page_cache))
    # Immediately evict surplus entries so RAM drops right away
    with _ThumbnailCache._lock:
        while len(_ThumbnailCache._store) > _ThumbnailCache.MAX:
            _ThumbnailCache._store.popitem(last=False)
    with _FullPageCache._lock:
        while len(_FullPageCache._store) > _FullPageCache.MAX:
            _FullPageCache._store.popitem(last=False)
