"""
Render.
"""
import os, time, tempfile
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from tools.render.caches import _ThumbnailCache
from tests.support import FX, _TMP, _app, _open_single_view, _settle, _spin


_CACHE_FIXTURE_BUILT = {}


def _cache_fixture(tmp, name, n_pages, label):
    key = (tmp, name, n_pages, label)
    p = _CACHE_FIXTURE_BUILT.get(key)
    if p is not None and os.path.exists(p):
        return p
    p = os.path.join(tmp, name)
    c = canvas.Canvas(p, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 40); c.drawCentredString(300, 400, f"{label}{i}"); c.showPage()
    c.save()
    _CACHE_FIXTURE_BUILT[key] = p
    return p


def _count_parses():
    """Context-manager-ish spy returning (restore, parsed_paths)."""
    import pypdfium2 as _pdfium
    parsed = []
    real = _pdfium.PdfDocument
    class _Spy(real):
        def __init__(self, inp, *a, **k):
            if isinstance(inp, str): parsed.append(inp)
            super().__init__(inp, *a, **k)
    _pdfium.PdfDocument = _Spy
    def restore(): _pdfium.PdfDocument = real
    return restore, parsed


def test_background_work_uses_one_mechanism():
    """Structural guard. There were three ways to run work off the GUI thread:
    QThread subclasses, bare threading.Thread(daemon=True), and _RenderQueue.
    The bare threads were owned by nobody and could not be cancelled or waited
    for. Everything fire-and-forget now goes through tools/jobs.py; _RenderQueue
    keeps its own loop for reasons written at its definition."""
    import ast, pathlib
    # _RenderQueue's own worker is the documented exception, identified by the
    # class it lives in. This used to be a line-number range, which every edit
    # above it silently moved out from under.
    ALLOWED_IN = {"_RenderQueue"}
    # tools/jobs.py is the mechanism, not a way around it: Progress.run_many
    # fans a batch of subprocesses out over one thread each, and those threads
    # have every property this guard exists to require — owned (joined before
    # run_many returns), cancellable (each polls the job's flag between waits),
    # and waited for. A thread here is the one mechanism doing its job.
    ALLOWED_FILES = {"jobs.py"}
    offenders = []
    for f in sorted(pathlib.Path(".").glob("tools/**/*.py")):
        if f.name in ALLOWED_FILES:
            continue
        tree = ast.parse(f.read_text())
        enclosing = {}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for n in ast.walk(cls):
                enclosing.setdefault(n, cls.name)
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                bases = [getattr(b, "id", getattr(b, "attr", "")) for b in n.bases]
                if any(b == "QThread" for b in bases):
                    offenders.append(f"{f}:{n.lineno} class {n.name}(QThread)")
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Thread":
                if enclosing.get(n) not in ALLOWED_IN:
                    offenders.append(f"{f}:{n.lineno} threading.Thread(...)")
    assert not offenders, "background work outside tools/jobs.py:\n  " + "\n  ".join(offenders)


def test_a_job_outlives_the_reference_that_started_it():
    """No worker may depend on a local staying in scope. Dropping every caller
    reference to a running job must not free it — that is the crash class:
    a QThread destroyed inside run() aborts the process."""
    import gc, threading as _th
    from tools import jobs
    entered, release, finished = _th.Event(), _th.Event(), []
    def work(job):
        entered.set()
        release.wait(5)
        return "survived"
    jobs.submit(work, name="orphan", on_done=finished.append)   # return value dropped
    assert entered.wait(5), "the job never started"
    gc.collect(); gc.collect()          # nothing local refers to it now
    release.set()
    assert _settle(None, lambda: finished == ["survived"], tries=300), \
        f"the orphaned job did not deliver its result ({finished})"
    assert jobs.cancel_all(2000) >= 0


def test_jobs_are_cancelled_on_tab_close_and_on_shutdown():
    """Every worker needs a cancellation path, reachable from tab close and from
    app shutdown. A print job outliving its dialog used to emit into a widget
    that was being deleted."""
    import time as _t
    from tools import jobs
    import tools.render.queue as pv
    from tools.viewer.panel import PageViewerPanel

    def polling(sink):
        """Work that notices cancellation, like the real job bodies do."""
        def work(job):
            for _ in range(600):
                if job.cancelled:
                    break
                _t.sleep(0.005)
            sink.append(job.cancelled)
        return work

    jobs.cancel_all(2000)
    vp = PageViewerPanel(); vp.resize(600, 400); vp.show()
    vp.open_file(FX["normal"]); _spin(40, 0.01)
    tab = vp.tabs.currentWidget()

    seen = []
    jobs.submit(polling(seen), owner=tab, name="tab-work")
    _spin(10, 0.01)
    vp._close_tab(vp.tabs.indexOf(tab))          # closing the tab cancels it
    assert _settle(vp, lambda: seen, tries=400), "the job never stopped"
    assert seen == [True], f"tab close did not cancel its work ({seen})"

    # …and the shutdown path cancels whatever is left. The render queue's own
    # shutdown is stubbed out here: it is a one-way switch, and killing it would
    # leave every later test with no renderer.
    seen2 = []
    jobs.submit(polling(seen2), owner=None, name="stray")
    _spin(10, 0.01)
    real_stop = pv._render_queue.shutdown
    pv._render_queue.shutdown = lambda *a, **k: None
    try:
        pv.shutdown_render_queue(2.0)
    finally:
        pv._render_queue.shutdown = real_stop
    assert seen2 == [True], f"shutdown did not cancel outstanding work ({seen2})"
    assert not jobs.active_jobs(), "jobs still running after shutdown"
    vp.deleteLater()


def test_document_cache_reuses_and_reloads():
    """The point of the cache: parse a file once, not once per page. And a file
    that changed on disk must not come back from it — the page manager rewrites
    its temp PDFs constantly, sometimes within the same millisecond."""
    from tools.render import document_cache as dc
    tmp = tempfile.mkdtemp(dir=_TMP)
    dc.close_all()
    a = _cache_fixture(tmp, "a.pdf", 2, "A")

    restore, parsed = _count_parses()
    try:
        for _ in range(6):
            with dc.page_document(a) as doc:
                assert len(doc) == 2
        assert parsed.count(a) == 1, f"parsed {parsed.count(a)}x for 6 opens"

        time.sleep(0.01)
        _cache_fixture(tmp, "a.pdf", 3, "B")      # same path, new revision
        with dc.page_document(a) as doc:
            assert len(doc) == 3, "an edited file came back from the cache"
        assert parsed.count(a) == 2, "the new revision was not parsed"
        assert dc.stats()["open"] == 1, "the superseded revision was left open"

        for i in range(dc.MAX_DOCUMENTS + 4):     # push past the cap
            with dc.page_document(_cache_fixture(tmp, f"x{i}.pdf", 1, "X")):
                pass
        assert dc.stats()["open"] <= dc.MAX_DOCUMENTS, dc.stats()
    finally:
        restore()
        dc.close_all()
    assert dc.stats()["open"] == 0, "close_all left documents open"


def test_document_cache_never_closes_a_document_in_use():
    """Eviction drops a handle from the registry at once but must leave the
    close to its last user. Closing a document mid-render is a use-after-free in
    pdfium, i.e. a crash with no Python traceback to find it by."""
    from tools.render import document_cache as dc
    tmp = tempfile.mkdtemp(dir=_TMP)
    dc.close_all()
    keep = _cache_fixture(tmp, "keep.pdf", 1, "K")

    handle = dc._checkout(keep)          # stand in for being inside page_document
    try:
        for i in range(dc.MAX_DOCUMENTS + 3):
            with dc.page_document(_cache_fixture(tmp, f"e{i}.pdf", 1, "E")):
                pass
        assert handle.retired, "the handle should have been evicted by now"
        assert handle.users == 1, f"users={handle.users}"
        with handle.lock:                # still usable while checked out
            page = handle.doc[0]
            try:
                assert page.get_width() > 0
            finally:
                page.close()
    finally:
        dc._checkin(handle)

    try:
        handle.doc[0]
        still_open = True
    except Exception:
        still_open = False
    assert not still_open, "an evicted document was left open after its last user"
    dc.close_all()


def test_a_narrower_thumbnail_is_shrunk_not_re_rendered():
    """Rendering a thumbnail costs what walking the page's drawing costs, near
    enough whatever size comes out — a 160x113 thumbnail of a page carrying
    580,000 stroked segments measured 1221 ms against 1307 ms for the whole
    sheet. Thumbnail widths are derived from the window's, so dragging it used
    to re-render every card at every width it passed through."""
    # The module _ThumbTask resolves _render_image in, which is where the spy
    # has to go — patching a re-export elsewhere would rebind a name nothing
    # looks up.
    import tools.render.queue as pv
    from tools.render.queue import _ThumbTask, _thumb_render_width
    from tools.render.caches import _ThumbnailCache
    _ThumbnailCache.invalidate()
    rendered = []
    original = pv._render_image

    def spy(*a, **k):
        rendered.append(a[2])          # the width asked of pdfium
        return original(*a, **k)

    pv._render_image = spy
    try:
        wide = _thumb_render_width(600)
        _ThumbTask(0, 0, FX["normal"], 0, 0, wide, None).run()
        assert rendered == [wide], rendered

        # Anything at or below that width comes from the wide one.
        for want in (wide, wide - 1, wide // 2, 64):
            _ThumbTask(0, 0, FX["normal"], 0, 0, want, None).run()
            img = _ThumbnailCache.get((FX["normal"], 0, 0, want))
            assert img is not None and img.width() == want, \
                f"width {want}: got {img and img.width()}"
        assert rendered == [wide], \
            f"a narrower thumbnail was rendered again, not shrunk: {rendered}"

        # A wider one is a real render: stretching the cached image would show.
        _ThumbTask(0, 0, FX["normal"], 0, 0, wide * 2, None).run()
        assert rendered == [wide, wide * 2], \
            f"a wider thumbnail was stretched from a smaller render: {rendered}"

        # And the widths asked for land on a ladder, or none of the above ever
        # gets a hit: the window changes them a pixel at a time.
        assert len({_thumb_render_width(w) for w in range(300, 380)}) == 1, \
            "thumbnail widths are not quantised"
        assert _thumb_render_width(300) >= 300, "a thumbnail would be stretched"
    finally:
        pv._render_image = original
        _ThumbnailCache.invalidate()


def _count_page_loads():
    """Spy on FPDF_LoadPage. Returns (restore, loaded) where `loaded` collects
    the page indexes asked for."""
    import pypdfium2 as _pdfium
    loaded = []
    real = _pdfium.PdfDocument.__getitem__

    def spy(self, i):
        loaded.append(i)
        return real(self, i)

    _pdfium.PdfDocument.__getitem__ = spy

    def restore():
        _pdfium.PdfDocument.__getitem__ = real
    return restore, loaded


def _heavy_page_pdf():
    """One page with enough vector work on it that rendering takes long enough
    to be interrupted — a few thousand strokes, which is an ordinary map or
    layout plan and nothing exotic."""
    p = os.path.join(_TMP, "heavy_render.pdf")
    if not os.path.exists(p):
        c = canvas.Canvas(p, pagesize=A4)
        c.setLineWidth(0.2)
        for i in range(6000):
            c.setStrokeColorRGB((i % 7) / 7, (i % 5) / 5, (i % 3) / 3)
            c.line((i * 7) % 595, (i * 13) % 842, (i * 29) % 595, (i * 37) % 842)
        c.showPage(); c.save()
    return p


def test_a_page_is_loaded_once_not_once_per_render():
    """Caching the document only moved the cost down a level: every render still
    called FPDF_LoadPage and threw the parse away afterwards. On a poster-sized
    page that was 351 ms — 1444 ms for the worst page of the file measured —
    paid again on every pan, every zoom step and every page turn back."""
    from tools.render import document_cache as dc
    from tools.render.region import render_region
    dc.close_all()
    restore, loaded = _count_page_loads()
    try:
        for i in range(5):
            img = render_region(FX["framed"], 0, 2.0, i * 20, 0, 200, 150)
            assert img is not None and img.width() == 200
        assert loaded.count(0) == 1, \
            f"page 0 was loaded {loaded.count(0)}x for 5 window renders"

        # …and the cache is bounded, or a long document would hold every page
        # it ever showed. A loaded page is not small.
        del loaded[:]
        for i in range(dc.MAX_PAGES + 3):
            render_region(_cache_fixture(_TMP, "many.pdf", dc.MAX_PAGES + 3, "M"),
                          i, 1.0, 0, 0, 60, 60)
        assert dc.stats()["pages"] <= dc.MAX_PAGES, dc.stats()
    finally:
        restore()
        dc.close_all()
    assert dc.stats()["pages"] == 0, "close_all left pages loaded"


def test_a_render_in_flight_can_be_abandoned():
    """A render used to be one uninterruptible call into pdfium: 130-550 ms on a
    complex page, during which the render thread held the process-wide pdfium
    lock. Turning the page meant waiting for a pre-render nobody wanted to
    finish first. pdfium's progressive API renders in slices instead, and the
    cancel is checked between them."""
    from tools.render.raster import render_window
    from tools.render.region import page_px_size, page_size_pt
    src = _heavy_page_pdf()
    w_pt, h_pt = page_size_pt(src, 0)
    px = page_px_size(w_pt, h_pt, 2.0)

    checks = [0]
    t0 = time.perf_counter()
    whole = render_window(src, 0, *px, should_cancel=lambda: checks.__setitem__(0, checks[0] + 1))
    full_ms = (time.perf_counter() - t0) * 1000
    assert whole is not None and (whole.width(), whole.height()) == px
    assert checks[0] > 3, \
        f"the render was one uninterruptible call (cancel asked {checks[0]}x)"

    stop = [0]
    def cancel_second_time():
        stop[0] += 1
        return stop[0] >= 2

    t0 = time.perf_counter()
    out = render_window(src, 0, *px, should_cancel=cancel_second_time)
    stop_ms = (time.perf_counter() - t0) * 1000
    assert out is None, "a cancelled render still produced an image"
    assert stop_ms < full_ms / 2, \
        f"cancelling took {stop_ms:.0f} ms of the {full_ms:.0f} ms render"
    return f"{full_ms:.0f} ms render, cancelled in {stop_ms:.0f} ms"


def test_closing_a_tab_releases_the_parsed_document():
    """A loaded page of a big PDF is hundreds of megabytes. Keeping the document
    and its pages parsed for a tab the user has closed is the largest single
    thing this app can hold on to for no reason."""
    from tools.viewer.panel import PageViewerPanel
    from tools.render import document_cache as dc
    dc.close_all()
    vp = PageViewerPanel(); vp.resize(900, 700); vp.show()
    try:
        vp.open_file(FX["normal"])
        _settle(vp, lambda: vp.tabs.count() and dc.stats()["open"], tries=200)
        assert dc.stats()["open"] >= 1, "the file was never parsed"
        vp._close_tab(vp.tabs.indexOf(vp.tabs.currentWidget()))
        _app.processEvents()
        paths = dc.stats()["paths"]
        assert not any(os.path.samefile(p, FX["normal"])
                       for p in paths if os.path.exists(p)), \
            f"the closed document is still cached: {paths}"
    finally:
        vp.deleteLater(); _app.processEvents()
        dc.close_all()


def test_the_prerender_window_follows_the_reader():
    """Pre-rendering ran once, 400 ms after the file opened, over a window around
    page 1 — so it warmed pages the user had already seen and never the ones
    ahead of them. Ten pages in, every turn was a cold render again."""
    from tools.render.caches import _FullPageCache
    vp, sv = _open_single_view(FX["booklet32"])
    try:
        _settle(vp, lambda: sv._prerender_tasks, tries=200)
        sv.go_to(20)
        assert _settle(vp, lambda: any(
            _FullPageCache.get(sv.pdf_path, i, 0,
                               sv._view.width(), sv._view.height()) is not None
            for i in (21, 22)), tries=400), \
            "nothing ahead of page 20 was pre-rendered"
    finally:
        vp.deleteLater(); _app.processEvents()


def test_render_paths_go_through_the_document_cache():
    """_render_image used to open and close the whole PDF for every single page.
    Five pages of one file should now cost one parse."""
    from tools.render.images import _render_image
    from tools.render import document_cache as dc
    dc.close_all()
    restore, parsed = _count_parses()
    try:
        for i in range(5):
            img = _render_image(FX["normal"], i, 90)
            assert not img.isNull() and img.width() == 90
            # not the "could not render" fill
            assert (img.pixel(img.width() // 2, img.height() // 2) & 0xFFFFFF) != 0x2A3A5A
        n = parsed.count(FX["normal"])
        assert n == 1, f"the file was parsed {n}x for 5 page renders"
    finally:
        restore()
        dc.close_all()


def test_the_page_cache_is_bounded_by_memory_not_by_a_page_count():
    """How many pages stay rendered depends on how big they are.

    Both caches counted entries. The full-page one stopped at twelve from 40 %
    of RAM upwards — so the slider did nothing above that — and each entry was
    assumed to be 70 MB when a page rendered to a normal window is nearer 2.
    A count of entries says nothing about the memory in use, which is what the
    setting is choosing.
    """
    from PyQt6.QtGui import QImage
    from tools.render.caches import _FullPageCache

    before = _FullPageCache.MAX_BYTES
    try:
        _FullPageCache.invalidate()
        # Ten identical renders, budget for about four of them.
        img = QImage(500, 500, QImage.Format.Format_RGB32)
        one = int(img.sizeInBytes())
        _FullPageCache.MAX_BYTES = one * 4 + one // 2
        for i in range(10):
            _FullPageCache.put(f"/nonexistent/doc.pdf", i, 0, 800, 600,
                               (img, 595.0, 842.0, 1.0, []), force=True)
        held = len(_FullPageCache._store)
        assert _FullPageCache._bytes <= _FullPageCache.MAX_BYTES, \
            f"{_FullPageCache._bytes} bytes held against a budget of " \
            f"{_FullPageCache.MAX_BYTES}"
        assert 3 <= held <= 5, f"expected about four pages to fit, held {held}"

        # Twice the budget, twice the pages — the thing the old count could not
        # express once it hit its ceiling.
        _FullPageCache.MAX_BYTES = one * 8 + one // 2
        for i in range(10, 20):
            _FullPageCache.put("/nonexistent/doc.pdf", i, 0, 800, 600,
                               (img, 595.0, 842.0, 1.0, []), force=True)
        assert 7 <= len(_FullPageCache._store) <= 9, \
            f"doubling the budget held {len(_FullPageCache._store)}, not ~8"

        # And a bigger page means fewer of them fit, without anyone saying so.
        small_capacity = _FullPageCache.capacity()
        _FullPageCache.invalidate()
        big = QImage(1500, 1500, QImage.Format.Format_RGB32)
        _FullPageCache.put("/nonexistent/big.pdf", 0, 0, 800, 600,
                           (big, 595.0, 842.0, 1.0, []), force=True)
        assert _FullPageCache.capacity() < small_capacity, (
            f"a page {int(big.sizeInBytes()) // one}x the size still reports "
            f"room for {_FullPageCache.capacity()} of them")
    finally:
        _FullPageCache.MAX_BYTES = before
        _FullPageCache.invalidate()
    return "budget honoured, capacity follows page size"


def test_switching_tabs_does_not_throw_away_the_other_tab_s_renders():
    """Going back to a file you had open should not start rendering from
    scratch.

    Every tab change evicted the full-page renders of every other tab, keeping
    only the page each was showing, on the reasoning that it "can re-render
    quickly when the user switches back". That is true of a page of text and
    false of the ones this application exists for — seconds each, and a whole
    document of them.

    It was written when the cache held six entries and evicting another tab was
    the only way to make room. The cache is bounded by memory now, and
    _priority_evict already drops other tabs first when room is actually
    needed."""
    from tools.viewer.panel import PageViewerPanel
    from tools.render.caches import _FullPageCache
    from tests.support import _settle, _spin

    _FullPageCache.invalidate()
    vp = PageViewerPanel(); vp.resize(900, 700); vp.show()
    try:
        vp.open_file(FX["normal"])
        _settle(vp, lambda: vp.tabs.count() and vp.tabs.currentWidget().single._last_pm,
                tries=300)
        sv = vp.tabs.currentWidget().single
        for i in range(4):
            sv.go_to(i + 1)
            _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                    tries=200)
        first = {k[1] for k in _FullPageCache._store if k[0] == FX["normal"]}
        assert len(first) >= 3, f"the fixture only cached {sorted(first)}"

        vp.open_file(FX["color"])
        _spin(40)
        for idx in (0, 1, 0):                 # back and forth, as a reader does
            vp.tabs.setCurrentIndex(idx)
            _spin(20)

        kept = {k[1] for k in _FullPageCache._store if k[0] == FX["normal"]}
        assert kept >= first, (
            f"switching tabs discarded pages {sorted(first - kept)} of the file "
            f"that was left — it will render them all again")
    finally:
        vp.deleteLater(); _app.processEvents()
    return f"{len(first)} rendered pages survived three tab switches"


def _filled_form_pdf(name="filled_form.pdf"):
    """A form with a value in it and NeedAppearances set — how nearly every
    tool that fills a form leaves it, saying "viewer, draw this"."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from pypdf import PdfWriter
    blank = os.path.join(_TMP, "blank_" + name)
    c = canvas.Canvas(blank, pagesize=A4)
    c.setFont("Helvetica", 12); c.drawString(60, 780, "Auftrag")
    c.acroForm.textfield(name="kunde", x=60, y=700, width=300, height=24,
                         borderWidth=1, fontSize=12)
    c.showPage(); c.save()

    out = os.path.join(_TMP, name)
    w = PdfWriter(clone_from=blank)
    w.update_page_form_field_values(w.pages[0], {"kunde": "Firma Muster GmbH"},
                                    auto_regenerate=True)
    with open(out, "wb") as f:
        w.write(f)
    return out


def _ink_width(pil):
    from PIL import ImageOps
    box = ImageOps.invert(pil.convert("L")).getbbox()
    return 0 if box is None else box[2] - box[0]


def test_a_filled_in_form_is_not_rendered_empty():
    """A field's typed value is painted from its appearance stream, and pdfium
    paints those only once a form environment exists on the document. Without
    one the page comes back with the field and its contents missing — so a
    delivery note the customer filled in showed blank here while Acrobat
    showed it filled.

    Checked through the document cache, which is what the viewer and the
    thumbnails render from.
    """
    from tools.render import document_cache as dc
    src = _filled_form_pdf("cache_form.pdf")

    try:
        with dc.page_document(src) as doc:
            page = doc[0]
            try:
                pil = page.render(scale=2,
                                  fill_color=(255, 255, 255, 255)).to_pil()
            finally:
                page.close()
    finally:
        # The registry is shared, and a document left in it changes what the
        # eviction tests further down see.
        dc.close_all()
    drawn = _ink_width(pil)

    # The label alone is about 80px at this scale; the field box and the text
    # in it reach several times that.
    assert drawn > 300, \
        f"only {drawn}px of ink — the filled field was not drawn"
    return f"the field and its value are drawn ({drawn}px of ink)"


def test_every_render_path_opens_documents_the_same_way():
    """The form environment has to be set up before any page handle is taken,
    which is not something to remember at nineteen separate call sites — get
    it wrong at one and that one renders filled forms as empty ones, which is
    how this started. Structural, so a twentieth cannot reintroduce it."""
    import ast, pathlib

    offenders = []
    exempt = 0
    for path in sorted(pathlib.Path("tools").rglob("*.py")):
        if path.name == "document_cache.py":
            continue        # the one place allowed to call the constructor
        source = path.read_text()
        lines = source.split("\n")
        tree = ast.parse(source, str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (isinstance(fn, ast.Attribute) and fn.attr == "PdfDocument"
                    and isinstance(fn.value, ast.Name)
                    and "pdfium" in fn.value.id):
                # There is one honest reason to open without forms, and it has
                # to say so on the line: forms.py measures the page as a
                # printer draws it, to check that flattening really baked the
                # values in. Marking it keeps that deliberate and rare rather
                # than making the rule advisory.
                if "no-forms:" in lines[node.lineno - 1]:
                    exempt += 1
                    continue
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "documents opened without the form environment:\n  "
        + "\n  ".join(offenders))
    assert exempt <= 1, (
        f"{exempt} places now opt out of form support; each one renders a "
        "filled form as empty and needs to be worth it")
    return f"every render path goes through open_document() ({exempt} marked exception)"
