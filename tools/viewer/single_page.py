"""
The one-page view — what the app shows by default.

Turning pages, and the zoom. The zoom is where most of this module is: past
MAX_RENDER_PX a page is no longer rendered as one bitmap but a window at a time
at the exact scale, so the view has to track which window it is holding, whether
that window still covers the viewport after a scroll, and what to put on screen
in the meantime. A gesture shows a cheap stand-in per step and renders once,
exactly, when it stops.

A few `except Exception` blocks here fall back to a harmless default (100%
zoom, no ruler guides for a page it cannot identify, the zoom-percentage
label read from `self._zoom` instead of computed from the screen) rather than
surface anything — the fallback is the correct behaviour, not a bug being
hidden. They are logged at debug level anyway, the same convention
tools/render/ uses, so a real recurring failure still shows up for anyone who
goes looking.
"""
import logging
import math
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QPushButton, QLabel, QFrame, QApplication,
                             QSizePolicy)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRect, QPoint, QSize
from PyQt6.QtGui import QPixmap, QCursor
from tools.app_state import theme_color
from tools.i18n import tr
from tools.shell.icons import icon, rotated
from tools.render.caches import _FullPageCache, _ThumbnailCache
from tools.render.images import MAX_RENDER_PX, _SCALE_EPS, _good_enough
from tools.render.queue import _PageRenderTask, _PageSignals, _RegionRenderTask, _RegionSignals, prerender_enabled, _render_queue, _target_scale
from tools.render.region import cached_page_size_pt, covers, page_px_size, region_for_viewport, snap_scale
from tools.viewer.canvas import PdfPageCanvas
from tools.viewer.rulers import RulerBar, RulerCorner
from tools.viewer.tab_base import owning_tab
from tools.viewer.rail import _NavRailColumn, _PageField, _PageTrack
import tools.viewer.strip as strip
from tools.viewer.color_label import ColourSpaceLabel
from tools.app_state import AppState
from tools.colorspace import count_grey_pages
from tools.theme import _TV, _register_themed


# How far the user may zoom in. Was 8x, which existed because the page was
# rendered as one bitmap and MAX_RENDER_PX clamped the result — past roughly
# 5x on A4 the picture stopped getting sharper, so more zoom would have been a
# lie. Window rendering costs the same at any zoom, so the limit is now just a
# question of what is useful.
MAX_ZOOM = 40.0
MIN_ZOOM = 0.1


class SinglePageView(QWidget):
    page_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pdf_path   = None
        self.model      = None
        self._current   = 0
        self._zoom      = 1.0   # 1.0 = Fit-to-window
        self._last_pm   = None
        self._last_zoom = 1.0
        # Which page _last_pm holds. It is a stand-in for zooming, and a
        # stand-in is only honest about the page it was rendered from — see
        # _stand_in_is_current.
        self._last_pm_key = None
        self._page_w_pt = 0.0   # page dimensions in PDF points (stored on render)
        self._page_h_pt = 0.0
        self._scroll_x  = 0.0  # Scroll-Offset (float für präzise Berechnung)
        self._scroll_y  = 0.0
        # "put me at the bottom of this page once its height is known" — see
        # _place_scroll. Never expressed as a coordinate.
        self._want_bottom = False
        # "…and bring this spot on the page into view once you know how big it
        # is" — a search hit on a page zoomed in past the window. Held in
        # points, resolved by _apply_reveal when there is a scale to resolve it
        # against, which on an unmeasured page is not until the render lands.
        self._want_reveal = None
        self._zoom_timer = QTimer()
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._render)
        self._size_retry_timer = QTimer()
        self._size_retry_timer.setSingleShot(True)
        self._size_retry_timer.timeout.connect(self._render)
        # Re-aims the pre-render window at wherever the user has got to. Single
        # shot and restarted on every render, so holding the arrow key down
        # queues one round of pre-rendering at the end, not one per page.
        self._prerender_timer = QTimer()
        self._prerender_timer.setSingleShot(True)
        self._prerender_timer.timeout.connect(self._prerender_all)
        self._prerender_aim = None   # page the window was last aimed at
        # Background render infrastructure
        self._render_gen     = 0
        self._render_task    = None          # current active _PageRenderTask
        # True while the page on screen is a stand-in scaled from another zoom.
        # Never the resting state: an exact render is in flight whenever it is set.
        self._showing_provisional = False
        # Window rendering, used past MAX_RENDER_PX (see _show_region)
        self._region_img     = None    # QPixmap of the rendered window
        self._region_rect    = None    # (px0, py0, w, h) in displayed page pixels
        self._region_scale   = 0.0
        self._region_chars   = []
        self._region_task    = None
        self._region_signals = _RegionSignals()
        self._region_signals.ready.connect(self._on_region_ready)
        # Colour-space label and the background work behind it. The scan's
        # completion is also when the page counter re-aggregates.
        self._color = ColourSpaceLabel(
            self._color_source, on_scan_complete=self.publish_colour_counts)
        # Which page and rotation _page_w_pt/_page_h_pt currently describe
        self._dims_key = None
        self._dims_rot = None
        self._render_signals = _PageSignals()
        self._render_signals.ready.connect(self._on_page_ready)
        self._strip_signals = _PageSignals()
        self._strip_signals.ready.connect(self._on_strip_page)
        self._strip_pending: set = set()
        self._strip_tasks: dict = {}   # (path, orig, rot) -> _PageRenderTask
        self._strip_gens: dict = {}    # task generation -> pending key
        self._strip_gen = 10 ** 9      # offset: never collide with paged gens
        self._strip_pm: dict = {}   # drawn pixmaps, see _render_continuous
        self._doc_scroll = 0.0
        # Where the scroll is heading, as against where it is. A wheel notch
        # moves the goal; the timer below walks the view to it over a few
        # frames, which is the difference between a document that scrolls and
        # one that jumps a third of a page at a time.
        self._scroll_goal = 0.0
        self._scroll_anim = QTimer(self)
        self._scroll_anim.setInterval(self.SCROLL_FRAME_MS)
        self._scroll_anim.timeout.connect(self._step_scroll_anim)
        # Pre-render (background warm-up) state
        self._prerender_tasks: list = []
        # Guides, per page, in points from the sheet's top-left corner. Page
        # coordinates rather than pixels so a guide 20 mm into the sheet stays
        # 20 mm into the sheet through a zoom, a scroll and a page turn.
        self._guides: dict = {}
        self._rulers_on = False
        # The last readings this view published on the status bus. Kept so a
        # tab or view switch can re-publish them without waiting for a render
        # (see publish_status) — the bar keeps no memory of its own.
        self._metrics = ""
        self._phys_pct = None
        # Preflight: "unknown" until a check has run.
        self._pf_state = "unknown"
        self._pf_issues = []
        # Search hits for the whole document, and which one is being looked at.
        # Held in page points — the same space the guides use — so a zoom or a
        # scroll moves them with the sheet instead of leaving them behind at
        # the pixels they were computed at.
        self._find_hits    = []
        self._find_current = -1
        # Continuous scrolling, off unless Darstellung says otherwise. When it
        # is off nothing below runs and the view behaves exactly as it always
        # has — one page at a time, turned by the wheel at its edges.
        self._continuous  = False
        self._strip       = []     # [(page_pos, y_top, w_px, h_px)] in the strip
        self._strip_h     = 0.0
        self._strip_key   = None   # what _strip was built for
        # The page the continuous fit is measured against: zoom 1.0 shows this
        # page whole, and every other sheet shares its pixels-per-point so the
        # strip keeps one scale. Re-resolved on load, mode change and Ctrl+0.
        self._cont_ref = None      # (w_pt, h_pt)
        # While the page manager is showing, the shared navigation rail drives
        # the grid instead of this view; the delegate carries the rail's
        # actions (see PdfTab._GridRail). None means "me" — preview mode.
        self.rail_delegate = None
        self._setup()
        _register_themed(self)

    def _setup(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Haupt-Bereich: Seite + rechte Leiste
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Seiten-Anzeigebereich, mit Linealen an den Kanten (Strg+R)
        self._view = PdfPageCanvas()
        self._view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        self._ruler_top  = RulerBar(horizontal=True)
        self._ruler_left = RulerBar(horizontal=False)
        self._ruler_corner = RulerCorner()
        page_area = QGridLayout()
        page_area.setContentsMargins(0, 0, 0, 0)
        page_area.setSpacing(0)
        page_area.addWidget(self._ruler_corner, 0, 0)
        page_area.addWidget(self._ruler_top,    0, 1)
        page_area.addWidget(self._ruler_left,   1, 0)
        page_area.addWidget(self._view,         1, 1)
        page_area.setRowStretch(1, 1)
        page_area.setColumnStretch(1, 1)
        main.addLayout(page_area, 1)
        # Off until Strg+R, as in Acrobat. Hidden here rather than through
        # _set_rulers_visible, which also syncs the toolbar button that the
        # info bar has not built yet.
        for w in (self._ruler_top, self._ruler_left, self._ruler_corner):
            w.setVisible(False)

        for bar in (self._ruler_top, self._ruler_left):
            bar.guide_previewed.connect(self._preview_guide)
            bar.guide_dropped.connect(self._drop_guide)
            bar.clear_requested.connect(self._clear_guides)
        self._view.guide_moved.connect(self._guide_moved)
        self._view.repainted.connect(self._sync_overlays)

        # ── Navigationsschiene ───────────────────────────────────────────────
        # 40 px where there were 50, and almost all of it is the track. The old
        # rail spent its height on a page number in 16 pt and two arrows, and
        # said nothing at all about how long the document was or where in it
        # you were — the two questions a rail exists to answer.
        self._nav_side = QWidget()
        self._nav_side.setObjectName("navSide")
        self._nav_side.setFixedWidth(40)
        sl = QVBoxLayout(self._nav_side)
        sl.setContentsMargins(0, 5, 0, 7)
        sl.setSpacing(4)

        self._nav_btns = []
        for ic, tip, fn in [(rotated(icon("chev", colour=theme_color("DIM")), 180),
                             tr("Vorherige Seite"), self._rail_prev),
                            (icon("chev", colour=theme_color("DIM")),
                             tr("Nächste Seite"),   self._rail_next)]:
            b = QPushButton()
            b.setIcon(ic)
            b.setIconSize(QSize(16, 16))
            b.setFixedSize(22, 16)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            self._nav_btns.append(b)

        sl.addWidget(self._nav_btns[0], alignment=Qt.AlignmentFlag.AlignHCenter)

        self._track = _PageTrack()
        self._track.picked.connect(self._on_track_picked)
        self._track.position_dragged.connect(self._on_track_dragged)
        sl.addWidget(self._track, 1, alignment=Qt.AlignmentFlag.AlignHCenter)

        sl.addWidget(self._nav_btns[1], alignment=Qt.AlignmentFlag.AlignHCenter)

        # The page over the total, read vertically — the only way a fraction
        # fits in 40 px. The number is the jump target: click it and type one.
        self._num_lbl = _PageField()
        self._num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._num_lbl.setToolTip(tr("Gehe zu Seite") + "  (Strg+G)")
        self._num_lbl.clicked.connect(self._rail_prompt_goto)
        sl.addWidget(self._num_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._nav_sep = QFrame()
        self._nav_sep.setFrameShape(QFrame.Shape.HLine)
        self._nav_sep.setFixedWidth(17)
        sl.addWidget(self._nav_sep, alignment=Qt.AlignmentFlag.AlignHCenter)

        self._tot_lbl = QLabel("0")
        self._tot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self._tot_lbl, alignment=Qt.AlignmentFlag.AlignHCenter)

        main.addWidget(self._nav_side)
        layout.addLayout(main, 1)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_theme()

    # ── continuous scrolling ─────────────────────────────────────────────────

    # The gutter between two sheets, so a page boundary stays visible — and the
    # bottom margin a sheet fitted by Ctrl+0 leaves, which is what keeps the
    # next sheet from poking into the frame as a sliver: the fit in
    # _display_scale uses it as its vertical pad, so a fitted sheet plus its
    # gutter is exactly the viewport. _build_strip gives the strip the same
    # room above its first sheet and below its last.
    GAP_PX = 14

    # Smooth scrolling, the way a reader does it. A mouse wheel arrives as
    # discrete notches, so a notch sets a target and the view is walked to it
    # over a handful of frames — the eye reads that as motion, where the same
    # distance applied in one go reads as a jolt. A touchpad already sends
    # pixel deltas and is followed exactly; easing something that is already
    # continuous only adds lag.
    SCROLL_FRAME_MS  = 16      # ~60 fps
    SCROLL_NOTCH_PX  = 110     # one wheel notch
    SCROLL_EASE      = 0.35    # of the remaining distance, per frame
    SCROLL_SNAP_PX   = 0.5     # close enough to stop
    # How many strip pages to warm beyond the viewport. One screenful each way
    # is enough that a fast flick rarely lands on blank paper, without flooding
    # the render queue the way pre-rendering the whole document would.
    STRIP_PREFETCH_SCREENS = 1.0
    STRIP_PM_MAX = 12          # drawn QPixmaps kept between frames

    def _max_scroll(self):
        """How far down the strip the view can go."""
        return max(0.0, self._strip_h - float(self._view.height()))

    def _clamp_scroll(self, value):
        return max(0.0, min(float(value), self._max_scroll()))

    def _jump_scroll(self, value):
        """Put the view somewhere at once, with no animation — a page picked
        from the rail, a find hit, a mode change. The goal has to move with it
        or the animator would immediately drag the view back."""
        self._scroll_anim.stop()
        self._doc_scroll = self._scroll_goal = self._clamp_scroll(value)

    def _step_scroll_anim(self):
        """One frame: close a fixed fraction of the remaining distance.

        Exponential rather than linear, so the scroll leaves quickly and
        settles gently instead of stopping dead — the same shape a reader
        expects from a scroll it did not have to think about.

        Calls _render_continuous rather than _render: the latter emits
        page_changed unconditionally, which at sixty frames a second would
        re-aim the pre-render and re-read the colour spaces on every frame.
        _render_continuous already tracks the current page and only announces
        a change when the page really changed.
        """
        gap = self._scroll_goal - self._doc_scroll
        if abs(gap) < self.SCROLL_SNAP_PX:
            self._doc_scroll = self._scroll_goal
            self._scroll_anim.stop()
        else:
            self._doc_scroll += gap * self.SCROLL_EASE
        scale = self._display_scale(self._zoom)
        if scale is None:
            self._scroll_anim.stop()
            return
        self._render_continuous(scale, self._view.width(), self._view.height())

    def scroll_by(self, dy_px, animate=True):
        """Move the view `dy_px` down the strip (negative is up)."""
        self._scroll_goal = self._clamp_scroll(self._scroll_goal + dy_px)
        if not animate:
            self._jump_scroll(self._scroll_goal)
            # Direct continuous paint: _render() would re-emit page_changed and
            # re-aim pre-render on every touchpad event.
            scale = self._display_scale(self._zoom)
            if scale is not None:
                self._render_continuous(scale, self._view.width(),
                                        self._view.height())
            else:
                self._render()
            return
        if not self._scroll_anim.isActive():
            self._scroll_anim.start()
        # First frame immediately so the wheel does not wait one timer tick
        # before anything moves — that tick was the hitch between notches.
        self._step_scroll_anim()

    def set_continuous(self, on):
        """Turn continuous scrolling on or off and redraw.

        Off is the default and is exactly what this view has always done — one
        page at a time, turned at its edges. Nothing below runs in that mode.
        Switching keeps the reader's place: the page being looked at stays the
        page being looked at."""
        on = bool(on)
        if on == self._continuous:
            return
        self._continuous = on
        self._strip_key  = None
        self._cont_ref   = None
        self._scroll_x   = 0.0
        self._scroll_y   = 0.0
        self._cancel_strip_tasks()
        self._strip_pm.clear()
        if self.rail_delegate is None:
            self._track.set_scroll_mode(on)
        else:
            # The rail is showing the page manager right now; whoever handed
            # the delegate over re-applies the mode when it hands the rail
            # back.
            pass
        if on:
            scale = self._display_scale(self._zoom) or 1.0
            self._build_strip(scale)
            # Leave scroll where load() placed it (0 on a fresh file); the
            # clamp inside _render_continuous keeps it in range otherwise.
        self._view.clear()
        self._render()

    def _page_size_pts(self, uid):
        return strip.page_size_pts(
            self.model, self.pdf_path, uid,
            (self._page_w_pt or 595.0, self._page_h_pt or 842.0))

    def _ensure_cont_ref(self):
        if self._cont_ref is None:
            self._cont_ref = strip.resolve_ref(
                self.model, self.pdf_path, self._current, (595.0, 842.0))
        return self._cont_ref

    def _build_strip(self, scale):
        """Rebuilt only when the zoom, the page list or the window width
        changes (see strip.build for the layout itself)."""
        if self.model is None:
            self._strip = []
            self._strip_h = 0.0
            self._strip_key = None
            return
        key = (id(self.model), tuple(self.model.order), round(scale, 6),
               self._view.width())
        if self._strip_key == key:
            return
        # Keep the viewport's place in the document when the strip is rebuilt
        # (zoom, resize). Without this the same _doc_scroll lands on a different
        # page once page heights change.
        old_h = self._strip_h
        old_scroll = self._doc_scroll
        old_goal = self._scroll_goal
        animating = abs(old_goal - old_scroll) > 1.0
        self._strip, self._strip_h = strip.build(
            self.model, self.pdf_path, scale, self.GAP_PX,
            (self._page_w_pt or 595.0, self._page_h_pt or 842.0))
        self._strip_key = key
        if old_h > 0 and self._strip_h > 0 and abs(self._strip_h - old_h) > 0.5:
            ratio = self._strip_h / old_h
            self._doc_scroll = self._clamp_scroll(old_scroll * ratio)
            if animating:
                self._scroll_goal = self._clamp_scroll(old_goal * ratio)
            else:
                self._scroll_goal = self._doc_scroll

    def _strip_top_of(self, pos):
        return strip.top_of(self._strip, pos)

    def _page_at_strip_y(self, y):
        return strip.page_at(self._strip, y, self._view.height())

    def _cancel_strip_tasks(self):
        for task in self._strip_tasks.values():
            try:
                task.cancel()
            except Exception:
                pass
        self._strip_tasks.clear()
        self._strip_pending.clear()
        self._strip_gens.clear()

    def _drop_strip_task(self, key):
        """Cancel and forget one in-flight strip request."""
        task = self._strip_tasks.pop(key, None)
        if task is not None:
            self._strip_gens.pop(getattr(task, "_strip_gen", None), None)
            try:
                task.cancel()
            except Exception:
                pass
        self._strip_pending.discard(key)

    def _strip_cache_entry(self, src_path, orig, rot, avail_w, avail_h, w_px):
        return strip.cache_entry(src_path, orig, rot, avail_w, avail_h, w_px)

    def _pixmap_for_strip(self, src_path, orig, rot, img, w_px, h_px):
        """QPixmap of `img` drawn at the sheet size, cached across frames."""
        pm_key = (src_path, orig, rot, int(w_px), int(h_px), img.cacheKey())
        pm = self._strip_pm.get(pm_key)
        if pm is not None:
            return pm
        pm = QPixmap.fromImage(img)
        if abs(pm.width() - w_px) > 1 or abs(pm.height() - h_px) > 1:
            # Fast while scrolling: SmoothTransformation on multi-megapixel
            # pages is what made continuous mode hitch every frame a new sheet
            # entered the viewport. Settle quality comes from an exact-size
            # render, not from this scale.
            smooth = (not self._scroll_anim.isActive()
                      and abs(self._scroll_goal - self._doc_scroll) < 1.0)
            pm = pm.scaled(max(1, int(w_px)), max(1, int(h_px)),
                           Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation if smooth
                           else Qt.TransformationMode.FastTransformation)
        if len(self._strip_pm) >= self.STRIP_PM_MAX:
            # Drop oldest insertion rather than wipe everything — wiping forced
            # a full rescan of every on-screen page on the next frame.
            try:
                del self._strip_pm[next(iter(self._strip_pm))]
            except StopIteration:
                pass
        self._strip_pm[pm_key] = pm
        return pm

    def _render_continuous(self, scale, avail_w, avail_h):
        """Draw every sheet that intersects the viewport."""
        self._build_strip(scale)
        if not self._strip:
            return
        max_scroll = max(0.0, self._strip_h - avail_h)
        self._doc_scroll  = max(0.0, min(self._doc_scroll, max_scroll))
        # The goal follows the same clamp, or a zoom that shortens the strip
        # would leave the animator pulling towards an offset off the end.
        self._scroll_goal = max(0.0, min(self._scroll_goal, max_scroll))
        top = self._doc_scroll
        bot = top + avail_h
        prefetch = avail_h * self.STRIP_PREFETCH_SCREENS

        # Which page the rest of this class means by "the page" — the rulers,
        # the colour label and the find highlights all still speak of one.
        pos = self._page_at_strip_y(top)
        page_changed = pos != self._current
        if page_changed:
            self._current = pos
            self._num_lbl.setText(str(pos + 1))
            self.page_changed.emit(pos + 1)
            QTimer.singleShot(0, self._color.update)
            if self._prerender_aim != self._current:
                self._prerender_aim = self._current
                self._prerender_timer.start(350)
        self._nav_show(len(self.model.order), pos + 1,
                       self._doc_scroll / max_scroll if max_scroll > 0 else 0.0)

        sheets, chars = [], []
        needed = set()
        pad = 16.0
        for p_pos, p_top, w_px, h_px in self._strip:
            in_view = not (p_top + h_px < top or p_top > bot)
            near = not (p_top + h_px < top - prefetch or p_top > bot + prefetch)
            if not near:
                continue
            uid = self.model.order[p_pos]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            rot = self.model.get_rotation(uid)
            key = (src_path, orig, rot)
            cached = self._strip_cache_entry(src_path, orig, rot,
                                            avail_w, avail_h, w_px)
            # _PageRenderTask still fits page×height. Convert the continuous
            # display scale into the zoom factor that makes target_scale land
            # on that same scale for this page's dimensions.
            strip_zoom = self._strip_render_zoom(
                scale, avail_w, avail_h, pad, src_path, orig, rot, w_px, h_px)
            if cached is None:
                needed.add(key)
                self._request_page(src_path, orig, rot, avail_w, avail_h,
                                   strip_zoom)
            elif not _good_enough(cached[3], scale) and cached[3] > 0:
                # Have pixels, but coarser than the strip scale — show them and
                # ask for a sharper render in the background.
                needed.add(key)
                self._request_page(src_path, orig, rot, avail_w, avail_h,
                                   strip_zoom)
            if not in_view:
                continue
            x = max(0.0, (avail_w - w_px) / 2.0) - self._scroll_x
            y = p_top - top
            pm = None
            if cached is not None:
                img, pw_pt, ph_pt, _cs, raw = cached
                pm = self._pixmap_for_strip(src_path, orig, rot, img, w_px, h_px)
                if p_pos == self._current and pw_pt > 0 and ph_pt > 0:
                    self._page_w_pt, self._page_h_pt = pw_pt, ph_pt
                    self._dims_key = (src_path, orig)
                    self._dims_rot = rot
                if raw:
                    r = (w_px / img.width()) if img.width() else 1.0
                    chars.extend((ch, x + a * r, y + b * r, x + c * r, y + d * r)
                                 for ch, a, b, c, d in raw)
            sheets.append((pm, x, y, w_px, h_px))

        # Drop pending work for pages that scrolled far off screen so the queue
        # stays aimed at what is about to be visible.
        for key in list(self._strip_pending):
            if key not in needed:
                self._drop_strip_task(key)

        self._view.set_sheets(sheets, chars, keep_selection=True)
        if sheets:
            # Labels from the current page's width when it is on screen, else
            # the first visible sheet — not always sheets[0], which is the top
            # of the viewport and often the page being left.
            label_w = sheets[0][3]
            for p_pos, p_top, w_px, h_px in self._strip:
                if p_pos == self._current and not (
                        p_top + h_px < top or p_top > bot):
                    label_w = w_px
                    break
            self._apply_zoom_labels_for(label_w)
        self._showing_provisional = any(pm is None for pm, *_rest in sheets)

    def _strip_render_zoom(self, display_scale, avail_w, avail_h, pad,
                           src_path, orig, rot, w_px, h_px):
        return strip.render_zoom(display_scale, avail_w, avail_h, pad,
                                 src_path, orig, rot, w_px, h_px, self._zoom)

    def _request_page(self, src_path, orig, rot, avail_w, avail_h, zoom=1.0):
        """Ask for a page the strip needs and has not got. The result lands in
        the shared cache and the repaint that follows picks it up.

        The in-flight set is what stops this asking again for a page it is
        already waiting on — without it, every repaint while a page renders
        queues another render of the same page."""
        key = (src_path, orig, rot)
        if key in self._strip_pending:
            return
        self._strip_pending.add(key)
        self._strip_gen += 1
        gen = self._strip_gen
        # Priority 0 for the page under the read position, 1 for neighbours:
        # strip pages used to sit behind every thumbnail at P1 forever.
        pri = 0 if key == self._current_page_key() else 1
        task = _PageRenderTask(gen, src_path, orig, rot, avail_w, avail_h, zoom,
                               signals=self._strip_signals)
        task._strip_gen = gen
        self._strip_gens[gen] = key
        self._strip_tasks[key] = task
        _render_queue.submit(task, pri)

    def _on_strip_page(self, gen, image, off_x, off_y,
                       page_w_pt, page_h_pt, scale, raw_chars,
                       provisional=False):
        """A page the strip was waiting for has arrived.

        The pending key is released by generation, not by looking in the cache:
        a task submitted before a resize lands in a different cache bucket than
        the view now reads, and a cache lookup here would find nothing — the
        key stayed pending forever, the strip never asked again, and the
        preview sat blank until something else happened to repaint. The task's
        generation identifies exactly which request finished.
        """
        if not provisional:
            key = self._strip_gens.pop(gen, None)
            if key is not None:
                self._strip_pending.discard(key)
                self._strip_tasks.pop(key, None)
        if self._continuous:
            ds = self._display_scale(self._zoom)
            if ds is not None:
                self._render_continuous(ds, self._view.width(),
                                        self._view.height())

    # ── the preflight light ──────────────────────────────────────────────────

    def set_preflight(self, state, issues=()):
        """state: "unknown" | "running" | "ok" | "warn"."""
        self._pf_state  = state
        self._pf_issues = list(issues)
        AppState.get().preflight_changed.emit(state, list(issues))

    def _show_preflight(self):
        """The findings, and the way through to the full check."""
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        if self._pf_issues:
            for issue in self._pf_issues[:8]:
                act = menu.addAction("● " + issue)
                act.setEnabled(False)
        else:
            act = menu.addAction(tr("Keine Hinweise."))
            act.setEnabled(False)
        menu.addSeparator()
        opener = getattr(self, "open_preflight_panel", None)
        act = menu.addAction(tr("Vollständige Prüfung öffnen…"))
        act.setEnabled(opener is not None)
        if opener is not None:
            act.triggered.connect(lambda: opener())
        menu.exec(QCursor.pos() - QPoint(0, menu.sizeHint().height()))

    def publish_colour_counts(self):
        """The colour/greyscale counter for the status bar (decision 5): the
        structure scan's verdict over every page of the tab, with the
        greyscale tool's pixel verdict standing in where it has run. Nothing
        until every page is known — half a count reads as a whole one, and
        the colour side of it is what a job gets billed by."""
        if self.model is None:
            AppState.get().colour_counts_changed.emit(None)
            return
        pages = [self.model.page_source(uid, self.pdf_path)
                 for uid in self.model.order]
        colour, grey, unknown = count_grey_pages(pages)
        AppState.get().colour_counts_changed.emit(
            None if unknown else (colour, grey))

    def publish_status(self):
        """Re-publish every reading this view owns, so the window status bar
        reflects it when this tab or view becomes active. The bar keeps no
        memory of its own: the active view says again what it already knows."""
        bus = AppState.get()
        bus.zoom_changed.emit(
            self._phys_pct if self._phys_pct is not None
            else int(self._zoom * 100))
        bus.page_metrics_changed.emit(self._metrics or "")
        self._color.republish()
        self.set_preflight(self._pf_state, self._pf_issues)
        self.publish_colour_counts()
        bus.ruler_changed.emit(self._rulers_on)

    def _apply_theme(self):
        t = _TV
        self._view.setStyleSheet(f"background:{t['viewer_bg']};")
        self._nav_side.setStyleSheet(
            f"QWidget#navSide{{background:{t['sidebar_bg']};border-left:1px solid {t['border']};}}")
        self._num_lbl.setStyleSheet(
            f"color:{t['text']};font-size:10px;font-weight:500;"
            f"background:{t['input_bg']};border:1px solid {t['border']};"
            f"border-radius:4px;padding:2px 0;min-width:27px;")
        self._nav_sep.setStyleSheet(f"color:{t['border']};")
        self._tot_lbl.setStyleSheet(
            f"color:{t['vdim']};font-size:9px;background:transparent;")
        self._track.update()
        # The chevrons are stroked in the shell theme's DIM and would keep the
        # colour of the theme the view was built under otherwise.
        dim = theme_color("DIM")
        self._nav_btns[0].setIcon(
            rotated(icon("chev", colour=dim), 180))
        self._nav_btns[1].setIcon(icon("chev", colour=dim))
        _nb = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:5px;font-size:12px;}}"
               f"QPushButton:hover{{background:{t['hover']};border-color:{t['acc']};}}")
        for b in self._nav_btns:
            b.setStyleSheet(_nb)

    # ── Zoom-Methoden ─────────────────────────────────────────────────────────

    def _display_scale(self, zoom):
        """Pixels per point the page is shown at. No ceiling: past
        MAX_RENDER_PX the page is not rendered in one piece any more, it is
        rendered a window at a time, so the zoom is free to keep going.

        Snapped to a scale that puts a whole number of pixels across the page,
        which is the only kind that can be rendered — see snap_scale in
        tools/render/region.py. Without it the scale laid out with and the scale
        rendered at differ in the seventh decimal, and every pan reads as a new
        zoom and re-renders instead of blitting.
        """
        avail_w = self._view.width()
        avail_h = self._view.height()
        if avail_w < 16 or avail_h < 16:
            return None
        pad = 16
        if self._continuous:
            # One reference page for the whole document: zoom 1.0 shows that
            # page whole — the same "fit" the paged view starts at — and every
            # other sheet shares its pixels-per-point, so the strip keeps one
            # scale however mixed the page sizes are.
            rw, rh = self._ensure_cont_ref()
            if rw <= 0 or rh <= 0:
                return None
            # The gutter below a sheet is the fit's bottom margin: a fitted
            # page plus its gutter is exactly the viewport, so the sheet after
            # it starts off screen instead of poking in as a sliver.
            fit = min((avail_w - pad) / rw, (avail_h - self.GAP_PX) / rh)
            return snap_scale(rw, rh, fit * zoom)
        if self._page_w_pt <= 0 or self._page_h_pt <= 0:
            return None
        fit = min((avail_w - pad) / self._page_w_pt,
                  (avail_h - pad) / self._page_h_pt)
        return snap_scale(self._page_w_pt, self._page_h_pt, fit * zoom)

    def _capped_display_size(self, zoom):
        """Size in pixels (w, h) the page occupies on screen at `zoom`, or
        (None, None) if its dimensions are not known yet.

        This is what the scroll limits and the zoom anchor are built on. It used
        to clamp to MAX_RENDER_PX, because that really was as large as the page
        could get — the render was one bitmap. With window rendering the page on
        screen keeps growing, and clamping here would have pinned the scroll
        range while the page kept getting bigger under it."""
        scale = self._display_scale(zoom)
        if scale is None:
            return None, None
        return self._page_px(scale)

    def _use_region_rendering(self, scale):
        """Is the page at this scale too large to render in one piece?"""
        if self._page_w_pt <= 0 or self._page_h_pt <= 0 or scale is None:
            return False
        return max(self._page_w_pt * scale,
                   self._page_h_pt * scale) > MAX_RENDER_PX

    def _apply_zoom(self, new_zoom, mx=None, my=None):
        """
        Ändert den Zoom und passt _scroll_x/y so an, dass der Inhaltspunkt
        unter (mx, my) in Widget-Koordinaten fixiert bleibt.
        Wenn mx/my None, wird die Viewport-Mitte verwendet.

        Kern-Invariante:
            off_x = max(0, (avail_w - eff_pm_w) / 2) - scroll_x
        Dabei ist eff_pm_w die tatsächlich angezeigte Breite, also:
            _last_pm.width() * (_zoom / _last_zoom)
        Das muss für old_zoom und new_zoom konsistent gelten.
        """
        avail_w = float(self._view.width())
        avail_h = float(self._view.height())
        if avail_w < 1 or avail_h < 1:
            self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))
            return

        if mx is None: mx = avail_w / 2.0
        if my is None: my = avail_h / 2.0

        old_zoom = self._zoom
        self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(new_zoom)))
        if old_zoom <= 0:
            return

        # Continuous: the document is one strip. Anchor the point under the
        # cursor in strip coordinates so a zoom does not jump to another page.
        # The paged branch below only knows about one sheet's _scroll_x/y.
        if self._continuous:
            old_scale = self._display_scale(old_zoom)
            new_scale = self._display_scale(self._zoom)
            if old_scale and new_scale and old_scale > 0:
                content_y = self._doc_scroll + my
                content_x = self._scroll_x + mx
                ratio = new_scale / old_scale
                self._strip_key = None
                self._strip_pm.clear()
                self._cancel_strip_tasks()
                self._build_strip(new_scale)
                self._jump_scroll(content_y * ratio - my)
                max_sx = 0.0
                for _p, _t, w_px, _h in self._strip:
                    max_sx = max(max_sx, max(0.0, w_px - avail_w))
                self._scroll_x = max(0.0, min(content_x * ratio - mx, max_sx))
            return

        if self._last_pm is None or self._last_zoom <= 0:
            return

        # Use cap-aware display sizes when page dimensions are known.
        # The naive formula `_last_pm.width() * (zoom / _last_zoom)` breaks
        # once the MAX_RENDER_PX render cap kicks in, because zooming further no
        # longer increases the actual pixel width — leading to wrong scroll
        # limits that push the page completely off-screen.
        eff_w_cap, eff_h_cap = self._capped_display_size(old_zoom)
        new_w_cap, new_h_cap = self._capped_display_size(self._zoom)

        if eff_w_cap is not None and new_w_cap is not None:
            eff_w, eff_h = eff_w_cap, eff_h_cap
            new_w, new_h = new_w_cap, new_h_cap
        else:
            # Fallback: linear extrapolation (no page dims yet)
            eff_w = self._last_pm.width()  * (old_zoom / self._last_zoom)
            eff_h = self._last_pm.height() * (old_zoom / self._last_zoom)
            ratio = self._zoom / old_zoom
            new_w = eff_w * ratio
            new_h = eff_h * ratio

        # Aktuelle linke/obere Kante der Seite im Viewport
        cur_off_x = max(0.0, (avail_w - eff_w) / 2.0) - self._scroll_x
        cur_off_y = max(0.0, (avail_h - eff_h) / 2.0) - self._scroll_y

        # Seitenanteil unter dem Mauszeiger (0=linke Kante, 1=rechte Kante)
        frac_x = (mx - cur_off_x) / eff_w if eff_w > 0 else 0.5
        frac_y = (my - cur_off_y) / eff_h if eff_h > 0 else 0.5

        # Neuen Scroll berechnen: frac soll wieder bei mx/my liegen
        new_off_base_x = max(0.0, (avail_w - new_w) / 2.0)
        new_off_base_y = max(0.0, (avail_h - new_h) / 2.0)
        self._scroll_x = new_off_base_x + frac_x * new_w - mx
        self._scroll_y = new_off_base_y + frac_y * new_h - my

        # Auf gültigen Bereich begrenzen
        max_sx = max(0.0, new_w - avail_w)
        max_sy = max(0.0, new_h - avail_h)
        self._scroll_x = max(0.0, min(self._scroll_x, max_sx))
        self._scroll_y = max(0.0, min(self._scroll_y, max_sy))

    def _zoom_in(self):
        self._apply_zoom(self._zoom * 1.25)
        self._render_preview()
        self._zoom_timer.start(120)

    def _zoom_out(self):
        self._apply_zoom(self._zoom / 1.25)
        self._render_preview()
        self._zoom_timer.start(120)

    def _zoom_fit(self):
        """Ctrl+0: fit the page (or, continuously, the reference page) whole."""
        self._zoom     = 1.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        if self._continuous:
            # Re-measure the fit against the page being read, keeping the
            # reader's place in the strip.
            self._cont_ref = None
            self._strip_key = None
            self._strip_pm.clear()
            self._cancel_strip_tasks()
        self._render()

    def _zoom_actual_size(self):
        """Ctrl+1: the page at its true physical size (100% = 1pt = 1/72 in).

        The zoom factor is actual scale ÷ the fit scale at zoom 1.0 — computed
        through _display_scale so it is the same arithmetic in both modes.
        This used to use the paged fit formula directly, which in continuous
        mode described a different fit than the one the strip is laid out
        with, so the shortcut zoomed to the wrong size.
        """
        try:
            win    = self.window().windowHandle()
            screen = (win.screen() if win and win.screen()
                      else QApplication.primaryScreen())
            phys_dpi = screen.physicalDotsPerInchX()
            dpr      = screen.devicePixelRatio()
            if phys_dpi < 50 or phys_dpi > 600:
                phys_dpi = screen.logicalDotsPerInchX() * dpr
            actual_scale = phys_dpi / 72.0   # px per PDF point at physical size
            base = self._display_scale(1.0)  # the fit this view actually uses
            if base and base > 0:
                self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, actual_scale / base))
            else:
                self._zoom = 1.0
        except Exception:
            logging.debug("single_page: could not read the screen's physical "
                          "DPI", exc_info=True)
            self._zoom = 1.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        if self._continuous:
            self._strip_key = None
            self._strip_pm.clear()
            self._cancel_strip_tasks()
        self._render()

    def wheelEvent(self, e):
        ctrl  = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        dy    = e.angleDelta().y()

        if ctrl:
            factor = 1.15 if dy > 0 else 1.0 / 1.15
            mp = self._view.mapFrom(self, e.position().toPoint())
            self._apply_zoom(self._zoom * factor, float(mp.x()), float(mp.y()))
            self._render_preview()
            self._zoom_timer.start(120)
            e.accept()
            return

        # Shift + wheel → horizontal scroll within page
        if shift:
            # Use vertical wheel axis (most mice don't have a horizontal wheel)
            dx = -dy   # wheel-down → scroll right, wheel-up → scroll left
            disp_w, _ = self._capped_display_size(self._zoom)
            if disp_w is None and self._last_pm is not None and self._last_zoom > 0:
                disp_w = self._last_pm.width() * (self._zoom / self._last_zoom)
            avail_w = float(self._view.width())
            max_sx  = max(0.0, (disp_w or 0.0) - avail_w)
            if max_sx > 0:
                step_x = max(50.0, avail_w * 0.18)
                self._scroll_x = max(0.0, min(self._scroll_x + (step_x if dx > 0 else -step_x), max_sx))
                self._render_preview(); self._schedule_settle()
            e.accept()
            return

        # Continuous: one scroll position for the whole document, and no page
        # flip at all — the boundary between two sheets is just more strip.
        if self._continuous:
            ang = e.angleDelta().y()
            pix = e.pixelDelta().y()
            # A mouse wheel reports whole detents (±120, and a pixel delta
            # alongside it on some platforms whose sign and size are not to be
            # trusted) — it is eased over frames like every other reader. A
            # touchpad reports many small deltas with a phase; following those
            # exactly is what makes the surface feel 1:1, and easing input
            # that is already smooth would only add lag.
            detent = abs(ang) >= 120
            if pix and not detent:
                self.scroll_by(-float(pix), animate=False)
            elif ang:
                self.scroll_by(-self.SCROLL_NOTCH_PX * (ang / 120.0))
            e.accept()
            return

        # No modifier: scroll vertically within page, page-flip only at boundary.
        disp_w, disp_h = self._capped_display_size(self._zoom)
        # Only use _last_pm as fallback when it belongs to the CURRENT page
        # (i.e. page dimensions are already known from a completed render).
        # Using stale dims from a previous page during a mid-flight render causes
        # incorrect max_sy → unexpected page flips.
        if disp_h is None and self._page_w_pt > 0 and self._last_pm is not None and self._last_zoom > 0:
            ratio  = self._zoom / self._last_zoom
            disp_h = self._last_pm.height() * ratio
        avail_h = float(self._view.height())
        max_sy  = max(0.0, (disp_h or 0.0) - avail_h)
        step    = max(50.0, avail_h * 0.18)

        # A page turn is never refused. It used to be skipped whenever a render
        # was in flight, which on a simple document is a few milliseconds and
        # on a complex one is most of the time — so scrolling towards a page
        # further on stopped dead at the first slow page in the way. The render
        # is cancelled and re-aimed instead, and the page that has not arrived
        # yet stands in as blank paper.
        if dy < 0:   # wheel down → scroll down, then next page
            if max_sy > 0 and self._scroll_y < max_sy - 0.5:
                self._scroll_y = min(self._scroll_y + step, max_sy)
                self._render_preview(); self._schedule_settle()
            else:
                self.next_page()          # lands at top of next page (scroll_y=0)
        else:        # wheel up → scroll up, then prev page at its BOTTOM
            if self._scroll_y > 0.5:
                self._scroll_y = max(self._scroll_y - step, 0.0)
                self._render_preview(); self._schedule_settle()
            else:
                self.prev_page(start_at_bottom=True)
        e.accept()

    # ── Window rendering, for zooms past MAX_RENDER_PX ───────────────────────

    def _known_page_size(self, src_path, orig, rot):
        """(w, h) in points of the page as displayed, from whatever has already
        measured it — (0, 0) if nothing has.

        Both sources are dictionary lookups. Neither ever parses: see
        _ensure_page_dims on why this must not touch pdfium.
        """
        size = cached_page_size_pt(src_path, orig)
        if size is not None:
            w, h = size
            return (h, w) if rot % 180 == 90 else (w, h)
        # _FullPageCache stores the dimensions already turned, and keys on the
        # rotation, so this needs no swap of its own.
        return _FullPageCache.get_dims(src_path, orig, rot)

    def _ensure_page_dims(self, src_path, orig, rot):
        """Keep _page_w_pt/_page_h_pt describing the page *as displayed*.

        Turning a page a quarter swaps its width and height, and everything
        downstream — the scale, the scroll range, which part of the page the
        window covers — is computed from them. They otherwise only change when a
        render finishes, so the first render after a rotation was laid out for
        the page's old shape.

        Turning to a *different* page used to leave them at zero — next_page
        clears them deliberately — and at zoom, zero dimensions mean _render()
        cannot tell that the page needs window rendering. It fell back to
        rendering the whole sheet in one bitmap, clamped to MAX_RENDER_PX: the
        most expensive render there is, and not the zoom that was asked for, so
        the view stayed on a stand-in. Taking the size from a cache that already
        knows it puts the new page straight into the right mode.

        Deliberately does no I/O: this runs on the GUI thread inside _render(),
        and asking pdfium for the size would queue behind the render worker's
        lock and stall the window.
        """
        key = (src_path, orig)
        if (self._dims_key == key and self._dims_rot is not None
                and self._page_w_pt > 0 and self._page_h_pt > 0):
            if (rot % 180) != (self._dims_rot % 180):
                self._page_w_pt, self._page_h_pt = self._page_h_pt, self._page_w_pt
                self._leave_region_mode()   # measured against the old shape
        elif self._dims_key != key or self._page_w_pt <= 0 or self._page_h_pt <= 0:
            # A different page: whatever window is rendered belongs to the old
            # one, and blitting it here would show the wrong page at the right
            # scroll position.
            self._leave_region_mode()
            w_pt, h_pt = self._known_page_size(src_path, orig, rot)
            if w_pt > 0 and h_pt > 0:
                self._page_w_pt, self._page_h_pt = w_pt, h_pt
        self._dims_key = key
        self._dims_rot = rot

    def _current_page_key(self):
        """(path, page index, rotation) of the page on screen, or None."""
        try:
            uid = self.model.order[self._current]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            return (src_path, orig, self.model.get_rotation(uid))
        except Exception:
            logging.debug("single_page: could not resolve the current page's "
                          "key", exc_info=True)
            return None

    def _stand_in_is_current(self):
        """Is _last_pm a picture of the page we are showing?

        It is only ever a stand-in — stretched to a new zoom while the exact
        render runs — and stretching the *previous* page is not a stand-in, it
        is the wrong page. On a simple document the real render lands in
        milliseconds and nobody sees it; on a complex one it takes seconds, so
        every page turn showed the page before it, and scrolling through a file
        showed page 1 over and over.
        """
        return (self._last_pm is not None and self._last_zoom > 0
                and self._last_pm_key is not None
                and self._last_pm_key == self._current_page_key())

    def _show_empty_sheet(self, src_path, orig, rot, avail_w, avail_h):
        """Blank paper the size this page will be, while it renders.

        The size comes from whatever already knows it — the size cache, or a
        previous render of the same page. With nothing to go on the canvas is
        cleared instead of guessing: a sheet of the wrong shape that then
        jumps is worse than none.
        """
        w_pt, h_pt = self._known_page_size(src_path, orig, rot)
        if w_pt <= 0 or h_pt <= 0:
            self._view.clear()
            return
        scale = _target_scale(avail_w, avail_h, self._zoom, w_pt, h_pt)
        page_px_w, page_px_h = page_px_size(w_pt, h_pt, scale, 0)
        self._place_scroll(page_px_w, page_px_h, avail_w, avail_h)
        ox = max(0.0, (avail_w - page_px_w) / 2.0) - self._scroll_x
        oy = max(0.0, (avail_h - page_px_h) / 2.0) - self._scroll_y
        self._view.show_placeholder(ox, oy, page_px_w, page_px_h)

    # ── rulers and guides ────────────────────────────────────────────────────

    MM_PER_PT = 25.4 / 72.0

    def _set_rulers_visible(self, on):
        self._rulers_on = bool(on)
        for w in (self._ruler_top, self._ruler_left, self._ruler_corner):
            w.setVisible(self._rulers_on)
        # Ctrl+R and the status-bar switch are two ways to the same state; the
        # bar has to show it even when the shortcut was what changed it.
        AppState.get().ruler_changed.emit(self._rulers_on)
        if self._rulers_on:
            self._sync_rulers()
        # The bars take 22 px of width and of height away from the page, or
        # give them back, so the page has to be laid out again for the room it
        # now has. Queued rather than called: Qt applies the new layout after
        # this returns, and _render measures the view.
        #
        # Without this the sheet stayed the size it had been rendered at while
        # the rulers measured the size it should have been — 477 px drawn
        # against 461 px assumed, on a 1100 px window — so every reading on
        # them was 3.4 % out. It came right the moment anything else caused a
        # render, which is why zooming once appeared to fix the rulers.
        QTimer.singleShot(0, self._render)

    def toggle_rulers(self):
        """Strg+R, as in Acrobat."""
        self._set_rulers_visible(not self._rulers_on)

    def _sheet_on_screen(self):
        """(left_px, top_px, px_per_pt) of the sheet, or None if unmeasured.

        Continuous mode answers for the page currently being read, which is the
        one everything else in this class already means by "the page".

        The one conversion between a place on the page and a place on screen.
        Everything about guides and rulers goes through it, so they cannot
        drift apart from the page they are measuring.
        """
        scale = self._display_scale(self._zoom)
        if scale is None or self._page_w_pt <= 0:
            return None
        page_px_w, page_px_h = self._page_px(scale)
        if self._continuous and self._strip:
            for pos, top, w_px, h_px in self._strip:
                if pos == self._current:
                    ox = max(0.0, (float(self._view.width()) - w_px) / 2.0) - self._scroll_x
                    oy = top - getattr(self, "_doc_scroll", 0.0)
                    page_px_w, page_px_h = w_px, h_px
                    break
            else:
                return None
        else:
            ox, oy = self._page_origin(page_px_w, page_px_h)
        if page_px_w <= 0:
            return None
        return ox, oy, page_px_w / self._page_w_pt

    def _sync_rulers(self):
        """Put the rulers and the guides where the page currently is."""
        if not self._rulers_on:
            return
        sheet = self._sheet_on_screen()
        if sheet is None:
            self._ruler_top.set_scale(0, 0)
            self._ruler_left.set_scale(0, 0)
            self._view.set_guides([], [])
            return
        ox, oy, px_per_pt = sheet
        px_per_mm = px_per_pt / self.MM_PER_PT
        self._ruler_top.set_scale(ox, px_per_mm)
        self._ruler_left.set_scale(oy, px_per_mm)
        page = self._guides.get(self._current_page_key(), {"h": [], "v": []})
        self._view.set_guides([oy + y * px_per_pt for y in page["h"]],
                              [ox + x * px_per_pt for x in page["v"]])

    def _sync_overlays(self):
        """Everything painted *over* the page follows it — one repaint, one
        place. Both overlays are held in page coordinates and converted here,
        so a zoom or a scroll moves them with the sheet rather than leaving
        them behind at the pixel position they were computed at."""
        self._sync_rulers()
        self._sync_find()

    # ── find highlights ──────────────────────────────────────────────────────

    def set_find_hits(self, hits, current=-1):
        """The search results for the whole document, and which one is active.

        Only the hits on the page being shown are ever drawn, but the whole
        list is kept so paging through the document needs no second search."""
        self._find_hits    = list(hits)
        self._find_current = int(current)
        self._track.set_hits(h.page + 1 for h in self._find_hits)
        self._sync_find()

    def clear_find_hits(self):
        self.set_find_hits([], -1)

    def _sync_find(self):
        """Put the search highlights where the page currently is."""
        if not self._find_hits:
            self._view.set_find_boxes([], [])
            return
        sheet = self._sheet_on_screen()
        if sheet is None:
            self._view.set_find_boxes([], [])
            return
        ox, oy, px_per_pt = sheet
        boxes, current = [], []
        for i, hit in enumerate(self._find_hits):
            if hit.page != self._current:
                continue
            for x0, y0, x1, y1 in hit.boxes:
                box = (ox + x0 * px_per_pt, oy + y0 * px_per_pt,
                       ox + x1 * px_per_pt, oy + y1 * px_per_pt)
                boxes.append(box)
                if i == self._find_current:
                    current.append(box)
        self._view.set_find_boxes(boxes, current)

    def _preview_guide(self, axis, px_along_ruler):
        """Dashed line following the drag out of a ruler."""
        if not self._rulers_on:
            return
        self._view.set_guide_preview((axis, px_along_ruler))

    def _drop_guide(self, axis, px_along_ruler):
        """A guide was let go. Off the page it is discarded, which is also how
        an unwanted one is thrown away: drag it back over its ruler."""
        self._view.set_guide_preview(None)
        sheet = self._sheet_on_screen()
        if sheet is None:
            return
        ox, oy, px_per_pt = sheet
        origin = oy if axis == "h" else ox
        extent = (self._page_h_pt if axis == "h" else self._page_w_pt) * px_per_pt
        if not (-1 <= px_along_ruler - origin <= extent + 1):
            return          # let go outside the sheet: nothing to measure from
        pt = (px_along_ruler - origin) / px_per_pt
        page = self._guides.setdefault(self._current_page_key(),
                                       {"h": [], "v": []})
        page[axis].append(pt)
        self._sync_rulers()

    def _guide_moved(self, axis, index, px):
        """A guide was dragged across the page — or off it, which removes it."""
        sheet = self._sheet_on_screen()
        page = self._guides.get(self._current_page_key())
        if sheet is None or page is None or not (0 <= index < len(page[axis])):
            return
        ox, oy, px_per_pt = sheet
        origin = oy if axis == "h" else ox
        extent = (self._page_h_pt if axis == "h" else self._page_w_pt) * px_per_pt
        if not (-1 <= px - origin <= extent + 1):
            page[axis].pop(index)
        else:
            page[axis][index] = (px - origin) / px_per_pt
        self._sync_rulers()

    def _clear_guides(self, everywhere):
        if everywhere:
            self._guides.clear()
        else:
            self._guides.pop(self._current_page_key(), None)
        self._sync_rulers()

    def _place_scroll(self, page_px_w, page_px_h, avail_w, avail_h):
        """Clamp the scroll to the page, honouring a pending "start at bottom".

        prev_page(start_at_bottom=True) used to write 999999 into _scroll_y and
        trust every path to clamp it. The paths that can only clamp against a
        height they know could not: on a page nothing had measured yet the
        number stayed, and the wheel then walked it down 137 pixels a click —
        8,537 clicks from the bottom of a page 3,154 pixels tall. Scrolling
        down still turned pages, so it looked as though only "up" was broken,
        and only the page button worked because it sets the scroll to zero.
        """
        max_sx = max(0.0, page_px_w - avail_w)
        max_sy = max(0.0, page_px_h - avail_h)
        if self._want_bottom and page_px_h > 0:
            self._scroll_y = max_sy
            self._want_bottom = False
        self._scroll_x = max(0.0, min(self._scroll_x, max_sx))
        self._scroll_y = max(0.0, min(self._scroll_y, max_sy))

    def reveal_page_point(self, x_pt, y_pt):
        """Scroll so that a place on the page is on screen.

        Used when a search hit is found on a page being viewed zoomed in: the
        page turn alone would leave the match somewhere off the bottom of the
        window with nothing to say so."""
        self._want_reveal = (float(x_pt), float(y_pt))
        if self._apply_reveal():
            self._render()

    def _apply_reveal(self, page_px_w=None, page_px_h=None):
        """Resolve a pending reveal against the page's size on screen. True when
        it was resolved (and cleared), False while the size is still unknown and
        the request has to wait for the render."""
        if self._want_reveal is None:
            return False
        if self._page_w_pt <= 0 or self._page_h_pt <= 0:
            return False
        if page_px_w is None:
            scale = self._display_scale(self._zoom)
            if scale is None:
                return False
            page_px_w, page_px_h = self._page_px(scale)
        x_pt, y_pt = self._want_reveal
        px = x_pt * page_px_w / self._page_w_pt
        py = y_pt * page_px_h / self._page_h_pt
        avail_w = float(self._view.width())
        avail_h = float(self._view.height())
        margin  = 60.0
        # Only move if the point is not comfortably on screen already: a hit
        # two lines below the last one should not jump the page under the eye.
        if page_px_w > avail_w and not (self._scroll_x + margin <= px
                                        <= self._scroll_x + avail_w - margin):
            self._scroll_x = px - avail_w / 2.0
        if page_px_h > avail_h and not (self._scroll_y + margin <= py
                                        <= self._scroll_y + avail_h - margin):
            self._scroll_y = py - avail_h / 3.0
        self._want_bottom = False
        self._place_scroll(page_px_w, page_px_h, avail_w, avail_h)
        self._want_reveal = None
        return True

    def _leave_region_mode(self):
        if self._region_task is not None:
            self._region_task.cancel()
            self._region_task = None
        self._region_img   = None
        self._region_rect  = None
        self._region_scale = 0.0

    def _schedule_settle(self, ms=120):
        """Render exactly, once the gesture stops. Zooming and scrolling both
        show a cheap stand-in per step and let this fire once at the end —
        rendering per step made a complex page crawl."""
        self._zoom_timer.start(ms)

    def _page_px(self, scale):
        """The sheet's size on screen in whole pixels at `scale`.

        The same number the renderer works to, so the window it produces lands
        exactly where the view expects it. _page_w_pt/_page_h_pt already
        describe the page as displayed, hence rotation=0 here.
        """
        return page_px_size(self._page_w_pt, self._page_h_pt, scale, 0)

    def _page_origin(self, page_px_w, page_px_h):
        """Where the sheet's top-left corner sits in widget coordinates."""
        avail_w = float(self._view.width())
        avail_h = float(self._view.height())
        return (max(0.0, (avail_w - page_px_w) / 2.0) - self._scroll_x,
                max(0.0, (avail_h - page_px_h) / 2.0) - self._scroll_y)

    def _blit_region(self):
        """Put the rendered window on screen at the current scroll position.
        No rendering — this is what makes panning inside the margin free."""
        if self._region_img is None or self._region_rect is None:
            return False
        px0, py0, _, _ = self._region_rect
        page_px_w, page_px_h = self._page_px(self._region_scale)
        ox, oy = self._page_origin(page_px_w, page_px_h)
        chars = [(ch, ox + x0, oy + y0, ox + x1, oy + y1)
                 for ch, x0, y0, x1, y1 in self._region_chars]
        self._view.set_page(self._region_img, chars,
                            int(ox + px0), int(oy + py0),
                            page_rect=(int(ox), int(oy),
                                       int(page_px_w), int(page_px_h)))
        self._apply_zoom_labels_for(page_px_w)
        return True

    def _show_region(self, src_path, orig, rot, scale, avail_w, avail_h):
        page_px_w, page_px_h = self._page_px(scale)
        # Clamp the scroll to the page as it is at this zoom before deciding
        # what is visible, or the window is computed for a position the view
        # cannot actually be at.
        self._place_scroll(page_px_w, page_px_h, avail_w, avail_h)

        same_scale = abs(self._region_scale - scale) <= scale * 1e-6
        if same_scale and covers(self._region_rect, page_px_w, page_px_h,
                                 avail_w, avail_h, self._scroll_x, self._scroll_y):
            self._blit_region()          # already have these pixels
            return

        rect = region_for_viewport(page_px_w, page_px_h, avail_w, avail_h,
                                   self._scroll_x, self._scroll_y)
        # Something to look at while the window renders: whatever is already on
        # screen, stretched to the new zoom. Provisional by definition — the
        # render below replaces it.
        self._region_preview(scale, page_px_w, page_px_h)

        if self._region_task is not None:
            self._region_task.cancel()
        self._render_gen += 1
        task = _RegionRenderTask(self._render_gen, src_path, orig, rot,
                                 scale, rect, self._region_signals)
        self._region_task = task
        _render_queue.submit(task, 0)

    def _region_preview(self, scale, page_px_w, page_px_h):
        """Stretch whatever pixels we have to the new zoom, so the view is never
        blank while a window renders.

        Crops to what is on screen *before* scaling. Scaling first and cropping
        never — which is what this did — meant a jump from 6x to 35x built a
        16428x23205 pixmap, 381 megapixels, on the GUI thread: three gigabytes
        and four seconds of freeze for an image of which all but one screenful
        was thrown away. That was the stall, and the smeared over-zoomed frame
        that flashed up before the real render landed.
        """
        src_pm, src_rect, src_scale = None, None, 0.0
        if self._region_img is not None and self._region_scale > 0:
            src_pm, src_rect, src_scale = (self._region_img, self._region_rect,
                                           self._region_scale)
        elif self._stand_in_is_current() and self._page_w_pt > 0:
            src_pm = self._last_pm
            src_rect = (0, 0, self._last_pm.width(), self._last_pm.height())
            src_scale = self._last_pm.width() / self._page_w_pt
        if src_pm is None or src_scale <= 0:
            # Nothing of this page to stretch. An empty sheet rather than the
            # previous page left sitting there while this one renders.
            ox, oy = self._page_origin(page_px_w, page_px_h)
            self._view.show_placeholder(ox, oy, page_px_w, page_px_h)
            self._showing_provisional = True
            return
        f = scale / src_scale
        rx, ry, _, _ = src_rect
        avail_w = float(self._view.width()); avail_h = float(self._view.height())

        # The visible slice, in the source image's own pixels.
        vis_x = self._scroll_x if page_px_w > avail_w else 0.0
        vis_y = self._scroll_y if page_px_h > avail_h else 0.0
        sx = int(max(0, math.floor(vis_x / f) - rx))
        sy = int(max(0, math.floor(vis_y / f) - ry))
        sw = int(min(src_pm.width()  - sx, math.ceil(min(avail_w, page_px_w) / f) + 2))
        sh = int(min(src_pm.height() - sy, math.ceil(min(avail_h, page_px_h) / f) + 2))
        if sw <= 0 or sh <= 0:
            return
        crop = src_pm.copy(QRect(sx, sy, sw, sh))
        pm = crop.scaled(max(1, int(sw * f)), max(1, int(sh * f)),
                         Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.FastTransformation)
        ox, oy = self._page_origin(page_px_w, page_px_h)
        self._view.set_page(pm, [],
                            int(ox + (rx + sx) * f), int(oy + (ry + sy) * f),
                            page_rect=(int(ox), int(oy),
                                       int(page_px_w), int(page_px_h)))
        self._showing_provisional = True
        self._apply_zoom_labels_for(page_px_w)

    def _apply_zoom_labels_for(self, page_px_w):
        """Zoom/size labels from the sheet's on-screen width."""
        class _W:                      # _apply_zoom_labels only reads .width()
            def __init__(self, w): self._w = int(w)
            def width(self): return self._w
        self._apply_zoom_labels(_W(page_px_w), self._page_w_pt, self._page_h_pt)

    def _on_region_ready(self, gen, image, px0, py0, scale, chars):
        if gen != self._render_gen:
            return                      # overtaken by a newer zoom or scroll
        self._region_img    = QPixmap.fromImage(image)
        self._region_rect   = (px0, py0, image.width(), image.height())
        self._region_scale  = scale
        self._region_chars  = chars
        self._region_task   = None
        self._showing_provisional = False
        self._blit_region()
        QTimer.singleShot(0, self._color.update)

    def _render_preview(self, schedule_settle=True):
        """Schnelle Qt-Skalierung als Vorschau während Zoom-Debounce.

        Never renders. This runs on every wheel click of a zoom or a scroll, and
        the exact render is the settle timer's job — one at the end of the
        gesture instead of one per click.

        `schedule_settle=False` is for the one caller that *is* the settle:
        _render() shows a stand-in on a cache miss while its own exact render is
        already in flight. Re-arming the timer there meant _render() ran again
        120 ms later, cancelled the render it had just started, and started
        another — so any page taking longer than 120 ms to render never finished
        one. That is every complex page: the view sat on an interpolated
        stand-in indefinitely while the render thread threw away the same work
        eight times a second.
        """
        avail_w = float(self._view.width())
        avail_h = float(self._view.height())
        if avail_w < 50 or avail_h < 50:
            return
        scale = self._display_scale(self._zoom)
        # Continuous keeps its own strip paint path. The single-page stand-in
        # below calls set_page and wipes the multi-sheet canvas mid-scroll.
        if self._continuous and scale is not None:
            self._render_continuous(scale, avail_w, avail_h)
            if schedule_settle:
                self._schedule_settle()
            return
        if scale is not None:
            # One path for both modes: crop to what is on screen, then scale.
            # The old whole-page branch stretched the entire sheet on every
            # wheel click — at 3.8x that is an 11-megapixel scale per click, of
            # which one screenful is kept.
            page_px_w, page_px_h = self._page_px(scale)
            self._place_scroll(page_px_w, page_px_h, avail_w, avail_h)
            if (self._region_scale > 0
                    and abs(self._region_scale - scale) <= scale * 1e-6
                    and covers(self._region_rect, page_px_w, page_px_h,
                               avail_w, avail_h, self._scroll_x, self._scroll_y)):
                self._blit_region()          # already have these pixels
            else:
                self._region_preview(scale, page_px_w, page_px_h)
            if schedule_settle:
                self._schedule_settle()
            return

        # Page dimensions not known yet (nothing rendered): extrapolate.
        if self._last_pm is None or self._last_zoom <= 0:
            return
        ratio = self._zoom / self._last_zoom
        new_w = max(1, int(self._last_pm.width()  * ratio))
        new_h = max(1, int(self._last_pm.height() * ratio))
        pm = self._last_pm.scaled(new_w, new_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation)
        off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
        off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)
        self._view.set_page(pm, [], off_x, off_y)
        AppState.get().zoom_changed.emit(int(self._zoom * 100))

    def load(self, pdf_path, model):
        # Cancel any in-flight pre-render tasks from previous file
        for t in self._prerender_tasks:
            t.cancel()
        self._prerender_tasks.clear()
        self._cancel_strip_tasks()
        self._strip_pm.clear()
        self._strip = []
        self._strip_h = 0.0
        self._strip_key = None
        self._cont_ref = None
        self._doc_scroll = 0.0
        self._scroll_goal = 0.0
        self._scroll_anim.stop()

        self.pdf_path  = pdf_path
        self.model     = model
        self._current  = 0
        self._page_w_pt = 0.0
        self._page_h_pt = 0.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        n = len(model.order)
        self._tot_lbl.setText(str(n))
        self.publish_colour_counts()
        self._nav_show(n, 1)
        self._prerender_aim = None   # a new file: re-aim even at the same index
        QTimer.singleShot(0, self._render)
        # Give the canvas focus so arrow keys work without needing a click first
        QTimer.singleShot(0, self._view.setFocus)

    @property
    def current_page(self):
        """The page on screen, 0-based."""
        return self._current

    def cancel_prerenders(self):
        """Cancel the speculative page renders. The pages already on screen
        stay; this frees the render queue and cache slots for whoever the
        user has switched to looking at."""
        for task in list(self._prerender_tasks):
            try: task.cancel()
            except Exception: pass   # a task past its checkpoint stops on its own
        self._prerender_tasks.clear()

    def stop_background_work(self):
        """Cancel every render this view has outstanding and stop it asking for
        more. Called when the tab closes; safe to call twice."""
        for timer in (self._zoom_timer, self._size_retry_timer,
                      self._prerender_timer, self._scroll_anim):
            try: timer.stop()
            except RuntimeError: pass   # C++ timer already destroyed
        self.cancel_prerenders()
        self._cancel_strip_tasks()
        for name in ("_render_task", "_region_task"):
            task = getattr(self, name, None)
            if task is not None:
                try: task.cancel()
                except Exception: pass   # as above; the attribute is cleared regardless
                setattr(self, name, None)

    def _prerender_all(self):
        """Pre-render a window of pages around the current position.

        Only submits as many tasks as fit in _FullPageCache so we never queue
        hundreds of renders for a file the cache can't hold anyway.
        Thumbnails are rendered on-demand by PageGrid — we don't bulk-pre-render
        them here to avoid flooding the pool for large PDFs.

        Continuous mode does not pre-render: the strip already asks for a
        screenful either side of the viewport as the user scrolls, and a second
        window of speculative renders here would compete with it for the one
        render thread — cancelling and re-submitting its own work.
        """
        if self._continuous:
            return
        if not prerender_enabled():
            return
        if not self.pdf_path or not self.model:
            return
        # A zoom or scroll is still in flight. Speculative work belongs after
        # the gesture, not in the middle of it: the page the user is actually
        # looking at is about to be rendered, and a pre-render started now holds
        # the render thread when that happens.
        if self._zoom_timer.isActive() or self._scroll_anim.isActive():
            self._prerender_timer.start(350)
            return
        # Skip pre-rendering if the system is low on free RAM (< 512 MB)
        try:
            with open("/proc/meminfo") as _mf:
                for _line in _mf:
                    if _line.startswith("MemAvailable:"):
                        if int(_line.split()[1]) < 512 * 1024:
                            return
                        break
        except (OSError, ValueError, IndexError):
            pass      # no procfs to ask: pre-render rather than refuse to
        avail_w = self._view.width()
        avail_h = self._view.height()
        if avail_w < 50 or avail_h < 50:
            return

        for t in self._prerender_tasks:
            t.cancel()
        self._prerender_tasks.clear()

        order   = self.model.order
        n       = len(order)
        cur     = max(0, min(self._current, n - 1))
        # Window: fill the cache forward then backward from current page.
        # Cap at _FullPageCache.MAX so we never queue more than the cache holds.
        # As many as the memory budget affords at this document's page size —
        # see _FullPageCache.capacity. This used to be a fixed entry count,
        # which meant a window of poster pages and a window of paperback pages
        # were the same number and only one of them fitted.
        window  = _FullPageCache.capacity()
        ahead   = min(window * 3 // 4, n)    # ~75 % forward
        behind  = window - ahead
        start   = max(0, cur - behind)
        end     = min(n, start + window)
        if end - start < window:
            start = max(0, end - window)

        for pos in range(start, end):
            # The page on screen is the direct render path's, not a speculative
            # neighbour. Warming it here races that path: fired a moment before
            # the direct render lands, it renders at a stale zoom and then drops
            # that finer-than-asked image into the shared cache bucket the view
            # reads from — so the next zoom finds a "good enough" entry and
            # shows it as final instead of flagged provisional.
            if pos == cur:
                continue
            uid = order[pos]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            rot            = self.model.get_rotation(uid)
            if _FullPageCache.get(src_path, orig, rot, avail_w, avail_h) is None:
                task = _PageRenderTask(0, src_path, orig, rot,
                                       avail_w, avail_h, self._zoom,
                                       signals=None)
                self._prerender_tasks.append(task)
                _render_queue.submit(task, 2)   # lowest priority: pre-render

    def refresh(self):
        if self.model:
            n = len(self.model.order)
            self._tot_lbl.setText(str(n))
            self.publish_colour_counts()
            self._current = min(self._current, max(0, n - 1))
            self._nav_show(n, self._current + 1)
            self._strip_key = None
            self._cont_ref = None
            self._cancel_strip_tasks()
            self._strip_pm.clear()
            self._render()

    def _render(self):
        if not self.pdf_path or not self.model:
            return
        n = len(self.model.order)
        if n == 0:
            return
        self._current = max(0, min(self._current, n-1))
        uid      = self.model.order[self._current]
        src_path, orig = self.model.page_source(uid, self.pdf_path)
        rot      = self.model.get_rotation(uid)

        avail_w = self._view.width()
        avail_h = self._view.height()

        if avail_w < 50 or avail_h < 50:
            self._size_retry_timer.start(150)
            return

        # Update page counter immediately
        self._num_lbl.setText(str(self._current + 1))
        self._nav_show(n, self._current + 1)
        self.page_changed.emit(self._current + 1)

        # The page's dimensions have to be right *before* anything is computed
        # from them. They used to arrive only with a finished render, so a page
        # that had just been rotated — or not yet rendered at all — was measured
        # as it used to be, and at deep zoom that misplaces the whole window.
        self._ensure_page_dims(src_path, orig, rot)
        self._apply_reveal()

        # Pre-rendering used to run once, 400 ms after the file opened, over a
        # window around page 1 — so it warmed the pages the user had already
        # seen and never the ones ahead of them. Ten pages in, every turn was a
        # cold render again. Re-aiming it here covers every way the page can
        # change, since they all end up in _render().
        #
        # Only when the page changes, though: _render() also runs on every zoom
        # settle and every resize, and starting speculative work in the middle
        # of a gesture is exactly the waste the settle timer exists to avoid.
        if self._prerender_aim != self._current:
            self._prerender_aim = self._current
            self._prerender_timer.start(350)
            # Ask which colour spaces this page uses *now*, alongside the
            # render rather than after it. The two share nothing — one
            # rasterises, the other reads the content stream — so the scan
            # used to sit waiting for a render it did not need, and only then
            # spend its own third of a second. Started here it overlaps, and
            # on the large pages where the scan is slow the render is slow
            # too, so the answer is usually there when the page appears.
            self._color.update()

        # ── Continuous: the whole strip, not one page ─────────────────────────
        if self._continuous:
            scale = self._display_scale(self._zoom)
            if scale is not None:
                self._render_continuous(scale, avail_w, avail_h)
                return

        # ── Too big to render whole: render the window instead ────────────────
        scale = self._display_scale(self._zoom)
        if self._use_region_rendering(scale):
            self._show_region(src_path, orig, rot, scale, avail_w, avail_h)
            return
        self._leave_region_mode()

        # Whole page. A cached render answers straight away when it is at
        # least as fine as this zoom needs; otherwise one is started and
        # something stands in until it lands.
        if self._show_cached_page(src_path, orig, rot, avail_w, avail_h):
            return
        self._start_page_render(src_path, orig, rot, avail_w, avail_h)

    def _show_cached_page(self, src_path, orig, rot, avail_w, avail_h):
        """Put an already-rendered page on screen, if there is one good enough.

        Returns True when nothing more is needed — the cached render was made
        at this scale or finer, so shrinking it is the finished article.
        Returns False when there is nothing cached, or only something coarser
        that would have to be enlarged, which is _start_page_render's job.
        """
        # ── Something to look at now; the real render may follow ──────────────
        # The cached render is resized to this zoom so the page appears at once.
        # Whether that resize is the finished article or only a stand-in is
        # _good_enough()'s call: shrinking is, enlarging is not.
        cached = _FullPageCache.get(src_path, orig, rot, avail_w, avail_h)
        if cached is None:
            # The viewport size a page was rendered under can drift a few
            # pixels while the layout settles — or when the chrome changes —
            # and cross a 50 px cache-bucket boundary on the way. The exact
            # bucket then reads empty although the page itself is rendered,
            # one bucket over. Having that render land in the view as a
            # stand-in (and queueing the exact one if it is coarser) is far
            # better than missing turn it into a blank sheet.
            cached = _FullPageCache.get_any(src_path, orig, rot)
        if cached is not None:
            img, page_w_pt, page_h_pt, cached_scale, raw_chars = cached
            # Compute how much we need to scale the cached image for this zoom
            ratio = 1.0
            target_scale = cached_scale
            if page_w_pt > 0 and page_h_pt > 0 and cached_scale > 0:
                target_scale = _target_scale(avail_w, avail_h, self._zoom,
                                             page_w_pt, page_h_pt, cached_scale)
                ratio = target_scale / cached_scale
            final = _good_enough(cached_scale, target_scale)
            # Qt-scale the cached pixmap to the requested zoom (GUI thread — fast)
            if abs(ratio - 1.0) > _SCALE_EPS:
                # Exactly the pixels a render at target_scale would have made,
                # computed the same way rather than by multiplying out a ratio.
                # One pixel narrower is a page shown at the wrong physical size,
                # and this tool puts that size on the status bar.
                disp_w, disp_h = page_px_size(page_w_pt, page_h_pt,
                                              target_scale, 0)
                pm = QPixmap.fromImage(img).scaled(
                    max(1, disp_w), max(1, disp_h),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            else:
                pm = QPixmap.fromImage(img)
            # Clamp to the page, and settle a pending "start at the bottom"
            # from prev_page now that the height is known.
            self._place_scroll(pm.width(), pm.height(), avail_w, avail_h)
            off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
            off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)
            # raw_chars are image-relative: multiply by ratio then add display offset
            display_chars = [(ch, off_x + x*ratio, off_y + y*ratio,
                                  off_x + x2*ratio, off_y + y2*ratio)
                             for ch, x, y, x2, y2 in raw_chars]
            self._last_pm   = pm
            self._last_zoom = self._zoom
            self._last_pm_key = (src_path, orig, rot)
            self._page_w_pt = page_w_pt   # needed by _capped_display_size
            self._page_h_pt = page_h_pt
            self._showing_provisional = not final
            self._view.set_page(pm, display_chars, off_x, off_y)
            self._apply_zoom_labels(pm, page_w_pt, page_h_pt)
            QTimer.singleShot(0, self._color.update)
            # Enlarged from a coarser render? Then this is a stand-in and the
            # page has to be rendered properly. Shrunk from a finer one? Then it
            # is already as good as a render and nothing more is needed.
            if self._render_task is not None:
                self._render_task.cancel()
                self._render_task = None
            self._render_gen += 1
            if not final:
                # stand_in_shown: the scaled cache entry is already on screen,
                # put there a few lines up. The task would otherwise smooth-scale
                # the same multi-megapixel image again on the render thread and
                # emit an identical frame, delaying the render being waited for.
                task = _PageRenderTask(self._render_gen, src_path, orig, rot,
                                       avail_w, avail_h, self._zoom,
                                       self._render_signals, stand_in_shown=True)
                self._render_task = task
                _render_queue.submit(task, 0)   # P0: active page, preempts low-pri
            # Answered either way: with the finished image, or with a stand-in
            # and the exact render already on its way. Returning False here
            # would start a second render of the same page.
            return True

        return False

    def _start_page_render(self, src_path, orig, rot, avail_w, avail_h):
        """Render this page in the background, showing a stand-in meanwhile."""
        # ── Cache miss: show preview, submit background render ────────────────
        if self._render_task is not None:
            self._render_task.cancel()
            self._render_task = None

        self._render_gen += 1
        gen = self._render_gen

        if self._stand_in_is_current():
            # No settle timer: the exact render is submitted below, and _render()
            # *is* the settle. See _render_preview.
            self._render_preview(schedule_settle=False)
        else:
            # Nothing of *this* page to stretch — show a scaled-up thumbnail of
            # it for instant feedback. This branch used to be unreachable after
            # the first render of a session, because the test above was only
            # "is there a previous pixmap", and after a page turn there always
            # is: the previous page's.
            thumb_img = _ThumbnailCache.get_any(src_path, orig, rot)
            if thumb_img is not None:
                pm = QPixmap.fromImage(thumb_img).scaled(
                    max(1, avail_w - 16), max(1, avail_h - 16),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation)
                off_x = (avail_w - pm.width())  // 2
                off_y = (avail_h - pm.height()) // 2
                self._view.set_page(pm, [], off_x, off_y)
                self._showing_provisional = True
            else:
                # Nothing of this page to show yet. An empty sheet, not the page
                # before it: leaving the last render on screen is how a slow
                # document came to look like the same page over and over.
                self._show_empty_sheet(src_path, orig, rot, avail_w, avail_h)
                self._showing_provisional = True

        task = _PageRenderTask(gen, src_path, orig, rot,
                               avail_w, avail_h, self._zoom,
                               self._render_signals)
        self._render_task = task
        _render_queue.submit(task, 0)   # P0: active page

    def _apply_zoom_labels(self, pm, page_w_pt, page_h_pt):
        """Update the size and physical zoom % readings from a rendered QPixmap."""
        if page_w_pt > 0 and page_h_pt > 0:
            mm_w = page_w_pt / 72 * 25.4
            mm_h = page_h_pt / 72 * 25.4
            metrics = tr('Masse: {p0:.0f} × {p1:.0f} mm').format(p0=mm_w, p1=mm_h)
            self._metrics = metrics
            AppState.get().page_metrics_changed.emit(metrics)
            try:
                win = self.window().windowHandle()
                screen = (win.screen() if win and win.screen()
                          else QApplication.primaryScreen())
                phys_dpi = screen.physicalDotsPerInchX()
                dpr      = screen.devicePixelRatio()
                if phys_dpi < 50 or phys_dpi > 600:
                    phys_dpi = screen.logicalDotsPerInchX() * dpr
                displayed_w_in = pm.width() * dpr / phys_dpi
                actual_w_in    = page_w_pt / 72.0
                phys_pct       = round(displayed_w_in / actual_w_in * 100)
                AppState.get().zoom_changed.emit(phys_pct)
                self._phys_pct  = phys_pct
                self._phys_base = phys_pct
            except Exception:
                logging.debug("single_page: could not compute the physical "
                              "zoom percentage", exc_info=True)
                AppState.get().zoom_changed.emit(int(self._zoom * 100))
        else:
            AppState.get().zoom_changed.emit(int(self._zoom * 100))

    def _on_page_ready(self, gen, image, off_x, off_y,
                       page_w_pt, page_h_pt, scale, raw_chars,
                       provisional=False):
        """Called on the GUI thread when a background render finishes.
        raw_chars are image-relative coords (no centering offset, no scroll).

        A provisional image is a stand-in scaled from another zoom: show it, but
        leave the task in place, because the exact render is still coming and
        must be what the user is left with.
        """
        if gen != self._render_gen:
            return   # stale – a newer render is already in flight

        pm = QPixmap.fromImage(image)

        # Compute scroll-adjusted display offsets. Place the scroll first, so
        # a pending "start at the bottom" from prev_page resolves against the
        # height this render just established.
        avail_w = self._view.width()
        avail_h = self._view.height()

        # The dimensions this render carries have to be recorded *before* the
        # scroll is placed, because placing it needs to know how big the page
        # really is at this zoom — which is not necessarily how big the pixmap
        # is. Past MAX_RENDER_PX a whole-page render comes back clamped, and a
        # clamped bitmap is a stand-in for a page that is larger than it.
        self._page_w_pt = page_w_pt
        self._page_h_pt = page_h_pt

        # A reveal asked for before anything had measured this page can be
        # resolved now that it has been.
        if self._want_reveal is not None:
            self._apply_reveal()

        # Clamp against the page, not against the picture of it. Honouring a
        # pending "start at the bottom" against a clamped render left the view
        # 81 px short of the bottom of a 4,081 px page — and the intent is
        # consumed by that placement, so the window render that followed a
        # moment later never corrected it. Only the wheel could, one click at a
        # time, which is precisely the class of bug _place_scroll exists to end.
        scale = self._display_scale(self._zoom)
        if scale is not None:
            true_w, true_h = self._page_px(scale)
        else:
            true_w, true_h = pm.width(), pm.height()
        self._place_scroll(true_w, true_h, avail_w, avail_h)
        off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
        off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)

        # raw_chars: image-relative → add scroll-adjusted centering offset
        display_chars = [(ch, off_x + x, off_y + y, off_x + x2, off_y + y2)
                         for ch, x, y, x2, y2 in raw_chars]

        self._last_pm   = pm
        self._last_zoom = self._zoom
        self._last_pm_key = self._current_page_key()
        # _page_w_pt/_page_h_pt were set above, before the scroll was placed.
        self._showing_provisional = provisional
        if not provisional:
            self._render_task = None
        self._view.set_page(pm, display_chars, off_x, off_y)
        self._apply_zoom_labels(pm, page_w_pt, page_h_pt)

        # The dimensions have only just arrived, and they may say this page is
        # too big to be shown in one bitmap at this zoom — which is exactly the
        # case _render() could not judge before the render, on a page nothing
        # had measured yet. What it produced is the whole sheet clamped to
        # MAX_RENDER_PX, i.e. not the zoom asked for, so ask again now that the
        # answer is knowable. _show_region emits through a different signal, so
        # this cannot come back round a second time.
        if (not provisional and page_w_pt > 0
                and self._use_region_rendering(self._display_scale(self._zoom))):
            QTimer.singleShot(0, self._render)

        QTimer.singleShot(0, self._color.update)

    def _color_source(self):
        """(src_path, orig) of the page on screen, for the colour label."""
        if not self.model or not self.pdf_path:
            return None
        uid = self.model.order[self._current]
        return self.model.page_source(uid, self.pdf_path)

    def next_page(self):
        if self._continuous:
            if self.model and self._current < len(self.model.order) - 1:
                self.go_to(self._current + 2)
            return
        if self.model and self._current < len(self.model.order) - 1:
            self._current += 1
            self._scroll_x  = 0.0
            self._scroll_y  = 0.0
            self._want_bottom = False
            self._page_w_pt = 0.0   # clear stale dims so wheel uses only fresh renders
            self._page_h_pt = 0.0
            self._render()

    def prev_page(self, start_at_bottom=False):
        if self._continuous:
            if self._current > 0:
                self.go_to(self._current)
            return
        if self._current > 0:
            self._current -= 1
            self._scroll_x  = 0.0
            self._page_w_pt = 0.0
            self._page_h_pt = 0.0
            # Recorded as intent, not as a coordinate: _place_scroll puts the
            # view at the bottom once something knows how tall the page is.
            self._scroll_y = 0.0
            self._want_bottom = bool(start_at_bottom)
            self._render()

    def go_to(self, page_1based):
        if self._continuous:
            n = len(self.model.order) if self.model else 0
            if n <= 0:
                return
            self._current = max(0, min(page_1based - 1, n - 1))
            scale = self._display_scale(self._zoom)
            if scale is not None and not self._strip:
                self._build_strip(scale)
            if self._strip:
                self._jump_scroll(self._strip_top_of(self._current))
            self._scroll_x = 0.0
            self._render()
            return
        self._current  = max(0, page_1based - 1)
        self._scroll_x  = 0.0
        self._scroll_y  = 0.0
        self._want_bottom = False
        self._page_w_pt = 0.0
        self._page_h_pt = 0.0
        self._render()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(80, self._render)

    def keyPressEvent(self, e):
        k    = e.key()
        ctrl  = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if ctrl:
            # ── Zoom (Acrobat: Ctrl++/Ctrl+-, Ctrl+0=fit, Ctrl+1=100%) ──────
            if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._zoom_in(); return
            if k == Qt.Key.Key_Minus:
                self._zoom_out(); return
            if k == Qt.Key.Key_0:
                self._zoom_fit(); return
            if k == Qt.Key.Key_1:
                self._zoom_actual_size(); return
            # ── Rulers (Acrobat: Ctrl+R) ─────────────────────────────────────
            if k == Qt.Key.Key_R:
                self.toggle_rulers(); return
            # ── Navigation ───────────────────────────────────────────────────
            if k == Qt.Key.Key_Home:
                self.go_to(1); return
            if k == Qt.Key.Key_End:
                if self.model: self.go_to(len(self.model.order)); return
            # ── Go to page (Acrobat: Ctrl+Shift+N or Ctrl+G) ─────────────────
            if k == Qt.Key.Key_G or (shift and k == Qt.Key.Key_N):
                self._go_to_dialog(); return
            # ── Print ─────────────────────────────────────────────────────────
            if k == Qt.Key.Key_P:
                parent = owning_tab(self.parent())
                if parent:
                    parent._print()
                return
            super().keyPressEvent(e)
            return

        # ── Page navigation (no modifier) ────────────────────────────────────
        if k in (Qt.Key.Key_Right, Qt.Key.Key_Down,
                 Qt.Key.Key_PageDown, Qt.Key.Key_Space, Qt.Key.Key_Return,
                 Qt.Key.Key_Enter):
            self.next_page()
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp,
                   Qt.Key.Key_Backspace):
            self.prev_page()
        elif k == Qt.Key.Key_Home:
            self.go_to(1)
        elif k == Qt.Key.Key_End:
            if self.model: self.go_to(len(self.model.order))
        else:
            super().keyPressEvent(e)

    def _sync_track_span(self):
        """How much of the document one screenful is, so the thumb is a
        proportion rather than a fixed block. Paged mode always shows exactly
        one page, so the span is 1 there and this only matters continuously."""
        if not (self._continuous and self._strip_h > 0):
            self._track._span = 1
            return
        n = max(1, len(self._strip))
        visible = float(self._view.height()) / max(1.0, self._strip_h) * n
        self._track._span = max(1, min(n, int(round(visible))))

    def _on_track_picked(self, page):
        """The rail was clicked in paged mode: jump to that page."""
        self._rail_handler().rail_go_to(page)

    def _on_track_dragged(self, frac):
        """The rail was dragged: scroll to that fraction of the view."""
        self._rail_handler().rail_drag_to(frac)

    # ── the rail as a shared scrollbar ───────────────────────────────────────
    #
    # The page manager drives the same rail while it is showing (PdfTab hands
    # over a delegate), so every rail action goes through the delegate when
    # there is one and lands here when there is not. The same split applies to
    # what the rail *shows*: while a delegate owns it, this view stops pushing
    # position updates to it (_nav_show).

    def _rail_handler(self):
        return self.rail_delegate if self.rail_delegate is not None else self

    def _rail_prev(self):
        self._rail_handler().rail_prev()

    def _rail_next(self):
        self._rail_handler().rail_next()

    def _rail_prompt_goto(self):
        self._rail_handler().rail_prompt_goto()

    def rail_prev(self):
        self.prev_page()

    def rail_next(self):
        self.next_page()

    def rail_go_to(self, page):
        if self.model and page != self._current + 1:
            self.go_to(page)

    def rail_drag_to(self, frac):
        """The rail was dragged in continuous mode: scroll to that fraction of
        the strip, following the pointer directly — a drag that jumps a page at
        a time reads as the rail moving in chunks."""
        if not self._continuous:
            return
        self._jump_scroll(frac * self._max_scroll())
        scale = self._display_scale(self._zoom)
        if scale is not None:
            self._render_continuous(scale, self._view.width(),
                                    self._view.height())

    def rail_prompt_goto(self):
        self._go_to_dialog()

    def take_nav_rail(self):
        """Hand the navigation rail to the caller's layout.

        The page manager shares the rail (see PdfTab), so it has to live
        outside this view's own layout while staying wired to it — signals do
        not care about parentage, only the widget tree does. The returned
        column forwards wheel events to whichever view the rail currently
        drives.
        """
        col = _NavRailColumn(self)
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._nav_side)
        return col

    def nav_set_document(self, n_pages, page):
        """Rail position from outside (the page manager's bridge)."""
        self._track.set_document(n_pages, page)

    def nav_set_fraction(self, frac):
        """Rail thumb as a 0..1 fraction of the view, from outside."""
        self._track.set_scroll_position(frac)

    def nav_scroll_mode(self, on):
        self._track.set_scroll_mode(on)

    def _nav_show(self, n_pages, page, frac=None):
        """Push position state to the rail — unless a delegate (the page
        manager) currently owns it and decides what it shows."""
        if self.rail_delegate is not None:
            return
        self._sync_track_span()
        self._track.set_document(n_pages, page)
        if frac is not None:
            self._track.set_scroll_position(frac)

    def _go_to_dialog(self):
        """Ctrl+G / Ctrl+Shift+N: Go-to-page input (like Acrobat)."""
        if not self.model:
            return
        from PyQt6.QtWidgets import QInputDialog
        n = len(self.model.order)
        page, ok = QInputDialog.getInt(
            self, tr("Gehe zu Seite"), tr('Seite (1 – {p0}):').format(p0=n),
            self._current + 1, 1, n)
        if ok:
            self.go_to(page)
