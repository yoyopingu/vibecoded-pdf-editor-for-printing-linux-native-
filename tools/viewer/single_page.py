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
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRect
from PyQt6.QtGui import QPixmap
from tools.colorspace import (cached_page_colorspaces, describe,
                              document_revision, page_colorspaces,
                              scan_document)
from tools.i18n import tr
from tools.render.caches import _FullPageCache, _ThumbnailCache
from tools.render.images import MAX_RENDER_PX, _SCALE_EPS, _good_enough
from tools.render.queue import _PageRenderTask, _PageSignals, _RegionRenderTask, _RegionSignals, prerender_enabled, _render_queue, _target_scale
from tools.render.region import cached_page_size_pt, covers, page_px_size, region_for_viewport, snap_scale
from tools.viewer.canvas import PdfPageCanvas
from tools.viewer.rulers import RulerBar, RulerCorner
from tools.viewer.tab_base import owning_tab
from tools.theme import _PREV_BTN, _TV, _register_themed


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
        # Colour-space label: the page the label is out for (None when it
        # already says the right thing), the job carrying that answer, and the
        # file revisions whose pages have all been read.
        self._cs_pending  = None
        self._cs_job      = None
        self._cs_scan_job = None
        self._cs_scanned: set = set()
        # Which page and rotation _page_w_pt/_page_h_pt currently describe
        self._dims_key = None
        self._dims_rot = None
        self._render_signals = _PageSignals()
        self._render_signals.ready.connect(self._on_page_ready)
        # Pre-render (background warm-up) state
        self._prerender_tasks: list = []
        # Guides, per page, in points from the sheet's top-left corner. Page
        # coordinates rather than pixels so a guide 20 mm into the sheet stays
        # 20 mm into the sheet through a zoom, a scroll and a page turn.
        self._guides: dict = {}
        self._rulers_on = False
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
        self._view.repainted.connect(self._sync_rulers)

        # Rechte Seitenleiste (Navigation)
        self._nav_side = QWidget()
        self._nav_side.setObjectName("navSide")
        self._nav_side.setFixedWidth(50)
        sl = QVBoxLayout(self._nav_side)
        sl.setContentsMargins(4, 10, 4, 10)
        sl.setSpacing(4)

        self._num_lbl = QLabel("1")
        self._num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self._num_lbl)

        self._nav_sep = QFrame()
        self._nav_sep.setFrameShape(QFrame.Shape.HLine)
        sl.addWidget(self._nav_sep)

        self._tot_lbl = QLabel("/ 0")
        self._tot_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self._tot_lbl)
        sl.addStretch()

        # Same metrics as the zoom cluster below — the two sets of controls sit
        # in the same corner of the preview and used to be different sizes.
        self._nav_btns = []
        for text, fn in [("▲", self.prev_page), ("▼", self.next_page)]:
            b = QPushButton(text)
            b.setFixedSize(*_PREV_BTN)
            b.clicked.connect(fn)
            sl.addWidget(b, alignment=Qt.AlignmentFlag.AlignCenter)
            self._nav_btns.append(b)

        main.addWidget(self._nav_side)
        layout.addLayout(main, 1)

        # Untere Info-Leiste
        self._info_bar = QWidget()
        self._info_bar.setObjectName("infoBar")
        self._info_bar.setFixedHeight(30)
        il = QHBoxLayout(self._info_bar)
        il.setContentsMargins(12, 0, 12, 0)
        il.setSpacing(20)

        self._size_lbl = QLabel(tr("Masse: —"))
        self._size_lbl.setObjectName("dimLabel")
        il.addWidget(self._size_lbl)

        self._color_lbl = QLabel(tr("Farbprofil: —"))
        self._color_lbl.setObjectName("dimLabel")
        il.addWidget(self._color_lbl)

        il.addStretch()

        # Lineale — Strg+R schaltet sie ebenfalls um, aber ein Kuerzel allein
        # findet niemand, der nicht weiss, dass es die Lineale gibt.
        self._ruler_btn = QPushButton("⊞")
        self._ruler_btn.setCheckable(True)
        self._ruler_btn.setFixedSize(*_PREV_BTN)
        self._ruler_btn.setToolTip(tr("Lineale und Hilfslinien") + "  (Strg+R)")
        self._ruler_btn.clicked.connect(lambda on: self._set_rulers_visible(on))
        il.addWidget(self._ruler_btn)

        # Zoom-Steuerung
        self._zoom_btns = []
        for txt, fn in [("−", self._zoom_out), ("fit", self._zoom_fit), ("+", self._zoom_in)]:
            zb = QPushButton(txt)
            zb.setFixedSize(*_PREV_BTN)
            zb.clicked.connect(fn)
            il.addWidget(zb)
            self._zoom_btns.append(zb)

        self._zoom_lbl = QLabel("100%")
        self._zoom_lbl.setObjectName("dimLabel")
        self._zoom_lbl.setFixedWidth(42)
        il.addWidget(self._zoom_lbl)

        layout.addWidget(self._info_bar)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._apply_theme()

    def _apply_theme(self):
        t = _TV
        self._view.setStyleSheet(f"background:{t['viewer_bg']};")
        self._nav_side.setStyleSheet(
            f"QWidget#navSide{{background:{t['sidebar_bg']};border-left:1px solid {t['border']};}}")
        self._num_lbl.setStyleSheet(
            f"color:{t['text']};font-size:16px;font-weight:bold;background:transparent;")
        self._nav_sep.setStyleSheet(f"color:{t['border']};")
        self._tot_lbl.setStyleSheet(
            f"color:{t['dim']};font-size:11px;background:transparent;")
        _nb = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:5px;font-size:12px;}}"
               f"QPushButton:hover{{background:{t['hover']};border-color:{t['acc']};}}")
        for b in self._nav_btns:
            b.setStyleSheet(_nb)
        self._info_bar.setStyleSheet(
            f"QWidget#infoBar{{background:{t['sidebar_bg']};border-top:1px solid {t['border']};}}")
        _zb = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:5px;font-size:12px;padding:0;}}"
               f"QPushButton:hover{{background:{t['hover']};border-color:{t['acc']};}}")
        for zb in self._zoom_btns:
            zb.setStyleSheet(_zb)

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
        if self._page_w_pt <= 0 or self._page_h_pt <= 0:
            return None
        avail_w = self._view.width()
        avail_h = self._view.height()
        if avail_w < 16 or avail_h < 16:
            return None
        pad = 16
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

        if self._last_pm is None or self._last_zoom <= 0 or old_zoom <= 0:
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
        self._zoom     = 1.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        self._render()

    def _zoom_actual_size(self):
        """Zoom so the page appears at its true physical size (100% = 1pt = 1/72 in)."""
        try:
            win    = self.window().windowHandle()
            screen = (win.screen() if win and win.screen()
                      else QApplication.primaryScreen())
            phys_dpi = screen.physicalDotsPerInchX()
            dpr      = screen.devicePixelRatio()
            if phys_dpi < 50 or phys_dpi > 600:
                phys_dpi = screen.logicalDotsPerInchX() * dpr
            actual_scale = phys_dpi / 72.0          # px per PDF point at physical size
            avail_w = self._view.width()
            avail_h = self._view.height()
            if self._page_w_pt > 0 and avail_w > 16 and avail_h > 16:
                pad       = 16
                fit_scale = min((avail_w - pad) / self._page_w_pt,
                                (avail_h - pad) / self._page_h_pt)
                self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, actual_scale / fit_scale)) if fit_scale > 0 else 1.0
            else:
                self._zoom = 1.0
        except Exception:
            logging.debug("single_page: could not read the screen's physical "
                          "DPI", exc_info=True)
            self._zoom = 1.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
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
        # Ctrl+R and the button are two ways to the same switch; the button has
        # to show the state even when the shortcut was what changed it.
        if self._ruler_btn.isChecked() != self._rulers_on:
            self._ruler_btn.setChecked(self._rulers_on)
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

        The one conversion between a place on the page and a place on screen.
        Everything about guides and rulers goes through it, so they cannot
        drift apart from the page they are measuring.
        """
        scale = self._display_scale(self._zoom)
        if scale is None or self._page_w_pt <= 0:
            return None
        page_px_w, page_px_h = self._page_px(scale)
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
        QTimer.singleShot(0, self._update_color_label)

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
        self._zoom_lbl.setText(f"{int(self._zoom * 100)}%")

    def load(self, pdf_path, model):
        # Cancel any in-flight pre-render tasks from previous file
        for t in self._prerender_tasks:
            t.cancel()
        self._prerender_tasks.clear()

        self.pdf_path  = pdf_path
        self.model     = model
        self._current  = 0
        self._page_w_pt = 0.0
        self._page_h_pt = 0.0
        self._scroll_x = 0.0
        self._scroll_y = 0.0
        n = len(model.order)
        self._tot_lbl.setText(f"/ {n}")
        self._prerender_aim = None   # a new file: re-aim even at the same index
        QTimer.singleShot(0, self._render)
        # Give the canvas focus so arrow keys work without needing a click first
        QTimer.singleShot(0, self._view.setFocus)

    def stop_background_work(self):
        """Cancel every render this view has outstanding and stop it asking for
        more. Called when the tab closes; safe to call twice."""
        for timer in (self._zoom_timer, self._size_retry_timer,
                      self._prerender_timer):
            try: timer.stop()
            except RuntimeError: pass   # C++ timer already destroyed
        for task in list(self._prerender_tasks):
            try: task.cancel()
            except Exception: pass   # a task past its checkpoint stops on its own
        self._prerender_tasks.clear()
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
        """
        if not prerender_enabled():
            return
        if not self.pdf_path or not self.model:
            return
        # A zoom or scroll is still in flight. Speculative work belongs after
        # the gesture, not in the middle of it: the page the user is actually
        # looking at is about to be rendered, and a pre-render started now holds
        # the render thread when that happens.
        if self._zoom_timer.isActive():
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
            uid = order[pos]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            rot            = self.model.get_rotation(uid)
            if _FullPageCache.get(src_path, orig, rot, avail_w, avail_h) is None:
                task = _PageRenderTask(0, src_path, orig, rot,
                                       avail_w, avail_h, 1.0,
                                       signals=None)
                self._prerender_tasks.append(task)
                _render_queue.submit(task, 2)   # lowest priority: pre-render

    def refresh(self):
        if self.model:
            n = len(self.model.order)
            self._tot_lbl.setText(f"/ {n}")
            self._current = min(self._current, max(0, n-1))
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
        self.page_changed.emit(self._current + 1)

        # The page's dimensions have to be right *before* anything is computed
        # from them. They used to arrive only with a finished render, so a page
        # that had just been rotated — or not yet rendered at all — was measured
        # as it used to be, and at deep zoom that misplaces the whole window.
        self._ensure_page_dims(src_path, orig, rot)

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
            self._update_color_label()

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
            QTimer.singleShot(0, self._update_color_label)
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
        """Update size + physical zoom % labels from a rendered QPixmap."""
        if page_w_pt > 0 and page_h_pt > 0:
            mm_w = page_w_pt / 72 * 25.4
            mm_h = page_h_pt / 72 * 25.4
            self._size_lbl.setText(tr('Masse: {p0:.0f} × {p1:.0f} mm').format(p0=mm_w, p1=mm_h))
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
                self._zoom_lbl.setText(f"{phys_pct}%")
                self._phys_pct  = phys_pct
                self._phys_base = phys_pct
            except Exception:
                logging.debug("single_page: could not compute the physical "
                              "zoom percentage", exc_info=True)
                self._zoom_lbl.setText(f"{int(self._zoom * 100)}%")
        else:
            self._zoom_lbl.setText(f"{int(self._zoom * 100)}%")

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
        self._place_scroll(pm.width(), pm.height(), avail_w, avail_h)
        off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
        off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)

        # raw_chars: image-relative → add scroll-adjusted centering offset
        display_chars = [(ch, off_x + x, off_y + y, off_x + x2, off_y + y2)
                         for ch, x, y, x2, y2 in raw_chars]

        self._last_pm   = pm
        self._last_zoom = self._zoom
        self._last_pm_key = self._current_page_key()
        self._page_w_pt = page_w_pt   # needed by _capped_display_size
        self._page_h_pt = page_h_pt
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

        QTimer.singleShot(0, self._update_color_label)

    def _update_color_label(self):
        """Say which colour spaces the page uses, without stalling to find out.

        Reading them means walking every content stream on the page — half a
        second on a large one, and this runs after every render, on the GUI
        thread. So: answer at once when it is already known, and otherwise ask
        in the background and fill the label in when the answer arrives.
        """
        try:
            if not self.model or not self.pdf_path:
                return
            uid = self.model.order[self._current]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
        except Exception:
            self._color_lbl.setText(tr("Farbprofil: —"))
            return

        # Read the whole file once, in the background, rather than a page at a
        # time as the user turns them. Cheap to ask for again — it is a stat
        # and two comparisons once the scan has been started.
        self._scan_colour_spaces(src_path)

        known = cached_page_colorspaces(src_path, orig)
        if known is not None:
            # Nothing is outstanding, and the label now describes this page.
            self._cs_pending = None
            self._color_lbl.setText(
                tr('Farbprofil: {p0}').format(p0=describe(known)))
            return

        # The page this answer will belong to. By the time it comes back the
        # user may have turned to another one, and a label from the page before
        # is worse than no label.
        want = (src_path, orig)
        if self._cs_pending == want:
            # Already asked for this page. Either the answer is still coming or
            # it came back unreadable — which page_colorspaces does not cache,
            # since "could not be read" is not an answer to remember — and
            # either way this runs after every render, so asking again would
            # mean re-reading an unreadable page for as long as it is on screen.
            return
        # Whatever was asked for the page before this one, nobody wants any
        # more. Left running, a turn through a long document queued one full
        # read of the file per page onto a pool with as many slots as there are
        # cores — so the answer for the page actually on screen waited behind
        # every page passed on the way to it, and the renders waited with it.
        self._cancel_colour_job()
        self._cs_pending = want
        self._color_lbl.setText(tr("Farbprofil: …"))
        from tools.jobs import submit
        job = submit(lambda job: page_colorspaces(src_path, orig),
                     owner=self, name="colorspace",
                     on_done=lambda names: self._apply_color_label(want, names))
        # Cleared on `finished` rather than on `done`, so a job that fails or is
        # cancelled releases the slot too — otherwise one such job would leave
        # the label convinced an answer was still coming and stop asking.
        job.signals.finished.connect(lambda j=job: self._colour_job_finished(j))
        self._cs_job = job

    def _cancel_colour_job(self):
        if self._cs_job is not None:
            self._cs_job.cancel()
            self._cs_job = None

    def _colour_job_finished(self, job):
        if self._cs_job is job:         # not one already replaced by a newer ask
            self._cs_job = None

    def _apply_color_label(self, want, names):
        if self._cs_pending != want:
            return                      # the user has moved on
        try:
            self._color_lbl.setText(
                tr('Farbprofil: {p0}').format(p0=describe(names)))
        except RuntimeError:
            pass                        # the view is being torn down

    def _scan_colour_spaces(self, src_path):
        """Read every page's colour spaces once, so page turns are free.

        The label used to read one page per turn, and reading a page means
        opening the file: 83 ms of cross-reference parsing against 0.36 ms to
        answer, on a 500-page document — and the 83 ms is the part that grows
        with the page count. So a long file was slow to browse precisely
        because it was long, and the whole document costs less than three of
        those turns (see tools.colorspace.scan_document).

        Once per file revision, and abandoned rather than finished if the user
        closes the tab: the pages read before that are kept, since they cost
        nothing to have.
        """
        revision = document_revision(src_path)
        if revision in self._cs_scanned or self._cs_scan_job is not None:
            return
        from tools.jobs import submit

        def scan(job):
            return scan_document(src_path, should_stop=lambda: job.cancelled)

        # Behind the page renders and behind the label's own page: this is work
        # for the turns to come, and nothing on screen is waiting for it.
        job = submit(scan, owner=self, name="colorspace-document", priority=-1,
                     on_done=lambda result: self._colour_scan_done(revision, result))
        job.signals.finished.connect(lambda j=job: self._colour_scan_finished(j))
        self._cs_scan_job = job

    def _colour_scan_finished(self, job):
        if self._cs_scan_job is job:
            self._cs_scan_job = None

    def _colour_scan_done(self, revision, result):
        _names, complete = result
        if complete:
            # A set, not one revision: a merged tab draws its pages from
            # several files, and remembering only the last would have the two
            # re-scanning each other away every time the user crossed the join.
            self._cs_scanned.add(revision)


    def next_page(self):
        if self.model and self._current < len(self.model.order) - 1:
            self._current += 1
            self._scroll_x  = 0.0
            self._scroll_y  = 0.0
            self._want_bottom = False
            self._page_w_pt = 0.0   # clear stale dims so wheel uses only fresh renders
            self._page_h_pt = 0.0
            self._render()

    def prev_page(self, start_at_bottom=False):
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
