"""
A small registry of open pypdfium2 documents.

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
"""

import logging
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager

# How many documents stay open. Small on purpose: each one holds a parsed PDF
# and an open file descriptor, and the viewer only ever works on a few at a time.
MAX_DOCUMENTS = 8

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


def get_handle(path):
    """The :class:`DocumentHandle` for `path`, opening the document if needed."""
    handle = _checkout(path)
    if handle is None:
        raise FileNotFoundError(path)
    _checkin(handle)
    return handle


def get_document(path):
    """The cached, still-open document for `path`.

    Note what this does *not* give you: the document is not pinned, so it may be
    evicted and closed by another thread's checkout at any moment, and no lock
    is held. Hold :data:`PDFIUM_LOCK` around every call you make on it, or use
    :func:`page_document`, which handles the lifetime and the locking together.
    """
    return get_handle(path).doc


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
    _close_docs(doomed)


def stats():
    """Snapshot of the registry, for diagnostics and tests."""
    with _registry_lock:
        return {
            "open": len(_registry),
            "max": MAX_DOCUMENTS,
            "paths": [h.path for h in _registry.values()],
            "in_use": sum(1 for h in _registry.values() if h.users),
        }
