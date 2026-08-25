"""
Flat PDF copies of what the page manager is showing.

Tools run on a PDF, but the page manager keeps the document's state — page
order, rotations, pages pulled in from other tabs, inserted blanks — in an
in-memory PageModel that only writes through on save. A tool that read the
file on disk saw the original order and the unrotated pages, and "Leere Seite
einfügen" (which appends the blank to the end of the file and only records
where it should appear) put that blank on the back of the cover. This module
flattens the model's view into a temp PDF so a tool reads what the user sees.

One snapshot per document, not one per edit: every ensure_view_snapshot()
drops the previous snapshot of the same source first, so a long session does
not leave a trail of stale temp files behind. They are whole customer
documents, sitting in the temp directory of a shared machine — calling
discard_snapshots_for() on tab close and discard_all_snapshots() on shutdown
is what keeps that trail empty.

sweep_orphan_snapshots() at startup mops up everything a previous run did not
get to delete (a crash, a kill, a version of this code from before the
discard_* helpers existed). Safe to run at startup because nothing of ours is
on disk yet.
"""
import logging
import os
import tempfile
import uuid

from tools.app_state import AppState


_VIEW_SNAPSHOTS: dict = {}     # model signature -> flattened temp PDF
_SNAPSHOT_PATHS: set = set()   # the values above, for "is this path one of ours?"


def _remember_snapshot(sig, path):
    _VIEW_SNAPSHOTS[sig] = path
    _SNAPSHOT_PATHS.add(path)


def _forget_snapshot(sig):
    """Drop one snapshot and return the file it pointed at, or None."""
    path = _VIEW_SNAPSHOTS.pop(sig, None)
    if path is not None:
        _SNAPSHOT_PATHS.discard(path)
    return path


def snapshot_dir():
    """Where the flattened copies live."""
    return os.path.join(tempfile.gettempdir(), "copyshop_view")


def discard_snapshots_for(base_path):
    """Delete the flattened copies made of one document.

    Called when its tab closes. Nothing used to: a snapshot was only ever
    replaced, by the next snapshot of the same document, so closing a tab left
    its copy behind and quitting left every copy behind. They are whole
    documents — a customer's file, sitting in the temp directory of a shared
    machine long after the job went out of the door — and on a counter that
    opens a hundred files a day it is the largest thing this application
    leaves lying around.
    """
    if not base_path:
        return
    for sig in [s for s in _VIEW_SNAPSHOTS if s[0] == base_path]:
        stale = _forget_snapshot(sig)
        if not stale:
            continue
        try:
            os.remove(stale)
        except OSError:
            logging.debug("could not remove the view snapshot %s", stale,
                          exc_info=True)


def discard_all_snapshots():
    """Delete every flattened copy this process made. For shutdown."""
    for sig in list(_VIEW_SNAPSHOTS):
        stale = _forget_snapshot(sig)
        if not stale:
            continue
        try:
            os.remove(stale)
        except OSError:
            logging.debug("could not remove the view snapshot %s", stale,
                          exc_info=True)


def sweep_orphan_snapshots():
    """Remove flattened copies left behind by runs that did not get to clean up.

    A crash, a kill, or any version of this application from before the two
    functions above existed. Safe at startup because nothing of ours is on
    disk yet, and because a snapshot is only ever a cache — ensure_view_snapshot
    checks the file is still there and writes it again if it is not.
    """
    try:
        for name in os.listdir(snapshot_dir()):
            if not name.startswith("view_") or not name.endswith(".pdf"):
                continue        # not ours; leave anything else alone
            path = os.path.join(snapshot_dir(), name)
            if path in _SNAPSHOT_PATHS:
                continue        # this run is using it
            try:
                os.remove(path)
            except OSError:
                logging.debug("could not sweep %s", path, exc_info=True)
    except FileNotFoundError:
        pass                    # nothing has ever been written
    except Exception:
        logging.debug("could not sweep the view snapshots", exc_info=True)


def _model_signature(model, base_path):
    """Everything about a PageModel that changes the document a tool should see."""
    return (base_path,
            tuple(model.order),
            tuple(sorted(model.src.items())),
            tuple(sorted((u, r) for u, r in model.rotations.items() if r)),
            tuple(sorted(model.foreign_src.items())))


def ensure_view_snapshot(base_path: str) -> str:
    """Path to a PDF that matches what "Seiten verwalten" is showing, writing
    one first if there is not one already.

    Named for the write, because there is one. The old name, displayed_pdf,
    read like an accessor and hid that calling it can put a file in the
    temp directory.

    Tools used to process the file on disk, but the page manager keeps the page
    order, the rotations, pages pulled in from other tabs and inserted blanks in
    an in-memory PageModel — the file only catches up when the user saves. A
    booklet built from a document whose pages had been reordered was therefore
    imposed from the *old* order, and "Leere Seite einfügen" (which appends the
    blank to the end of the file and only records where it should appear) put
    that blank on the back of the cover.

    Returns `base_path` untouched when the model is a plain 1:1 view of the file
    — the common case, so nothing is written — and otherwise a temp PDF
    flattened into display order, cached per model state so repeated tool runs
    reuse it. Same page-for-page result as PdfTab.save_to().
    """
    model = getattr(AppState.get(), "page_model", None)
    if model is None or not getattr(model, "order", None) or not base_path:
        return base_path
    if base_path in _SNAPSHOT_PATHS:
        return base_path       # already flattened — never apply the model twice
    try:
        sig = _model_signature(model, base_path)
    except Exception:
        return base_path                       # not a model we understand
    cached = _VIEW_SNAPSHOTS.get(sig)
    if cached and os.path.isfile(cached):
        return cached
    try:
        from pypdf import PdfReader, PdfWriter
        n_file = len(PdfReader(base_path, strict=False).pages)
        # Identity view — the file already *is* what the user sees.
        if (not any(model.rotations.values()) and not model.foreign_src
                and [model.src.get(u) for u in model.order] == list(range(n_file))):
            return base_path
        readers = {}
        def _rdr(p):
            if p not in readers: readers[p] = PdfReader(p, strict=False)
            return readers[p]
        writer = PdfWriter()
        for uid in model.order:
            src_path, orig = model.page_source(uid, base_path)
            page = _rdr(src_path).pages[orig]
            rot  = model.get_rotation(uid)
            if rot: page.rotate(rot)
            writer.add_page(page)
        tmp_dir = snapshot_dir()
        os.makedirs(tmp_dir, exist_ok=True)
        out = os.path.join(tmp_dir, f"view_{uuid.uuid4().hex[:8]}.pdf")
        with open(out, "wb") as f:
            writer.write(f)
    except Exception:
        return base_path        # never block a tool because the snapshot failed
    # Drop the previous snapshot of the same file — one temp file per document,
    # not one per edit.
    discard_snapshots_for(base_path)
    _remember_snapshot(sig, out)
    return out
