"""
Viewer Zoom.
"""
import os, time
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tests.support import _TMP, _app, _open_single_view, _settle, _spin
from tools.render.queue import _render_queue
from tools.render.caches import _FullPageCache
import tools.render.region as REGION


def _fine_detail_pdf(name="fine_detail.pdf"):
    """A page of small text — interpolation is measurable on it."""
    p = os.path.join(_TMP, name)
    if not os.path.exists(p):
        c = canvas.Canvas(p, pagesize=A4)
        c.setFont("Helvetica", 9)
        for y in range(40, 800, 11):
            c.drawString(30, y, "The quick brown fox jumps over the lazy dog 0123456789 " * 2)
        c.showPage(); c.save()
    return p


def _target_scale_of(sv, zoom):
    pad = 16
    fit = min((sv._view.width() - pad) / sv._page_w_pt,
              (sv._view.height() - pad) / sv._page_h_pt)
    scale = getattr(sv, "_display_scale", None)
    if scale is not None:               # uncapped: the page may exceed one bitmap
        got = scale(zoom)
        if got: return got
    import tools.render.queue as _pv     # older build: the render was clamped
    cap = getattr(_pv, "MAX_RENDER_PX", 4000)
    return min(fit * zoom, cap / sv._page_w_pt, cap / sv._page_h_pt)


def _matches_pdfium(pm, path, page_index, scale):
    """Is this pixmap the actual pdfium render at `scale`, pixel for pixel?"""
    from PyQt6.QtGui import QImage as _QI
    doc = pdfium.PdfDocument(path)
    try:
        pg = doc[page_index]
        truth = pg.render(scale=scale).to_pil().convert("RGB")
        pg.close()
    finally:
        doc.close()
    img = pm.toImage().convertToFormat(_QI.Format.Format_RGB32)
    if (img.width(), img.height()) != truth.size:
        return False, f"{img.width()}x{img.height()} vs pdfium {truth.size[0]}x{truth.size[1]}"
    tb = truth.tobytes()
    total = img.width() * img.height()
    bits = img.bits(); bits.setsize(total * 4); buf = bytes(bits)
    acc = n = 0
    for i in range(0, total * 4, 4 * 997):        # sample, ~1 in 1000 pixels
        j = (i // 4) * 3
        acc += abs(buf[i+2] - tb[j]) + abs(buf[i+1] - tb[j+1]) + abs(buf[i] - tb[j+2])
        n += 1
    mean = acc / max(1, n)
    return mean < 0.5, f"mean channel difference {mean:.2f}"


def test_zoom_settles_on_a_real_render_not_an_interpolation():
    """The resting image must be an actual render at the zoom being shown.

    It used to be whatever was cached, Qt-scaled: any zoom within 1.45x of the
    cached scale was declared good enough and no real render was ever queued, so
    the user was left looking at interpolated pixels. 1.3x and 1.4x sit squarely
    in that old band."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        for zoom in (1.3, 1.4):
            sv._zoom = zoom
            sv._render()
            assert _settle(vp, lambda: sv._render_task is None
                           and not getattr(sv, "_showing_provisional", False),
                           tries=400), f"zoom {zoom} never settled"
            ok, detail = _matches_pdfium(sv._last_pm, src, 0, _target_scale_of(sv, zoom))
            assert ok, f"zoom {zoom} settled on an interpolated image — {detail}"
    finally:
        vp.deleteLater()


def test_zoom_shows_a_provisional_image_before_the_real_one():
    """Zooming *in* past what has been rendered has to show something at once,
    flagged as a stand-in, with the real render queued behind it. (Zooming out
    does not: shrinking a finer render is already as good as rendering.)"""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        before = sv._last_pm.width()
        sv._zoom = 1.3
        sv._render()                      # synchronous part only
        assert sv._last_pm.width() > before, "nothing was shown for the new zoom"
        assert getattr(sv, "_showing_provisional", False), \
            "the stand-in was not flagged provisional"
        assert sv._render_task is not None, "no exact render was queued"
        assert _settle(vp, lambda: not getattr(sv, "_showing_provisional", False),
                       tries=400)
        assert sv._render_task is None, "the task outlived the final render"
    finally:
        vp.deleteLater()


def test_a_newer_zoom_cancels_the_render_still_in_flight():
    """Requirement on the generation counter: a render overtaken by a newer
    request must be cancelled, and must not be able to paint over the newer one
    if it finishes anyway."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        sv._zoom = 2.0
        sv._render()
        stale_task = sv._render_task
        stale_gen  = sv._render_gen
        assert stale_task is not None

        sv._zoom = 3.0
        sv._render()
        assert not stale_task._active, "the overtaken render was not cancelled"
        assert sv._render_gen > stale_gen, "the generation counter did not move"
        assert sv._render_task is not stale_task

        assert _settle(vp, lambda: sv._render_task is None
                       and not getattr(sv, "_showing_provisional", False),
                       tries=500), "never settled"
        settled = sv._last_pm.width()

        # A late emission carrying the stale generation must be ignored.
        sv._on_page_ready(stale_gen, sv._last_pm.toImage().scaled(40, 40),
                          0, 0, 100.0, 100.0, 1.0, [], False)
        assert sv._last_pm.width() == settled, \
            "a stale render was allowed to overwrite the current one"

        ok, detail = _matches_pdfium(sv._last_pm, src, 0, _target_scale_of(sv, 3.0))
        assert ok, f"did not settle on the newest zoom — {detail}"
    finally:
        vp.deleteLater()


def _settled_deep(vp, sv):
    return _settle(vp, lambda: sv._render_task is None and sv._region_task is None
                   and not getattr(sv, "_showing_provisional", False), tries=800)


def test_deep_zoom_renders_the_window_not_the_whole_page():
    """MAX_RENDER_PX used to clamp the render, so past about 5x on A4 the page
    stopped getting sharper — the zoom control kept moving and the picture did
    not. It is a switch now: above it only the part of the page on screen is
    rendered, at the exact scale, so the page on screen keeps growing while the
    bitmap stays the size of the window."""
    import tools.render.queue as pv
    from tools.render.region import render_region
    assert isinstance(pv.MAX_RENDER_PX, int) and pv.MAX_RENDER_PX > 0
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        seen = []
        for zoom in (2.0, 12.0, 40.0):
            sv._zoom = zoom
            sv._render()
            assert _settled_deep(vp, sv), f"zoom {zoom} never settled"
            scale = _target_scale_of(sv, zoom)
            page_px = max(sv._page_w_pt * scale, sv._page_h_pt * scale)
            region  = sv._region_scale > 0
            bitmap  = sv._region_img if region else sv._last_pm
            seen.append((zoom, page_px, region, bitmap.width(), bitmap.height()))
            assert max(bitmap.width(), bitmap.height()) <= pv.MAX_RENDER_PX + 1, \
                f"zoom {zoom}: bitmap {bitmap.width()}x{bitmap.height()} is unbounded"

        # the page really does keep growing past the old ceiling
        assert seen[-1][1] > pv.MAX_RENDER_PX * 4, \
            f"page at 40x is only {seen[-1][1]:.0f}px — still clamped"
        assert not seen[0][2], "2x should still render the page in one piece"
        assert seen[1][2] and seen[2][2], "deep zoom should render a window"

        # and what is on screen is a real render at that exact scale
        scale = _target_scale_of(sv, 40.0)
        px0, py0, w, h = sv._region_rect
        fresh = render_region(src, 0, scale, px0, py0, w, h, 0)
        a = sv._region_img.toImage().convertToFormat(fresh.format())
        assert (a.width(), a.height()) == (fresh.width(), fresh.height())
        M = 8      # ignore the outermost pixels, which the window edge clips
        diff = 0
        for y in range(M, a.height() - M, 17):
            for x in range(M, a.width() - M, 17):
                pa, pf = a.pixel(x, y), fresh.pixel(x, y)
                diff = max(diff, abs((pa & 0xFF) - (pf & 0xFF)))
        assert diff <= 2, f"deep zoom is not a real render (max channel diff {diff})"
    finally:
        vp.deleteLater()


def test_zoom_gesture_does_not_build_page_sized_pixmaps():
    """A fast zoom must stay cheap in both time and memory.

    The stand-in used to be made by scaling the *whole page* to the new zoom and
    only then showing one screenful of it: a spin of the wheel from 6x to 35x
    built a 16428x23205 pixmap — 381 megapixels, three gigabytes — on the GUI
    thread. That was seconds of freeze, and the smeared over-zoomed frame that
    flashed up before the real render arrived. It crops first now."""
    from tools.viewer.canvas import PdfPageCanvas
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src, w=1100, h=780)
    biggest = [0, (0, 0)]
    orig = PdfPageCanvas.set_page
    def spy(self, pixmap, *a, **k):
        if pixmap is not None:
            n = pixmap.width() * pixmap.height()
            if n > biggest[0]: biggest[0] = n; biggest[1] = (pixmap.width(), pixmap.height())
        return orig(self, pixmap, *a, **k)
    PdfPageCanvas.set_page = spy
    try:
        for _ in range(16):
            sv._zoom_in()          # what a wheel click calls
            _spin(3, 0.0)
        assert sv._zoom > 20, f"the gesture only reached {sv._zoom:.1f}x"
        w, h = biggest[1]
        # a screenful, with room for the render margin — not a page
        assert biggest[0] <= 12_000_000, \
            f"a {w}x{h} = {biggest[0]/1e6:.0f} Mpx image was put on screen mid-gesture"
    finally:
        PdfPageCanvas.set_page = orig
        vp.deleteLater()


def test_zoom_gesture_renders_once_not_once_per_step():
    """Every wheel click used to queue its own render of the page. On a complex
    page that is seconds of work thrown away — the settle timer exists so the
    exact render happens once, when the gesture stops."""
    import tools.render.queue as pv
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src, w=1100, h=780)
    started = []
    originals = {}
    for name in ("_PageRenderTask", "_RegionRenderTask"):
        cls = getattr(pv, name)
        originals[name] = cls.run
        def mk(o):
            def run(self):
                # This document only: pre-renders left over from an earlier
                # test's viewer run on the same shared queue, and counting them
                # would make this measure the suite's timing, not the gesture.
                if getattr(self, "_path", None) == src:
                    started.append(1)
                return o(self)
            return run
        cls.run = mk(cls.run)
    try:
        # Other tests use this same document and share this queue; work they
        # left behind would be counted against this gesture.
        pv._render_queue.cancel_queued(0)
        _spin(20, 0.005)
        del started[:]
        for _ in range(12):
            sv._zoom_in()
            _spin(3, 0.0)          # faster than the settle timer
        during = len(started)
        assert during <= 2, f"{during} renders started during a 12-click gesture"
        # …and the exact render still arrives once the gesture stops
        assert _settle(vp, lambda: sv._render_task is not None
                       or sv._region_task is not None
                       or len(started) > during, tries=300), \
            "the gesture was never followed by a render"
        assert _settled_deep(vp, sv), "the view never settled"
        assert len(started) <= during + 2, \
            f"{len(started)} renders in total for one gesture"
    finally:
        for name, fn in originals.items():
            getattr(pv, name).run = fn
        vp.deleteLater()


def test_zooming_out_reuses_the_finer_render():
    """Shrinking a render made at a higher scale is supersampling — as sharp as
    rendering again. Re-rendering for it made zooming out on a complex page as
    slow as zooming in, for nothing visible."""
    import tools.render.queue as pv
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src, w=1100, h=780)
    started = []
    orig = pv._PageRenderTask.run
    def counting(self):
        started.append(1)
        return orig(self)
    pv._PageRenderTask.run = counting
    try:
        sv._zoom = 3.0                       # render fine once
        sv._render()
        assert _settled_deep(vp, sv)
        assert started, "the zoom-in did not render"
        started.clear()
        for zoom in (2.4, 1.9, 1.5, 1.2, 1.0):
            sv._zoom = zoom
            sv._render()
            assert _settled_deep(vp, sv), f"zoom {zoom} never settled"
            assert not sv._showing_provisional, \
                f"zoom {zoom} left a stand-in on screen"
        assert not started, f"zooming out re-rendered {len(started)} times"
    finally:
        pv._PageRenderTask.run = orig
        vp.deleteLater()


def test_deep_zoom_memory_does_not_grow_with_zoom():
    """The whole point: cost is set by the window, not the zoom. At 40x an A4
    page would be some 450 megapixels as one bitmap."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        sizes = []
        for zoom in (10.0, 20.0, 40.0):
            sv._zoom = zoom
            sv._render()
            assert _settled_deep(vp, sv), f"zoom {zoom} never settled"
            assert sv._region_scale > 0, f"zoom {zoom} did not use window rendering"
            sizes.append(sv._region_img.width() * sv._region_img.height())
        assert max(sizes) < 4_000_000, f"window grew to {max(sizes)} px"
        assert max(sizes) - min(sizes) < 200_000, \
            f"window size varies with zoom: {sizes}"
    finally:
        vp.deleteLater()


def test_panning_inside_the_margin_costs_no_render():
    """The window is rendered larger than the viewport so a small pan is a blit,
    not a re-render."""
    from tools.render.region import REGION_MARGIN_PX
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        sv._zoom = 20.0
        sv._render()
        assert _settled_deep(vp, sv)
        assert sv._region_scale > 0
        before_rect = sv._region_rect
        renders = []
        real = pv_module = None
        import tools.render.queue as pv_module
        orig = pv_module._RegionRenderTask.run
        def counting(self):
            renders.append(1)
            return orig(self)
        pv_module._RegionRenderTask.run = counting
        try:
            sv._scroll_y += REGION_MARGIN_PX // 2      # inside the margin
            sv._render()
            _spin(10, 0.01)
            assert not renders, "a pan inside the margin triggered a render"
            assert sv._region_rect == before_rect, "the window was rebuilt anyway"

            sv._scroll_y += REGION_MARGIN_PX * 4       # well outside it
            sv._render()
            assert _settled_deep(vp, sv)
            assert renders, "a pan past the margin did not render"
        finally:
            pv_module._RegionRenderTask.run = orig
    finally:
        vp.deleteLater()


def test_deep_zoom_window_always_covers_the_visible_sheet():
    """Any part of the sheet on screen that the window does not cover shows as
    background. The page is a fractional number of pixels wide, and truncating
    that left the window one pixel short at the far corner."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src, w=1100, h=780)
    try:
        gaps = []
        for zoom in (10.0, 25.0, 40.0):
            sv._zoom = zoom
            sv._render()
            assert _settled_deep(vp, sv)
            scale = sv._display_scale(zoom)
            pw, ph = sv._page_w_pt * scale, sv._page_h_pt * scale
            aw, ah = sv._view.width(), sv._view.height()
            for fx in (0.0, 0.5, 1.0):
                for fy in (0.0, 1.0):
                    sv._scroll_x = fx * max(0.0, pw - aw)
                    sv._scroll_y = fy * max(0.0, ph - ah)
                    sv._render()
                    assert _settled_deep(vp, sv)
                    px0, py0, rw, rh = sv._region_rect
                    ox, oy = sv._page_origin(pw, ph)
                    l, t = ox + px0, oy + py0
                    vl, vt = max(0.0, ox), max(0.0, oy)
                    vr, vb = min(float(aw), ox + pw), min(float(ah), oy + ph)
                    if not (l <= vl + 0.5 and t <= vt + 0.5
                            and l + rw >= vr - 0.5 and t + rh >= vb - 0.5):
                        gaps.append((zoom, fx, fy))
        assert not gaps, f"the window left part of the sheet uncovered at {gaps}"
    finally:
        vp.deleteLater()


def test_deep_zoom_is_correct_for_a_rotated_page():
    """Window rendering maps the visible rectangle back through the page
    manager's rotation; get that inverse wrong and the page comes out scrambled
    or shows the wrong corner."""
    from tools.render.region import render_region
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        tab = vp.tabs.currentWidget()
        tab._build_manage_once()
        tab.model.selected = {tab.model.order[0]}
        tab._manage_panel.grid.rotate_selected(90)
        sv._zoom = 12.0
        sv._render()
        assert _settled_deep(vp, sv)
        assert sv._region_scale > 0, "not in window mode"
        assert sv._page_w_pt > sv._page_h_pt, "rotation did not reach the view"

        scale = sv._region_scale
        px0, py0, w, h = sv._region_rect
        fresh = render_region(src, 0, scale, px0, py0, w, h, 90)
        a = sv._region_img.toImage().convertToFormat(fresh.format())
        diff = 0
        for y in range(8, a.height() - 8, 13):
            for x in range(8, a.width() - 8, 13):
                diff = max(diff, abs((a.pixel(x, y) & 0xFF) - (fresh.pixel(x, y) & 0xFF)))
        assert diff <= 2, f"rotated window render differs (max channel diff {diff})"
    finally:
        vp.deleteLater()


def test_a_slow_page_still_finishes_one_render():
    """A page that takes longer to render than the settle interval used to never
    finish a render at all.

    _render() showed a stand-in through _render_preview(), which armed the settle
    timer; the settle timer is _render(); 120 ms later it ran again, cancelled
    the render it had started and started another. Every complex page — which is
    exactly the case the window rendering exists for — sat on an interpolated
    stand-in for as long as it was looked at, while the render thread threw the
    same work away eight times a second."""
    import tools.render.queue as pv
    from tools.render.caches import _FullPageCache
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    started = []
    original = pv._PageRenderTask.run

    def slow_run(self):
        if getattr(self, "_path", None) == src and self._sig is not None:
            started.append(1)
            time.sleep(0.25)          # comfortably past the 120 ms settle
        return original(self)

    pv._PageRenderTask.run = slow_run
    try:
        # Earlier tests use this same document and leave work on the shared
        # queue; it would be counted as this view's.
        pv._render_queue.cancel_queued(0)
        _spin(20, 0.005)
        del started[:]
        _FullPageCache.invalidate()   # force the cache-miss path, stand-in and all
        assert sv._last_pm is not None, "the stand-in path needs a previous render"
        sv._render()
        assert _settle(vp, lambda: sv._render_task is None
                       and not sv._showing_provisional, tries=400), \
            f"never settled on a real render ({len(started)} renders started)"
        assert len(started) <= 2, \
            f"{len(started)} renders for one page: they are cancelling each other"
    finally:
        pv._PageRenderTask.run = original
        vp.deleteLater(); _app.processEvents()
    return f"{len(started)} render(s)"


def _distinct_pages_pdf(name="turn_pages.pdf"):
    """Five pages, each a solid colour, so which page is on screen is obvious."""
    from reportlab.lib import colors
    out = os.path.join(_TMP, name)
    if not os.path.exists(out):
        c = canvas.Canvas(out, pagesize=A4)
        for hexcol in ("#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff"):
            c.setFillColor(colors.HexColor(hexcol))
            c.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
            c.showPage()
        c.save()
    return out, ["#ff0000", "#00ff00", "#0000ff", "#ffff00", "#ff00ff"]


def _dominant(view):
    """The commonest colour on screen, or "blank"."""
    from collections import Counter
    pm = view._pixmap
    if pm is None:
        return "blank"
    img = pm.toImage()
    seen = Counter()
    for y in range(0, img.height(), max(1, img.height() // 30)):
        for x in range(0, img.width(), max(1, img.width() // 30)):
            seen[img.pixel(x, y) & 0xFFFFFF] += 1
    return f"#{seen.most_common(1)[0][0]:06x}"


def test_turning_a_page_never_shows_the_page_before_it():
    """While the new page renders, the view may show a thumbnail of it or
    nothing — never the page that was on screen a moment ago.

    _last_pm is the previous render, kept so a zoom can stretch it into a
    stand-in while the exact render runs. The cache-miss branch reached for it
    on a page turn as well, where it is not a stand-in for anything: it is a
    picture of a different page. The thumbnail branch below it — which fetches
    a thumbnail of the page actually being shown — was therefore unreachable
    after the first render of a session.

    On a simple document the render lands in milliseconds and nobody sees it.
    On a complex one it takes seconds, so every page turn showed the page
    before it, and scrolling through the file showed page 1 over and over."""
    src, want = _distinct_pages_pdf()
    vp, sv = _open_single_view(src, 1000, 760)
    try:
        # Deep enough to need window rendering, which is what a complex page
        # gets and what makes each render slow enough to be visible.
        sv._zoom = 8.0
        sv._render()
        _settle(vp, lambda: sv._region_task is None and sv._render_task is None,
                tries=300)

        stale = []
        for i in range(1, 5):
            # Hold the render back rather than race it: what is on screen
            # before it lands is the thing being tested, and how long that
            # lasts is exactly what varies between a simple document and a
            # complex one.
            held = []
            real_submit = _render_queue.submit
            _render_queue.submit = lambda task, pri=1: held.append(task)
            try:
                sv.next_page()
                _spin(3)
                during = _dominant(sv._view)
            finally:
                _render_queue.submit = real_submit
            if during not in ("blank", want[i]):
                stale.append((i + 1, during, want[i]))

            # Now let it render for real and check it arrives at the right page.
            for task in held:
                task.cancel()
            sv._render()
            _settle(vp, lambda: sv._region_task is None and sv._render_task is None,
                    tries=400)
            settled = _dominant(sv._view)
            assert settled == want[i], \
                f"page {i+1} settled showing {settled}, expected {want[i]}"
        assert not stale, (
            "showed another page while rendering: "
            + ", ".join(f"page {p} showed {got} (wanted {exp})" for p, got, exp in stale))
    finally:
        vp.deleteLater(); _app.processEvents()
    return "4 turns, none showed the page before"


def test_scrolling_up_into_an_unmeasured_page_does_not_strand_the_view():
    """Wheel-up at the top of a page goes to the bottom of the one before it.

    That was expressed by writing 999999 into _scroll_y and trusting every
    render path to clamp it. The paths that clamp against a height they know
    could not do it for a page nothing had measured yet, so the number stayed —
    and the wheel walked it down 137 pixels a click, thousands of clicks from
    a page 3,154 pixels tall. Scrolling *down* still turned pages, because the
    same bogus offset read as "past the bottom", so it looked as though only up
    was broken; the page button worked because it sets the offset to zero.

    Renders are held back here so the page really is unmeasured, which is the
    state the bug needed and which a complex document is in for seconds."""
    src, _ = _distinct_pages_pdf("strand_pages.pdf")
    vp, sv = _open_single_view(src, 1000, 760)
    try:
        sv._zoom = 6.0
        sv._render()
        _settle(vp, lambda: sv._region_task is None and sv._render_task is None,
                tries=300)

        # Forget every measurement, so the page we land on is one nothing knows
        # the size of.
        _FullPageCache.invalidate()
        REGION._page_sizes.clear()

        held = []
        real_submit = _render_queue.submit
        _render_queue.submit = lambda task, pri=1: held.append(task)
        try:
            sv._current = 4
            sv._scroll_y = 0.0
            sv.prev_page(start_at_bottom=True)
            _spin(3)
            stranded = sv._scroll_y
        finally:
            _render_queue.submit = real_submit
            for task in held:
                task.cancel()

        page_px_h = sv._page_px(sv._display_scale(sv._zoom) or 0.0)[1]
        assert stranded <= max(page_px_h, float(sv._view.height())), (
            f"scroll left at {stranded:,.0f} on a page that is at most "
            f"{page_px_h:,.0f} pixels tall")
        assert sv._want_bottom, \
            "the intent to show the bottom was dropped rather than deferred"

        # And once something does measure the page, that intent is honoured.
        sv._render()
        _settle(vp, lambda: sv._region_task is None and sv._render_task is None,
                tries=400)
        page_px_h = sv._page_px(sv._display_scale(sv._zoom))[1]
        want = max(0.0, page_px_h - sv._view.height())
        assert abs(sv._scroll_y - want) < 2, \
            f"did not land at the bottom: {sv._scroll_y:.0f}, expected {want:.0f}"
        assert not sv._want_bottom, "the intent was not cleared once honoured"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "no bogus offset, and the bottom is still reached"


def test_saving_over_the_open_file_does_not_leave_the_old_render_on_screen():
    """Ctrl+S is save_to(tab.pdf_path) — the page manager writes over the file
    it is showing. Every render cache key still matched afterwards while the
    pixels behind them described the file as it used to be, so the viewer went
    on showing the document from before the save.

    caches.py claimed the opposite in its own docstring — "keyed so that a page
    rewritten on disk cannot come back as its previous revision" — but neither
    key had a revision in it. The entries carry one now."""
    from reportlab.lib import colors
    import shutil

    def make(path, hexcol):
        c = canvas.Canvas(path, pagesize=A4)
        c.setFillColor(colors.HexColor(hexcol))
        c.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        c.showPage(); c.save()

    live = os.path.join(_TMP, "rev_live.pdf")
    other = os.path.join(_TMP, "rev_other.pdf")
    make(live, "#ff0000")
    make(other, "#0000ff")

    vp, sv = _open_single_view(live, 900, 700)
    try:
        assert _dominant(sv._view) == "#ff0000", "fixture did not open red"

        # Rewrite in place, as saving over the open document does. The mtime
        # has to actually differ, which on a coarse clock it otherwise may not.
        time.sleep(1.1)
        shutil.copyfile(other, live)

        sv._render()
        _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                tries=300)
        shown = _dominant(sv._view)
        assert shown == "#0000ff", \
            f"showing {shown} — the render from before the file was written"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "the rewritten file is what is shown"


def test_the_wheel_scrolls_past_pages_that_have_not_rendered():
    """Turning a page must never wait for a render.

    The wheel only turned the page `elif self._render_task is None` — on a
    simple document a render is in flight for a few milliseconds and nobody
    notices, on a complex one it is in flight nearly always. Scrolling towards
    a page further on therefore stopped dead at the first slow page in the way.
    Measured on eight heavy pages: seven turns in 0.3 s once the gate is gone,
    against one turn in 1.6 s with it.

    The render is cancelled and re-aimed by _render() as it always was; the
    page that has not arrived yet stands in as blank paper."""
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtCore import Qt as _Qt

    src, want = _distinct_pages_pdf("wheel_pages.pdf")
    vp, sv = _open_single_view(src, 900, 700)
    try:
        # Hold every render back, so a render is outstanding for the whole of
        # this — the state the gate used to refuse to turn a page in.
        held = []
        real_submit = _render_queue.submit
        _render_queue.submit = lambda task, pri=1: held.append(task)
        try:
            sv._render()          # puts a task in flight and leaves it there
            _spin(2)
            start = sv._current
            for _ in range(len(want) * 2):
                e = QWheelEvent(QPointF(400, 300), QPointF(400, 300),
                                QPoint(0, -120), QPoint(0, -120),
                                _Qt.MouseButton.NoButton,
                                _Qt.KeyboardModifier.NoModifier,
                                _Qt.ScrollPhase.NoScrollPhase, False)
                sv.wheelEvent(e)
                _spin(1)
                if sv._current == len(want) - 1:
                    break
            reached = sv._current
        finally:
            _render_queue.submit = real_submit
            for task in held:
                task.cancel()
        assert reached == len(want) - 1, (
            f"the wheel got from page {start+1} to page {reached+1} of "
            f"{len(want)} while a render was outstanding")
    finally:
        vp.deleteLater(); _app.processEvents()
    return f"reached page {len(want)} with a render in flight throughout"


def test_a_render_that_fails_is_retried_and_never_leaves_the_page_blank():
    """A render returning nothing meant two different things and was treated as
    one: cancelled because a newer render is coming, or failed.

    Abandoning a cancelled render is right — another is already on its way.
    Abandoning a failed one leaves the page blank for as long as it is on
    screen, because nothing else is going to ask for it. That is what "the
    biggest page sometimes will not render at all" looks like from outside, and
    the biggest page is the one whose bitmap is most likely to fail.

    Every stand-in is taken away first — no cache, no thumbnail, no previous
    render of this page — so what ends up on screen can only have come from
    the failure path itself."""
    import tools.render.queue as Q
    from tools.render.caches import _ThumbnailCache

    src, _ = _distinct_pages_pdf("retry_pages.pdf")
    vp, sv = _open_single_view(src, 900, 700)
    try:
        real = Q.render_window

        def strip_every_stand_in():
            _FullPageCache.invalidate()
            _ThumbnailCache.invalidate()
            sv._last_pm = None
            sv._last_pm_key = None
            sv._view.clear()

        # 1. Fails once, then renders. The page has to arrive.
        calls = [0]
        def fail_once(*a, **k):
            calls[0] += 1
            return None if calls[0] == 1 else real(*a, **k)
        strip_every_stand_in()
        Q.render_window = fail_once
        try:
            sv._render()
            _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                    tries=400)
            _spin(10)
            recovered = sv._view._pixmap is not None
        finally:
            Q.render_window = real
        assert recovered, "one failed render left the page blank"

        # 2. Never renders. Something has to be shown — the placeholder — and
        #    it must not be blank paper pretending to be the page.
        strip_every_stand_in()
        Q.render_window = lambda *a, **k: None
        try:
            sv._render()
            _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                    tries=400)
            _spin(10)
            shown = sv._view._pixmap
        finally:
            Q.render_window = real
        assert shown is not None, \
            "a page that cannot be rendered at all was left blank"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "recovers from one failure, and shows the placeholder after two"


def test_continuous_scroll_moves_without_rebuilding_scale():
    """Continuous mode must keep one display scale across the strip.

    The scale used to be recomputed from the current page's width on every
    paint. Scrolling onto a wider or narrower sheet rebuilt the whole strip
    under the pointer and jumped the scroll position — the hitch that made
    continuous scrolling feel broken on mixed-size documents.
    """
    src = os.path.join(_TMP, "cont_mixed.pdf")
    if not os.path.exists(src):
        from reportlab.lib.pagesizes import A4 as _A4
        c = canvas.Canvas(src, pagesize=_A4)
        for pw, ph in ((_A4[0], _A4[1]), (700.0, 500.0), (_A4[0], _A4[1]),
                       (400.0, 600.0), (_A4[0], _A4[1])):
            c.setPageSize((pw, ph))
            c.setFont("Helvetica", 36)
            c.drawString(40, ph / 2, f"{int(pw)}x{int(ph)}")
            c.showPage()
        c.save()

    vp, sv = _open_single_view(src, 900, 700)
    try:
        sv.set_continuous(True)
        sv._render()
        assert _settle(vp, lambda: bool(sv._strip) and len(sv._view._sheets) > 0,
                       tries=400), "strip never painted"
        scale0 = sv._display_scale(sv._zoom)
        assert scale0 and scale0 > 0

        # Scroll deep enough to change the current page several times.
        positions = []
        for _ in range(40):
            sv.scroll_by(180, animate=False)
            _spin(2)
            positions.append((sv._current, sv._doc_scroll,
                              sv._display_scale(sv._zoom)))
            if sv._doc_scroll >= sv._max_scroll() - 1:
                break

        scales = {round(s, 6) for _c, _y, s in positions if s}
        assert len(scales) == 1, \
            f"display scale changed while scrolling: {sorted(scales)}"
        assert any(c > 0 for c, _y, _s in positions), \
            "never left page 1 while scrolling the strip"
        # Scroll position must advance monotonically (no jumps backward from a
        # strip rebuild mid-gesture).
        ys = [y for _c, y, _s in positions]
        for a, b in zip(ys, ys[1:]):
            assert b + 0.5 >= a, \
                f"scroll jumped backward from {a:.1f} to {b:.1f}"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "one scale, forward-only scroll through mixed pages"


def test_continuous_wheel_eases_and_touchpad_does_not_lag():
    """A wheel notch eases; a pixel delta is applied at once.

    Easing a touchpad (which already sends smooth deltas) was the lag. A notch
    that jumped the full distance in one frame was the jolt. A detent mouse
    wheel is eased even when the platform sets a pixel delta alongside the
    angle — on some compositors that pixel delta's size and sign are not to be
    trusted, and following it made the wheel jump instead of scroll.
    """
    from PyQt6.QtCore import QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtCore import Qt as _Qt

    src = os.path.join(_TMP, "cont_wheel.pdf")
    if not os.path.exists(src):
        c = canvas.Canvas(src, pagesize=A4)
        for i in range(8):
            c.setFont("Helvetica", 48)
            c.drawString(80, 700, f"PAGE {i + 1}")
            c.showPage()
        c.save()

    vp, sv = _open_single_view(src, 900, 700)
    try:
        sv.set_continuous(True)
        sv._render()
        assert _settle(vp, lambda: bool(sv._strip), tries=300)

        def wheel(angle, pixel=0):
            sv.wheelEvent(QWheelEvent(
                QPointF(400, 300), QPointF(400, 300),
                QPoint(0, pixel), QPoint(0, angle),
                _Qt.MouseButton.NoButton,
                _Qt.KeyboardModifier.NoModifier,
                _Qt.ScrollPhase.NoScrollPhase, False))

        # Notch: goal moves, position eases toward it over frames.
        before = sv._doc_scroll
        wheel(-120)
        assert sv._scroll_goal > before, "wheel notch did not move the goal"
        mid = sv._doc_scroll
        assert mid < sv._scroll_goal or abs(mid - sv._scroll_goal) < 1, \
            "notch jumped straight to the goal instead of easing"
        _spin(30)
        assert abs(sv._doc_scroll - sv._scroll_goal) < 1.0, \
            "eased scroll never settled on the goal"

        # Wheel up must come back down the strip, detents only.
        down = sv._doc_scroll
        assert down > 0, "fixture did not scroll down first"
        for _ in range(20):
            wheel(120)
            _spin(2)
        _spin(30)
        assert sv._doc_scroll < 1.0, \
            f"wheel-up never returned to the top (at {sv._doc_scroll:.1f})"

        # A detent with a pixel delta alongside (some compositors) is eased
        # like a detent, not followed as pixels.
        sv._jump_scroll(0); _spin(2)
        wheel(-120, pixel=-120)
        assert sv._scroll_goal > 0, "detent with pixel delta did not scroll"
        _spin(30)

        # Touchpad: small deltas, no detent — applied immediately, no animator.
        sv._scroll_anim.stop()
        sv._doc_scroll = sv._scroll_goal = 0.0
        sv.scroll_by(48, animate=False)
        assert not sv._scroll_anim.isActive(), \
            "touchpad path left the animator running"
        assert abs(sv._doc_scroll - 48) < 0.5, \
            f"immediate scroll landed at {sv._doc_scroll}, not 48"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "notch eases (pixel delta or not), up returns, pixel scroll is immediate"


def test_continuous_mode_opens_painted_and_fit_to_the_page():
    """Two startup requirements of the continuous strip.

    With the setting already on, the preview used to open blank: strip page
    renders were submitted before the layout gave the view its final size, so
    the finished render landed in a different cache bucket than the view read,
    the in-flight key was never released, and nothing asked again or repainted
    until the user scrolled or zoomed.

    And "fit" is the whole page, not the width: the strip is measured against
    one reference page, so at zoom 1.0 that page — and every page the same
    shape — is entirely on screen, the same fit the paged view starts at.
    """
    from tools.viewer.panel import PageViewerPanel
    from tools.render.caches import _FullPageCache
    from tools.shell.settings import AppSettings

    src = os.path.join(_TMP, "cont_startup.pdf")
    if not os.path.exists(src):
        c = canvas.Canvas(src, pagesize=A4)
        for i in range(6):
            c.setFont("Helvetica", 48)
            c.drawString(80, 700, f"PAGE {i + 1}")
            c.showPage()
        c.save()

    AppSettings.get().set_continuous_scroll(True)
    _FullPageCache.invalidate()
    vp = PageViewerPanel(); vp.resize(1000, 760); vp.show()
    try:
        vp.open_file(src)
        sv = vp.tabs.currentWidget().single
        assert sv._continuous, "the saved preference did not reach the tab"
        # No scrolling, no zooming: the strip paints by itself.
        assert _settle(vp, lambda: len(sv._view._sheets) > 0
                       and all(s[0] is not None for s in sv._view._sheets),
                       tries=400), \
            "continuous preview stayed blank without user interaction"

        w_px, h_px = sv._strip[0][2], sv._strip[0][3]
        vw, vh = sv._view.width(), sv._view.height()
        assert w_px <= vw + 2 and h_px <= vh + 2, \
            f"page is {w_px}x{h_px} in a {vw}x{vh} view — not fitted whole"
    finally:
        AppSettings.get().set_continuous_scroll(False)
        vp.deleteLater(); _app.processEvents()
    return "paints by itself, and the page is fitted whole"


def test_continuous_fit_shows_no_sliver_of_the_next_page():
    """Ctrl+0 at a page boundary: the fitted page, and nothing of the next.

    The fit used to leave 16 px of slack under a height-fitted sheet while the
    gutter between sheets was 14 px, so 2 px of the next sheet poked into the
    bottom of the frame — reading as a hairline artefact rather than a page.
    The gutter is now the fit's bottom margin, so a fitted sheet plus its
    gutter is exactly the viewport; and the strip carries a gutter of
    breathing room above the first sheet and below the last, so neither opens
    glued to an edge.
    """
    src = os.path.join(_TMP, "cont_fit_sliver.pdf")
    if not os.path.exists(src):
        c = canvas.Canvas(src, pagesize=A4)
        for i in range(6):
            c.setFont("Helvetica", 48)
            c.drawString(80, 700, f"PAGE {i + 1}")
            c.showPage()
        c.save()

    # Portrait A4 in a taller-than-wide viewport: the fit is height-limited,
    # which is the case that left the sliver.
    vp, sv = _open_single_view(src, 900, 700)
    try:
        sv.set_continuous(True)
        sv._render()
        assert _settle(vp, lambda: bool(sv._strip), tries=300)

        # Half a gutter of breathing room at both ends of the strip — which,
        # at Ctrl+0 fit, is exactly what centring works out to.
        assert sv._strip[0][1] == sv.GAP_PX / 2.0, "no room above the first sheet"
        last = sv._strip[-1]
        assert sv._strip_h == last[1] + last[3] + sv.GAP_PX / 2.0, \
            "no room below the last sheet"

        # At the very top of the document, page 1 fitted: page 2 stays off
        # screen below it.
        assert sv._strip_top_of(1) >= sv._view.height() - 0.5, \
            "page 2 pokes into the frame below a fitted page 1"

        # Ctrl+0 from mid-scroll, then a boundary as a jump lands on one:
        # the sheet after the current one starts at or below the viewport.
        sv.scroll_by(500, animate=False); _spin(3)
        sv._zoom_fit(); _spin(10)
        sv._jump_scroll(sv._strip_top_of(sv._current)); _spin(3)
        avail_h = sv._view.height()
        assert sv._strip_top_of(sv._current) == sv._doc_scroll
        if sv._current + 1 < len(sv._strip):
            nxt = sv._strip_top_of(sv._current + 1) - sv._doc_scroll
            assert nxt >= avail_h - 0.5, \
                f"the next sheet starts {nxt - avail_h:.1f}px inside the frame"

        # And the fitted page itself is still whole.
        scale = sv._display_scale(sv._zoom)
        rw, rh = sv._cont_ref
        assert rw * scale <= sv._view.width() + 2, "Ctrl+0 overflows the width"
        assert rh * scale <= avail_h + 2, "Ctrl+0 overflows the height"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "a fitted page fills the frame; the next sheet stays off screen"


def test_continuous_ctrl0_and_ctrl1():
    """Ctrl+0 fits the reference page whole; Ctrl+1 is the physical size.

    Ctrl+1 used to compute its zoom from the paged fit formula, which in
    continuous mode describes a different fit than the strip is laid out with,
    so the shortcut zoomed to the wrong size. Both now go through
    _display_scale, the one arithmetic the strip actually uses.
    """
    from PyQt6.QtGui import QKeyEvent
    from PyQt6.QtCore import Qt as _Qt

    src = os.path.join(_TMP, "cont_zoom_keys.pdf")
    if not os.path.exists(src):
        c = canvas.Canvas(src, pagesize=A4)
        for i in range(4):
            c.setFont("Helvetica", 48)
            c.drawString(80, 700, f"PAGE {i + 1}")
            c.showPage()
        c.save()

    vp, sv = _open_single_view(src, 1000, 760)
    try:
        sv.set_continuous(True)
        sv._render()
        assert _settle(vp, lambda: bool(sv._strip), tries=300)

        def key(k):
            sv.keyPressEvent(QKeyEvent(
                QKeyEvent.Type.KeyPress, k,
                _Qt.KeyboardModifier.ControlModifier))

        sv.scroll_by(400, animate=False); _spin(3)
        key(_Qt.Key.Key_0); _spin(10)
        assert abs(sv._zoom - 1.0) < 1e-6, "Ctrl+0 did not reset the zoom"
        scale = sv._display_scale(sv._zoom)
        rw, rh = sv._cont_ref
        assert rw * scale <= sv._view.width() + 2, "Ctrl+0 did not fit the width"
        assert rh * scale <= sv._view.height() + 2, "Ctrl+0 did not fit the height"

        key(_Qt.Key.Key_1); _spin(10)
        got = sv._display_scale(sv._zoom)
        scr = _app.primaryScreen()
        want = scr.physicalDotsPerInchX() / 72.0
        assert abs(got - want) / want < 0.02, \
            f"Ctrl+1 landed at {got:.4f} px/pt, wanted {want:.4f}"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "Ctrl+0 fits the page whole, Ctrl+1 is the physical size"


def test_the_rail_drag_scrolls_smoothly_in_continuous_mode():
    """Dragging the rail thumb follows the pointer, page by fraction.

    The track answered a drag with page numbers, so the view jumped a page at
    a time and the thumb moved in chunks. In continuous mode it now reports
    the position along the strip, and the thumb mirrors the exact scroll
    position instead of snapping to the current page.
    """
    from PyQt6.QtCore import QPointF as _QPF, QEvent as _QE, Qt as _Qt
    from PyQt6.QtGui import QMouseEvent

    src = os.path.join(_TMP, "cont_rail.pdf")
    if not os.path.exists(src):
        c = canvas.Canvas(src, pagesize=A4)
        for i in range(10):
            c.setFont("Helvetica", 48)
            c.drawString(80, 700, f"PAGE {i + 1}")
            c.showPage()
        c.save()

    vp, sv = _open_single_view(src, 900, 700)
    try:
        sv.set_continuous(True)
        sv._render()
        assert _settle(vp, lambda: bool(sv._strip) and sv._strip_h > 0,
                       tries=300)
        assert sv._max_scroll() > 0, "fixture does not scroll"

        track = sv._track
        assert track._scroll_mode, "the track did not switch to scroll mode"

        def press(y):
            track.mousePressEvent(QMouseEvent(
                _QE.Type.MouseButtonPress, _QPF(6, y), _QPF(6, y),
                _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                _Qt.KeyboardModifier.NoModifier))

        def move(y):
            track.mouseMoveEvent(QMouseEvent(
                _QE.Type.MouseMove, _QPF(6, y), _QPF(6, y),
                _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton,
                _Qt.KeyboardModifier.NoModifier))

        h = track.height()
        seen = []
        for frac in (0.2, 0.3, 0.4, 0.5, 0.55):
            press(h * frac)
            move(h * frac)
            _spin(2)
            seen.append(sv._doc_scroll)
        assert all(b > a for a, b in zip(seen, seen[1:])), \
            f"the drag moved in chunks or backwards: {[round(s,1) for s in seen]}"
        # Distinct positions for distinct drag fractions — not page snaps.
        assert len({round(s, 1) for s in seen}) == len(seen), \
            "dragging produced page snaps, not a proportional scroll"
        # The thumb follows the scroll, not the page.
        expect = seen[-1] / sv._max_scroll()
        assert abs(track._scroll_frac - expect) < 0.01, \
            "the thumb does not mirror the scroll position"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "the drag is proportional and the thumb follows the scroll"
