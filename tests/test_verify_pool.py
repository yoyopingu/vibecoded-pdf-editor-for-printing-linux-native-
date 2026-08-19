"""
Verifying pages in worker processes.

This is the check that stops a blacked-out page reaching a press, so the bar
for spreading it over processes is not "faster" — it is "the same answer, or
an honest admission that there isn't one". Every test here is about the answer
rather than the speed.
"""
import os
import subprocess
import sys
import time

import pikepdf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from tools.jobs import Cancelled, Progress
from tools.pageverify import BLACKOUT_LIMIT, verify_range
from tools.panels import _verify
from tests.support import _TMP


class _FakeJob:
    """A Job as far as Progress is concerned. Mirrors test_cancel.py."""

    def __init__(self):
        self.cancelled = False
        self.messages = []

    def report(self, message):
        self.messages.append(message)


def _report():
    return Progress(_FakeJob())


def _pair(n_pages, damage=(), name="vp"):
    """(original, candidate) where the candidate has `damage` blacked out.

    The candidate is a copy of the original with a solid black rectangle
    painted over the named pages — which is exactly what Ghostscript does to a
    transparency group it cannot convert, while exiting 0.
    """
    src = os.path.join(_TMP, f"{name}_src.pdf")
    cand = os.path.join(_TMP, f"{name}_cand.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 60)
        c.drawString(70, 700, f"PAGE{i + 1:03d}")
        c.setFont("Helvetica", 20)
        c.drawString(70, 300, "content " * 6)
        c.showPage()
    c.save()
    with pikepdf.open(src) as pdf:
        for i in damage:
            pdf.pages[i].contents_add(pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f"))
        pdf.save(cand)
    return src, cand


# ── planning ─────────────────────────────────────────────────────────────────

def test_a_short_verification_stays_in_this_process():
    """A worker costs ~75 ms to start and has to parse both documents before
    its first render. Under the threshold that is more than the work."""
    for n in range(1, _verify.MIN_PAGES_TO_SPREAD):
        assert _verify._plan_workers(n, cpu_count=64) == 1, n
    assert _verify._plan_workers(_verify.MIN_PAGES_TO_SPREAD, cpu_count=64) > 1
    return f"nothing under {_verify.MIN_PAGES_TO_SPREAD} pages leaves the process"


def test_cheap_pages_do_not_start_workers_however_many_there_are():
    """The regression this probe was written for.

    Page count says nothing about cost. A thirty-page text export cleared the
    old page-count threshold, started two workers to save ninety milliseconds
    of rendering, and came out slower than before any of this existed. The
    decision is made from the measured first pages instead, so a document of
    cheap pages stays in this process no matter how many of them there are.
    """
    n = 60
    src, cand = _pair(n, damage=(17,), name="vp_cheap")
    assert _verify._plan_workers(n) > 1, "the fixture no longer clears the page floor"
    spread = []
    real = _verify._verify_in_workers
    _verify._verify_in_workers = lambda *a, **k: (spread.append(1), real(*a, **k))[1]
    try:
        bad = _verify._verify_pages_intact(src, cand, range(n), _report())
    finally:
        _verify._verify_in_workers = real
    assert not spread, "workers were started for pages that verify in microseconds"
    assert set(bad) == {17}, sorted(bad)
    return f"{n} cheap pages verified in-process, damage still caught"


def test_the_worker_count_is_capped():
    """Each worker holds two open documents and renders full pages; one per
    core on a big machine is memory, not speed."""
    assert _verify._plan_workers(10_000, cpu_count=64) == _verify.MAX_WORKERS
    assert _verify._plan_workers(10_000, cpu_count=2) == 2
    for n in (24, 40, 100):
        w = _verify._plan_workers(n, cpu_count=64)
        assert w <= n // _verify.MIN_PAGES_PER_WORKER, (n, w)
    return f"at most {_verify.MAX_WORKERS} workers"


def test_dealing_pages_loses_none_and_repeats_none():
    """Every page must be checked exactly once. A page silently dropped from
    the split is a page that ships unverified, which is the failure this whole
    module exists to prevent."""
    for n in (24, 25, 47, 145, 1000):
        for workers in (2, 3, 4):
            wanted = list(range(n))
            lots = _verify._deal(wanted, workers)
            flat = sorted(i for lot in lots for i in lot)
            assert flat == wanted, (n, workers)
            assert len(lots) == min(workers, n)
    # Sparse selections are the normal case for greyscale.
    sparse = [3, 9, 40, 41, 42, 99]
    assert sorted(i for lot in _verify._deal(sparse, 3) for i in lot) == sparse
    return "a partition, for dense and sparse selections alike"


# ── the answer ───────────────────────────────────────────────────────────────

def test_workers_and_this_process_give_the_same_verdict():
    """The only thing that matters. If these two ever disagree, the fast path
    is shipping a different opinion about whether a job is safe to print."""
    n = 32
    src, cand = _pair(n, damage=(0, 7, 31), name="vp_same")
    wanted = list(range(n))
    here = _verify._verify_here(src, cand, wanted, None, "")
    workers = _verify._plan_workers(n)
    assert workers > 1, "the fixture stopped reaching the worker path"
    pooled = _verify._verify_in_workers(src, cand, wanted, _report(), "", workers)
    assert pooled is not None, "the workers fell back — see the log"
    assert pooled == here, f"workers {sorted(pooled)} vs in-process {sorted(here)}"
    assert set(here) == {0, 7, 31}, sorted(here)
    return f"{workers} workers, identical verdicts, damage on {sorted(here)}"


def test_damage_is_blamed_on_the_right_page_in_a_sparse_selection():
    """Pages are dealt round-robin, so a page's position in its worker's lot
    has nothing to do with its page number. Every verdict travels with its own
    original index — reporting position 2 as page 3 would send the operator to
    reprint a sheet that was fine."""
    n = 40
    # Damage pages that land in different workers, at different positions.
    damaged = (5, 6, 7, 38)
    src, cand = _pair(n, damage=damaged, name="vp_sparse")
    # A sparse subset that still includes the damaged ones.
    wanted = [i for i in range(n) if i % 2 == 1 or i in damaged]
    pooled = _verify._verify_in_workers(src, cand, wanted, _report(), "",
                                        _verify._plan_workers(len(wanted)))
    assert pooled is not None, "the workers fell back"
    assert set(pooled) == set(damaged), \
        f"blamed {sorted(pooled)}, damage was on {sorted(damaged)}"
    return f"damage on {sorted(damaged)} reported as {sorted(pooled)}"


def test_a_broken_worker_falls_back_instead_of_passing_the_job():
    """A verification that cannot run must never look like a pass.

    The worker is made to fail; _verify_pages_intact has to notice, verify in
    this process instead, and still find the blacked-out page. Returning {}
    here — 'no damage found' — is the dangerous answer.
    """
    n = 32
    src, cand = _pair(n, damage=(11,), name="vp_broken")
    real = _verify._verify_in_workers
    real_thr = _verify.SPREAD_IF_PROJECTED_OVER
    calls = []
    _verify._verify_in_workers = lambda *a, **k: (calls.append(1), None)[1]
    # These fixture pages are cheap, so the probe would rightly decide to stay
    # in this process. Force the decision the other way to reach the fallback.
    _verify.SPREAD_IF_PROJECTED_OVER = 0.0
    try:
        bad = _verify._verify_pages_intact(src, cand, range(n), _report())
    finally:
        _verify._verify_in_workers = real
        _verify.SPREAD_IF_PROJECTED_OVER = real_thr
    assert calls, "the worker path was never attempted"
    assert set(bad) == {11}, f"fallback lost the damaged page: {sorted(bad)}"
    return "fell back and still caught page 12"


def test_a_worker_that_dies_is_a_fallback_not_a_pass():
    """The same, one level lower: the real worker command is made to fail, so
    the failure is detected by return code rather than by a stub."""
    n = 32
    src, cand = _pair(n, damage=(4,), name="vp_dies")
    real_exe, real_thr = sys.executable, _verify.SPREAD_IF_PROJECTED_OVER
    try:
        sys.executable = "/nonexistent/python-that-is-not-there"
        _verify.SPREAD_IF_PROJECTED_OVER = 0.0    # force the worker path
        out = _verify._verify_in_workers(src, cand, list(range(n)), _report(), "", 2)
        assert out is None, f"a dead worker returned a verdict: {out}"
        # And the public entry point still returns the right answer.
        bad = _verify._verify_pages_intact(src, cand, range(n), _report())
    finally:
        sys.executable = real_exe
        _verify.SPREAD_IF_PROJECTED_OVER = real_thr
    assert set(bad) == {4}, f"fallback lost the damaged page: {sorted(bad)}"
    return "dead worker -> in-process answer, page 5 still caught"


def test_stopping_is_not_mistaken_for_a_broken_worker():
    """Stop must travel, not be swallowed by the fallback.

    Cancelled is an Exception, so the broad clause that turns a failed worker
    into "verify in this process instead" caught it too — and a Stop pressed
    during verification silently restarted the whole verification here, taking
    longer than not pressing it. Asserted against a stub rather than a timer
    because the real workers finish a plain fixture in ~0.1 s; the reaping of
    live children is covered by run_many's own test, which is the code path
    that spawns these.
    """
    n = 32
    src, cand = _pair(n, damage=(3,), name="vp_cancel")
    report = _report()
    real = Progress.run_many
    real_thr = _verify.SPREAD_IF_PROJECTED_OVER
    Progress.run_many = lambda self, *a, **k: (_ for _ in ()).throw(Cancelled())
    _verify.SPREAD_IF_PROJECTED_OVER = 0.0    # force the worker path
    try:
        try:
            _verify._verify_in_workers(src, cand, list(range(n)), report, "", 2)
        except Cancelled:
            pass
        else:
            raise AssertionError("Stop was swallowed and reported as a fallback")
        # And through the public entry point: still Cancelled, not a slow
        # in-process verification that ignores the button.
        started = time.time()
        try:
            _verify._verify_pages_intact(src, cand, range(n), report)
        except Cancelled:
            pass
        else:
            raise AssertionError("Stop did not reach _verify_pages_intact")
        assert time.time() - started < 2, "it verified anyway before giving up"
    finally:
        Progress.run_many = real
        _verify.SPREAD_IF_PROJECTED_OVER = real_thr
    return "Cancelled propagates instead of falling back"


def test_no_verification_worker_outlives_the_run():
    """Whatever happened above, nothing is left rendering in the background."""
    leftover = subprocess.run(["pgrep", "-f", "tools.pageverify"],
                              capture_output=True, text=True).stdout.strip()
    assert not leftover, f"verification workers still running: {leftover}"
    return "no strays"


# ── the leaf ─────────────────────────────────────────────────────────────────

def test_the_worker_module_pulls_in_no_gui_toolkit():
    """Structural, and the whole reason tools/pageverify.py is separate from
    tools/panels/_verify.py.

    A worker imports this module once per process. tools/i18n.py imports
    PyQt6.QtCore, so a single convenience import of tr() in here would put a
    GUI toolkit in every worker — which is exactly the cost main.py's deferred
    import was written to avoid.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = ("import sys; import tools.pageverify; "
             "gui = [m for m in sys.modules if m.startswith('PyQt')]; "
             "print(','.join(gui))")
    env = dict(os.environ, PYTHONPATH=root)
    r = subprocess.run([sys.executable, "-c", probe],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr[-500:]
    assert not r.stdout.strip(), f"the worker module imported Qt: {r.stdout}"
    return "no PyQt in a worker"


def test_the_worker_runs_as_a_module_and_answers_in_json():
    """The parent parses stdout as JSON, so the contract is worth pinning:
    exit 0 and a {"bad": {index: reason}} object, keyed by original index."""
    import json
    n = 6
    src, cand = _pair(n, damage=(2,), name="vp_cli")
    job = os.path.join(_TMP, "vp_cli_job.json")
    with open(job, "w") as f:
        json.dump({"src": src, "cand": cand, "indices": [1, 2, 3]}, f)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-m", "tools.pageverify", job],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=root))
    assert r.returncode == 0, r.stderr[-500:]
    payload = json.loads(r.stdout)
    assert set(payload["bad"]) == {"2"}, payload
    assert "schwarz" in payload["bad"]["2"], payload
    return "exit 0, JSON keyed by original page index"


def test_verify_range_keys_by_original_index_not_by_position():
    """The unit underneath both paths. Handed [10, 20, 30] it must answer
    about pages 10, 20 and 30 — not 0, 1 and 2."""
    src, cand = _pair(32, damage=(20,), name="vp_keys")
    bad = verify_range(src, cand, [10, 20, 30])
    assert set(bad) == {20}, sorted(bad)
    assert BLACKOUT_LIMIT > 0
    return "keyed by page number, not by position in the list"
