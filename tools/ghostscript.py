"""
Running Ghostscript: finding it, and running one conversion as several.

Five places shell out to Ghostscript — the PDF/X export, greyscale, compress
and CMYK tools, and the print spooler — and each of them used to find it and
name it its own way. Four of the conversions also handed it the whole document
as a single process. Ghostscript is single-threaded per document, so that left
every core but one idle for minutes on exactly the files that take minutes.

Pages convert independently, so a conversion can be split by page range and
the ranges run at the same time: `-dFirstPage`/`-dLastPage` restrict a run to
its own slice of the input, and the slices are stitched back together with
pikepdf afterwards. What comes out is assembled from the same pages in the
same order, so everything downstream — the page-count check, the blackout
verification in _verify.py, the damaged-page retries — sees one file and does
not need to know it was made by more than one process.

Below the threshold this does nothing at all: one command, straight to the
output, no page-range flags and no merge step, which is byte for byte what
the tools did before this existed. Splitting a short document costs more in
process startup than it saves.
"""
import contextlib
import logging
import os
import shutil
import tempfile

from tools.i18n import tr


# A chunk has to do enough work to make its own Ghostscript startup a rounding
# error rather than a tax. grayscale.py's batch retry measured that startup in
# this codebase — "23 pages x 0.3 s each = 7 s of process overhead" — which is
# why it batches damaged pages into one process instead of spawning one each.
# The same arithmetic in reverse sets the floor here: under about eight pages a
# chunk spends a noticeable fraction of its life starting up.
MIN_CHUNK_PAGES = 8

# Below twice the chunk floor the split would come out as one chunk anyway, so
# this only skips the temp-file and merge bookkeeping on documents it could not
# have helped.
MIN_PAGES_TO_CHUNK = 2 * MIN_CHUNK_PAGES

# Deliberately below the core count. Every one of these conversions is
# image-heavy prepress work, and a PDF/X-3 export rasterises whole pages at
# press resolution — pdfx.py caps a single page at MAX_RASTER_PIXELS
# (240 Mpx) because this application has already run into the limits of
# rasterising one page at a time. Twelve of those at once on a twelve-core
# machine is a memory problem, not a speed-up, and the fourth worker is
# already well past the point of diminishing returns on wall-clock.
MAX_WORKERS = 4


def ghostscript_binary():
    """The Ghostscript executable, or None.

    The name differs by platform, and four of the five callers used to look for
    `gs` alone and then put the literal string "gs" in the command — so on
    Windows they reported Ghostscript missing on a machine that had it, while
    the greyscale tool right next to them found it. One lookup, one answer.
    """
    return (shutil.which("gs") or shutil.which("gswin64c")
            or shutil.which("gswin32c"))


def require_ghostscript():
    """The Ghostscript executable, or a RuntimeError saying how to install it."""
    gs_bin = ghostscript_binary()
    if not gs_bin:
        raise RuntimeError(tr(
            "Ghostscript nicht gefunden.\n"
            "Installation:  sudo pacman -S ghostscript"))
    return gs_bin


def plan_chunks(n_pages, cpu_count=None):
    """The page ranges to convert concurrently, as 1-based inclusive pairs.

    Returns a single whole-document range when splitting is not worth it, so
    callers never branch on whether chunking happened — they always get a list
    of ranges and always hand it to run_chunked.
    """
    n_pages = max(1, int(n_pages))
    if n_pages < MIN_PAGES_TO_CHUNK:
        return [(1, n_pages)]
    cpus = cpu_count or os.cpu_count() or 1
    workers = min(cpus, MAX_WORKERS, n_pages // MIN_CHUNK_PAGES)
    if workers < 2:
        return [(1, n_pages)]
    # The remainder goes to the first chunks, one page each, rather than all of
    # it onto the last one. A parallel run finishes when its slowest chunk
    # finishes, and the chunk carrying the whole remainder is that chunk.
    size, extra = divmod(n_pages, workers)
    ranges, first = [], 1
    for i in range(workers):
        last = first + size - 1 + (1 if i < extra else 0)
        ranges.append((first, last))
        first = last + 1
    return ranges


def page_range_flags(first, last):
    """The Ghostscript flags restricting a run to one page range.

    Empty for the whole document, so a build_cmd can splice this in
    unconditionally. The flags have to precede the input PDF on the command
    line — they apply to the next document Ghostscript opens — but an
    intervening PostScript prologue does not disturb them, which is what lets
    the PDF/X export chunk despite passing its pdfmark defs first (verified
    against Ghostscript 10.07).
    """
    if first is None or last is None:
        return []
    return [f"-dFirstPage={first}", f"-dLastPage={last}"]


def failed(results):
    """The first chunk that did not exit 0, or None if they all did.

    run_many either fills every slot or raises, so every result here is a real
    CompletedProcess. Each tool words its own Ghostscript failure, so this
    only reports which one to complain about rather than raising a message of
    its own.
    """
    for r in results:
        if r.returncode != 0:
            return r
    return None


def run_chunked(report, build_cmd, out, n_pages, *, timeout=None,
                cpu_count=None, carry_document_state=False):
    """Run a Ghostscript conversion as several concurrent ones and assemble
    the result into `out`.

    `build_cmd(dest, first, last)` returns the argv for one chunk: the whole
    command, writing to `dest`, covering the 1-based inclusive page range
    `first`..`last` — both None when the whole document goes through in one
    run. A builder rather than a command template because the four callers put
    their output path in three different places (`-o`, `-sOutputFile=`) and
    their input in two, and templating over that is harder to read than
    letting each one write its own command line.

    Returns the CompletedProcess of every chunk, for the caller to check with
    failed(). Nothing is merged when any chunk failed: `out` is left
    unwritten, which is what the callers' existing "Ghostscript produced no
    output" checks already report.

    carry_document_state copies the catalog and docinfo entries that a merge
    would otherwise drop; see _carry_document_state.
    """
    ranges = plan_chunks(n_pages, cpu_count)
    if len(ranges) == 1:
        return [report.run(build_cmd(out, None, None),
                           text=True, errors="replace", timeout=timeout)]

    report(tr("Ghostscript: {p0} Seitenbereiche parallel …").format(p0=len(ranges)))
    parts = []
    try:
        for _ in ranges:
            fd, part = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            parts.append(part)
        results = report.run_many(
            [build_cmd(part, first, last)
             for part, (first, last) in zip(parts, ranges)],
            timeout=timeout, text=True, errors="replace")
        if failed(results) is None:
            report(tr("Seitenbereiche werden zusammengefuegt …"))
            merge_chunks(parts, out, carry_document_state=carry_document_state)
        return results
    finally:
        for part in parts:
            with contextlib.suppress(OSError):
                os.remove(part)


def merge_chunks(parts, out, *, carry_document_state=False):
    """Assemble the chunk outputs into one document at `out`, in page order."""
    import pikepdf

    with contextlib.ExitStack() as stack:
        merged = stack.enter_context(pikepdf.Pdf.new())
        first_pdf = None
        for part in parts:
            pdf = stack.enter_context(pikepdf.open(part))
            if first_pdf is None:
                first_pdf = pdf
            for page in pdf.pages:
                merged.pages.append(page)
        if carry_document_state and first_pdf is not None:
            _carry_document_state(first_pdf, merged)
        merged.save(out)


def _carry_document_state(src_pdf, dst_pdf):
    """Copy the document-level entries that appending pages does not.

    pikepdf's pages.append() copies page objects and nothing else, so a merged
    file comes out with no catalog and no docinfo of its own. For PDF/X that
    loses the output intent and the /GTS_PDFXVersion marker — which is to say
    it loses the thing that makes the file PDF/X rather than an ordinary PDF,
    while every chunk that went into it had one.

    Taking them from the first chunk is taking them from all of them: the
    output intent is built from the pdfmark prologue and the ICC path, neither
    of which varies by page range, and every chunk of a real export was
    measured coming back with a byte-identical ICC stream and the same
    OutputConditionIdentifier. test_gs_pool.py holds that to be true rather
    than trusting it, because it is a fact about Ghostscript's behaviour and
    not about this code.
    """
    import pikepdf

    if "/OutputIntents" in src_pdf.Root:
        # Element by element, not the array itself: pdfwrite writes
        # /OutputIntents as a direct array holding an indirect intent
        # dictionary, and copy_foreign refuses a direct object. Each intent has
        # to go through it, though — it owns the embedded ICC profile stream,
        # and an intent copied without its profile is an intent naming a
        # printing condition it cannot prove.
        dst_pdf.Root["/OutputIntents"] = pikepdf.Array(
            [dst_pdf.copy_foreign(intent)
             for intent in src_pdf.Root["/OutputIntents"]])
    for key in ("/GTS_PDFXVersion", "/Trapped"):
        if key in src_pdf.docinfo:
            # By value: these are scalars, and carrying the reference would tie
            # the merged file to a chunk that is deleted moments later.
            dst_pdf.docinfo[key] = pikepdf.Object.parse(
                src_pdf.docinfo[key].unparse())


def unlink(*paths):
    """Remove temp files, ignoring the ones that were never created.

    The tools each build several temp files and drop them in a finally, which
    is right, and it was six lines of try/except OSError per function to say
    so. None is accepted so a path that a failure left unbound still cleans up
    — grayscale.py used to catch NameError here for exactly that case.
    """
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            logging.debug("could not remove the temp file %s", path, exc_info=True)
