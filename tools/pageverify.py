"""
Comparing a converted page against its original — the part that renders.

This module is deliberately a leaf: stdlib, pypdfium2 and Pillow, and nothing
from the rest of the application. It is run as a worker process
(``python -m tools.pageverify job.json``), and every import it has is paid for
again in every worker. tools/i18n.py pulls in PyQt6, so a worker that reached
for a translated string would drag a GUI toolkit into a process that never
draws anything — the same cost main.py's deferred import exists to avoid.

Why a separate process at all
-----------------------------
pdfium is serialised process-wide behind one lock, so verifying 145 pages is
145 renders that cannot overlap with each other or with the viewer. Measured on
an image-heavy PDF/X-4 export, the verification was 3.5 s of a 5.7 s export —
more than Ghostscript itself. Threads cannot help: they would queue on the same
lock. A separate process has its own pdfium and its own lock, so the work
actually runs at the same time.

The pages are the unit of work and the verdicts are the only thing that comes
back — a dict of page index to reason, a few dozen bytes. The renders never
cross the boundary.
"""
import json
import sys
from tools.render.document_cache import open_document as _open_pdf


VERIFY_SCALE   = 0.30    # ~180 px across an A4 page — enough to see a blackout
BLACKOUT_LIMIT = 0.004   # 0.4 % of the page turning solid black is already wrong


def conversion_damage(ref_l, got_l):
    """(blacked_out, vanished) as fractions of the page.

    `blacked_out` is the share of pixels that were clearly light in the original
    and came back solid black; `vanished` is the reverse — content that was dark
    and is now blank paper. Both are compared against a *greyscale* render of the
    original, so a legitimate colour→grey conversion scores ~0: only gross
    damage registers, not the small colorimetric differences between
    Ghostscript's conversion and Pillow's."""
    from PIL import ImageChops
    if got_l.size != ref_l.size:
        got_l = got_l.resize(ref_l.size)
    light = ref_l.point(lambda v: 255 if v > 160 else 0)
    dark  = ref_l.point(lambda v: 255 if v < 90  else 0)
    now_black = got_l.point(lambda v: 255 if v < 50  else 0)
    now_blank = got_l.point(lambda v: 255 if v > 230 else 0)
    total = ref_l.size[0] * ref_l.size[1] or 1
    hit = lambda a, b: ImageChops.darker(a, b).histogram()[255] / total
    return hit(light, now_black), hit(dark, now_blank)


def verify_range(src, cand, indices, scale=VERIFY_SCALE, on_page=None):
    """Which of `indices` came out damaged in `cand` compared with `src`.

    Keyed by the original page index throughout — never by position in
    `indices`. A sparse selection is the normal case here (greyscale converts
    the pages that need it, not a prefix), and reporting damage against the
    wrong page number sends the operator to reprint a sheet that was fine.

    `on_page` is called with each index as it completes, for progress and for
    the caller's cancellation check; it may raise to abort the walk.

    Both documents are opened once and held for the whole walk: going page by
    page reopened them 2N times, and parsing is the expensive half on the heavy
    documents this runs on. No lock is taken — in a worker this process owns
    pdfium outright, and the in-process caller holds the lock around the call.
    """
    bad = {}
    src_doc = cand_doc = None
    try:
        try:
            src_doc = _open_pdf(src)
            cand_doc = _open_pdf(cand)
        except Exception:
            # Opened one and then the other failed: close what opened, or the
            # document handle leaks for the life of the process.
            for d in (src_doc, cand_doc):
                if d is not None:
                    d.close()
            raise
        for i in indices:
            if on_page is not None:
                on_page(i)
            try:
                ref = src_doc[i].render(scale=scale).to_pil().convert("L")
                got = cand_doc[i].render(scale=scale).to_pil().convert("L")
                blacked, vanished = conversion_damage(ref, got)
            except Exception:
                # Could not check it — treat as damaged rather than assume it
                # is fine. Silently shipping an unverified page is the whole
                # problem.
                bad[i] = "unverified"
                continue
            if blacked > BLACKOUT_LIMIT:
                bad[i] = f"{blacked * 100:.1f}% schwarz"
            elif vanished > BLACKOUT_LIMIT:
                bad[i] = f"{vanished * 100:.1f}% verschwunden"
    finally:
        for d in (src_doc, cand_doc):
            if d is not None:
                d.close()
    return bad


def main(argv):
    """Worker entry point: one JSON job in, one JSON verdict out.

    The job file carries {src, cand, indices, scale}; stdout carries
    {"bad": {index: reason}}. A file rather than argv because a 145-page
    selection is a long list, and stdout rather than a file because the verdict
    is small and the parent is already reading the pipe.

    Anything unexpected exits non-zero with the reason on stderr — the parent
    then verifies in its own process instead, which is slower and always
    correct. A verification that cannot run must never look like a pass.
    """
    if len(argv) != 2:
        print("usage: python -m tools.pageverify JOB.json", file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        job = json.load(f)
    bad = verify_range(job["src"], job["cand"], job["indices"],
                       job.get("scale", VERIFY_SCALE))
    json.dump({"bad": {str(k): v for k, v in bad.items()}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
