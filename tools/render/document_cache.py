"""
A small registry of open pypdfium2 documents, and of the pages inside them.

Rendering a page used to reparse the whole file. Every thumbnail, every page
turn and every tool preview called ``pdfium.PdfDocument(path)``, pulled one page
out of it and closed the document again — so a 400-page PDF was parsed 400 times
to build its thumbnail strip. This keeps a handful of documents open and hands
those out instead.

Use :func:`page_document`, which takes the locks, keeps the document alive for
the duration of the block, and gives you the document itself::

    with page_document(path) as doc:
        page = doc[index]
        try:
            ...
        finally:
            page.close()

Close pages explicitly like that. A document that is never closed keeps every
page ever loaded from it registered until the garbage collector gets round to
it — measured at ~125 live pdfium page handles during a scroll, versus 1 when
pages are closed as they are finished with.


Why there is a page cache as well
---------------------------------
Caching the *document* only moved the cost down one level: ``doc[index]`` is
``FPDF_LoadPage``, which parses that page's content stream, and the viewer threw
the result away after every single render. On an A0 poster (3370x2384pt, 20 MB)
that is, measured per page, per render:

    page 0   351 ms      page 1  1444 ms      page 2  104 ms      page 3  212 ms

against 20-230 ms to actually rasterise a window of the same page once it is
loaded. Panning at deep zoom paid the parse again on every step; page 1 cost a
second and a half to look at, every time. :func:`open_page` keeps the loaded
page instead, so the second look at a page costs the render and nothing else.

The cache is deliberately tiny — :data:`MAX_PAGES` — because a loaded page is
not small: the same poster's pages measured 60-800 MB resident each. Four is
enough for what the viewer actually does (the page on screen, its neighbours,
and whatever a pre-render is warming) and bounds the worst case.

Pages belong to their document, so a handle going out of service closes its
pages first; nothing in :data:`_pages` can outlive the document it came from.


Locking
-------
This module was asked for per-document locking, on the reasoning that pdfium's
caches are only unsafe for concurrent work on the *same* document. That turns
out not to hold for this build (pypdfium2 5.8.0 / pdfium). Measured, three
times each:

  * 8 threads, 8 separate files, each thread opening and rendering its *own*
    document object, no lock: ``double free or corruption``, ``malloc():
    unaligned tcache chunk``, ``Segmentation fault``.
  * The same with the documents opened serially up front, so that only
    rendering overlaps: same crashes. It is not open/close contention —
    rendering itself shares mutable global state across documents.
  * The same workload with one process-wide lock: clean, every run.

So :data:`PDFIUM_LOCK` serialises every pdfium call in the process, and is the
lock that actually provides safety. ``tools.page_viewer`` re-exports it as
``_pdfium_lock``, so documents opened ad hoc by the tools and the print path are
mutually exclusive with the cached ones — one lock for all of pdfium, wherever
the document came from.

Each handle still carries its own :class:`threading.Lock`, taken inside the
global one. It costs an uncontended acquire per render and expresses the real
ownership: if pdfium ever becomes safe across documents, dropping the global
lock is all that stands between this and genuine parallel rendering.

Lock order
----------
``_registry_lock`` and :data:`PDFIUM_LOCK` are **never held at the same time**.
Bookkeeping collects the documents that need closing and closes them after the
registry lock is released. That is what keeps this deadlock-free, since closing
is itself a pdfium call.

``_pages_lock`` is a leaf: it is taken under either of the other two and never
holds anything itself, and no pdfium call is ever made while it is held. Page
eviction follows the same collect-then-close shape as document eviction.
"""

import logging
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager

# How many documents stay open. Small on purpose: each one holds a parsed PDF
# and an open file descriptor, and the viewer only ever works on a few at a time.
MAX_DOCUMENTS = 8

# How many *pages* stay loaded, across all documents. Much smaller than the
# document cap: a parsed page of a poster-sized PDF measured 60-800 MB resident,
# where the document itself was 24 MB. Four covers the page on screen, the two
# it can be turned to, and one being pre-rendered.
MAX_PAGES = 4

# Every pdfium call in the process goes through this. See "Locking" above for
# the measurements behind it; do not narrow it without repeating them.
PDFIUM_LOCK = threading.Lock()


class DocumentHandle:
    """One open document, the lock that guards it, and its cache identity.

    ``users`` counts the callers currently inside :func:`page_document` for this
    handle. Eviction drops a handle from the registry immediately but leaves the
    close to the last user, so a document can never be closed out from under a
    render in progress.
    """

    __slots__ = ("path", "key", "doc", "lock", "users", "retired")

    def __init__(self, path, key, doc):
        self.path    = path
        self.key     = key
        self.doc     = doc
        self.lock    = threading.Lock()   # per document, taken inside PDFIUM_LOCK
        self.users   = 0
        self.retired = False

    def __repr__(self):
        return (f"<DocumentHandle {os.path.basename(self.path)} "
                f"users={self.users}{' retired' if self.retired else ''}>")


# key -> handle, in least-recently-used order. Guarded by _registry_lock, which
# is only ever held for bookkeeping — never across a parse, a render or a close.
_registry: "OrderedDict" = OrderedDict()
_registry_lock = threading.Lock()

# (document key, page index) -> loaded page, least-recently-used order. See
# "Why there is a page cache as well" above. Guarded by _pages_lock, a leaf.
_pages: "OrderedDict" = OrderedDict()
_pages_lock = threading.Lock()


def _forget_pages(key):
    """Drop a document's pages from the page cache, without closing them.

    Closing is the document's job: ``PdfDocument.close()`` closes the pages
    loaded from it, and a retired handle that is still in use has its close
    deferred to the last user — so closing pages here would be closing them out
    from under a render that is still going.
    """
    with _pages_lock:
        for k in [k for k in _pages if k[0] == key]:
            _pages.pop(k, None)


def _stat_key(path):
    """(path, mtime, size) — or None when the file cannot be stat'd.

    Nanosecond mtime on purpose: a PDF rewritten twice within the same
    millisecond, which is exactly what the page manager does with its temp
    files, must not come back from the cache as the previous revision.
    """
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)


def _close_docs(docs):
    """Close documents that are out of service. Never called under the registry
    lock — closing is a pdfium call and needs PDFIUM_LOCK."""
    if not docs:
        return
    with PDFIUM_LOCK:
        for doc in docs:
            try:
                doc.close()
            except Exception:
                logging.debug("document_cache: close failed", exc_info=True)


def _retire_locked(handle, doomed):
    """Take a handle out of service. Caller holds _registry_lock.

    Appends to `doomed` rather than closing, so the close can happen once the
    registry lock is released. users is only ever non-zero between a checkout
    and its matching checkin, so a zero here means nobody holds this handle's
    lock and nobody is about to take it.
    """
    handle.retired = True
    _forget_pages(handle.key)
    if handle.users == 0:
        doomed.append(handle.doc)


def _checkout(path):
    """Reserve the handle for `path`, opening the document if needed.

    Returns None when the path cannot be stat'd, i.e. cannot be cached.
    """
    key = _stat_key(path)
    if key is None:
        return None

    with _registry_lock:
        handle = _registry.get(key)
        if handle is not None:
            _registry.move_to_end(key)
            handle.users += 1
            return handle
        # Same file, different revision: the old parse is worthless now.
        stale = [k for k in _registry if k[0] == key[0]]

    # Parsing happens outside the registry lock — a large PDF must not hold up
    # every other thread's bookkeeping — but inside PDFIUM_LOCK like any other
    # pdfium call.
    import pypdfium2 as pdfium
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)

    doomed = []
    with _registry_lock:
        for k in stale:
            victim = _registry.pop(k, None)
            if victim is not None:
                _retire_locked(victim, doomed)
        handle = _registry.get(key)
        if handle is not None:
            # Another thread opened the same document while we were parsing.
            doomed.append(doc)
            _registry.move_to_end(key)
        else:
            handle = DocumentHandle(path, key, doc)
            _registry[key] = handle
        # Reserved before eviction runs, so even being evicted immediately
        # cannot close a document this caller is about to use.
        handle.users += 1
        while len(_registry) > MAX_DOCUMENTS:
            _, victim = _registry.popitem(last=False)
            _retire_locked(victim, doomed)
    _close_docs(doomed)
    return handle


def _checkin(handle):
    doomed = []
    with _registry_lock:
        handle.users -= 1
        if handle.retired and handle.users == 0:
            doomed.append(handle.doc)
    _close_docs(doomed)


@contextmanager
def page_document(path):
    """Yield an open document for `path`, with the pdfium locks held.

    The document stays alive for the whole block even if it is evicted meanwhile.
    Do not keep a reference to it, or to anything loaded from it, past the block.
    """
    handle = _checkout(path)
    if handle is None:
        # Not cacheable (nothing to stat). Behave the way this code did before
        # the cache existed: open, use, close.
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument(path)
            try:
                yield doc
            finally:
                try:
                    doc.close()
                except Exception:
                    logging.debug("document_cache: close failed", exc_info=True)
        return
    try:
        with PDFIUM_LOCK:
            with handle.lock:
                yield handle.doc
    finally:
        _checkin(handle)


@contextmanager
def open_page(path, index):
    """Yield a loaded page of `path`, with the pdfium locks held.

    The page stays loaded afterwards — do not close it, and do not keep a
    reference to it past the block. Anything derived from it (a textpage, a
    bitmap's parent) must still be closed by the caller as before.

    This is :func:`page_document` plus the page cache; use it wherever a render
    or a text extraction needs one page, which is nearly everywhere. Fall back
    to page_document only when the whole document is the subject.
    """
    handle = _checkout(path)
    if handle is None:
        # Not cacheable (nothing to stat): open, use, close, like before.
        import pypdfium2 as pdfium
        with PDFIUM_LOCK:
            doc = pdfium.PdfDocument(path)
            page = None
            try:
                page = doc[index]
                yield page
            finally:
                for obj in (page, doc):
                    try:
                        if obj is not None:
                            obj.close()
                    except Exception:
                        logging.debug("document_cache: close failed", exc_info=True)
        return
    try:
        with PDFIUM_LOCK:
            with handle.lock:
                cache_key = (handle.key, index)
                with _pages_lock:
                    page = _pages.get(cache_key)
                    if page is not None:
                        _pages.move_to_end(cache_key)
                if page is None:
                    page = handle.doc[index]        # FPDF_LoadPage: the expensive bit
                    doomed = []
                    with _pages_lock:
                        _pages[cache_key] = page
                        _pages.move_to_end(cache_key)
                        # max(1, ...): the page just checked out is the newest,
                        # so a cap of at least one can never evict the one this
                        # caller is about to render.
                        while len(_pages) > max(1, MAX_PAGES):
                            doomed.append(_pages.popitem(last=False)[1])
                    # Closing is a pdfium call, so it happens out here — the
                    # page lock is a leaf and nothing pdfium ever runs under it.
                    for victim in doomed:
                        try:
                            victim.close()
                        except Exception:
                            logging.debug("document_cache: page close failed",
                                          exc_info=True)
                yield page
    finally:
        _checkin(handle)


def release(path):
    """Drop everything cached for `path` — the parsed document and its loaded
    pages. Called when the tab showing it closes.

    Handles still in use are retired rather than closed, exactly as eviction
    does, so this can never pull a document out from under a render in flight.
    """
    doomed = []
    with _registry_lock:
        try:
            target = os.path.abspath(path)
        except (TypeError, ValueError):
            return 0
        keys = [k for k in _registry if k[0] == target]
        for k in keys:
            handle = _registry.pop(k, None)
            if handle is not None:
                _retire_locked(handle, doomed)
    _close_docs(doomed)
    return len(keys)


def close_all():
    """Close every cached document. Called on shutdown.

    Handles still in use are dropped from the registry now and closed by their
    last user, so this never pulls a document out from under a running render.
    """
    doomed = []
    with _registry_lock:
        handles = list(_registry.values())
        _registry.clear()
        for handle in handles:
            _retire_locked(handle, doomed)
    with _pages_lock:
        _pages.clear()          # closed with their documents, below
    _close_docs(doomed)


def stats():
    """Snapshot of the registry, for diagnostics and tests."""
    with _pages_lock:
        pages = len(_pages)
    with _registry_lock:
        return {
            "open": len(_registry),
            "max": MAX_DOCUMENTS,
            "paths": [h.path for h in _registry.values()],
            "in_use": sum(1 for h in _registry.values() if h.users),
            "pages": pages,
            "pages_max": MAX_PAGES,
        }
