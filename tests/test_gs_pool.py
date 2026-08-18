"""
Running one Ghostscript conversion as several.

The risk this covers is not that chunking is slow — it is that it is wrong in
ways that look right. A merge that puts the pages back in the wrong order, a
damaged page blamed on the wrong page number, or a PDF/X file that quietly
lost its output intent all produce a plausible PDF that fails on press.
"""
import os
import shutil
import subprocess
import threading
import time

import pikepdf
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from tools.ghostscript import (MAX_WORKERS, MIN_CHUNK_PAGES, MIN_PAGES_TO_CHUNK,
                               ghostscript_binary, merge_chunks,
                               page_range_flags, plan_chunks, run_chunked)
from tools.jobs import Cancelled, Progress, null_progress
from tests.support import _TMP


class _FakeJob:
    """A Job as far as Progress is concerned. Mirrors test_cancel.py."""

    def __init__(self):
        self.cancelled = False
        self.messages = []

    def report(self, message):
        self.messages.append(message)


def _numbered_pdf(n_pages, name):
    """A document whose every page says which page it is.

    The marker is what makes a mis-ordered merge visible: pages that all look
    alike would come back through any permutation looking correct.
    """
    path = os.path.join(_TMP, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 60)
        c.drawString(80, 700, f"PAGE{i + 1:03d}")
        c.showPage()
    c.save()
    return path


def _labels(path):
    return [p.extract_text().strip().replace("\n", "") for p in PdfReader(path).pages]


# ── planning ─────────────────────────────────────────────────────────────────

def test_chunk_plan_covers_every_page_exactly_once():
    """The plan is a partition of the document, or pages are lost or doubled.

    Checked over a matrix rather than a couple of cases: the remainder
    handling is the part that goes wrong, and it only goes wrong at page
    counts that do not divide evenly by the worker count.
    """
    for n_pages in list(range(1, 80)) + [145, 500, 1001]:
        for cpus in (1, 2, 3, 4, 8, 12, 64):
            ranges = plan_chunks(n_pages, cpu_count=cpus)
            covered = [p for first, last in ranges for p in range(first, last + 1)]
            assert covered == list(range(1, n_pages + 1)), (
                f"{n_pages} pages on {cpus} cpus: {ranges} covers {covered[:12]}…")
            assert all(first <= last for first, last in ranges), ranges
    return "every page in exactly one range, contiguous and in order"


def test_short_documents_are_not_split_at_all():
    """Splitting costs a Ghostscript startup per chunk. Below the threshold
    that is all it would buy, so the command has to come out exactly as it did
    before any of this existed — one run, no page-range flags, no merge."""
    for n_pages in range(1, MIN_PAGES_TO_CHUNK):
        assert plan_chunks(n_pages, cpu_count=64) == [(1, max(1, n_pages))], n_pages
    # And the flags a single whole-document run gets are none.
    assert page_range_flags(None, None) == []
    assert page_range_flags(3, 7) == ["-dFirstPage=3", "-dLastPage=7"]
    return f"nothing under {MIN_PAGES_TO_CHUNK} pages is split"


def test_the_worker_count_is_capped_on_both_sides():
    """Neither the core count nor the page count may run away with it.

    A 12-core machine must not start twelve Ghostscript processes each
    rasterising a press-resolution page, and a 20-page document must not be
    cut into 16 chunks of one page because the machine has 16 cores.
    """
    assert len(plan_chunks(1000, cpu_count=64)) == MAX_WORKERS
    assert len(plan_chunks(1000, cpu_count=2)) == 2
    for n_pages in (20, 40, 100):
        ranges = plan_chunks(n_pages, cpu_count=64)
        assert len(ranges) <= n_pages // MIN_CHUNK_PAGES, (n_pages, ranges)
        assert all(last - first + 1 >= 2 for first, last in ranges), ranges
    return f"at most {MAX_WORKERS} workers, and never a chunk of one page"


# ── run_many ─────────────────────────────────────────────────────────────────

def test_results_come_back_in_the_order_the_commands_were_given():
    """Chunk N's output file is matched to chunk N's page range by position,
    so a results list in finish order would assemble the document wrong."""
    report = Progress(_FakeJob())
    # Deliberately reversed durations: the last command finishes first.
    cmds = [["sh", "-c", f"sleep 0.{9 - i}; printf {i}"] for i in range(4)]
    results = report.run_many(cmds, text=True)
    assert [r.stdout for r in results] == ["0", "1", "2", "3"], \
        [r.stdout for r in results]
    return "given order, not finish order"


def test_stopping_kills_every_child_not_just_one():
    """The bug this design has to avoid. Stop reached one Ghostscript when
    there was one; with four running, killing one and abandoning three leaves
    three cores busy on work nobody will collect."""
    job = _FakeJob()
    report = Progress(job)
    marker = "copyshop_run_many_victim"
    started = time.time()
    threading.Timer(0.5, lambda: setattr(job, "cancelled", True)).start()
    try:
        report.run_many([["sh", "-c", f"sleep 30 # {marker}"] for _ in range(4)])
    except Cancelled:
        pass
    else:
        raise AssertionError("a cancelled batch returned normally")
    elapsed = time.time() - started
    assert elapsed < 8, f"took {elapsed:.1f}s to stop four children"

    leftover = subprocess.run(["pgrep", "-f", marker],
                              capture_output=True, text=True).stdout.strip()
    assert not leftover, f"children survived the cancel: {leftover}"
    return f"all four stopped and reaped in {elapsed:.1f}s"


def test_one_command_timing_out_does_not_wait_for_the_others():
    """A per-command timeout, and the rest give up rather than finishing work
    whose result is already being thrown away."""
    report = Progress(_FakeJob())
    started = time.time()
    try:
        report.run_many([["sleep", "30"], ["sleep", "30"]], timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("the timeout was not honoured")
    elapsed = time.time() - started
    assert elapsed < 8, f"the batch took {elapsed:.1f}s to report a 0.5s timeout"
    return f"reported in {elapsed:.1f}s, siblings abandoned"


def test_max_workers_actually_bounds_how_many_run_at_once():
    """The cap is the whole memory argument for MAX_WORKERS. If it does not
    hold, a press-resolution export can still start twelve rasterisers."""
    report = Progress(_FakeJob())
    live = os.path.join(_TMP, "concurrency_peak")
    shutil.rmtree(live, ignore_errors=True)
    os.makedirs(live)
    # Each command announces itself, holds still, and leaves. The peak count of
    # files present is the peak concurrency.
    cmds = [["sh", "-c", f"touch {live}/{i}; sleep 0.6; rm {live}/{i}"]
            for i in range(8)]
    peak = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            peak.append(len(os.listdir(live)))
            time.sleep(0.02)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        report.run_many(cmds, max_workers=2)
    finally:
        stop.set(); watcher.join()
    assert max(peak) <= 2, f"ran {max(peak)} at once with max_workers=2"
    assert max(peak) == 2, f"never reached the cap ({max(peak)}) — did it serialise?"
    return f"peaked at {max(peak)} with the cap at 2"


# ── merging ──────────────────────────────────────────────────────────────────

def test_a_chunked_conversion_keeps_the_pages_in_order():
    """The whole point, end to end through real Ghostscript.

    A page landing at the wrong index is the failure that survives every check
    downstream — the page count is right, no page is blacked out, and the file
    opens. Only the content says otherwise.
    """
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    src = _numbered_pdf(40, "pool_ordered.pdf")
    out = os.path.join(_TMP, "pool_ordered_out.pdf")

    def build(dest, first, last):
        return [gs_bin, "-o", dest, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH",
                "-dQUIET", *page_range_flags(first, last), src]

    report = Progress(_FakeJob())
    results = run_chunked(report, build, out, 40, timeout=300, cpu_count=4)
    assert len(results) == 4, f"expected four chunks, ran {len(results)}"
    assert all(r.returncode == 0 for r in results)
    assert _labels(out) == [f"PAGE{i + 1:03d}" for i in range(40)], _labels(out)[:8]
    return "40 pages over 4 processes, back in order"


def test_the_unsplit_path_produces_the_same_document():
    """Below the threshold nothing is chunked, and that path must not have
    drifted from the chunked one — same pages, same order, same count."""
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    src = _numbered_pdf(6, "pool_short.pdf")
    out = os.path.join(_TMP, "pool_short_out.pdf")

    seen = []

    def build(dest, first, last):
        seen.append((first, last))
        return [gs_bin, "-o", dest, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH",
                "-dQUIET", *page_range_flags(first, last), src]

    results = run_chunked(Progress(_FakeJob()), build, out, 6, timeout=300)
    assert len(results) == 1 and seen == [(None, None)], seen
    assert _labels(out) == [f"PAGE{i + 1:03d}" for i in range(6)]
    return "one process, no page-range flags, identical output"


def test_a_failing_chunk_writes_no_output_at_all():
    """Half a converted document is worse than none: the page count would be
    short, and the tools' own 'Ghostscript produced no output' check is what
    should fire. So a failed chunk must leave the destination unwritten."""
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    src = _numbered_pdf(40, "pool_failing.pdf")
    out = os.path.join(_TMP, "pool_failing_out.pdf")
    if os.path.exists(out):
        os.remove(out)

    def build(dest, first, last):
        # The third chunk is handed a file that does not exist.
        bad = first is not None and first > 20
        return [gs_bin, "-o", dest, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH",
                "-dQUIET", *page_range_flags(first, last),
                (src + ".missing" if bad else src)]

    results = run_chunked(Progress(_FakeJob()), build, out, 40,
                          timeout=300, cpu_count=4)
    assert any(r.returncode != 0 for r in results), \
        "a missing input was reported as success"
    assert not os.path.exists(out), "a failed chunked run wrote a document anyway"
    return "nothing written when a chunk fails"


def test_damage_in_a_later_chunk_is_blamed_on_the_right_page():
    """The mapping bug, one layer further out than the greyscale one.

    test_greyscale_subset_verify_compares_the_right_pages covers a converted
    page being compared against the wrong original. This covers the same
    class arriving through chunking: page 30 is damaged inside chunk 3, where
    it sits at position 5 of that chunk's output. Reporting page 6 would be a
    plausible, wrong answer, and the operator would reprint the wrong sheet.
    """
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    from tools.panels._verify import _verify_pages_intact

    src = _numbered_pdf(40, "pool_damage.pdf")
    out = os.path.join(_TMP, "pool_damage_out.pdf")

    def build(dest, first, last):
        return [gs_bin, "-o", dest, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH",
                "-dQUIET", *page_range_flags(first, last), src]

    # Black out original page 30 (index 29) in whichever chunk carries it, the
    # way Ghostscript does it for real: exit 0, with the damage only visible in
    # the rendering.
    real_run_many = Progress.run_many

    def faked(self, cmds, *a, **k):
        results = real_run_many(self, cmds, *a, **k)
        for cmd in cmds:
            dest = cmd[cmd.index("-o") + 1]
            first = next((int(x.split("=")[1]) for x in cmd
                          if x.startswith("-dFirstPage=")), 1)
            last = next((int(x.split("=")[1]) for x in cmd
                         if x.startswith("-dLastPage=")), 40)
            if not first <= 30 <= last:
                continue
            with pikepdf.open(dest, allow_overwriting_input=True) as pdf:
                pdf.pages[30 - first].contents_add(
                    pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f"))
                pdf.save(dest)
        return results

    Progress.run_many = faked
    try:
        run_chunked(Progress(_FakeJob()), build, out, 40, timeout=300, cpu_count=4)
    finally:
        Progress.run_many = real_run_many

    assert len(plan_chunks(40, cpu_count=4)) == 4, "the fixture stopped chunking"
    damaged = _verify_pages_intact(src, out, range(40), null_progress())
    assert set(damaged) == {29}, \
        f"damage reported on the wrong page(s): {sorted(damaged)} (wanted page 30)"
    return "page 30 blamed for page 30, not for its position in its chunk"


# ── PDF/X document state ─────────────────────────────────────────────────────

def test_merging_keeps_what_makes_the_file_pdfx():
    """Appending pages copies pages and nothing else.

    Every chunk of a PDF/X run comes back carrying the output intent, and a
    merge built only from page objects drops all of them — leaving a file that
    claims nothing, embeds no printing condition, and is rejected at the RIP.
    This also holds the assumption the merge rests on: that the chunks agree,
    so taking the intent from the first is taking it from any of them.
    """
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    from tools.panels._icc import fallback_cmyk_icc
    from tools.panels.pdfx import _pdfx_defs

    icc = fallback_cmyk_icc()
    if not icc:
        return "SKIP (no cmyk icc profile)"
    src = _numbered_pdf(24, "pool_pdfx.pdf")
    defs = os.path.join(_TMP, "pool_pdfx_defs.ps")
    with open(defs, "w") as f:
        f.write(_pdfx_defs(icc, "FOGRA39", "Coated FOGRA39", "PDF/X-4"))

    def build(dest, first, last):
        return [gs_bin, "-dPDFX=4", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE",
                "-dQUIET", "-sDEVICE=pdfwrite", "-dPDFSETTINGS=/prepress",
                "-sProcessColorModel=DeviceCMYK", "-sColorConversionStrategy=CMYK",
                "-dEmbedAllFonts=true", "-dSubsetFonts=true",
                "-dCompatibilityLevel=1.6", f"-sOutputFile={dest}",
                *page_range_flags(first, last), defs, src]

    def state_of(path):
        with pikepdf.open(path) as pdf:
            intent = pdf.Root["/OutputIntents"][0]
            profile = intent["/DestOutputProfile"]
            return (str(pdf.docinfo.get("/GTS_PDFXVersion", "")),
                    str(intent.get("/OutputConditionIdentifier", "")),
                    int(profile.get("/N", 0)),
                    bytes(profile.read_bytes()))

    chunked = os.path.join(_TMP, "pool_pdfx_chunked.pdf")
    whole = os.path.join(_TMP, "pool_pdfx_whole.pdf")
    report = Progress(_FakeJob())
    assert len(plan_chunks(24, cpu_count=4)) > 1, "the fixture stopped chunking"
    run_chunked(report, build, chunked, 24, timeout=600, cpu_count=4,
                carry_document_state=True)
    run_chunked(report, build, whole, 1, timeout=600)

    assert _labels(chunked) == _labels(whole), "the chunked export lost page order"
    assert state_of(chunked) == state_of(whole), (
        "the merged PDF/X file does not carry what the unsplit one does:\n"
        f"  chunked: {state_of(chunked)[:3]}\n  whole:   {state_of(whole)[:3]}")
    return "output intent, ICC bytes and PDF/X marker all survive the merge"


def test_carrying_document_state_is_off_by_default():
    """X-4 keeps optional content, which lives in the catalog and not on a
    page — so it is the profile that must NOT be chunked. Nothing should carry
    catalog state unless it asked to, or a merge would look like it preserved
    layers it had in fact dropped."""
    gs_bin = ghostscript_binary()
    if not gs_bin:
        return "SKIP (no ghostscript)"
    src = _numbered_pdf(20, "pool_plain.pdf")
    parts = []
    for i, (first, last) in enumerate([(1, 10), (11, 20)]):
        part = os.path.join(_TMP, f"pool_plain_{i}.pdf")
        subprocess.run([gs_bin, "-o", part, "-sDEVICE=pdfwrite", "-dNOPAUSE",
                        "-dBATCH", "-dQUIET", *page_range_flags(first, last), src],
                       check=True, capture_output=True)
        parts.append(part)
    out = os.path.join(_TMP, "pool_plain_out.pdf")
    merge_chunks(parts, out)
    with pikepdf.open(out) as pdf:
        assert "/OutputIntents" not in pdf.Root
    assert _labels(out) == [f"PAGE{i + 1:03d}" for i in range(20)]
    return "a plain merge stays plain"


# ── the binary ───────────────────────────────────────────────────────────────

def test_every_tool_looks_for_ghostscript_the_same_way():
    """Four of the five callers looked for `gs` alone and then put the literal
    string "gs" in the command, so on Windows they reported Ghostscript
    missing on a machine where the greyscale tool right beside them found it —
    which does look for gswin64c. Structural, so a sixth caller cannot
    reintroduce it."""
    import pathlib
    tools = pathlib.Path(__file__).resolve().parent.parent / "tools"
    offenders = []
    for path in sorted(tools.rglob("*.py")):
        if path.name == "ghostscript.py":
            continue                      # the one that is allowed to look
        source = path.read_text()
        if 'which("gs")' in source or "which('gs')" in source:
            offenders.append(str(path.relative_to(tools)))
        if '"gs",' in source or "'gs'," in source:
            offenders.append(f"{path.relative_to(tools)} (literal argv[0])")
    assert not offenders, ("callers finding Ghostscript their own way: "
                           + ", ".join(offenders))
    return "one lookup, in tools/ghostscript.py"
