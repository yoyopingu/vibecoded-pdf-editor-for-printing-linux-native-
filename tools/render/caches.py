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

Both are bounded by *bytes*, not by a number of entries. A rendered page is
whatever its size and the window make it — a few hundred kilobytes for a small
page in a small window, tens of megabytes for a poster at full zoom — so a
count of entries says nothing about the memory in use. Counting them was also
why the RAM setting appeared to do nothing: the full-page cache was capped at
twelve entries from 40 % of RAM upwards, and each was assumed to be 70 MB when
a real one is nearer 2 MB.

Eviction is by priority, not by age. _set_active names the page the user is
looking at, and _priority_evict drops other tabs first, then other pages of this
tab, and the page on screen last of all — an LRU alone would happily evict the
one image that is about to be painted.
"""
import threading
import time
from collections import OrderedDict

from tools.render.document_cache import _stat_key


_REVISION_CACHE: "dict" = {}
_REVISION_TTL = 0.1  # seconds


def invalidate_revision(path):
    """Force re-stat of `path` on next revision check."""
    _REVISION_CACHE.pop(path, None)


def _image_bytes(image):
    """How much memory this render actually occupies.

    The whole point of budgeting in bytes: a page is whatever its size and the
    window make it, and counting entries said nothing about that.
    """
    try:
        return int(image.sizeInBytes())
    except Exception:
        return max(1, image.width() * image.height() * 4)


def _revision(path):
    """What the file at `path` is right now — size and mtime, or None if it
    cannot be stat'd. Two renders of the same path with different revisions are
    renders of different documents.

    Cached with a short TTL to avoid repeated stat() calls during rapid access.
    """
    now = time.monotonic()
    cached = _REVISION_CACHE.get(path)
    if cached is not None:
        rev, ts = cached
        if now - ts < _REVISION_TTL:
            return rev
    rev = _stat_key(path)
    _REVISION_CACHE[path] = (rev, now)
    return rev


_active_path: str = ""
_active_page: int = 0


def _set_active(path: str, page: int):
    global _active_path, _active_page
    _active_path = path
    _active_page = page


def _priority_evict(store: "OrderedDict", key_path_fn, key_page_fn, active_path, active_page):
    """Evict one entry with priority: other-tab > same-tab-other-page > current
    page. key_path_fn(k) and key_page_fn(k) extract path and page index from a
    key. Returns the entry removed as (key, value), or None if empty."""
    # 1st pass: evict oldest entry from a non-active tab
    for k in store:
        if key_path_fn(k) != active_path:
            return k, store.pop(k)
    # 2nd pass: evict oldest entry from active tab that isn't the current page
    for k in store:
        if key_page_fn(k) != active_page:
            return k, store.pop(k)
    # Last resort: evict true LRU (current page — should almost never happen)
    if store:
        return store.popitem(last=False)
    return None


class _ByteBoundedCache:
    """A cache bounded by bytes and evicted by priority, not by age.

    Shared by _ThumbnailCache and _FullPageCache below, which were the same
    class written out twice: same locking, same "keep at least one entry",
    same priority-eviction call, same per-path invalidate/evict_tab. They
    differ only in what a key looks like beyond its first two elements and
    in how a stored entry's size and freshness are read out of it — both of
    which stay in the subclass, in get()/put().

    Every key used here has the pdf path as element 0 and the page index as
    element 1, in both caches, so invalidation, per-tab eviction and priority
    eviction can all key off (k[0], k[1]) without knowing anything else about
    the rest of the key.

    Not instantiated: each subclass keeps its own _lock/_store/_bytes/
    MAX_BYTES as class attributes — a module-level singleton per cache, the
    same shape both classes already had before this was factored out, so
    subclassing changes nothing about how callers use _ThumbnailCache.get()
    or _FullPageCache.put().
    """
    _lock: threading.Lock
    _store: "OrderedDict"
    MAX_BYTES: int
    _bytes = 0

    @classmethod
    def set_max_bytes(cls, value: int):
        """Thread-safe setter for MAX_BYTES that trims immediately."""
        with cls._lock:
            cls.MAX_BYTES = value
            cls._trim_locked()

    @classmethod
    def invalidate(cls, pdf_path=None):
        with cls._lock:
            if pdf_path is None:
                cls._store.clear()
                cls._bytes = 0
            else:
                for k in [k for k in cls._store if k[0] == pdf_path]:
                    cls._bytes -= cls._store.pop(k)[1]

    @classmethod
    def evict_tab(cls, pdf_path, keep_page=None):
        """Evict every entry for pdf_path, optionally keeping one page index."""
        with cls._lock:
            drop = [k for k in cls._store
                    if k[0] == pdf_path and (keep_page is None or k[1] != keep_page)]
            for k in drop:
                cls._bytes -= cls._store.pop(k)[1]

    @classmethod
    def _trim_locked(cls):
        """Evict until inside the budget. Never below one entry: the page on
        screen has to stay cached however small the budget is set. Caller
        must already hold cls._lock."""
        while cls._bytes > cls.MAX_BYTES and len(cls._store) > 1:
            dropped = _priority_evict(cls._store, lambda k: k[0], lambda k: k[1],
                                      _active_path, _active_page)
            if dropped is None:
                break
            cls._bytes -= dropped[1][1]


class _ThumbnailCache(_ByteBoundedCache):
    """LRU cache for rendered thumbnail QImages.
    Keyed by (pdf_path, page_idx, rotation, render_width).
    Stores QImage (thread-safe); caller converts to QPixmap on GUI thread.
    """
    _lock  = threading.Lock()
    _store: OrderedDict = OrderedDict()   # key -> (revision, nbytes, QImage)
    MAX_BYTES = 64 * 1024 * 1024          # raised by apply_performance_settings
    _bytes = 0

    @classmethod
    def get(cls, key):
        with cls._lock:
            entry = cls._store.get(key)
            if entry is None:
                return None
            rev, nbytes, image = entry
            if rev != _revision(key[0]):
                cls._store.pop(key)      # the file has been written since
                cls._bytes -= nbytes
                return None
            cls._store.move_to_end(key)
            return image

    @classmethod
    def put(cls, key, image):
        with cls._lock:
            old = cls._store.pop(key, None)
            if old is not None:
                cls._bytes -= old[1]
            nbytes = _image_bytes(image)
            cls._store[key] = (_revision(key[0]), nbytes, image)
            cls._bytes += nbytes
            cls._trim_locked()

    @classmethod
    def get_any(cls, path, pidx, rot):
        """Return any cached image for this page, ignoring render_width.
        Used as a placeholder when the exact render_width is not cached yet."""
        rev = _revision(path)
        with cls._lock:
            for k, (entry_rev, _nbytes, image) in cls._store.items():
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
            for (p, pi, r, w), (entry_rev, _nbytes, image) in cls._store.items():
                if (p == path and pi == pidx and r == rot and w >= width
                        and entry_rev == rev):
                    if best is None or w < best[0]:
                        best = (w, image)
        return best[1] if best else None


class _FullPageCache(_ByteBoundedCache):
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
    _store: OrderedDict = OrderedDict()   # key -> (revision, nbytes, entry)
    MAX_BYTES = 192 * 1024 * 1024         # raised by apply_performance_settings
    _bytes = 0

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
            rev, nbytes, entry = stored
            if rev != _revision(path):
                cls._store.pop(key)      # the file has been written since
                cls._bytes -= nbytes
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
                _, _, _, ex_scale, _ = existing[2]
                if new_scale < ex_scale * 0.99:
                    cls._store.move_to_end(key)  # refresh LRU even if not replacing
                    return
            if existing is not None:
                cls._bytes -= existing[1]
                cls._store.pop(key, None)
            nbytes = _image_bytes(entry[0])
            cls._store[key] = (rev, nbytes, entry)
            cls._bytes += nbytes
            cls._trim_locked()

    @classmethod
    def capacity(cls, default=8):
        """Roughly how many pages fit in the budget, at the size being rendered.

        "How many pages stay rendered" is not a number anyone can set directly:
        it is the memory budget divided by what a page of this document, in
        this window, actually costs. Measured from what is cached rather than
        assumed, so a poster and a paperback get different answers.
        """
        with cls._lock:
            if not cls._store:
                return default
            average = max(1, cls._bytes // len(cls._store))
        return max(2, min(64, cls.MAX_BYTES // average))

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
            for (p, pi, r, _aw, _ah), (entry_rev, _nbytes, entry) in cls._store.items():
                if p == path and pi == pidx and r == rot and entry_rev == rev:
                    img = entry[0]
                    if img.width() >= width and (best is None
                                                 or img.width() < best.width()):
                        best = img
        return best

    @classmethod
    def get_any(cls, path, pidx, rot):
        """Any cached full-page render for this page, whatever viewport bucket
        it was made for. Returns the full entry, or None.

        The bucket an entry lands in comes from the viewport size at render
        time. That size can move a few pixels while the layout settles — or
        when the chrome changes — and if it crosses a 50 px bucket boundary
        the render ends up in the bucket *next to* the one the view now reads
        from. The exact-bucket lookup then misses although the page really is
        rendered. This is the fallback that finds it, to reuse as a stand-in;
        it must only ever return the *same* page, so it is what keeps a page
        turn from picking up the page before it.
        """
        rev = _revision(path)
        with cls._lock:
            for (p, pi, r, _aw, _ah), (entry_rev, _nbytes, entry) in cls._store.items():
                if p == path and pi == pidx and r == rot and entry_rev == rev:
                    return entry
        return None

    @classmethod
    def get_dims(cls, path, pidx, rot):
        """Return (page_w_pt, page_h_pt) from any cached entry for this page.
        Returns (0, 0) if not cached yet."""
        rev = _revision(path)
        with cls._lock:
            for (p, pi, r, _aw, _ah), (entry_rev, _nbytes, entry) in cls._store.items():
                if p == path and pi == pidx and r == rot and entry_rev == rev:
                    return entry[1], entry[2]
        return 0.0, 0.0
