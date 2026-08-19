"""
Checking that a colour conversion did not destroy the page.

Shared by Compress, Grayscale, PDF/X and ColourProfile.

The rendering and the comparison live in tools/pageverify.py, which imports no
Qt so it can also be run as a worker process. This module is the part that
knows about jobs, progress and the process-wide pdfium lock, and decides
whether a verification is big enough to be worth spreading over several
processes.
"""
import json
import logging
import gc
import os
import sys
import tempfile
import time

from tools.ghostscript import MAX_WORKERS, unlink
from tools.jobs import Cancelled
from tools.pageverify import VERIFY_SCALE as _VERIFY_SCALE, verify_range
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.i18n       import tr


# Below this there is nothing to win. A worker costs ~75 ms to start and has to
# parse both documents before its first render, so a handful of pages is paid
# for twice over.
MIN_PAGES_TO_SPREAD = 24

# Pages per worker, at least. Same reasoning as ghostscript.MIN_CHUNK_PAGES:
# a worker wants enough renders in front of it to make its own startup a
# rounding error.
MIN_PAGES_PER_WORKER = 12

# How many pages to verify here, timed, before deciding whether to spread the
# rest. The page count on its own is a bad predictor: a text page verifies in
# about 3 ms and a full-bleed photo in about 140 ms, so thirty pages is either
# a tenth of a second or four seconds and nothing about the number says which.
# Measuring a few costs nothing — they are pages that had to be verified anyway.
PROBE_PAGES = 8

# Spread only when the pages left look like more work than the workers cost to
# start. Set from the measured regression this replaced: a 30-page text export
# spawned two workers to save 90 ms of rendering and came out 40 ms slower.
SPREAD_IF_PROJECTED_OVER = 1.5   # seconds


def _page_luma(path, index, scale=_VERIFY_SCALE):
    """Greyscale render of one page, for comparing before against after."""
    import pypdfium2 as pdfium
    gc.disable()
    try:
        with _pdfium_lock:
            doc = pdfium.PdfDocument(path)
            try:
                return doc[index].render(scale=scale).to_pil().convert("L")
            finally:
                doc.close()
    finally:
        gc.collect(); gc.enable()


def _plan_workers(n_pages, cpu_count=None):
    """How many worker processes to spread `n_pages` over. 1 means do it here."""
    if n_pages < MIN_PAGES_TO_SPREAD:
        return 1
    cpus = cpu_count or os.cpu_count() or 1
    return max(1, min(cpus, MAX_WORKERS, n_pages // MIN_PAGES_PER_WORKER))


def _deal(wanted, workers):
    """Split `wanted` into `workers` lists, dealt round-robin.

    Round-robin rather than contiguous slices: a document's heavy pages come in
    runs — a photo section, a chapter of maps — and contiguous slices would put
    one whole run in one worker while another finished early on text. Dealing
    interleaves them, and since every index carries its own verdict back, the
    order pages are checked in does not matter.
    """
    lots = [[] for _ in range(workers)]
    for n, i in enumerate(wanted):
        lots[n % workers].append(i)
    return [lot for lot in lots if lot]


def _verify_in_workers(src, cand, wanted, report, label, workers):
    """Verify across worker processes. Returns the verdicts, or None to say
    'could not, do it in this process instead'.

    None rather than an exception on failure, and never a partial answer: this
    is the check that stops a blacked-out page reaching a press, so the only
    two acceptable outcomes are a complete verdict and an honest admission that
    there is not one.
    """
    lots = _deal(wanted, workers)
    jobs = []
    try:
        cmds = []
        for lot in lots:
            fd, job = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump({"src": src, "cand": cand, "indices": lot,
                           "scale": _VERIFY_SCALE}, f)
            jobs.append(job)
            cmds.append([sys.executable, "-m", "tools.pageverify", job])

        if report:
            report(tr('Prüfe {p0} Seiten in {p1} Prozessen …{p2}').format(
                p0=len(wanted), p1=len(cmds), p2=label))
        # PYTHONPATH rather than cwd: the worker has to import tools.pageverify,
        # and changing the working directory would change what the relative
        # paths in the job file mean. They are absolute, but only because
        # nothing has needed them not to be yet.
        env = dict(os.environ)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        results = report.run_many(cmds, text=True, errors="replace", env=env)

        bad = {}
        for r in results:
            if r.returncode != 0:
                logging.error("page verification worker failed (exit %d): %s",
                              r.returncode, (r.stderr or "").strip()[:400])
                return None
            payload = json.loads(r.stdout)
            bad.update({int(k): v for k, v in payload["bad"].items()})
        return bad
    except Cancelled:
        # Stop is not a failure to fall back from. Cancelled is an Exception,
        # so without this the broad clause below would swallow it and quietly
        # start the whole verification again in this process — a Stop that
        # made the work take longer.
        raise
    except Exception:
        # Anything at all — no interpreter, no importable package, malformed
        # output: fall back rather than report a document unverified.
        logging.exception("could not verify in worker processes")
        return None
    finally:
        unlink(*jobs)


def _verify_here(src, cand, wanted, report, label, done_before=0, total=None):
    """Verify in this process, under the pdfium lock.

    The lock is held for the whole render loop, not released per page. Each
    render is ~10 ms, so the entire verify is a few seconds under one lock take;
    releasing between pages let the viewer's render queue sneak in a 500 ms
    thumbnail between every 10 ms verify render, stretching a 3 s verify into
    minutes on a 145-page document. The viewer freezes for those few seconds —
    acceptable during a conversion the user is waiting on — instead of
    degrading for minutes.

    `done_before`/`total` are for the probe, which verifies the first pages
    here and may hand the rest to workers: without them the progress line
    restarts at "Seite 1" partway through a document.
    """
    done = [done_before]
    total = len(wanted) if total is None else total

    def step(_i):
        # Between pages, never inside one: a page is the unit of work here and
        # half a comparison tells nobody anything.
        done[0] += 1
        if report is not None and getattr(report, "cancelled", False):
            report.check()
        if report and done[0] % 5 == 1:
            report(tr('Prüfe Seite {p0} / {p1} …{p2}').format(
                p0=done[0], p1=total, p2=label))

    # See grayscale._scan_pages: PIL images from to_pil() can land in cyclic GC
    # on the render worker's thread, firing pdfium finalizers without the lock.
    gc.disable()
    try:
        with _pdfium_lock:
            return verify_range(src, cand, wanted, _VERIFY_SCALE, on_page=step)
    finally:
        gc.collect()
        gc.enable()


def _verify_pages_intact(src, cand, pages, report, label=""):
    """Which of `pages` came out damaged in `cand` compared with `src`.

    Ghostscript reports success and exits 0 while blacking out a transparency
    group or a soft-masked image — the failure this exists to catch. It is
    silent, it is invisible until the job is printed, and it is not something a
    return code will ever tell us about, so every converted page is looked at.

    A long verification is spread over worker processes. pdfium is serialised
    process-wide, so this was the one part of a conversion that could not be
    made to use more than one core — measured at 3.5 s of a 5.7 s PDF/X-4
    export, more than Ghostscript itself. Workers each have their own pdfium,
    and the viewer keeps rendering while they run because this process never
    takes the lock.

    Whether to spread is decided by verifying the first PROBE_PAGES here and
    timing them, not by the page count. The count cannot tell a text page from
    a photograph, and guessing from it made a thirty-page text export *slower*:
    two workers started to save ninety milliseconds of rendering.

    Used by compress, greyscale, CMYK and PDF/X; `report` may be None.
    Returns {page_index: reason}."""
    wanted = sorted(pages)
    if not wanted:
        return {}
    can_spread = report is not None and hasattr(report, "run_many")
    if not can_spread or len(wanted) < MIN_PAGES_TO_SPREAD:
        return _verify_here(src, cand, wanted, report, label)

    probe, rest = wanted[:PROBE_PAGES], wanted[PROBE_PAGES:]
    started = time.monotonic()
    bad = _verify_here(src, cand, probe, report, label, total=len(wanted))
    per_page = (time.monotonic() - started) / len(probe)

    workers = _plan_workers(len(rest))
    if workers > 1 and per_page * len(rest) > SPREAD_IF_PROJECTED_OVER:
        spread = _verify_in_workers(src, cand, rest, report, label, workers)
        if spread is not None:
            bad.update(spread)
            return bad
        if report:
            report(tr("Prüfung läuft in diesem Prozess weiter …"))
    if rest:
        bad.update(_verify_here(src, cand, rest, report, label,
                                done_before=len(probe), total=len(wanted)))
    return bad
