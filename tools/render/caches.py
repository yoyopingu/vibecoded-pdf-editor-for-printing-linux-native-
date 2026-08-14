"""
What has already been rendered.

Two LRUs of finished images: thumbnails by (path, page, rotation, width), whole
pages by (path, page, rotation, viewport bucket).

A path is not a document. The page manager writes over the file it is showing —
Ctrl+S is save_to(tab.pdf_path) — and after that every one of those keys still
matches while the pixels behind them describe the file as it used to be. This
module's own docstring used to claim the keys made that impossible; they never
had a revision in them.

So each entry carries the revision of the file it was rendered from, and a read
that finds a different one drops it and misses. tools/colorspace.py and
tools/render/region.py key on the same _stat_key for the same reason.

Eviction is by priority, not by age. _set_active names the page the user is
looking at, and _priority_evict drops other tabs first, then other pages of this
tab, and the page on screen last of all — an LRU alone would happily evict the
one image that is about to be painted.
"""
import threading
from collections import OrderedDict

from tools.render.document_cache import _stat_key


def _revision(path):
    """What the file at `path` is right now — size and mtime, or None if it
    cannot be stat'd. Two renders of the same path with different revisions are
    renders of different documents."""
    return _stat_key(path)


_active_path: str = ""
_active_page: int = 0


def _set_active(path: str, page: int):
    global _active_path, _active_page
    _active_path = path
    _active_page = page


def _priority_evict(store: "OrderedDict", key_path_fn, key_page_fn, active_path, active_page):
    """Evict one entry from store with priority: other-tab > same-tab-other-page > current page.
    key_path_fn(k) and key_page_fn(k) extract path and page index from a key."""
    # 1st pass: evict oldest entry from a non-active tab
    for k in store:
        if key_path_fn(k) != active_path:
            del store[k]
            return
    # 2nd pass: evict oldest entry from active tab that isn't the current page
    for k in store:
        if key_page_fn(k) != active_page:
            del store[k]
            return
    # Last resort: evict true LRU (current page — should almost never happen)
    store.popitem(last=False)


class _ThumbnailCache:
    """LRU cache for rendered thumbnail QImages.
    Keyed by (pdf_path, page_idx, rotation, render_width).
    Stores QImage (thread-safe); caller converts to QPixmap on GUI thread.
    """
    _lock  = threading.Lock()
    _store: OrderedDict = OrderedDict()
    MAX    = 300   # ~300 small thumbnails ≈ reasonable memory

    @classmethod
    def get(cls, key):
        with cls._lock:
            entry = cls._store.get(key)
            if entry is None:
                return None
            rev, image = entry
            if rev != _revision(key[0]):
                del cls._store[key]      # the file has been written since
                return None
            cls._store.move_to_end(key)
            return image

    @classmethod
    def put(cls, key, image):
        with cls._lock:
            cls._store[key] = (_revision(key[0]), image)
            cls._store.move_to_end(key)
            while len(cls._store) > cls.MAX:
                _priority_evict(cls._store,
                                lambda k: k[0], lambda k: k[1],
                                _active_path, _active_page)

    @classmethod
    def invalidate(cls, pdf_path=None):
        with cls._lock:
            if pdf_path is None:
                cls._store.clear()
            else:
                for k in [k for k in cls._store if k[0] == pdf_path]:
                    del cls._store[k]

    @classmethod
    def evict_tab(cls, pdf_path, keep_page=None):
        """Evict all thumbnails for pdf_path, optionally keeping one page index."""
        with cls._lock:
            drop = [k for k in cls._store
                    if k[0] == pdf_path and (keep_page is None or k[1] != keep_page)]
            for k in drop:
                del cls._store[k]

    @classmethod
    def get_any(cls, path, pidx, rot):
        """Return any cached image for this page, ignoring render_width.
        Used as a placeholder when the exact render_width is not cached yet."""
        rev = _revision(path)
        with cls._lock:
            for k, (entry_rev, image) in cls._store.items():
                if (k[0] == path and k[1] == pidx and k[2] == rot
                        and entry_rev == rev):
                    return image
        return None

    @classmethod
    def get_at_least(cls, path, pidx, rot, width):
        """The narrowest cached image for this page that is still at least
        `width` across, or None.

        Shrinking one of those is supersampling — as good as rendering at the
        smaller size, and on a heavy page not remotely the same price. Rendering
        a thumbnail costs what walking the page's drawing costs, near enough
        regardless of how few pixels come out: a 160x113 thumbnail of a page
        with 580,000 stroked segments on it measured 1221 ms against 1307 ms for
        the whole sheet at 1384x979. Widening the window changes the width every
        thumbnail is wanted at, and that used to re-render every one of them.
        """
        best = None
        rev = _revision(path)
        with cls._lock:
            for (p, pi, r, w), (entry_rev, image) in cls._store.items():
                if (p == path and pi == pidx and r == rot and w >= width
                        and entry_rev == rev):
                    if best is None or w < best[0]:
                        best = (w, image)
        return best[1] if best else None


class _FullPageCache:
    """LRU cache for full-page render results. Zoom is NOT part of the key: the
    entry holds one render, at whatever scale it was made, and a request at a
    different zoom Qt-scales it as a *stand-in* only — see _PageRenderTask.run,
    which then renders the page properly at the scale actually asked for and
    stores that here in its place (put(force=True)).

    Key:   (pdf_path, page_idx, rotation, aw_bucket, ah_bucket)
           aw/ah are bucketed to the nearest 50 px so minor window resizes
           don't fragment the cache.
    Value: (QImage, page_w_pt, page_h_pt, render_scale, raw_chars)
           raw_chars = [(ch, x0, y0, x1, y1)] pixel-coords relative to the
           top-left of the rendered image (no centering offset, no scroll).
           Multiply by (target_scale / render_scale) to rescale for any zoom.
    """
    _lock  = threading.Lock()
    _store: OrderedDict = OrderedDict()
    MAX    = 6     # 6 full pages ≈ safe default (~400 MB); raised by apply_performance_settings

    @classmethod
    def _key(cls, path, pidx, rot, aw, ah):
        return (path, pidx, rot, (aw // 50) * 50, (ah // 50) * 50)

    @classmethod
    def get(cls, path, pidx, rot, aw, ah):
        key = cls._key(path, pidx, rot, aw, ah)
        with cls._lock:
            stored = cls._store.get(key)
            if stored is None:
                return None
            rev, entry = stored
            if rev != _revision(path):
                del cls._store[key]      # the file has been written since
                return None
            cls._store.move_to_end(key)
            return entry   # (QImage, pw_pt, ph_pt, render_scale, raw_chars)

    @classmethod
    def put(cls, path, pidx, rot, aw, ah, entry, force=False):
        """Store a render.

        `force` marks a render made for what is on screen right now. That one
        always wins, even at a lower resolution than what is already cached:
        it is the exact render for the current zoom, so keeping a higher-res
        entry instead only means re-rendering it again on the next call — which
        is precisely what zooming back out used to do, four renders for four
        repaints.

        Without `force` the higher-resolution entry is kept, which is what a
        speculative pre-render wants."""
        key = cls._key(path, pidx, rot, aw, ah)
        rev = _revision(path)
        _, _, _, new_scale, _ = entry
        with cls._lock:
            existing = cls._store.get(key)
            if existing is not None and not force and existing[0] == rev:
                _, _, _, ex_scale, _ = existing[1]
                if new_scale < ex_scale * 0.99:
                    cls._store.move_to_end(key)  # refresh LRU even if not replacing
                    return
            cls._store[key] = (rev, entry)
            cls._store.move_to_end(key)
            while len(cls._store) > cls.MAX:
                _priority_evict(cls._store,
                                lambda k: k[0], lambda k: k[1],
                                _active_path, _active_page)

    @classmethod
    def invalidate(cls, pdf_path=None):
        with cls._lock:
            if pdf_path is None:
                cls._store.clear()
            else:
                for k in [k for k in cls._store if k[0] == pdf_path]:
                    del cls._store[k]

    @classmethod
    def evict_tab(cls, pdf_path, keep_page=None):
        """Evict all full-page renders for pdf_path, optionally keeping one page index."""
        with cls._lock:
            drop = [k for k in cls._store
                    if k[0] == pdf_path and (keep_page is None or k[1] != keep_page)]
            for k in drop:
                del cls._store[k]

    @classmethod
    def get_at_least(cls, path, pidx, rot, width):
        """The narrowest cached full-page render at least `width` across, or
        None. Same argument as _ThumbnailCache.get_at_least, one size up: the
        viewer has usually rendered this page already, and on a heavy page
        shrinking that render is free where rendering it again is a second
        walk over the whole drawing."""
        best = None
        rev = _revision(path)
        with cls._lock:
            for (p, pi, r, _aw, _ah), (entry_rev, entry) in cls._store.items():
                if p == path and pi == pidx and r == rot and entry_rev == rev:
                    img = entry[0]
                    if img.width() >= width and (best is None
                                                 or img.width() < best.width()):
                        best = img
        return best

    @classmethod
    def get_dims(cls, path, pidx, rot):
        """Return (page_w_pt, page_h_pt) from any cached entry for this page.
        Returns (0, 0) if not cached yet."""
        rev = _revision(path)
        with cls._lock:
            for (p, pi, r, _aw, _ah), (entry_rev, entry) in cls._store.items():
                if p == path and pi == pidx and r == rot and entry_rev == rev:
                    return entry[1], entry[2]
        return 0.0, 0.0
