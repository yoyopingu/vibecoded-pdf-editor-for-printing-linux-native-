"""
Viewer Zoom.
"""
import os, time
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tests.support import _TMP, _app, _open_single_view, _settle, _spin


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
    import tools.page_viewer as _pv     # older build: the render was clamped
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
    import tools.page_viewer as pv
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
    from tools.page_viewer import PdfPageCanvas
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
    import tools.page_viewer as pv
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
    import tools.page_viewer as pv
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
        import tools.page_viewer as pv_module
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
    import tools.page_viewer as pv
    from tools.page_viewer import _FullPageCache
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
