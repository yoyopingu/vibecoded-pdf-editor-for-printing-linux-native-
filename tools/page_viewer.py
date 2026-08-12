"""
Page Viewer v3.7
=================
- Einzelseite passt auf Fensterbreite
- Seitenweises Springen (kein fluessiges Scrollen)
- Shortcuts via QApplication eventFilter (funktionieren immer)
- Seiten-Verwaltung unveraendert
- Textauswahl + Kopieren (Strg+C)
"""
import os, io, math, threading, heapq, atexit, logging
from collections import OrderedDict
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTabWidget, QFrame, QFileDialog, QApplication, QMenu,
    QScrollArea, QSizePolicy, QStackedWidget, QSplitter,
    QDialog, QSpinBox, QLineEdit, QCheckBox,
    QListWidget, QListWidgetItem, QAbstractItemView
)
from PyQt6.QtCore import (Qt, QSize, pyqtSignal, QMimeData, QObject, QEvent,
                           QTimer, QRect, QRectF, QPoint)
from PyQt6.QtGui import (
    QPixmap, QImage, QColor, QDrag, QPainter, QPen, QIcon,
    QKeySequence, QShortcut, QTransform, QBrush, QCursor, QPageLayout
)
from tools.app_state import AppState
from tools.i18n      import tr

# ── Moved out of this file; re-exported so existing imports keep working ──
from tools.viewer.model import _positions_to_str, _parse_positions, PageModel
from tools.viewer.theme import _DARK_TV, _LIGHT_TV, _TV, _TOP_BTN_W, _PREV_BTN, _DROP_THICKNESS, _DROP_HALO, _paint_drop_marker, _theme_panels, _register_themed, set_viewer_theme
from tools.render.queue import _thumb_render_width, _ThumbSignals, _ThumbTask, _RenderQueue, _render_queue, shutdown_render_queue, _PageSignals, _RegionSignals, _RegionRenderTask, _target_scale, _PageRenderTask, _prerender_enabled, apply_performance_settings
from tools.render.images import MAX_RENDER_PX, _SCALE_EPS, _good_enough, pil_to_qpixmap, _render_image, render_page, _rotate_char_boxes
from tools.render.caches import _active_path, _active_page, _set_active, _priority_evict, _ThumbnailCache, _FullPageCache

CARD_W = 110
CARD_H = 155
GAP    = 10
MARGIN = 12


# Two scales count as the same when they differ by less than this. Below it,
# re-rendering buys nothing the eye can see.



# How far the user may zoom in. Was 8x, which existed because the page was
# rendered as one bitmap and MAX_RENDER_PX clamped the result — past roughly
# 5x on A4 the picture stopped getting sharper, so more zoom would have been a
# lie. Window rendering costs the same at any zoom, so the limit is now just a
# question of what is useful.
MAX_ZOOM = 40.0
MIN_ZOOM = 0.1






# ── Global pypdfium2 serialisation lock ──────────────────────────────────────
# libpdfium is not thread-safe, and not only per document: two threads
# rendering two *different* documents corrupt the heap (measurements in
# tools/render/document_cache.py). Every pdfium call in the process is
# serialised through this one lock.
#
# It now lives in the document cache and is re-exported here, so that documents
# opened ad hoc — by the tools, the print path — and the cached ones the viewer
# renders from are mutually exclusive with each other. Imported eagerly on
# purpose: the cache module itself pulls in nothing heavier than the standard
# library, and defers importing pypdfium2 exactly as this module does.
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock




# ── Thread-safe image rendering ───────────────────────────────────────────────
# Returns QImage (NOT QPixmap) so it is safe to call from any thread.
# QPixmap must only be created on the GUI thread.



# Keep the old helper for places that need QPixmap on the main thread.


# ── Active-tab priority state (updated on tab/page change) ───────────────────
# Eviction order: other-tab entries first → current-tab non-current-page → current page (never)






# ── Module-level LRU thumbnail cache (thread-safe) ───────────────────────────



# ── Module-level LRU full-page cache (thread-safe) ───────────────────────────



# ── Background thumbnail rendering ───────────────────────────────────────────







# ── Single-thread priority render queue ─────────────────────────────────────
# The one place that does NOT use tools/jobs.py, on purpose. Three things it
# needs that a QThreadPool does not give:
#
#   * Exactly one worker. pdfium is serialised by PDFIUM_LOCK anyway (see
#     tools/render/document_cache.py — two threads rendering two *different*
#     documents corrupt the heap), so extra pool threads would only queue on
#     that lock while holding a thread and a part-built bitmap each. A pool of
#     one is expressible, but then the pool buys nothing and costs the two
#     behaviours below.
#   * Preemption of the *running* task. When the user turns a page, the render
#     already in flight for a thumbnail is cancelled mid-flight so the page they
#     are looking at is not stuck behind it. QThreadPool has no notion of the
#     currently-running runnable; this queue tracks it (self._running) and
#     cancels it.
#   * Exact removal of queued work by priority. cancel_queued() drops every
#     pending pre-render in one pass. QThreadPool::tryTake needs a handle on
#     each runnable and races with the worker picking one up; a heap the queue
#     owns does not.
#
# Rebuilding those three on top of a pool would be more code than this loop, so
# the loop stays. What it shares with jobs.py is what actually matters: the
# thread is owned by a module-level singleton, every task has cancel(), and
# shutdown() cancels and joins.
#
# A single thread with a priority heap gives better ordering at lower overhead:
#   priority 0 = active page (user is watching)
#   priority 1 = visible thumbnails
#   priority 2 = background pre-renders
# A new P0 task preempts whatever is running by cancelling it so the user
# never waits behind a thumbnail or a pre-render.





def _gs_blacked_out(before, after, budget=60):
    """Page indexes that went from paper-white to near-black during Ghostscript's
    colour conversion for printing.

    Deliberately coarser than the page-by-page check the Grayscale and CMYK tools
    use: this step also scales, fits and re-centres, so content legitimately
    moves and a per-pixel comparison would flag healthy pages. Comparing mean
    brightness is immune to that and still catches the case that matters — a page
    that was mostly white coming back mostly black. Large documents are sampled,
    because this runs before every print and must not add noticeable delay.

    Returns [] when nothing is wrong and None if it could not be checked."""
    import pypdfium2 as pdfium
    try:
        with _pdfium_lock:
            a = pdfium.PdfDocument(before); b = pdfium.PdfDocument(after)
            try:
                n = min(len(a), len(b))
                if not n:
                    return None
                step = max(1, n // budget)
                bad = []
                for i in range(0, n, step):
                    pa = a[i].render(scale=0.12).to_pil().convert("L")
                    pb = b[i].render(scale=0.12).to_pil().convert("L")
                    ma = sum(pa.get_flattened_data()) / (pa.size[0] * pa.size[1] or 1)
                    mb = sum(pb.get_flattened_data()) / (pb.size[0] * pb.size[1] or 1)
                    if ma > 200 and mb < 80:
                        bad.append(i)
                return bad
            finally:
                a.close(); b.close()
    except Exception:
        logging.exception("print: could not verify the colour conversion")
        return None




atexit.register(shutdown_render_queue)


# ── Background single-page rendering ─────────────────────────────────────────

    # generation, image, off_x, off_y, page_w_pt, page_h_pt, scale, chars,
    # provisional — True means this is a stand-in scaled from a render made at
    # some other zoom, and the real one at this zoom is still coming.


    # generation, image, px0, py0, scale, chars
    # px0/py0 locate the image in displayed page-pixel space; chars are in the
    # same space, relative to the page's top-left corner.










# ── Runtime performance controls ─────────────────────────────────────────────





# ── Theme colours (updated by set_viewer_theme) ──────────────────────────────



# Shared width for the viewer's top-bar buttons, so "Öffnen", "Seiten verwalten"
# and "Drucken" line up as one set instead of three different sizes.

# Shared size for the small square controls around the preview (page ▲▼ and the
# zoom cluster). They were 34×26 and 28×22 — near-identical but not quite, which
# is exactly the kind of mismatch that reads as sloppy.

# ── Drop marker ──────────────────────────────────────────────────────────────
# Where a dragged page or file will land. Drawn as a slim rounded blue slot the
# size of a page card's silhouette, with a soft halo behind it — it reads as the
# outline of the pages being carried sliding into the gap. It used to be a line
# with arrowheads barbed onto both ends, which looked like a crooked arrow
# rather than a page.



import weakref as _weakref






# ── PDF-Seiten-Canvas mit Textauswahl ────────────────────────────────────────

class PdfPageCanvas(QWidget):
    """
    Zeigt eine PDF-Seite als Pixmap und ermöglicht
    Textauswahl per Maus sowie Kopieren mit Strg+C.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap     = None
        self._chars      = []   # Liste von (char, x0, y0, x1, y1) in Widget-Koordinaten
        self._sel_start  = -1
        self._sel_end    = -1
        self._dragging   = False
        self._offset_x   = 0
        self._offset_y   = 0
        self._page_rect  = None   # sheet outline when the pixmap is a region

        self.setStyleSheet(f"background:{_TV['viewer_bg']};")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(QCursor(Qt.CursorShape.IBeamCursor))

    def set_page(self, pixmap, chars, offset_x, offset_y, page_rect=None):
        """
        pixmap   — gerendertes QPixmap
        chars    — [(char, x0, y0, x1, y1), …] in Widget-Pixelkoordinaten
        offset_x/y — wo die Pixmap innerhalb des Widgets platziert wird
        page_rect — (x, y, w, h) of the whole sheet in widget coords, when the
                    pixmap is only the visible part of a page too large to
                    render in one piece. The outline is drawn around this
                    instead of around the pixmap, which would otherwise put a
                    hairline through the middle of the page.
        """
        self._pixmap    = pixmap
        self._chars     = chars
        self._sel_start = -1
        self._sel_end   = -1
        self._offset_x  = offset_x
        self._offset_y  = offset_y
        self._page_rect = page_rect
        self.update()

    def clear(self):
        self._pixmap = None
        self._page_rect = None
        self._chars  = []
        self._sel_start = self._sel_end = -1
        self.update()

    # ── Maus-Events ──────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            idx = self._char_at(e.position().toPoint())
            self._sel_start = idx
            self._sel_end   = idx
            self._dragging  = True
            self.update()

    def mouseMoveEvent(self, e):
        if self._dragging:
            idx = self._char_at(e.position().toPoint())
            self._sel_end = idx
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._dragging = False

    def mouseDoubleClickEvent(self, e):
        """Doppelklick: ganzes Wort auswählen."""
        idx = self._char_at(e.position().toPoint())
        if idx < 0 or not self._chars:
            return
        text = "".join(c for c, *_ in self._chars)
        # Wort-Grenzen suchen
        start = idx
        while start > 0 and text[start-1] not in " \t\r\n":
            start -= 1
        end = idx
        while end < len(text)-1 and text[end+1] not in " \t\r\n":
            end += 1
        self._sel_start = start
        self._sel_end   = end
        self.update()

    # ── Tastatur ─────────────────────────────────────────────────────────────

    def keyPressEvent(self, e):
        if e.matches(QKeySequence.StandardKey.Copy):
            self._copy()
        elif e.matches(QKeySequence.StandardKey.SelectAll):
            if self._chars:
                self._sel_start = 0
                self._sel_end   = len(self._chars) - 1
                self.update()
        else:
            super().keyPressEvent(e)

    def _select_all(self):
        if self._chars:
            self._sel_start = 0
            self._sel_end   = len(self._chars) - 1
            self.update()

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        sel  = self._selected_text()
        cp   = menu.addAction(tr("Kopieren"))
        cp.setEnabled(bool(sel))
        cp.triggered.connect(self._copy)
        sa = menu.addAction(tr("Alles auswählen"))
        sa.triggered.connect(self._select_all)
        menu.exec(e.globalPos())

    # ── Zeichnen ─────────────────────────────────────────────────────────────

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(_TV['viewer_bg']))

        if not self._pixmap:
            p.end()
            return

        p.drawPixmap(self._offset_x, self._offset_y, self._pixmap)

        # Hairline around the page so it stays defined on a light background.
        # Solid black drew a hard halo around the sheet — on the dark backdrop
        # the white page needs no help separating, and the line only made the
        # edge look burnt.
        p.setPen(QPen(QColor(0, 0, 0, 45), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self._page_rect is not None:
            rx, ry, rw, rh = self._page_rect
            p.drawRect(int(rx), int(ry), int(rw) - 1, int(rh) - 1)
        else:
            p.drawRect(self._offset_x, self._offset_y,
                       self._pixmap.width() - 1, self._pixmap.height() - 1)

        # Auswahl-Highlights
        if self._sel_start >= 0 and self._sel_end >= 0 and self._chars:
            lo = min(self._sel_start, self._sel_end)
            hi = max(self._sel_start, self._sel_end)
            sel_color = QColor(66, 135, 245, 80)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(sel_color))
            for i in range(lo, hi + 1):
                if i < len(self._chars):
                    _, x0, y0, x1, y1 = self._chars[i]
                    p.drawRect(QRect(
                        int(x0), int(y0),
                        max(1, int(x1 - x0)),
                        max(1, int(y1 - y0))
                    ))
        p.end()

    # ── Hilfsfunktionen ──────────────────────────────────────────────────────

    def _char_at(self, pt):
        """Gibt Index des Zeichens zurück das den Punkt enthält, sonst -1."""
        if not self._chars:
            return -1
        x, y = pt.x(), pt.y()
        # Exakter Treffer
        for i, (_, x0, y0, x1, y1) in enumerate(self._chars):
            if x0 <= x <= x1 and y0 <= y <= y1:
                return i
        # Nächstes Zeichen in der Nähe (für klicken zwischen Zeichen)
        best_i, best_d = -1, float("inf")
        for i, (_, x0, y0, x1, y1) in enumerate(self._chars):
            cx = (x0 + x1) / 2
            cy = (y0 + y1) / 2
            d  = (cx - x)**2 + (cy - y)**2
            if d < best_d:
                best_d = d
                best_i = i
        return best_i if best_d < 40**2 else -1

    def _selected_text(self):
        if self._sel_start < 0 or self._sel_end < 0 or not self._chars:
            return ""
        lo = min(self._sel_start, self._sel_end)
        hi = max(self._sel_start, self._sel_end)
        return "".join(self._chars[i][0] for i in range(lo, min(hi+1, len(self._chars))))

    def _copy(self):
        text = self._selected_text()
        if text:
            QApplication.clipboard().setText(text)


# ── Datenmodell ───────────────────────────────────────────────────────────────



# ══════════════════════════════════════════════════════════════════════════════
# EINZELSEITEN-ANSICHT
# Zeigt eine Seite auf volle Breite, springt seitenweise
# ══════════════════════════════════════════════════════════════════════════════

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
        self._page_w_pt = 0.0   # page dimensions in PDF points (stored on render)
        self._page_h_pt = 0.0
        self._scroll_x  = 0.0  # Scroll-Offset (float für präzise Berechnung)
        self._scroll_y  = 0.0
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
        # Which page and rotation _page_w_pt/_page_h_pt currently describe
        self._dims_key = None
        self._dims_rot = None
        self._render_signals = _PageSignals()
        self._render_signals.ready.connect(self._on_page_ready)
        # Pre-render (background warm-up) state
        self._prerender_tasks: list = []
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

        # Seiten-Anzeigebereich
        self._view = PdfPageCanvas()
        self._view.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding)
        main.addWidget(self._view, 1)

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
        from tools.render.region import snap_scale
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

        if dy < 0:   # wheel down → scroll down, then next page
            if max_sy > 0 and self._scroll_y < max_sy - 0.5:
                self._scroll_y = min(self._scroll_y + step, max_sy)
                self._render_preview(); self._schedule_settle()
            elif self._render_task is None:
                self.next_page()          # lands at top of next page (scroll_y=0)
        else:        # wheel up → scroll up, then prev page at its BOTTOM
            if self._scroll_y > 0.5:
                self._scroll_y = max(self._scroll_y - step, 0.0)
                self._render_preview(); self._schedule_settle()
            elif self._render_task is None:
                self.prev_page(start_at_bottom=True)
        e.accept()

    # ── Window rendering, for zooms past MAX_RENDER_PX ───────────────────────

    def _known_page_size(self, src_path, orig, rot):
        """(w, h) in points of the page as displayed, from whatever has already
        measured it — (0, 0) if nothing has.

        Both sources are dictionary lookups. Neither ever parses: see
        _ensure_page_dims on why this must not touch pdfium.
        """
        from tools.render.region import cached_page_size_pt
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
        from tools.render.region import page_px_size
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
        from tools.render.region import region_for_viewport, covers
        page_px_w, page_px_h = self._page_px(scale)
        # Clamp the scroll to the page as it is at this zoom before deciding
        # what is visible, or the window is computed for a position the view
        # cannot actually be at.
        self._scroll_x = max(0.0, min(self._scroll_x, max(0.0, page_px_w - avail_w)))
        self._scroll_y = max(0.0, min(self._scroll_y, max(0.0, page_px_h - avail_h)))

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
        elif self._last_pm is not None and self._page_w_pt > 0:
            src_pm = self._last_pm
            src_rect = (0, 0, self._last_pm.width(), self._last_pm.height())
            src_scale = self._last_pm.width() / self._page_w_pt
        if src_pm is None or src_scale <= 0:
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
            self._scroll_x = max(0.0, min(self._scroll_x, max(0.0, page_px_w - avail_w)))
            self._scroll_y = max(0.0, min(self._scroll_y, max(0.0, page_px_h - avail_h)))
            from tools.render.region import covers
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
        self._cs_cache = {}   # clear color-space cache on new document
        if hasattr(self, '_pikepdf_doc') and self._pikepdf_doc is not None:
            try: self._pikepdf_doc.close()
            except Exception: pass
        self._pikepdf_doc = None
        self._pikepdf_path = None
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
            except Exception: pass
        for task in list(self._prerender_tasks):
            try: task.cancel()
            except Exception: pass
        self._prerender_tasks.clear()
        for name in ("_render_task", "_region_task"):
            task = getattr(self, name, None)
            if task is not None:
                try: task.cancel()
                except Exception: pass
                setattr(self, name, None)

    def _prerender_all(self):
        """Pre-render a window of pages around the current position.

        Only submits as many tasks as fit in _FullPageCache so we never queue
        hundreds of renders for a file the cache can't hold anyway.
        Thumbnails are rendered on-demand by PageGrid — we don't bulk-pre-render
        them here to avoid flooding the pool for large PDFs.
        """
        if not _prerender_enabled:
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
        except Exception:
            pass
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
        window  = _FullPageCache.MAX          # e.g. 30
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

        # ── Too big to render whole: render the window instead ────────────────
        scale = self._display_scale(self._zoom)
        if self._use_region_rendering(scale):
            self._show_region(src_path, orig, rot, scale, avail_w, avail_h)
            return
        self._leave_region_mode()

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
                pm = QPixmap.fromImage(img).scaled(
                    max(1, int(img.width()  * ratio)),
                    max(1, int(img.height() * ratio)),
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            else:
                pm = QPixmap.fromImage(img)
            # Clamp scroll to the actual page bounds (handles the 999999 sentinel
            # used by prev_page(start_at_bottom=True)).
            self._scroll_x = max(0.0, min(self._scroll_x, max(0.0, pm.width()  - avail_w)))
            self._scroll_y = max(0.0, min(self._scroll_y, max(0.0, pm.height() - avail_h)))
            off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
            off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)
            # raw_chars are image-relative: multiply by ratio then add display offset
            display_chars = [(ch, off_x + x*ratio, off_y + y*ratio,
                                  off_x + x2*ratio, off_y + y2*ratio)
                             for ch, x, y, x2, y2 in raw_chars]
            self._last_pm   = pm
            self._last_zoom = self._zoom
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
            return

        # ── Cache miss: show preview, submit background render ────────────────
        if self._render_task is not None:
            self._render_task.cancel()
            self._render_task = None

        self._render_gen += 1
        gen = self._render_gen

        if self._last_pm is not None and self._last_zoom > 0:
            # No settle timer: the exact render is submitted below, and _render()
            # *is* the settle. See _render_preview.
            self._render_preview(schedule_settle=False)
        else:
            # No previous render — show a scaled-up thumbnail for instant feedback
            thumb_img = _ThumbnailCache.get_any(src_path, orig, rot)
            if thumb_img is not None:
                pm = QPixmap.fromImage(thumb_img).scaled(
                    max(1, avail_w - 16), max(1, avail_h - 16),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation)
                off_x = (avail_w - pm.width())  // 2
                off_y = (avail_h - pm.height()) // 2
                self._view.set_page(pm, [], off_x, off_y)

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

        # Compute scroll-adjusted display offsets.
        # Clamp first so the 999999 sentinel from prev_page(start_at_bottom)
        # resolves to the actual page bottom.
        avail_w = self._view.width()
        avail_h = self._view.height()
        self._scroll_x = max(0.0, min(self._scroll_x, max(0.0, pm.width()  - avail_w)))
        self._scroll_y = max(0.0, min(self._scroll_y, max(0.0, pm.height() - avail_h)))
        off_x = int(max(0.0, (avail_w - pm.width())  / 2.0) - self._scroll_x)
        off_y = int(max(0.0, (avail_h - pm.height()) / 2.0) - self._scroll_y)

        # raw_chars: image-relative → add scroll-adjusted centering offset
        display_chars = [(ch, off_x + x, off_y + y, off_x + x2, off_y + y2)
                         for ch, x, y, x2, y2 in raw_chars]

        self._last_pm   = pm
        self._last_zoom = self._zoom
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
        try:
            if not self.model or not self.pdf_path:
                return
            uid = self.model.order[self._current]
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            cs = self._detect_colorspace(src_path, orig)
            self._color_lbl.setText(tr('Farbprofil: {p0}').format(p0=cs))
        except Exception:
            self._color_lbl.setText(tr("Farbprofil: —"))

    def _detect_colorspace(self, pdf_path, page_idx):
        cache_key = (pdf_path, page_idx)
        if not hasattr(self, '_cs_cache'):
            self._cs_cache = {}
        if cache_key in self._cs_cache:
            return self._cs_cache[cache_key]
        try:
            import pikepdf, re as _re
            # Reuse the open pikepdf handle for the same file to avoid reopening on every page
            if not hasattr(self, '_pikepdf_path') or self._pikepdf_path != pdf_path:
                if hasattr(self, '_pikepdf_doc') and self._pikepdf_doc is not None:
                    try: self._pikepdf_doc.close()
                    except Exception: pass
                self._pikepdf_doc = pikepdf.open(pdf_path)
                self._pikepdf_path = pdf_path
            pdf = self._pikepdf_doc
            page = pdf.pages[page_idx]
            cs_names = set()

            # Regexes for content-stream colour operators (fill + stroke).
            _re_rgb  = _re.compile(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+(?:rg|RG)\b')
            _re_cmyk = _re.compile(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[kK]\b')
            _re_gray = _re.compile(r'(?:^|[^\d.])[\d.]+\s+[gG]\b')

            def _content_bytes(obj):
                # A page keeps its stream(s) in /Contents; a Form XObject *is* the
                # stream, so read it directly.
                if "/Contents" in obj:
                    contents = obj.get("/Contents")
                    data = b""
                    if isinstance(contents, pikepdf.Array):
                        for c in contents:
                            try: data += bytes(c.read_bytes())
                            except Exception: pass
                    elif contents is not None:
                        try: data = bytes(contents.read_bytes())
                        except Exception: pass
                    return data
                try:
                    return bytes(obj.read_bytes())
                except Exception:
                    return b""

            def _scan(obj, depth, seen):
                # Recurse through Form XObjects so nested content (e.g. N-Up,
                # stamps, merged pages) is inspected too — otherwise the page's
                # own stream is just "/Fm0 Do" and no colour is ever found.
                if depth > 8:
                    return
                res = obj.get("/Resources")
                if res is not None:
                    cs_d = res.get("/ColorSpace")
                    if isinstance(cs_d, pikepdf.Dictionary):
                        for v in cs_d.values():
                            try:
                                cs_names.add(str(v[0]) if isinstance(v, pikepdf.Array) else str(v))
                            except Exception:
                                pass
                    xobj = res.get("/XObject")
                    if isinstance(xobj, pikepdf.Dictionary):
                        for v in xobj.values():
                            try:
                                sub = v.get("/Subtype")
                                if sub == pikepdf.Name("/Image"):
                                    cs = v.get("/ColorSpace")
                                    if cs is not None:
                                        cs_names.add(str(cs[0]) if isinstance(cs, pikepdf.Array) else str(cs))
                                elif sub == pikepdf.Name("/Form"):
                                    try:    key = v.objgen
                                    except Exception: key = id(v)
                                    if key not in seen:
                                        seen.add(key)
                                        _scan(v, depth + 1, seen)
                            except Exception:
                                pass
                try:
                    text = _content_bytes(obj).decode("latin-1", errors="replace")
                    if _re_rgb.search(text):  cs_names.add("/DeviceRGB")
                    if _re_cmyk.search(text): cs_names.add("/DeviceCMYK")
                    if _re_gray.search(text): cs_names.add("/DeviceGray")
                except Exception:
                    pass

            _scan(page, 0, set())

            has_rgb  = bool(cs_names & {"/DeviceRGB", "/CalRGB", "/ICCBased"})
            has_cmyk = "/DeviceCMYK" in cs_names
            has_gray = bool(cs_names & {"/DeviceGray", "/CalGray"})
            if has_rgb and has_cmyk:
                result = "RGB + CMYK"
            elif has_cmyk:
                result = "CMYK"
            elif has_rgb:
                result = "RGB"
            elif has_gray:
                result = "Grayscale"
            else:
                result = "—"
            self._cs_cache[cache_key] = result
            return result
        except Exception:
            return "—"

    def next_page(self):
        if self.model and self._current < len(self.model.order) - 1:
            self._current += 1
            self._scroll_x  = 0.0
            self._scroll_y  = 0.0
            self._page_w_pt = 0.0   # clear stale dims so wheel uses only fresh renders
            self._page_h_pt = 0.0
            self._render()

    def prev_page(self, start_at_bottom=False):
        if self._current > 0:
            self._current -= 1
            self._scroll_x  = 0.0
            self._page_w_pt = 0.0
            self._page_h_pt = 0.0
            # Use a large sentinel; both render paths clamp it to the real max_sy
            self._scroll_y = 999999.0 if start_at_bottom else 0.0
            self._render()

    def go_to(self, page_1based):
        self._current  = max(0, page_1based - 1)
        self._scroll_x  = 0.0
        self._scroll_y  = 0.0
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
                parent = self.parent()
                while parent and not isinstance(parent, PdfTab):
                    parent = parent.parent()
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


# ══════════════════════════════════════════════════════════════════════════════
# THUMBNAIL-KARTE
# ══════════════════════════════════════════════════════════════════════════════

class PageCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, display_pos, orig_idx, pixmap, rotation=0, parent=None,
                 card_w=CARD_W, card_h=CARD_H):
        super().__init__(parent)
        self.display_pos = display_pos
        self.orig_idx    = orig_idx
        self._card_w     = card_w
        self._card_h     = card_h
        self.setFixedSize(card_w+16, card_h+28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected            = False
        self._drag_pos            = None
        self._pending_ctrl_click  = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(2)

        self.img = QLabel()
        self.img.setFixedSize(card_w, card_h)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setStyleSheet(
            f"border:1px solid {_TV['border']};background:{_TV['card_bg']};border-radius:2px;")
        if pixmap is not None:
            if rotation:
                pixmap = pixmap.transformed(QTransform().rotate(rotation))
            pixmap = pixmap.scaled(card_w, card_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self.img.setPixmap(pixmap)
        layout.addWidget(self.img)

        num_size = max(9, min(13, card_w // 10))
        self.num = QLabel(str(display_pos + 1))
        self.num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # border:none — QLabel is a QFrame, so without it the label picks up the
        # selected card's 2px accent border and gets a box of its own.
        self.num.setStyleSheet(
            f"color:{_TV['dim']};font-size:{num_size}px;"
            "background:transparent;border:none;")
        layout.addWidget(self.num)
        self._update_style()

    def set_image(self, image: QImage):
        """Called from the GUI thread with a freshly rendered QImage."""
        pm = QPixmap.fromImage(image)
        pm = pm.scaled(self._card_w, self._card_h,
                       Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
        self.img.setPixmap(pm)

    def set_selected(self, sel):
        self._selected = sel
        self._update_style()

    def _update_style(self):
        if self._selected:
            self.setStyleSheet(
                f"QFrame{{background:{_TV['sel_bg']};border:2px solid {_TV['acc']};border-radius:5px;}}")
        else:
            self.setStyleSheet(
                "QFrame{background:transparent;border:2px solid transparent;"
                "border-radius:5px;}")

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_pos = e.position().toPoint()
        mods  = QApplication.keyboardModifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        # Ctrl+click on an already-selected card: don't deselect yet — wait to
        # see if the user drags (multi-drag) or just releases (then deselect).
        if ctrl and self._selected:
            self._pending_ctrl_click = True
        else:
            self._pending_ctrl_click = False
            self.clicked.emit(self.display_pos)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and getattr(self, '_pending_ctrl_click', False):
            # No drag happened — process the deferred Ctrl+click now
            self._pending_ctrl_click = False
            self.clicked.emit(self.display_pos)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.MouseButton.LeftButton): return
        if self._drag_pos is None: return
        if (e.position().toPoint() - self._drag_pos).manhattanLength() < 12: return

        mods  = QApplication.keyboardModifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        # Deferred Ctrl+click: card stays selected for multi-drag, no deselect
        self._pending_ctrl_click = False

        # Find parent grid
        grid = self.parent()
        while grid and not isinstance(grid, PageGrid):
            grid = grid.parent()

        # If card is not selected yet, select it now as single
        if not self._selected:
            self.clicked.emit(self.display_pos)

        is_multi = (grid and self._selected and len(grid.model.selected) > 1)

        drag = QDrag(self)
        mime = QMimeData()
        # Ctrl+drag = copy; plain drag = move
        # Format: "copy_multi:<pos>", "copy:<pos>", "multi:<pos>", "<pos>"
        if ctrl:
            prefix = "copy_multi" if is_multi else "copy"
        else:
            prefix = "multi" if is_multi else ""
        mime.setText(f"{prefix}:{self.display_pos}" if prefix else str(self.display_pos))
        drag.setMimeData(mime)

        if is_multi and grid:
            n_sel = len(grid.model.selected)
            pm = QPixmap(self.size())
            pm.fill(QColor("#1e3a5a"))
            from PyQt6.QtGui import QPainter as _P, QFont as _F
            p = _P(pm); p.setPen(QColor("#eaeaea"))
            f = _F(); f.setPointSize(11); f.setBold(True); p.setFont(f)
            label = tr('+{p0} Seiten').format(p0=n_sel) if ctrl else tr('{p0} Seiten').format(p0=n_sel)
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, label)
            p.end()
            drag.setPixmap(pm)
        else:
            pm = QPixmap(self.size())
            self.render(pm)
            if ctrl:
                # Overlay a "+" to signal copy
                from PyQt6.QtGui import QPainter as _P, QFont as _F
                p = _P(pm); p.setPen(QColor("#4caf50"))
                f = _F(); f.setPointSize(18); f.setBold(True); p.setFont(f)
                p.drawText(pm.rect().adjusted(0, 0, -4, -4),
                           Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, "+")
                p.end()
            drag.setPixmap(pm)

        drag.setHotSpot(e.position().toPoint())
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        drag.exec(actions)



# ══════════════════════════════════════════════════════════════════════════════
# THUMBNAIL-GRID
# ══════════════════════════════════════════════════════════════════════════════

class PageGrid(QWidget):
    order_changed     = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, model, pdf_path, parent=None):
        super().__init__(parent)
        self.model    = model
        self.pdf_path = pdf_path
        self._cards   = []
        self._card_render_widths = []   # render_w per card, parallel to _cards
        self._drop_indicator = -1
        self._last_click_pos   = None  # für Shift+Click Bereichsauswahl
        self._card_w  = CARD_W   # zoombarer Thumbnail-Breite
        self._card_h  = CARD_H   # zoombarer Thumbnail-Höhe
        # Background thumbnail rendering
        self._thumb_gen     = 0
        self._thumb_tasks   = []        # active _ThumbTask objects
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        # Debounce timer for resize-triggered rebuilds in single-page mode
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)
        self._scroll_connected = False  # connect scrollbar only once
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rebuild()
        _register_themed(self)

    def _apply_theme(self):
        self._rebuild()

    def zoom_in(self):
        step = 20 if self._card_w < 300 else 40 if self._card_w < 600 else 80
        self._card_w = min(1400, self._card_w + step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_out(self):
        step = 20 if self._card_w <= 300 else 40 if self._card_w <= 600 else 80
        self._card_w = max(60, self._card_w - step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_reset(self):
        self._card_w = CARD_W
        self._card_h = CARD_H
        self._rebuild()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if e.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            e.accept()
        else:
            e.ignore()  # Scroll an ScrollArea weitergeben

    def _per_row(self):
        w = self.width() or 800  # Fallback wenn noch nicht gezeichnet
        return max(1, (w - 2*MARGIN + GAP) // (self._card_w+16+GAP))

    def _rebuild(self):
        # Crash-Guard: verhindert doppelten Aufruf
        if getattr(self, '_rebuilding', False):
            return
        self._rebuilding = True
        try:
            # Cancel all pending thumbnail tasks
            for t in self._thumb_tasks:
                t.cancel()
            self._thumb_tasks.clear()
            _render_queue.cancel_queued(1)
            self._thumb_gen += 1
            gen = self._thumb_gen

            # Build a uid→pixmap map from the existing cards so we can reuse
            # them as placeholders instead of going blank during re-renders.
            old_cards = self._cards[:]
            old_pm_by_uid = {}
            for c in old_cards:
                pm = c.img.pixmap()
                if pm and not pm.isNull():
                    old_pm_by_uid[c.orig_idx] = pm
            self._cards.clear()

            per_row   = self._per_row()
            is_single = (per_row == 1)
            grid_w    = max(100, self.width() or 800)
            self._card_render_widths.clear()

            for pos, uid in enumerate(self.model.order):
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                rot            = self.model.get_rotation(uid)

                if is_single:
                    c_w = max(60, grid_w - 2*MARGIN - 16)
                    pw, ph = _FullPageCache.get_dims(src_path, orig, rot)
                    if pw > 0 and ph > 0:
                        c_h = int(c_w * pw / ph) if rot in (90, 270) else int(c_w * ph / pw)
                    else:
                        c_h = int(c_w * CARD_H / CARD_W)
                    render_w = _thumb_render_width(c_w * 1.5)
                else:
                    c_w      = self._card_w
                    c_h      = self._card_h
                    render_w = _thumb_render_width(max(self._card_w * 2, 200))

                self._card_render_widths.append(render_w)

                # Show cached image if available; otherwise use old placeholder.
                # Tasks are NOT submitted here — _schedule_visible() does that
                # lazily based on the scroll position (avoids flooding for big PDFs).
                cached = _ThumbnailCache.get((src_path, orig, rot, render_w))
                if cached is None:
                    cached = _ThumbnailCache.get_any(src_path, orig, rot)

                if cached is not None:
                    pm = QPixmap.fromImage(cached).scaled(
                        c_w, c_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                else:
                    pm = old_pm_by_uid.get(uid)
                    if pm is not None:
                        pm = pm.scaled(c_w, c_h,
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.FastTransformation)

                card = PageCard(pos, uid, pm, 0, self, c_w, c_h)
                card.set_selected(self.model.is_selected(pos))
                card.clicked.connect(self._on_click)
                self._cards.append(card)

            # Destroy old cards after new ones are ready
            for c in old_cards:
                c.hide()
                c.deleteLater()
            self._relayout()
            # Kick off thumbnail loading for currently visible cards only
            QTimer.singleShot(0, self._connect_scroll)
            QTimer.singleShot(0, self._schedule_visible)
        finally:
            self._rebuilding = False

    def _on_thumb_ready(self, gen, cidx, image):
        """GUI thread — receive rendered thumbnail from background worker."""
        if gen != self._thumb_gen:
            return   # stale
        if cidx < 0 or cidx >= len(self._cards):
            return
        self._cards[cidx].set_image(image)

    # ── Lazy thumbnail loading ────────────────────────────────────────────────

    def _get_scroll_area(self):
        """Return the QScrollArea this grid lives in, or None."""
        from PyQt6.QtWidgets import QScrollArea
        p = self.parent()           # viewport
        if p is None: return None
        p = p.parent()              # QScrollArea
        return p if isinstance(p, QScrollArea) else None

    def _connect_scroll(self):
        """Connect the parent scroll-bar to _schedule_visible (once only)."""
        if self._scroll_connected:
            return
        sa = self._get_scroll_area()
        if sa is None:
            return
        sa.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self._scroll_connected = True

    def _schedule_visible(self, _=None):
        """Submit thumb tasks only for cards visible in the scroll viewport.
        A one-viewport buffer above and below is included so scrolling feels
        instant.  Already-cached and already-scheduled cards are skipped."""
        if not self._cards:
            return
        sa = self._get_scroll_area()
        if sa is not None:
            scroll_y    = sa.verticalScrollBar().value()
            viewport_h  = sa.viewport().height() or 600
            y_min = max(0, scroll_y - viewport_h)          # 1 vp above
            y_max = scroll_y + 2 * viewport_h              # 2 vp below (scroll direction)
        else:
            y_min, y_max = 0, 9_999_999   # no scroll area — show all

        gen      = self._thumb_gen
        per_row  = self._per_row()
        is_single = (per_row == 1)

        if is_single:
            # Cards stacked vertically; heights vary
            y = MARGIN
            for i, card in enumerate(self._cards):
                card_h = card.height() or self._card_h
                y_top, y_bot = y, y + card_h
                y += card_h + GAP
                if y_bot < y_min or y_top > y_max:
                    continue
                self._maybe_schedule(i, gen)
        else:
            # Uniform grid
            cell_h  = self._card_h + 28 + GAP
            row_min = max(0, int((y_min - MARGIN) // cell_h))
            row_max = int((y_max - MARGIN) // cell_h) + 1
            for i, card in enumerate(self._cards):
                if i // per_row < row_min or i // per_row > row_max:
                    continue
                self._maybe_schedule(i, gen)

    def _maybe_schedule(self, cidx, gen):
        """Submit a thumb task for card[cidx] if its thumbnail isn't cached yet."""
        if cidx >= len(self._cards) or cidx >= len(self._card_render_widths):
            return
        card     = self._cards[cidx]
        render_w = self._card_render_widths[cidx]
        uid      = card.orig_idx
        src_path, orig = self.model.page_source(uid, self.pdf_path)
        rot      = self.model.get_rotation(uid)
        key      = (src_path, orig, rot, render_w)
        if _ThumbnailCache.get(key) is not None:
            return   # already cached
        # Prune finished tasks, then check for an in-flight task for this card
        self._thumb_tasks = [t for t in self._thumb_tasks if t._active]
        for t in self._thumb_tasks:
            if t._cidx == cidx:
                return  # already in flight
        task = _ThumbTask(gen, cidx, src_path, orig, rot,
                          render_w, self._thumb_signals)
        self._thumb_tasks.append(task)
        _render_queue.submit(task, 1)   # P1: visible thumbnails

    def _card_tops(self):
        """Return a list of y-offsets for each card (single-page mode only)."""
        tops = []
        y = MARGIN
        for card in self._cards:
            tops.append(y)
            y += card.height() + GAP
        return tops

    def _relayout(self):
        if not self._cards:
            self.setMinimumHeight(200); return
        pr = self._per_row()
        if pr == 1:
            # Single-page mode: stack cards vertically, each filling full width
            y = MARGIN
            for card in self._cards:
                card.move(MARGIN, y)
                card.show()
                y += card.height() + GAP
            self.setMinimumHeight(y + MARGIN)
        else:
            cell_w = self._card_w+16+GAP
            cell_h = self._card_h+28+GAP
            rows   = (len(self._cards)+pr-1)//pr
            for i, card in enumerate(self._cards):
                card.move(MARGIN + i%pr*cell_w, MARGIN + i//pr*cell_h)
                card.show()
            self.setMinimumHeight(MARGIN + rows*cell_h + MARGIN)
        self.update()

    def resizeEvent(self, e):
        # In single-page mode the card width must track the widget width.
        # Debounce to avoid triggering _rebuild on every pixel of a drag-resize.
        if self._per_row() == 1:
            self._resize_timer.start(120)
        else:
            self._relayout()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_indicator < 0 or not self._cards: return
        pr = self._per_row()
        p  = QPainter(self)
        if pr == 1:
            tops = self._card_tops()
            idx  = min(self._drop_indicator, len(tops))
            if idx < len(tops):
                y = tops[idx] - GAP//2
            else:
                y = tops[-1] + self._cards[-1].height() + GAP//2
            _paint_drop_marker(p, MARGIN, y - _DROP_THICKNESS/2.0,
                               self._cards[0].width(), horizontal=True)
        else:
            cell_w = self._card_w+16+GAP
            cell_h = self._card_h+28+GAP
            pos    = min(self._drop_indicator, len(self._cards))
            col    = pos % pr; row = pos // pr
            x      = MARGIN + col*cell_w - GAP//2
            y      = MARGIN + row*cell_h
            _paint_drop_marker(p, x - _DROP_THICKNESS/2.0, y, self._card_h)
        p.end()

    def _pos_from_point(self, pt):
        if not self._cards: return 0
        n  = len(self._cards)
        pr = self._per_row()
        if pr == 1:
            # Single-page mode: find which card the y coordinate falls in
            tops = self._card_tops()
            rel_y = pt.y()
            for i, top in enumerate(tops):
                bottom = top + self._cards[i].height()
                if rel_y < (top + bottom) // 2:
                    return i
            return n
        cell_w = self._card_w + 16 + GAP
        cell_h = self._card_h + 28 + GAP
        rel_x  = pt.x() - MARGIN
        rel_y  = pt.y() - MARGIN
        col    = max(0, min(rel_x // cell_w, pr - 1))
        row    = max(0, rel_y // cell_h)
        pos    = row * pr + col
        if pos >= n:
            return n
        if rel_x - col * cell_w > cell_w // 2:
            pos += 1
        return min(pos, n)

    def mousePressEvent(self, e):
        # Klick auf leeren Hintergrund → Auswahl aufheben
        if e.button() == Qt.MouseButton.LeftButton:
            self.model.deselect_all()
            self._update_selection()
            self.selection_changed.emit()
        super().mousePressEvent(e)

    def _on_click(self, pos):
        mods  = QApplication.keyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if shift and self._last_click_pos is not None:
            # Bereichsauswahl: alle Seiten zwischen letztem Klick und jetzt
            lo = min(self._last_click_pos, pos)
            hi = max(self._last_click_pos, pos)
            for i in range(lo, hi + 1):
                uid = self.model.order[i]
                self.model.selected.add(uid)
        else:
            self.model.select(pos, multi=ctrl)
            self._last_click_pos = pos

        self._update_selection()
        self.selection_changed.emit()

    def _update_selection(self):
        for i, card in enumerate(self._cards):
            card.set_selected(self.model.is_selected(i))

    def handle_drop(self, from_pos, to_pos, multi=False, copy=False):
        self._drop_indicator = -1; self.update()
        if copy:
            # Ctrl+drag: duplicate pages at destination, leave originals in place
            if multi:
                sel_uids = [u for u in self.model.order if u in self.model.selected]
            else:
                if 0 <= from_pos < len(self.model.order):
                    sel_uids = [self.model.order[from_pos]]
                else:
                    sel_uids = []
            insert_at = min(to_pos, len(self.model.order))
            for i, uid in enumerate(sel_uids):
                new_uid = self.model._new_uid()
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                if src_path == self.pdf_path:
                    self.model.src[new_uid] = orig
                else:
                    self.model.src[new_uid] = orig
                    self.model.foreign_src[new_uid] = (src_path, orig)
                rot = self.model.rotations.get(uid, 0)
                if rot:
                    self.model.rotations[new_uid] = rot
                self.model.order.insert(insert_at + i, new_uid)
        elif multi:
            self.model.move_selection(to_pos)
        else:
            self.model.move(from_pos, to_pos)
        self._rebuild(); self.order_changed.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasText(): e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasText(): return
        e.acceptProposedAction()
        self._drop_indicator = self._pos_from_point(e.position().toPoint())
        self.update()

    def dragLeaveEvent(self, e):
        self._drop_indicator = -1; self.update()

    def dropEvent(self, e):
        if not e.mimeData().hasText(): return
        to_pos = self._drop_indicator
        if to_pos < 0:
            to_pos = self._pos_from_point(e.position().toPoint())
        self._drop_indicator = -1; self.update()
        text = e.mimeData().text()
        e.acceptProposedAction()

        if text.startswith("copy_multi:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, multi=True, copy=True)
            except (ValueError, IndexError): return
        elif text.startswith("copy:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, copy=True)
            except (ValueError, IndexError): return
        elif text.startswith("multi:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, multi=True)
            except (ValueError, IndexError): return
        else:
            try: from_pos = int(text)
            except Exception: return
            self.handle_drop(from_pos, to_pos)

    # Public
    def rotate_selected(self, deg):
        self.model.rotate_selected(deg); self._rebuild(); self.order_changed.emit()

    def delete_selected(self):
        self.model.delete_selected(); self._rebuild()
        self.order_changed.emit(); self.selection_changed.emit()

    def select_all(self):
        self.model.select_all(); self._update_selection(); self.selection_changed.emit()

    def deselect_all(self):
        self.model.deselect_all(); self._update_selection(); self.selection_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# GLOBALER EVENT-FILTER FUER SHORTCUTS
# Registriert auf QApplication — funktioniert immer unabhaengig vom Fokus
# ══════════════════════════════════════════════════════════════════════════════

class ManageShortcutFilter(QObject):
    """
    Fängt Tastendruecke auf App-Ebene ab.
    Nur aktiv wenn manage_panel sichtbar ist.
    """
    def __init__(self, manage_panel, parent=None):
        super().__init__(parent)
        self.panel = manage_panel

    def eventFilter(self, obj, event):
        # Never intercept while a modal dialog is open — its widgets own the keys.
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        # Claim ShortcutOverride so widgets don't eat our Ctrl combos.
        # MUST use accept()+return False (not return True) so Qt still dispatches
        # the subsequent KeyPress — return True eats the event entirely.
        if t == QEvent.Type.ShortcutOverride:
            if not self.panel.isVisible():
                return False
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit):
                return False
            k    = event.key()
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl and k in (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_V,
                              Qt.Key.Key_X, Qt.Key.Key_Z, Qt.Key.Key_Y,
                              Qt.Key.Key_D):
                event.accept()
            # Key_P is handled by the global QShortcut at PdfViewerWidget level;
            # don't claim it here so the global shortcut can fire.
            return False

        if t != QEvent.Type.KeyPress:
            return False
        if not self.panel.isVisible():
            return False

        # Shortcuts nicht abfangen wenn ein Textfeld fokussiert ist
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return False

        k     = event.key()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not ctrl:
            self.panel._delete(); return True
        if ctrl:
            if k == Qt.Key.Key_A: self.panel.grid.select_all();   return True
            if k == Qt.Key.Key_D: self.panel.grid.deselect_all(); return True
            if k == Qt.Key.Key_C: self.panel._copy();              return True
            if k == Qt.Key.Key_X: self.panel._cut();               return True
            if k == Qt.Key.Key_V: self.panel._paste();             return True
            if k == Qt.Key.Key_Z and not shift: self.panel._undo(); return True
            if (k == Qt.Key.Key_Z and shift) or k == Qt.Key.Key_Y:
                self.panel._redo(); return True
        return False


# ══════════════════════════════════════════════════════════════════════════════
# VERWALTUNGS-LEISTE
# ══════════════════════════════════════════════════════════════════════════════

class ManagePanel(QWidget):
    closed = pyqtSignal()

    # Shared cross-tab clipboard: list of (pdf_path, orig_page_idx, rotation)
    _shared_clipboard: list = []

    def __init__(self, model, pdf_path, grid, parent=None, tab=None):
        super().__init__(parent)
        self.setObjectName("managePanel")
        self.model        = model
        self.pdf_path     = pdf_path
        self.grid         = grid
        # The owning PdfTab, kept explicitly. Manage mode reparents this panel
        # into a splitter owned by the viewer, so the parent chain no longer
        # leads back to the tab — see _swap_source.
        self.tab          = tab if tab is not None else parent
        self._history     = []
        self._redo_stack  = []
        self._filter    = None
        self.setMinimumWidth(220)
        self._setup()
        _register_themed(self)
        self.destroyed.connect(self._cleanup_filter)

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Titel oben (fixiert)
        self._title_w = QWidget()
        self._title_w.setObjectName("manageTitleW")
        self._title_w.setFixedHeight(36)
        tl = QHBoxLayout(self._title_w)
        tl.setContentsMargins(10, 0, 10, 0)
        self._title_lbl = QLabel(tr("Seiten verwalten"))
        self._title_lbl.setStyleSheet(
            "font-size:13px;font-weight:bold;background:transparent;")
        tl.addWidget(self._title_lbl)
        outer.addWidget(self._title_w)

        # Scrollbarer Bereich
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content_w = QWidget()
        self._content_w.setObjectName("manageContentW")
        layout = QVBoxLayout(self._content_w)
        layout.setContentsMargins(10, 8, 22, 10)
        layout.setSpacing(5)
        self._scroll_area.setWidget(self._content_w)
        outer.addWidget(self._scroll_area, 1)

        sel_lbl = QLabel(tr("Auswahl  (z.B. 1, 3, 5-8)"))
        sel_lbl.setObjectName("sectionLabel")
        layout.addWidget(sel_lbl)

        self.sel_edit = QLineEdit()
        self.sel_edit.setPlaceholderText(tr("z.B. 1, 3, 5-8, 12  →  Enter"))
        self.sel_edit.returnPressed.connect(self._apply_sel_edit)
        layout.addWidget(self.sel_edit)
        layout.addWidget(self._sep())

        # Zoom and rotation are both one-click view actions, so they share a
        # single row of icon buttons. Rotation used to be its own section with
        # two full-width buttons — three rows of sidebar for two clicks — while
        # this row carried a "↺" that looked like rotate but reset the zoom.
        self._section(layout, tr("ANSICHT"))
        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        self._zoom_btns_manage = []
        def _icon_btn(text, tip, slot):
            b = QPushButton(text)
            b.setFixedSize(32, 26)
            b.setToolTip(tr(tip))
            b.clicked.connect(slot)
            view_row.addWidget(b)
            self._zoom_btns_manage.append(b)
            return b
        _icon_btn("−",   "Thumbnails verkleinern", lambda: self._zoom_grid(-1))
        _icon_btn("+",   "Thumbnails vergroessern", lambda: self._zoom_grid(+1))
        _icon_btn("1:1", "Zoom zuruecksetzen",      lambda: self._zoom_grid(0))
        view_row.addSpacing(10)
        _icon_btn("↺", "Auswahl 90° gegen den Uhrzeigersinn drehen",
                  lambda: self.grid.rotate_selected(270))
        _icon_btn("↻", "Auswahl 90° im Uhrzeigersinn drehen",
                  lambda: self.grid.rotate_selected(90))
        view_row.addStretch()
        layout.addLayout(view_row)
        layout.addWidget(self._sep())

        self._section(layout, tr("AUSWAHL"))
        layout.addWidget(self._btn(tr("Alle auswaehlen  (Strg+A)"),  self.grid.select_all))
        layout.addWidget(self._btn(tr("Auswahl aufheben  (Strg+D)"), self.grid.deselect_all))
        layout.addWidget(self._sep())

        self._section(layout, tr("OPERATIONEN"))
        layout.addWidget(self._btn(tr("Loeschen  (Entf)"),         self._delete))
        layout.addWidget(self._btn(tr("Kopieren  (Strg+C)"),       self._copy))
        layout.addWidget(self._btn(tr("Einfuegen  (Strg+V)"),      self._paste))
        layout.addWidget(self._btn(tr("Rueckgaengig  (Strg+Z)"),   self._undo))
        layout.addWidget(self._btn(tr("Extrahieren..."),            self._extract))
        layout.addWidget(self._btn(tr("Als neuen Tab oeffnen"),    self._open_as_tab))
        layout.addWidget(self._btn(tr("Leere Seite einfuegen"),    self._insert_blank))
        # Takes several files at once, which is what the separate
        # "ZUSAMMENFUEHREN ▸ PDFs zusammenfuehren..." section did — same dialog,
        # same insert position, one button.
        layout.addWidget(self._btn(tr("Aus Datei(en) einfuegen..."), self._insert_from_file))
        layout.addWidget(self._sep())

        # Speichern / Speichern unter live in Datei ▸ at the top of the window
        # (Strg+S / Strg+Umschalt+S) — they are document-wide, not page-manager
        # actions, and having them in both places invited saving twice.

        self._section(layout, tr("TRENNEN"))
        layout.addWidget(self._btn(tr("Auswahl als Datei speichern"), self._split_selection))
        layout.addWidget(self._btn(tr("Jede Seite als Datei"),        self._split_each))
        layout.addWidget(self._btn(tr("Alle N Seiten..."),            self._split_n))

        layout.addStretch()

        back_btn = QPushButton(tr("◀  Einzelansicht  [Tab / Esc]"))
        back_btn.setObjectName("secondaryBtn")
        back_btn.clicked.connect(self.closed.emit)
        layout.addWidget(back_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:10px;min-height:32px;background:transparent;")
        layout.addWidget(self.status)

    def showEvent(self, e):
        """Installiere Event-Filter wenn Panel sichtbar wird."""
        super().showEvent(e)
        if self._filter is None:
            self._filter = ManageShortcutFilter(self)
            QApplication.instance().installEventFilter(self._filter)

    def hideEvent(self, e):
        """Entferne Event-Filter wenn Panel unsichtbar wird."""
        super().hideEvent(e)
        self._cleanup_filter()

    def _cleanup_filter(self):
        if self._filter:
            QApplication.instance().removeEventFilter(self._filter)
            self._filter = None

    def _apply_theme(self):
        t = _TV
        self.setStyleSheet(
            f"QWidget#managePanel{{background:{t['sidebar_bg']};border-right:1px solid {t['border']};}}")
        self._title_w.setStyleSheet(
            f"QWidget#manageTitleW{{background:{t['sidebar_bg']};}}")
        self._title_lbl.setStyleSheet(
            f"color:{t['text']};font-size:13px;font-weight:bold;background:transparent;")
        self._scroll_area.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._content_w.setStyleSheet(
            f"QWidget#manageContentW{{background:{t['sidebar_bg']};}}")
        _zs = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:4px;font-size:13px;}}"
               f"QPushButton:hover{{background:{t['hover']};}}")
        for b in getattr(self, '_zoom_btns_manage', []):
            b.setStyleSheet(_zs)
        if hasattr(self, '_zoom_hint_lbl'):
            self._zoom_hint_lbl.setStyleSheet(
                f"color:{t['vdim']};font-size:9px;background:transparent;")
        if hasattr(self, 'sel_edit'):
            self.sel_edit.setStyleSheet(
                f"QLineEdit{{background:{t['input_bg']};color:{t['text']};"
                f"border:1px solid {t['input_brd']};border-radius:3px;"
                f"padding:3px 6px;font-size:12px;}}"
                f"QLineEdit:focus{{border:1px solid {t['acc']};}}")

    def _section(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        layout.addWidget(lbl)

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.setObjectName("secondaryBtn")
        b.clicked.connect(fn)
        b.setMinimumHeight(28)
        return b

    def _sep(self):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color:{_TV['border']};margin:3px 0;")
        return f

    def update_info(self):
        positions = sorted(i+1 for i, u in enumerate(self.model.order)
                           if u in self.model.selected)
        self.sel_edit.blockSignals(True)
        self.sel_edit.setText(self._positions_to_str(positions))
        self.sel_edit.blockSignals(False)

    _positions_to_str = staticmethod(_positions_to_str)

    def _apply_sel_edit(self):
        """Parst den Eingabetext und setzt Auswahl — nur bei Enter."""
        positions = _parse_positions(self.sel_edit.text(), len(self.model.order))
        if positions:
            self.model.selected = {self.model.order[i] for i in positions}
            self.grid._update_selection()
            self.grid.selection_changed.emit()
        # Feld immer auf die kompakte Darstellung normalisieren (auch bei
        # ungültiger Eingabe — die Auswahl bleibt dann unverändert).
        self.update_info()

    def _snapshot(self):
        return (
            list(self.model.order),
            dict(self.model.rotations),
            dict(self.model.src),
            self.model._next_uid,
            dict(self.model.foreign_src),
            self.pdf_path,
        )

    def _restore_snapshot(self, snap):
        order, rotations, model_src, next_uid, foreign_src, pdf_path = snap
        self.model.order       = order
        self.model.rotations   = rotations
        self.model.src         = model_src
        self.model._next_uid   = next_uid
        self.model.foreign_src = foreign_src
        self.model.selected.clear()
        if pdf_path and pdf_path != self.pdf_path:
            self.pdf_path      = pdf_path
            self.grid.pdf_path = pdf_path

    def _save_history(self):
        self._history.append(self._snapshot())
        self._redo_stack.clear()   # new action clears redo branch
        if len(self._history) > 50: self._history.pop(0)

    def _cap_redo(self):
        if len(self._redo_stack) > 50: self._redo_stack.pop(0)

    def _delete(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        self._save_history()
        n = len(self.model.selected)
        self.grid.delete_selected()
        self.status.setText(tr('{p0} Seite(n) geloescht.  Strg+Z = Rueckgaengig.').format(p0=n))

    def _copy(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        # Store (pdf_path, orig_page_idx, rotation) in shared cross-tab clipboard
        ManagePanel._shared_clipboard = []
        for u in self.model.order:
            if u in self.model.selected:
                path, orig = self.model.page_source(u, self.pdf_path)
                rot = self.model.rotations.get(u, 0)
                ManagePanel._shared_clipboard.append((path, orig, rot))
        n = len(ManagePanel._shared_clipboard)
        self.status.setText(
            tr('{p0} Seite(n) kopiert.  Strg+V = Einfuegen (auch in anderen Tabs).').format(p0=n))

    def _cut(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        self._copy()
        self._save_history()
        n = len(self.model.selected)
        self.grid.delete_selected()
        self.status.setText(
            tr('{p0} Seite(n) ausgeschnitten.  Strg+V = Einfuegen.').format(p0=n))

    def _paste(self):
        if not ManagePanel._shared_clipboard:
            self.status.setText(tr("Nichts zum Einfuegen.  Zuerst Strg+C.")); return
        self._save_history()
        if self.model.selected:
            positions = [i for i, u in enumerate(self.model.order)
                         if u in self.model.selected]
            insert_at = max(positions) + 1
        else:
            insert_at = len(self.model.order)
        # Paste from shared clipboard — source may be a different pdf_path
        for i, (src_path, orig_idx, rot) in enumerate(ManagePanel._shared_clipboard):
            new_uid = self.model._new_uid()
            if src_path == self.pdf_path:
                self.model.src[new_uid] = orig_idx
            else:
                # Foreign page: store dummy in src, real source in foreign_src
                self.model.src[new_uid] = orig_idx
                self.model.foreign_src[new_uid] = (src_path, orig_idx)
            if rot:
                self.model.rotations[new_uid] = rot
            self.model.order.insert(insert_at + i, new_uid)
        self.grid._rebuild(); self.grid.order_changed.emit()
        n = len(ManagePanel._shared_clipboard)
        self.status.setText(tr('{p0} Seite(n) eingefuegt.').format(p0=n))

    def _undo(self):
        if not self._history:
            self.status.setText(tr("Nichts zum Rueckgaengig.")); return
        self._redo_stack.append(self._snapshot())
        self._cap_redo()
        self._restore_snapshot(self._history.pop())
        self.grid._rebuild(); self.grid.order_changed.emit()
        self.status.setText(tr("Rueckgaengig.  Strg+Y = Wiederholen."))

    def _redo(self):
        if not self._redo_stack:
            self.status.setText(tr("Nichts zum Wiederholen.")); return
        self._history.append(self._snapshot())
        if len(self._history) > 50: self._history.pop(0)
        self._restore_snapshot(self._redo_stack.pop())
        self.grid._rebuild(); self.grid.order_changed.emit()
        self.status.setText(tr("Wiederholt."))

    def _zoom_grid(self, direction):
        """direction: +1=rein, -1=raus, 0=reset"""
        if direction > 0:   self.grid.zoom_in()
        elif direction < 0: self.grid.zoom_out()
        else:               self.grid.zoom_reset()

    def _extract(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Extrahieren als"), "", tr("PDF (*.pdf)"))
        if not path: return
        try:
            from pypdf import PdfReader, PdfWriter
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter()
            for uid in self.model.order:
                if uid in self.model.selected:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot  = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    writer.add_page(page)
            with open(path, "wb") as f: writer.write(f)
            AppState.get().open_result(path, "Extrahiert")
            self.status.setText(tr('{p0} Seite(n) extrahiert.').format(p0=len(self.model.selected)))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _open_as_tab(self):
        """Ausgewählte Seiten als neue temporäre PDF in neuem Tab öffnen.

        Asks whether to copy or move them. Moving is the case the separate
        "Nach Bereichen..." split button used to cover, and it is easier to do
        by picking the pages you want than by typing ranges into a prompt."""
        from PyQt6.QtWidgets import QMessageBox
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        box = QMessageBox(self)
        box.setWindowTitle(tr("Als neuen Tab oeffnen"))
        box.setText(tr('{p0} Seite(n) in einem neuen Tab oeffnen.').format(
            p0=len(self.model.selected)))
        box.setInformativeText(tr("Sollen die Seiten hier ebenfalls bleiben?"))
        keep = box.addButton(tr("Kopieren  (hier behalten)"),
                             QMessageBox.ButtonRole.AcceptRole)
        move = box.addButton(tr("Verschieben  (hier entfernen)"),
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() not in (keep, move):
            return
        try:
            import tempfile
            from pypdf import PdfReader, PdfWriter
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter(); n = 0
            for uid in self.model.order:
                if uid in self.model.selected:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot  = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    writer.add_page(page); n += 1
            # Temporäre Datei — bleibt solange der Tab offen ist
            tmp = tempfile.NamedTemporaryFile(
                suffix=".pdf", delete=False,
                prefix="copyshop_sel_")
            with open(tmp.name, "wb") as f: writer.write(f)
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            # Only remove them here once the new file is safely written.
            if box.clickedButton() is move:
                self._save_history()
                self.model.delete_selected()
                self.grid._rebuild(); self.grid.order_changed.emit()
            AppState.get().open_result(tmp.name, f"{stem} [{n}S]")
            self.status.setText(
                (tr('{p0} Seite(n) in neuen Tab verschoben.')
                 if box.clickedButton() is move
                 else tr('{p0} Seite(n) als neuer Tab geoeffnet.')).format(p0=n))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _swap_source(self, new_path):
        """Point the page manager — and everything else that resolves the model's
        page indexes — at a rebuilt file.

        The grid, the owning tab and AppState each cached the old path, and only
        the panel's own copy was updated. Anything reading the document therefore
        resolved the model against the *previous* file: the tools kept processing
        the pre-edit PDF (the booklet tool imposed the old page order, so an
        inserted blank surfaced as the back of the cover), and switching tabs
        snapped the viewer back to it too."""
        self.pdf_path = new_path
        self.grid.pdf_path = new_path
        # Use the recorded tab, not a walk up the parent chain: in manage mode
        # this panel lives in the viewer's splitter, so the walk found no PdfTab
        # and silently skipped the update. The single view then resolved the new
        # page indexes against the old, shorter file — an inserted blank page is
        # past its last index, so the render threw and the preview showed the
        # blue "could not render" fallback at a bogus size.
        tab = self.tab if isinstance(self.tab, PdfTab) else None
        if tab is None:
            tab = self.parent()
            while tab is not None and not isinstance(tab, PdfTab):
                tab = tab.parent()
        if tab is not None:
            tab.pdf_path = new_path
            tab.single.pdf_path = new_path
        if AppState.get().page_model is self.model:
            AppState.get().current_pdf = new_path

    def _insert_blank(self):
        try:
            from pypdf import PdfReader, PdfWriter
            import tempfile
            self._save_history()
            reader = PdfReader(self.pdf_path, strict=False)
            if self.model.selected:
                positions = [i for i, u in enumerate(self.model.order)
                             if u in self.model.selected]
                insert_at = max(positions) + 1
            else:
                insert_at = len(self.model.order)
            # Size the blank like the page it follows, as that page is displayed
            # (page 0's raw MediaBox gave a mixed-size document an A4 sheet in
            # the middle of its A5 pages, and a portrait one next to landscape).
            if self.model.order:
                ref_uid = self.model.order[min(max(0, insert_at - 1),
                                               len(self.model.order) - 1)]
                ref_path, ref_orig = self.model.page_source(ref_uid, self.pdf_path)
                ref = PdfReader(ref_path, strict=False).pages[ref_orig]
                rot = (int(ref.get("/Rotate", 0) or 0)
                       + self.model.get_rotation(ref_uid)) % 360
            else:
                ref, rot = reader.pages[0], 0
            pw = float(ref.mediabox.width)
            ph = float(ref.mediabox.height)
            if rot in (90, 270): pw, ph = ph, pw
            # Leerseite ans Ende der Datei anhängen
            new_orig = len(reader.pages)
            writer = PdfWriter()
            for page in reader.pages: writer.add_page(page)
            writer.add_blank_page(pw, ph)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
            with open(tmp, "wb") as f: writer.write(f)
            # Neue UID für die Leerseite
            new_uid = self.model._new_uid()
            self.model.src[new_uid] = new_orig
            self.model.order.insert(insert_at, new_uid)
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            self.status.setText(tr("Leere Seite eingefuegt."))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _insert_from_file(self):
        """Insert the pages of one or more PDFs after the selection.

        Takes several files because this replaced the separate "PDFs
        zusammenfuehren..." button, which asked the same question and inserted at
        the same place — the only difference was that it opened the result in a
        new tab instead of editing this one, which is not what a page manager is
        for."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("PDF(s) einfuegen"), "", tr("PDF Dateien (*.pdf)"))
        if not paths: return
        try:
            from pypdf import PdfReader, PdfWriter
            self._save_history()
            import tempfile
            ins_pages = []
            for p in paths:
                ins_pages.extend(PdfReader(p, strict=False).pages)
            n_ins = len(ins_pages)
            if self.model.selected:
                positions = [i for i, u in enumerate(self.model.order)
                             if u in self.model.selected]
                insert_at = max(positions) + 1
            else:
                insert_at = len(self.model.order)
            # Neue UIDs für die eingefügten Seiten
            # pdf_path bleibt — wir hängen neue Seiten an die bestehende Datei
            # Einfachster Weg: Neue PDF bauen, Model neu initialisieren
            writer = PdfWriter()
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            for i, uid in enumerate(self.model.order):
                if i == insert_at:
                    for p in ins_pages: writer.add_page(p)
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot  = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                writer.add_page(page)
            if insert_at >= len(self.model.order):
                for p in ins_pages: writer.add_page(p)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
            with open(tmp, "wb") as f: writer.write(f)
            from pypdf import PdfReader as PR
            n_new = len(PR(tmp).pages)
            # Model neu aufbauen mit frischen UIDs
            self.model.__init__(n_new)
            self.model.selected.clear()
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            self.status.setText(tr('{p0} Seite(n) eingefuegt.').format(p0=n_ins))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    # ── Trennen ──────────────────────────────────────────────────────────────
    def _split_selection(self):
        """Save selected pages as a new PDF file and open in new tab."""
        from PyQt6.QtWidgets import QFileDialog
        from pypdf import PdfReader, PdfWriter
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Auswahl speichern als"), "", tr("PDF (*.pdf)"))
        if not path: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter()
            for uid in self.model.order:
                if uid not in self.model.selected: continue
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                writer.add_page(page)
            with open(path, "wb") as f: writer.write(f)
            n = len(self.model.selected)
            self.status.setText(f"OK: {n} {tr('Seite(n) gespeichert.')}")
            AppState.get().open_result(path, os.path.basename(path))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _split_each(self):
        from PyQt6.QtWidgets import QFileDialog
        from pypdf import PdfReader, PdfWriter
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner waehlen"))
        if not out_dir: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            # If pages are selected only split those, otherwise split all in model order
            uids = ([uid for uid in self.model.order if uid in self.model.selected]
                    if self.model.selected else list(self.model.order))
            for i, uid in enumerate(uids):
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                w = PdfWriter(); w.add_page(page)
                p = os.path.join(out_dir, f"{stem}_seite{i+1:03d}.pdf")
                with open(p, "wb") as f: w.write(f)
            self.status.setText(f"OK: {len(uids)} {tr('Dateien erstellt')}")
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _split_n(self):
        from PyQt6.QtWidgets import QFileDialog, QInputDialog
        from pypdf import PdfReader, PdfWriter
        n, ok = QInputDialog.getInt(self, tr("Seiten pro Teil"),
                                    tr("Wie viele Seiten pro Datei?"), 1, 1, 9999)
        if not ok: return
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner waehlen"))
        if not out_dir: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            uids = ([uid for uid in self.model.order if uid in self.model.selected]
                    if self.model.selected else list(self.model.order))
            chunk = 0
            for start in range(0, len(uids), n):
                chunk += 1; w = PdfWriter()
                for uid in uids[start:start+n]:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    w.add_page(page)
                p = os.path.join(out_dir, f"{stem}_teil{chunk:03d}.pdf")
                with open(p, "wb") as f: w.write(f)
            self.status.setText(f"OK: {chunk} {tr('Dateien erstellt')}")
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))


class _PrintPreview(QWidget):
    """Left-side print preview panel — mirrors Acrobat's layout preview."""

    # Delivers a finished background render to the GUI thread. A signal is
    # auto-queued across threads; the previous QTimer.singleShot(0, …) was
    # created ON the render thread, which has no event loop, so it never fired
    # and the preview stayed blank forever.
    _render_ready = pyqtSignal(int, object, float, float)

    # Physical paper sizes in mm  (width × height in portrait)
    _PAPER_MM = {
        "A4":        (210.0, 297.0), "A3":     (297.0, 420.0),
        "A5":        (148.0, 210.0), "Letter": (215.9, 279.4),
        "Legal":     (215.9, 355.6), "B4":     (250.0, 353.0),
        "B5":        (176.0, 250.0), "Executive": (184.2, 266.7),
        "Folio":     (215.9, 330.2),
    }

    def __init__(self, pdf_path, model, parent=None):
        super().__init__(parent)
        self._render_ready.connect(self._on_render_done)
        self._pdf_path  = pdf_path
        self._model     = model
        # Subset of page positions (into model.order) the preview walks through.
        # Mirrors the dialog's page selection (all / current / range).
        self._pages     = list(range(len(model.order)))
        self._current   = 0        # index into self._pages
        self._render_token = 0     # bumped each render; stale deliveries dropped
        self._pixmap    = None      # rendered page image
        self._page_w_pt = 595.0     # PDF page dimensions in points
        self._page_h_pt = 842.0
        # Settings mirrored from the dialog
        self._margin_mm  = 3.0
        self._scale_idx  = 2        # default: Shrink to Printable Area
        self._paper_key  = "A4"
        self._orient_idx = 0        # 0=auto, 1=portrait, 2=landscape
        self.setObjectName("printPreviewPanel")
        self.setFixedWidth(260)
        self.setStyleSheet(
            f"QWidget#printPreviewPanel{{background:{_TV['sidebar_bg']};}}")

        from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        lyt = QVBoxLayout(self)
        lyt.setContentsMargins(10, 14, 10, 10)
        lyt.setSpacing(4)

        hdr = QLabel(tr("VORSCHAU"))
        hdr.setStyleSheet(
            f"font-size:10px;font-weight:bold;letter-spacing:1px;"
            f"color:{_TV['dim']};background:transparent;")
        lyt.addWidget(hdr)

        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setMinimumHeight(240)
        lyt.addWidget(self._canvas, 1)

        # Info line: scale% + dimensions
        self._info_lbl = QLabel("")
        self._info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_lbl.setStyleSheet(
            f"font-size:10px;color:{_TV['dim']};background:transparent;")
        lyt.addWidget(self._info_lbl)

        # Clip warning (shown only when 100% overflows printable area)
        self._clip_lbl = QLabel(tr("⚠ Inhalt wird beschnitten"))
        self._clip_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._clip_lbl.setStyleSheet(
            f"font-size:10px;font-weight:bold;"
            f"color:{_TV['acc']};background:transparent;")
        self._clip_lbl.hide()
        lyt.addWidget(self._clip_lbl)

        # Page navigation
        nav = QHBoxLayout()
        nav.setSpacing(4)
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedSize(28, 28)
        self._prev_btn.setObjectName("secondaryBtn")
        self._prev_btn.clicked.connect(self._prev_page)
        self._page_lbl = QLabel(tr("Seite 1 / 1"))
        self._page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_lbl.setStyleSheet(
            f"font-size:10px;color:{_TV['dim']};background:transparent;")
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedSize(28, 28)
        self._next_btn.setObjectName("secondaryBtn")
        self._next_btn.clicked.connect(self._next_page)
        nav.addWidget(self._prev_btn)
        nav.addWidget(self._page_lbl, 1)
        nav.addWidget(self._next_btn)
        lyt.addLayout(nav)

        self._render_page()

    # ── Public API called by PrintDialog ──────────────────────────────────────

    def update_settings(self, scale_idx, paper_key, orient_idx, margin_mm):
        changed = (self._scale_idx  != scale_idx  or
                   self._paper_key  != paper_key  or
                   self._orient_idx != orient_idx or
                   self._margin_mm  != margin_mm)
        self._scale_idx  = scale_idx
        self._paper_key  = paper_key
        self._orient_idx = orient_idx
        self._margin_mm  = margin_mm
        if changed:
            self._redraw()

    def set_margin_mm(self, mm):
        self.update_settings(self._scale_idx, self._paper_key,
                             self._orient_idx, mm)

    # ── Internal ──────────────────────────────────────────────────────────────

    def set_pages(self, positions):
        """Restrict the preview to a subset of page positions (the print
        selection: all / current page / range). Jumps to the first page of the
        new selection."""
        positions = list(positions) if positions else list(range(len(self._model.order)))
        if positions == self._pages:
            return
        self._pages   = positions
        self._current = 0
        self._render_page()

    def _prev_page(self):
        if self._current > 0:
            self._current -= 1
            self._render_page()

    def _next_page(self):
        if self._current < len(self._pages) - 1:
            self._current += 1
            self._render_page()

    def _render_page(self):
        self._render_token += 1
        token = self._render_token
        n     = len(self._pages)
        total = len(self._model.order)
        if n == 0:
            self._page_lbl.setText("—")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            self._pixmap = None
            self._redraw()
            return
        if self._current >= n:
            self._current = n - 1
        pos = self._pages[self._current]        # position into model.order
        # Show the real page number (of the whole document) plus position in the
        # selection when a subset is being printed.
        if n == total:
            self._page_lbl.setText(tr('Seite {p0} / {p1}').format(p0=pos + 1, p1=total))
        else:
            self._page_lbl.setText(
                tr('Seite {p0}   ({p1} / {p2} ausgewählt)').format(p0=pos + 1, p1=self._current + 1, p2=n))
        self._prev_btn.setEnabled(self._current > 0)
        self._next_btn.setEnabled(self._current < n - 1)
        self._pixmap    = None
        self._page_w_pt = 595.0
        self._page_h_pt = 842.0
        self._redraw()   # show blank immediately while loading

        if pos >= total:
            return
        uid      = self._model.order[pos]
        src_path, orig = self._model.page_source(uid, self._pdf_path)
        rot      = self._model.get_rotation(uid)

        import threading, weakref
        self_ref = weakref.ref(self)

        def _bg(job):
            try:
                import pypdfium2 as pdfium
                with _pdfium_lock:
                    doc = pdfium.PdfDocument(src_path)
                    try:
                        page = doc[orig]
                        pw_pt = page.get_width()
                        ph_pt = page.get_height()
                        render_scale = 240.0 / max(pw_pt, ph_pt, 1)
                        bm  = page.render(scale=render_scale)
                        pil = bm.to_pil()
                    finally:
                        doc.close()
                if rot:
                    pil = pil.rotate(-rot, expand=True)
                    if rot % 180:
                        pw_pt, ph_pt = ph_pt, pw_pt
                buf = io.BytesIO()
                pil.save(buf, "PNG")
                data = buf.getvalue()
                obj = self_ref()
                if obj is not None and not job.cancelled:
                    try:
                        # Auto-queued to the GUI thread (widget lives there).
                        obj._render_ready.emit(token, data, pw_pt, ph_pt)
                    except RuntimeError:
                        pass   # widget was deleted
            except Exception:
                pass
        from tools.jobs import submit
        self._render_job = submit(_bg, owner=self, name="print-preview-render")

    def _on_render_done(self, token, data, pw_pt, ph_pt):
        if token != self._render_token:
            return   # selection/page changed while rendering — discard stale result
        pm = QPixmap()
        pm.loadFromData(data)
        self._pixmap    = pm
        self._page_w_pt = pw_pt
        self._page_h_pt = ph_pt
        self._redraw()

    def _paper_dims_mm(self):
        """Returns (w_mm, h_mm) for the selected paper in the correct orientation."""
        pw, ph = self._PAPER_MM.get(self._paper_key, (210.0, 297.0))
        # Auto-orient: match paper to page shape
        page_landscape = self._page_w_pt > self._page_h_pt
        if self._orient_idx == 0:   # auto
            paper_landscape = page_landscape
        elif self._orient_idx == 2: # explicit landscape
            paper_landscape = True
        else:                       # explicit portrait
            paper_landscape = False
        if paper_landscape and pw < ph:
            pw, ph = ph, pw
        elif not paper_landscape and pw > ph:
            pw, ph = ph, pw
        return pw, ph

    def _redraw(self):
        from PyQt6.QtCore import QRectF
        cw = self._canvas.width()
        ch = self._canvas.height()
        if cw < 20 or ch < 20:
            return

        paper_w_mm, paper_h_mm = self._paper_dims_mm()
        page_w_mm = self._page_w_pt * 25.4 / 72.0
        page_h_mm = self._page_h_pt * 25.4 / 72.0
        full_bleed = self._margin_mm < 0.5
        m = self._margin_mm
        printable_w = paper_w_mm if full_bleed else max(1.0, paper_w_mm - 2*m)
        printable_h = paper_h_mm if full_bleed else max(1.0, paper_h_mm - 2*m)

        # Compute the scale factor that will actually be applied when printing
        scale_fit  = min(printable_w / max(page_w_mm, 0.001),
                         printable_h / max(page_h_mm, 0.001))
        if self._scale_idx == 0:        # Fit
            content_scale = scale_fit
        elif self._scale_idx == 1:      # 100 %
            content_scale = 1.0
        else:                           # Shrink only
            content_scale = min(1.0, scale_fit)

        content_w_mm = page_w_mm * content_scale
        content_h_mm = page_h_mm * content_scale
        will_clip = (content_w_mm > printable_w + 0.5 or
                     content_h_mm > printable_h + 0.5)

        # Map the paper rectangle into the canvas
        pad = 14
        s = min((cw - pad) / max(paper_w_mm, 1),
                (ch - pad) / max(paper_h_mm, 1))
        pw = int(paper_w_mm * s)
        ph = int(paper_h_mm * s)
        ox = (cw - pw) // 2
        oy = (ch - ph) // 2

        canvas_pm = QPixmap(cw, ch)
        canvas_pm.fill(QColor(_TV['sidebar_bg']))
        p = QPainter(canvas_pm)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Drop shadow
        p.fillRect(ox + 3, oy + 3, pw, ph, QColor(0, 0, 0, 60))
        # White paper
        p.fillRect(ox, oy, pw, ph, QColor(255, 255, 255))

        # Printable-area rect (where content can go)
        if full_bleed:
            pr = QRectF(ox, oy, pw, ph)
        else:
            mx = m * s
            my = m * s
            pr = QRectF(ox + mx, oy + my, pw - 2*mx, ph - 2*my)

        # Content rect — centred within the printable area
        cw_px = content_w_mm * s
        ch_px = content_h_mm * s
        cx = pr.x() + (pr.width()  - cw_px) / 2
        cy = pr.y() + (pr.height() - ch_px) / 2
        content_rect = QRectF(cx, cy, cw_px, ch_px)

        # Draw page image into content_rect (clipped to printable_area if overflows)
        if self._pixmap and not self._pixmap.isNull():
            p.save()
            p.setClipRect(pr)           # clip to printable area
            scaled_page = self._pixmap.scaled(
                max(1, int(cw_px)), max(1, int(ch_px)),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(cx), int(cy), scaled_page)
            # Tint the clipped-off region red so the user sees what gets cut
            if will_clip:
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver)
                # draw a red overlay on the content rect outside the printable area
                full_content = QRectF(cx, cy, cw_px, ch_px)
                clip_tint = QColor(220, 60, 60, 60)
                p.fillRect(full_content, clip_tint)
            p.restore()
        else:
            # No image yet — grey placeholder
            p.fillRect(content_rect, QColor(200, 200, 200))

        # Margin indicator — dashed line showing the printable-area boundary
        if not full_bleed:
            pen = QPen(QColor(180, 100, 100, 200), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(pr.toRect())

        # Paper border
        p.setPen(QPen(QColor(140, 140, 140), 1))
        p.drawRect(ox, oy, pw - 1, ph - 1)

        p.end()
        self._canvas.setPixmap(canvas_pm)

        # Info line
        pct = content_scale * 100.0
        info = (f"{pct:.0f}%  ·  "
                f"{page_w_mm:.0f}×{page_h_mm:.0f} mm  →  "
                f"{paper_w_mm:.0f}×{paper_h_mm:.0f} mm")
        self._info_lbl.setText(info)
        self._clip_lbl.setVisible(will_clip)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._redraw()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._redraw)


_PRINTER_LIST_CACHE = None   # (names:list[str], default:str) — filled on first enumerate


class PrintDialog(QDialog):
    """
    Vollstaendiger Druckdialog mit allen gaengigen Optionen.
    Verwendet Qt QPrinter wenn verfuegbar, sonst Ghostscript/lp als Fallback.
    """

    def done(self, result_code):
        """Close down the background work this dialog started.

        Printer enumeration and the print job itself run on the pool. Closing
        the dialog used to leave them running against a widget on its way out;
        they now stop at their next checkpoint and their results are dropped.
        """
        try:
            from tools.jobs import cancel_owner
            cancel_owner(self)
        except Exception:
            pass
        super().done(result_code)

    # Delivers the async-enumerated printer list to the GUI thread.
    _printers_loaded = pyqtSignal(list, str)

    # Print-job results delivered from the worker thread to the GUI thread.
    # (A background thread has no event loop, so QTimer.singleShot never fires
    # there — signals are auto-queued to the GUI thread instead.)
    _print_status   = pyqtSignal(str)
    _print_finished = pyqtSignal(object, int, object)   # pages, copies, skipped
    _print_failed   = pyqtSignal(str)
    _print_qt_send  = pyqtSignal(object)                # packed args tuple

    # Exact paper dimensions in points (portrait baseline, ISO 216)
    _PAPER_PTS = {
        "A4":        (595.28, 841.89),  "A3":     (841.89, 1190.55),
        "A5":        (419.53, 595.28),  "Letter": (612.0,  792.0),
        "Legal":     (612.0,  1008.0),  "B4":     (708.66, 1000.63),
        "B5":        (498.90, 708.66),  "Executive": (521.86, 756.0),
        "Folio":     (612.0,  936.0),
    }

    def __init__(self, pdf_path, model, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.pdf_path = pdf_path
        self.model    = model
        self._progress = None       # transfer-progress popup while a job spools
        self.setWindowTitle(tr("Drucken"))
        self.setMinimumSize(820, 540)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._setup()

    def _setup(self):
        from PyQt6.QtWidgets import (
            QGridLayout, QRadioButton, QComboBox, QSpinBox,
            QLineEdit, QCheckBox, QLabel, QPushButton,
            QVBoxLayout, QHBoxLayout, QFrame, QScrollArea, QWidget
        )

        # Solid themed background. Without this the dialog inherits the system
        # palette; on a light desktop the transparent settings pane then shows
        # light, and the (light) theme text/labels become invisible — the
        # "invisible hitboxes". Painting the panel colour keeps text readable
        # regardless of the OS theme.
        self.setStyleSheet(f"QDialog{{background:{_TV['panel_bg']};}}")

        # ── Layout helpers ────────────────────────────────────────────────────
        def _sep():
            f = QFrame()
            f.setFrameShape(QFrame.Shape.HLine)
            f.setFixedHeight(1)
            f.setStyleSheet(
                f"background:{_TV['border']};border:none;margin:3px 0;")
            return f

        def _sec(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"font-size:10px;font-weight:bold;letter-spacing:1px;"
                f"color:{_TV['dim']};background:transparent;")
            lbl.setContentsMargins(0, 4, 0, 0)
            return lbl

        def _lbl(text):
            """Row label in a grid: right-aligned, theme colour, transparent bg."""
            l = QLabel(text)
            l.setStyleSheet(
                f"color:{_TV['text']};background:transparent;")
            l.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return l

        # ── Dialog layout: [ preview | divider | settings ] on top, a pinned
        #    action bar at the bottom that is ALWAYS visible (outside the scroll
        #    area, so the Print button never gets clipped on short screens).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addLayout(root, 1)

        # Left: preview panel
        self._preview = _PrintPreview(self.pdf_path, self.model, self)
        root.addWidget(self._preview)

        # 1-px vertical divider — must use background, not color, for QFrame
        div = QFrame()
        div.setFrameShape(QFrame.Shape.VLine)
        div.setFixedWidth(1)
        div.setStyleSheet(
            f"QFrame{{background:{_TV['border']};border:none;}}")
        root.addWidget(div)

        # Right: scrollable settings
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Scope to QScrollArea — a bare "border:none;" here cascades to every
        # child widget and strips the comboboxes'/buttons' own borders, making
        # them look like plain text ("invisible" clickable controls).
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        root.addWidget(scroll, 1)

        right = QWidget()
        right.setObjectName("printSettingsPane")
        # Scope to this widget only, so the transparent background does not
        # cascade onto the child controls.
        right.setStyleSheet(
            "QWidget#printSettingsPane{background:transparent;}")
        scroll.setWidget(right)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(18, 14, 18, 14)
        rl.setSpacing(6)

        n = len(self.model.order)

        # Title — filename, truncated with ellipsis if too long
        title = QLabel(os.path.basename(self.pdf_path))
        title.setStyleSheet(
            f"font-size:13px;font-weight:bold;color:{_TV['text']};"
            f"background:transparent;")
        title.setWordWrap(False)
        rl.addWidget(title)
        rl.addWidget(_sep())

        # ── DRUCKER ──────────────────────────────────────────────────────────
        rl.addWidget(_sec(tr("DRUCKER")))
        self.printer_combo = QComboBox()
        self._hw_margin_mm = 3.0
        rl.addWidget(self.printer_combo)
        rl.addWidget(_sep())

        # ── SEITEN ───────────────────────────────────────────────────────────
        rl.addWidget(_sec(tr("SEITEN")))
        self.radio_all     = QRadioButton(tr("Alle Seiten  (1 – {n})").format(n=n))
        self.radio_current = QRadioButton(tr("Aktuelle Seite"))
        self.radio_range   = QRadioButton(tr("Seitenbereich:"))
        self.radio_all.setChecked(True)
        rl.addWidget(self.radio_all)
        rl.addWidget(self.radio_current)

        range_row = QHBoxLayout()
        range_row.setContentsMargins(0, 0, 0, 0)
        range_row.setSpacing(6)
        range_row.addWidget(self.radio_range)
        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText(tr("z.B.  1-3, 5, 7-9"))
        self.range_edit.setFixedWidth(160)
        self.range_edit.setEnabled(False)
        self.radio_range.toggled.connect(self.range_edit.setEnabled)
        range_row.addWidget(self.range_edit)
        range_row.addStretch()
        rl.addLayout(range_row)
        rl.addWidget(_sep())

        # ── SEITENHANDHABUNG ─────────────────────────────────────────────────
        rl.addWidget(_sec(tr("SEITENHANDHABUNG")))
        pg = QGridLayout()
        pg.setHorizontalSpacing(8)
        pg.setVerticalSpacing(6)
        pg.setColumnMinimumWidth(0, 145)
        pg.setColumnStretch(1, 1)

        pg.addWidget(_lbl(tr("Skalierung:")), 0, 0)
        self.scale_combo = QComboBox()
        self.scale_combo.addItems([
            tr("An Seite anpassen"),
            tr("Originalgrösse  (100%)"),
            tr("Auf bedruckbaren Bereich verkleinern"),
        ])
        self.scale_combo.setItemData(
            0, tr("Skaliert hoch und runter — Seite füllt den Druckbereich vollständig (Acrobat: Fit Page)"),
            Qt.ItemDataRole.ToolTipRole)
        self.scale_combo.setItemData(
            1, tr("Druckt in Originalgrösse — Inhalt kann am Rand beschnitten werden"),
            Qt.ItemDataRole.ToolTipRole)
        self.scale_combo.setItemData(
            2, tr("Verkleinert nur wenn nötig, vergrössert nie (Acrobat: Shrink to Printable Area)"),
            Qt.ItemDataRole.ToolTipRole)
        pg.addWidget(self.scale_combo, 0, 1)

        self._margin_lbl = QLabel("")
        self._margin_lbl.setStyleSheet(
            f"font-size:10px;color:{_TV['dim']};background:transparent;")
        self._margin_lbl.setWordWrap(True)
        self._margin_lbl.setMinimumHeight(28)
        pg.addWidget(self._margin_lbl, 1, 0, 1, 2)

        pg.addWidget(_lbl(tr("Ausrichtung:")), 2, 0)
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(
            [tr("Automatisch"), tr("Hochformat"), tr("Querformat")])
        pg.addWidget(self.orient_combo, 2, 1)

        pg.addWidget(_lbl(tr("Papier:")), 3, 0)
        self.paper_combo = QComboBox()
        pg.addWidget(self.paper_combo, 3, 1)

        rl.addLayout(pg)
        rl.addWidget(_sep())

        # ── AUSGABE ──────────────────────────────────────────────────────────
        rl.addWidget(_sec(tr("AUSGABE")))
        out = QGridLayout()
        out.setHorizontalSpacing(8)
        out.setVerticalSpacing(6)
        out.setColumnMinimumWidth(0, 145)
        out.setColumnStretch(1, 1)

        out.addWidget(_lbl(tr("Kopien:")), 0, 0)
        copies_row = QHBoxLayout()
        copies_row.setContentsMargins(0, 0, 0, 0)
        copies_row.setSpacing(8)
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 999)
        self.copies_spin.setValue(1)
        self.copies_spin.setFixedWidth(60)
        copies_row.addWidget(self.copies_spin)
        self.collate_check = QCheckBox(tr("Sortieren  (1,2,3 / 1,2,3)"))
        self.collate_check.setChecked(True)
        copies_row.addWidget(self.collate_check)
        copies_row.addStretch()
        out.addLayout(copies_row, 0, 1)

        out.addWidget(_lbl(tr("Farbe:")), 1, 0)
        self.color_combo = QComboBox()
        # "Drucker-Standard" sends no colour option at all, so the queue's own
        # setting decides — that is what lets a job be re-routed or configured
        # from somewhere else. Every other PDF viewer on Linux behaves this way.
        self.color_combo.addItem(tr("Drucker-Standard"), "auto")
        self.color_combo.addItem(tr("Farbe"),            "color")
        self.color_combo.addItem(tr("Graustufen"),       "mono")
        self.color_combo.setToolTip(tr(
            "Drucker-Standard: keine Vorgabe senden — der Drucker bzw. die "
            "Warteschlange entscheidet.\n"
            "Die Farbinformation bleibt in jedem Fall in der Datei erhalten."))
        out.addWidget(self.color_combo, 1, 1)

        out.addWidget(_lbl(tr("Farbkonvertierung:")), 2, 0)
        self.colorconv_combo = QComboBox()
        self.colorconv_combo.addItems([
            tr("Unverändert"),
            tr("→ CMYK  (für CMYK-Drucker)"),
            tr("→ sRGB  (für RGB-Drucker)"),
        ])
        self.colorconv_combo.setToolTip(
            tr("Unverändert: Druckertreiber entscheidet (empfohlen mit ICC-Profilen)\n"
               "→ CMYK: Vor dem Druck in CMYK umrechnen\n"
               "→ sRGB: Vor dem Druck in sRGB umrechnen"))
        out.addWidget(self.colorconv_combo, 2, 1)
        self.color_combo.currentIndexChanged.connect(
            lambda _: self.colorconv_combo.setEnabled(
                self.color_combo.currentData() != "mono"))

        duplex_row = QHBoxLayout()
        duplex_row.setContentsMargins(0, 0, 0, 0)
        duplex_row.setSpacing(8)
        self.duplex_check = QCheckBox(tr("Beidseitig drucken  (Duplex)"))
        duplex_row.addWidget(self.duplex_check)
        # Binding edge: long edge (book, back upright) vs short edge (notepad,
        # back rotated 180°). Only meaningful when duplex is on.
        self.duplex_edge_combo = QComboBox()
        self.duplex_edge_combo.addItem(tr("Lange Seite (Buch)"),       "long")
        self.duplex_edge_combo.addItem(tr("Kurze Seite (Notizblock)"), "short")
        self.duplex_edge_combo.setToolTip(
            tr("Lange Seite: Rückseite steht gleich herum wie die Vorderseite "
               "(Bindung an der langen Kante, wie ein Buch).\n"
               "Kurze Seite: Rückseite ist um 180° gedreht "
               "(Bindung an der kurzen Kante, wie ein Notizblock)."))
        self.duplex_edge_combo.setEnabled(False)
        # Edge selection only applies when duplex is enabled.
        self.duplex_check.toggled.connect(self.duplex_edge_combo.setEnabled)
        duplex_row.addWidget(self.duplex_edge_combo)
        duplex_row.addStretch()
        out.addLayout(duplex_row, 3, 0, 1, 2)

        rl.addLayout(out)
        rl.addStretch(1)

        # ── Pinned action bar (status + buttons), always visible ────────────
        # Lives in the dialog's vertical layout, OUTSIDE the scroll area, so the
        # Print button is never clipped when the settings don't fit the height.
        bottom = QWidget()
        bottom.setObjectName("printActionBar")
        bottom.setStyleSheet(
            f"QWidget#printActionBar{{background:{_TV['panel_bg']};"
            f"border-top:1px solid {_TV['border']};}}")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(18, 8, 18, 10); bl.setSpacing(6)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("dimLabel")
        self.status_lbl.setWordWrap(True)
        bl.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch()
        cancel_btn = QPushButton(tr("Abbrechen"))
        cancel_btn.setObjectName("secondaryBtn")
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)
        print_btn = QPushButton(tr("  Drucken  "))
        print_btn.setObjectName("actionBtn")
        print_btn.setMinimumWidth(110)
        print_btn.clicked.connect(self._do_print)
        btn_row.addWidget(print_btn)
        bl.addLayout(btn_row)

        outer.addWidget(bottom)

        # Deliver print-job results from the worker thread to the GUI thread.
        self._print_status.connect(self._on_print_status)
        self._print_finished.connect(self._finish)
        self._print_failed.connect(self._on_print_failed)
        self._print_qt_send.connect(lambda a: self._qt_send_to_printer(*a))

        # All widgets created — populate printers (triggers _on_printer_changed)
        self._load_printers()

        # Live preview: update whenever any print-affecting setting changes
        self.scale_combo.currentIndexChanged.connect(self._sync_preview)
        self.paper_combo.currentIndexChanged.connect(self._sync_preview)
        self.orient_combo.currentIndexChanged.connect(self._sync_preview)

        # Preview follows the page selection (all / current page / range)
        self.radio_all.toggled.connect(self._sync_preview_pages)
        self.radio_current.toggled.connect(self._sync_preview_pages)
        self.radio_range.toggled.connect(self._sync_preview_pages)
        self.range_edit.textChanged.connect(self._sync_preview_pages)
        self._sync_preview_pages()

    def _current_page_pos(self):
        """Position of the page the viewer is showing, or None if unknown.

        Shared by the preview and the print job so they can never disagree
        about which page "Aktuelle Seite" means."""
        parent = self.parent()
        while parent is not None and not isinstance(parent, PdfTab):
            parent = parent.parent()
        if parent is not None and getattr(parent, "single", None) is not None:
            pos = parent.single._current
            if 0 <= pos < len(self.model.order):
                return pos
        return None

    def _preview_pages(self):
        """Page positions the preview should show for the current selection.

        Quiet counterpart to _get_pages() — never touches the status label.
        Returns a list of 0-based positions, or None if the range is currently
        incomplete/invalid (caller then leaves the preview unchanged).
        """
        n = len(self.model.order)
        if self.radio_current.isChecked():
            cur = self._current_page_pos()
            return None if cur is None else [cur]
        if self.radio_range.isChecked():
            text = self.range_edit.text().strip()
            if not text:
                return list(range(n))
            # Same rules as _get_pages(), deliberately: this used to silently
            # clamp, so "5-99" on a ten-page file previewed pages 5–10 and then
            # printing rejected it. The preview must not show a job that will
            # not run.
            pages = []
            try:
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        lo, hi = int(a.strip()), int(b.strip())
                        if lo < 1 or hi > n or lo > hi:
                            return None
                        pages.extend(range(lo - 1, hi))
                    else:
                        p = int(part)
                        if p < 1 or p > n:
                            return None
                        pages.append(p - 1)
            except ValueError:
                return None
            pages = [p for p in sorted(set(pages)) if 0 <= p < n]
            return pages or None
        return list(range(n))   # "Alle Seiten"

    def _sync_preview_pages(self):
        pages = self._preview_pages()
        if pages:   # None/empty → leave the current preview in place
            self._preview.set_pages(pages)

    def _load_printers(self):
        """Populate the printer combo.

        Enumerating printers via CUPS (QPrinterInfo.availablePrinters() or
        `lpstat -e`) takes ~1–2 s — doing it during construction froze the whole
        dialog before it appeared. Instead the dialog opens instantly with a
        placeholder and the list is fetched in a background thread (subprocess —
        safe off the GUI thread, unlike Qt's print classes). The result is
        cached for the session so subsequent opens are instant.
        """
        self.printer_combo.clear()

        # Session cache → instant repeat opens
        if _PRINTER_LIST_CACHE is not None:
            names, default = _PRINTER_LIST_CACHE
            self._apply_printer_list(names, default)
            return

        # First open: show a placeholder, fetch the list off-thread
        self.printer_combo.addItem(tr("Drucker werden geladen…"), "none")
        self.printer_combo.setEnabled(False)
        self._printers_loaded.connect(self._apply_printer_list)

        import threading, weakref
        self_ref = weakref.ref(self)

        def _bg(job):
            names, default = [], ""
            try:
                import subprocess
                r = subprocess.run(["lpstat", "-e"],
                                   capture_output=True, text=True, errors="replace", timeout=15)
                names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            except Exception:
                pass
            if not names:
                # Fallback to Qt enumeration if lpstat is unavailable
                try:
                    from PyQt6.QtPrintSupport import QPrinterInfo
                    names = list(QPrinterInfo.availablePrinterNames())
                except Exception:
                    pass
            try:
                import subprocess
                r = subprocess.run(["lpstat", "-d"],
                                   capture_output=True, text=True, errors="replace", timeout=10)
                out = r.stdout.strip()
                if ":" in out:
                    cand = out.split(":", 1)[1].strip()
                    if cand in names:
                        default = cand
            except Exception:
                pass
            obj = self_ref()
            if obj is not None and not job.cancelled:
                try:
                    obj._printers_loaded.emit(names, default)
                except RuntimeError:
                    pass   # dialog closed
        from tools.jobs import submit
        submit(_bg, owner=self, name="printer-list")

    def _apply_printer_list(self, names, default):
        """Populate the combo from an enumerated printer list (GUI thread)."""
        global _PRINTER_LIST_CACHE
        _PRINTER_LIST_CACHE = (list(names), default)

        try:
            self.printer_combo.currentIndexChanged.disconnect(self._on_printer_changed)
        except TypeError:
            pass   # not connected yet

        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        self.printer_combo.setEnabled(True)
        for nm in names:
            self.printer_combo.addItem(nm, nm)
        if self.printer_combo.count() == 0:
            self.printer_combo.addItem(tr("Kein Drucker gefunden"), "none")
        if default:
            idx = self.printer_combo.findData(default)
            if idx >= 0:
                self.printer_combo.setCurrentIndex(idx)
        self.printer_combo.blockSignals(False)

        self.printer_combo.currentIndexChanged.connect(self._on_printer_changed)
        self._on_printer_changed()   # applies the printer's own defaults

    # Fallback paper list used when printer reports no supported sizes
    _FALLBACK_PAPERS = [
        ("A4  (210 × 297 mm)",    "A4"),
        ("A3  (297 × 420 mm)",    "A3"),
        ("A5  (148 × 210 mm)",    "A5"),
        ("Letter  (216 × 279 mm)", "Letter"),
        ("Legal  (216 × 356 mm)", "Legal"),
    ]

    def _on_printer_changed(self):
        """Aktualisiert Papierformat, Duplex und Farbe basierend auf dem gewaehlten Drucker."""
        try:
            from PyQt6.QtPrintSupport import QPrinterInfo, QPrinter
            from PyQt6.QtGui import QPageSize

            printer_name = self.printer_combo.currentData()
            have_info = printer_name and printer_name not in ("lp", "none")
            info = QPrinterInfo.printerInfo(printer_name) if have_info else None
            valid = info is not None and not info.isNull()

            _qt_to_lp = {
                QPageSize.PageSizeId.A4:        "A4",
                QPageSize.PageSizeId.A3:        "A3",
                QPageSize.PageSizeId.A5:        "A5",
                QPageSize.PageSizeId.Letter:    "Letter",
                QPageSize.PageSizeId.Legal:     "Legal",
                QPageSize.PageSizeId.B4:        "B4",
                QPageSize.PageSizeId.B5:        "B5",
                QPageSize.PageSizeId.Executive: "Executive",
                QPageSize.PageSizeId.Folio:     "Folio",
            }

            # ── Paper sizes ───────────────────────────────────────────────────
            prev_paper = self.paper_combo.currentData()
            self.paper_combo.blockSignals(True)
            self.paper_combo.clear()

            populated = False
            if valid:
                try:
                    supported = info.supportedPageSizes()
                    if supported:
                        seen = set()
                        for ps in supported:
                            key = _qt_to_lp.get(ps.id(), ps.name())
                            if key in seen:
                                continue
                            seen.add(key)
                            mm = ps.size(QPageSize.Unit.Millimeter)
                            label = f"{ps.name()}  ({mm.width():.0f} × {mm.height():.0f} mm)"
                            self.paper_combo.addItem(label, key)
                        populated = True
                except Exception:
                    pass

            if not populated:
                for label, key in self._FALLBACK_PAPERS:
                    self.paper_combo.addItem(label, key)

            # Default to the printer's OWN default page size (fall back to the
            # previously selected paper, then the first entry).
            default_paper = None
            if valid:
                try:
                    default_paper = _qt_to_lp.get(info.defaultPageSize().id())
                except Exception:
                    pass
            target_paper = default_paper or prev_paper
            if target_paper:
                idx = self.paper_combo.findData(target_paper)
                if idx >= 0:
                    self.paper_combo.setCurrentIndex(idx)
            self.paper_combo.blockSignals(False)

            # ── Duplex ────────────────────────────────────────────────────────
            # Qt's supportedDuplexModes()/defaultDuplexMode() is UNRELIABLE for
            # driverless/IPP printers on Linux/CUPS — it reports DuplexNone even
            # for printers that clearly duplex (verified: Brother HL-L5210DN,
            # EPSON ET-8500). The real job is spooled via lp/CUPS (not QPrinter),
            # and CUPS applies the sides= option regardless, so gating the
            # checkbox on this query wrongly greyed it out (the "cosmetic" bug).
            # Keep the control ALWAYS enabled; use the query only for a default.
            self.duplex_check.setEnabled(True)
            self.duplex_check.setToolTip("")
            default_duplex, edge = False, "long"
            if valid:
                try:
                    dm = info.defaultDuplexMode()
                    default_duplex = (dm != QPrinter.DuplexMode.DuplexNone)
                    if dm == QPrinter.DuplexMode.DuplexShortSide:
                        edge = "short"
                except Exception:
                    pass
            self.duplex_check.setChecked(default_duplex)
            ei = self.duplex_edge_combo.findData(edge)
            if ei >= 0:
                self.duplex_edge_combo.setCurrentIndex(ei)
            # Explicitly sync the edge combo to the checkbox — setChecked() only
            # emits toggled() when the state actually changes, so an unchanged
            # (still-unchecked) checkbox would otherwise leave the combo stale.
            self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())

            # ── Color ─────────────────────────────────────────────────────────
            # Same lesson as duplex above: Qt's colour query is not to be trusted
            # on driverless/IPP queues. It reports defaultColorMode() ==
            # GrayScale for an EPSON ET-8500 and for both Xerox colour presses
            # here, and the old code turned that into "open in Graustufen" *and*
            # disabled the control — so a colour press printed monochrome and the
            # user could not switch it back.
            #
            # The control is therefore always enabled and always opens on
            # "Drucker-Standard", which sends no colour option at all. Nothing is
            # forced, nothing is destroyed, and the queue (or whoever picks the
            # settings downstream) decides. A mono-only printer is worth a hint,
            # never a lock.
            self.color_combo.setEnabled(True)
            self.color_combo.blockSignals(True)
            self.color_combo.setCurrentIndex(0)          # "Drucker-Standard"
            self.color_combo.blockSignals(False)
            mono_only = False
            if valid:
                try:
                    modes = info.supportedColorModes()
                    mono_only = bool(modes) and all(
                        m == QPrinter.ColorMode.GrayScale for m in modes)
                except Exception:
                    mono_only = False
            self.color_combo.setToolTip(
                tr("Dieser Drucker meldet nur Graustufen — die Farbe bleibt in "
                   "der Datei erhalten und kann anderswo gedruckt werden.")
                if mono_only else tr(
                    "Drucker-Standard: keine Vorgabe senden — der Drucker bzw. "
                    "die Warteschlange entscheidet.\n"
                    "Die Farbinformation bleibt in jedem Fall in der Datei erhalten."))
            self.colorconv_combo.setEnabled(
                self.color_combo.currentData() != "mono")

            # ── Hardware margins (determines "Fit Page" / "Shrink" behaviour) ──
            self._hw_margin_mm = 3.0  # safe default
            if valid:
                try:
                    tmp = QPrinter(info, QPrinter.PrinterMode.ScreenResolution)
                    layout    = tmp.pageLayout()
                    paper_r   = layout.fullRect(QPageLayout.Unit.Millimeter)
                    page_r    = layout.paintRect(QPageLayout.Unit.Millimeter)
                    margin = min(
                        abs(page_r.left()    - paper_r.left()),
                        abs(page_r.top()     - paper_r.top()),
                        abs(paper_r.right()  - page_r.right()),
                        abs(paper_r.bottom() - page_r.bottom()),
                    )
                    self._hw_margin_mm = max(0.0, margin)
                except Exception:
                    import logging
                    logging.warning("Hardware margin detection failed", exc_info=True)

            self._update_margin_label()
            self._sync_preview()

        except Exception:
            import logging
            logging.warning("Printer capability query failed", exc_info=True)

    def _update_margin_label(self):
        """Updates the info label below the scale combo to reflect hardware margins."""
        m = self._hw_margin_mm
        if m < 0.5:
            text = tr("Randloser Druck — bei gleichem Seitenformat keine Skalierung")
            tip  = tr("Dieser Drucker unterstützt randlosen Druck (full-bleed). "
                      "'An Seite anpassen' ändert eine A4-Seite auf A4-Papier nicht.")
        else:
            text = tr("Druckrand: ca. {m:.1f} mm  (roter Rahmen in Vorschau)").format(m=m)
            tip  = tr("Ca. {m:.1f} mm Hardware-Rand kann nicht bedruckt werden. "
                      "'An Seite anpassen' verkleinert den Inhalt auf den bedruckbaren Bereich.").format(m=m)
        self._margin_lbl.setText(text)
        self._margin_lbl.setToolTip(tip)
        self.scale_combo.setToolTip(tip)

    def _sync_preview(self):
        """Push current dialog settings into the preview widget."""
        self._preview.update_settings(
            scale_idx  = self.scale_combo.currentIndex(),
            paper_key  = self.paper_combo.currentData() or "A4",
            orient_idx = self.orient_combo.currentIndex(),
            margin_mm  = self._hw_margin_mm,
        )

    def _detect_pdf_paper(self):
        """Auto-select the paper size that best matches the PDF's first page."""
        try:
            from pypdf import PdfReader
            reader = PdfReader(self.pdf_path, strict=False)
            if not reader.pages:
                return
            box = reader.pages[0].mediabox
            pw = float(box.width);  ph = float(box.height)
            if pw > ph:             # normalise to portrait for comparison
                pw, ph = ph, pw
            best_key, best_diff = None, float("inf")
            for key, (w, h) in self._PAPER_PTS.items():
                diff = abs(pw - w) + abs(ph - h)
                if diff < best_diff and diff < 15:   # 15 pt ≈ 5 mm tolerance
                    best_diff, best_key = diff, key
            if best_key:
                idx = self.paper_combo.findData(best_key)
                if idx >= 0:
                    self.paper_combo.setCurrentIndex(idx)
                    self._sync_preview()
        except Exception:
            pass

    def _get_pages(self):
        """Returns list of page indices to print, or None on validation error."""
        n = len(self.model.order)
        if self.radio_all.isChecked():
            return list(range(n))
        elif self.radio_current.isChecked():
            cur = self._current_page_pos()
            if cur is None:
                # Never quietly substitute page 1 for the page the user is
                # looking at — that prints the wrong sheet and says nothing.
                self.status_lbl.setText(tr(
                    "Aktuelle Seite kann nicht ermittelt werden — bitte "
                    "»Alle Seiten« oder einen Bereich waehlen."))
                return None
            return [cur]
        else:
            text = self.range_edit.text().strip()
            if not text:
                return list(range(n))
            pages = []
            try:
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        lo, hi = int(a.strip()), int(b.strip())
                        if lo < 1 or hi > n or lo > hi:
                            raise ValueError()
                        pages.extend(range(lo - 1, hi))
                    else:
                        p = int(part)
                        if p < 1 or p > n:
                            raise ValueError()
                        pages.append(p - 1)
            except ValueError:
                self.status_lbl.setText(
                    tr("Ungültiger Seitenbereich — bitte Zahlen zwischen 1 und {n} eingeben.").format(n=n))
                return None
            pages = [p for p in sorted(set(pages)) if 0 <= p < n]
            if not pages:
                self.status_lbl.setText(
                    tr("Kein gültiger Seitenbereich für {n}-seitige Datei.").format(n=n))
                return None
            return pages

    def _write_subset_pdf(self, pages, dest_path):
        """Write a subset of pages (with rotations) to dest_path."""
        from pypdf import PdfReader, PdfWriter
        readers = {}
        def _rdr(p):
            if p not in readers:
                readers[p] = PdfReader(p, strict=False)
            return readers[p]

        writer = PdfWriter()
        skipped = []
        try:
            for pos in pages:
                uid            = self.model.order[pos]
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                rot            = self.model.get_rotation(uid)
                try:
                    writer.add_page(_rdr(src_path).pages[orig])
                    if rot:
                        writer.pages[-1].rotate(rot)   # rotate writer's copy, not reader's
                except Exception:
                    skipped.append(pos + 1)
            if not writer.pages:
                raise RuntimeError(tr("Keine Seiten konnten gelesen werden (Datei beschädigt?)."))
            with open(dest_path, "wb") as f:
                writer.write(f)
        finally:
            for r in readers.values():
                try: r.stream.close()
                except Exception: pass
        return skipped

    def _set_printing(self, busy):
        """Disable/re-enable controls while a print job is in progress."""
        for w in [self.printer_combo, self.copies_spin,
                  self.scale_combo, self.paper_combo, self.orient_combo,
                  self.color_combo, self.colorconv_combo,
                  self.collate_check, self.duplex_check, self.duplex_edge_combo,
                  self.radio_all, self.radio_current, self.radio_range,
                  self.range_edit]:
            w.setEnabled(not busy)
        for btn in self.findChildren(QPushButton):
            btn.setEnabled(not busy)
        # The edge selector is only usable while duplex is on — re-sync it to
        # the checkbox after a job so it doesn't stay enabled when duplex is off.
        if not busy:
            self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())

    def _do_print(self):
        pages_to_print = self._get_pages()
        if pages_to_print is None:
            return
        if not pages_to_print:
            self.status_lbl.setText(tr("Keine Seiten ausgewählt."))
            return

        printer_name = self.printer_combo.currentData()
        if printer_name == "none":
            self.status_lbl.setText(tr("Kein Drucker verfügbar."))
            return

        copies    = self.copies_spin.value()
        color_mode = self.color_combo.currentData() or "auto"
        colorconv = self.colorconv_combo.currentIndex()
        collate   = self.collate_check.isChecked()
        duplex    = self.duplex_check.isChecked()
        duplex_edge = self.duplex_edge_combo.currentData() or "long"
        scale_idx  = self.scale_combo.currentIndex()
        paper_key  = self.paper_combo.currentData() or "A4"
        orient_idx = self.orient_combo.currentIndex()

        try:
            from pypdf import PdfReader
            if PdfReader(self.pdf_path, strict=False).is_encrypted:
                self.status_lbl.setText(
                    tr("Fehler: PDF ist passwortgeschützt — "
                       "bitte zuerst entsperren."))
                return
        except Exception:
            pass

        n = len(pages_to_print)
        self.status_lbl.setText(
            tr("Sende {n} Seite(n) an »{name}«…").format(
                n=n, name=self.printer_combo.currentText()))
        self._set_printing(True)

        # Progress popup — shows the transfer stages as they happen.
        from PyQt6.QtWidgets import QProgressDialog
        self._progress = QProgressDialog(
            tr("Druckauftrag wird vorbereitet…"), "", 0, 100, self)
        self._progress.setWindowTitle(tr("Drucken"))
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setCancelButton(None)       # a spooling job can't be cancelled
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setValue(8)
        self._progress.show()
        QApplication.processEvents()

        # ── Query printer DPI on GUI thread — QPrinter must not be used from ──
        # ── background threads (Qt constraint).                               ──
        try:
            from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
            if printer_name and printer_name not in ("lp", "none"):
                _pi = QPrinterInfo.printerInfo(printer_name)
                _qp = (QPrinter(_pi, QPrinter.PrinterMode.HighResolution)
                       if not _pi.isNull()
                       else QPrinter(QPrinter.PrinterMode.HighResolution))
            else:
                _qp = QPrinter(QPrinter.PrinterMode.HighResolution)
            qt_dpi = _qp.resolution()
            del _qp
        except Exception:
            qt_dpi = 600   # laser-printer safe default
        hw_margin_mm = self._hw_margin_mm   # capture now; _on_printer_changed won't run in bg

        import shutil, threading, weakref
        self_ref = weakref.ref(self)

        def _report(msg):
            obj = self_ref()
            if obj is not None:
                try:
                    obj._print_status.emit(msg)   # queued to the GUI thread
                except RuntimeError:
                    pass

        def _bg(job):
            errors = []

            # ── Primary: Ghostscript + lp/CUPS ───────────────────────────────
            if shutil.which("lp"):
                try:
                    skipped = self._print_via_gs(
                        pages_to_print, copies, color_mode, collate, duplex,
                        duplex_edge, colorconv, printer_name, scale_idx,
                        paper_key, orient_idx, hw_margin_mm, _report)
                    obj = self_ref()
                    if obj is not None:
                        obj._print_finished.emit(pages_to_print, copies, skipped)
                    return
                except Exception as e:
                    errors.append(f"GS/lp: {e}")
                    _report(tr("GS-Pfad fehlgeschlagen — Versuche Qt-Fallback…"))
                    # The rasteriser cannot do the colour-space conversions, so
                    # say so instead of printing a job that quietly ignores the
                    # setting the operator chose.
                    if colorconv in (1, 2):
                        _report(tr(
                            "Hinweis: Der Fallback kann die gewaehlte "
                            "Farbraum-Umwandlung nicht ausfuehren — es wird "
                            "ohne sie gedruckt."))

            # ── Fallback: Qt rasteriser ───────────────────────────────────────
            # Pre-render pages in background (pdfium, no QPrinter); draw on GUI thread.
            try:
                rendered, skipped = self._prerender_for_qt(
                    pages_to_print, color_mode, scale_idx, orient_idx,
                    paper_key, qt_dpi, hw_margin_mm, _report)
            except Exception as e:
                errors.append(f"Qt render: {e}")
                msg = tr("Druckfehler:") + "\n" + "\n".join(errors)
                obj = self_ref()
                if obj is not None and not job.cancelled:
                    obj._print_failed.emit(msg)
                return

            obj = self_ref()
            if obj is not None and not job.cancelled:
                obj._print_qt_send.emit((
                    rendered, skipped, pages_to_print, copies, color_mode,
                    collate, duplex, duplex_edge, printer_name, paper_key,
                    orient_idx))

        from tools.jobs import submit
        self._print_job = submit(_bg, owner=self, name="print-job")

    def _progress_pct(self, msg):
        """Map a status message to an approximate transfer-progress percentage.

        Matches on language-independent tokens (the ``x / y`` fraction) and on
        both the German and English keyword stems, so translated status text
        still advances the progress bar correctly.
        """
        import re
        m = re.search(r'(\d+)\s*/\s*(\d+)', msg)   # "Seite x / y" / "page x / y"
        if m:   # per-page rendering (Qt fallback) gives a real fraction
            x, tot = int(m.group(1)), max(1, int(m.group(2)))
            return 15 + int(60 * x / tot)
        low = msg.lower()
        if "zusammenstellen" in low or "collecting" in low:  return 20
        if "ghostscript" in low or "normalis" in low:        return 50
        if "sende an drucker" in low or "sending to printer" in low: return 85
        if "fallback" in low or "render" in low:             return 30
        return None

    def _on_print_status(self, msg):
        self.status_lbl.setText(msg)
        if self._progress is not None:
            self._progress.setLabelText(msg)
            pct = self._progress_pct(msg)
            if pct is not None:
                # Only ever advance the bar, never jump backwards.
                self._progress.setValue(max(self._progress.value(), pct))

    def _close_progress(self):
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _on_print_failed(self, msg):
        self._close_progress()
        self.status_lbl.setText(msg)
        self._set_printing(False)   # keep the dialog open so the user can retry

    def _finish(self, pages, copies, skipped):
        # Count what was actually sent, not what was asked for: a page that
        # could not be read was already dropped from the job, so reporting the
        # requested figure told the operator more sheets were coming than the
        # printer had been given — while listing the skipped pages in the same
        # breath.
        sent  = max(0, len(pages) - len(skipped or []))
        total = sent * copies
        msg = tr("Druckauftrag gesendet: "
                 "{pages} Seite(n) × {copies} Kopie(n) = {total} Blatt.").format(
                     pages=sent, copies=copies, total=total)
        if skipped:
            msg += tr("  (Übersprungen: S. {skipped})").format(skipped=skipped)
        self.status_lbl.setText(msg)
        if self._progress is not None:
            self._progress.setLabelText(tr("Fertig — an Drucker gesendet."))
            self._progress.setValue(100)
        # Briefly show 100 %, then close the popup AND the print dialog.
        QTimer.singleShot(600, self._after_print_close)

    def _after_print_close(self):
        self._close_progress()
        self.accept()

    def _recenter_on_paper(self, src_path, dest_path, paper_w_pt, paper_h_pt):
        """Enlarge every page's media box to the full physical sheet size and
        centre the existing (already-scaled) content on it.

        Ghostscript fits the content to the *printable area* (media set to
        paper − hardware margins).  This step places that printable-area page,
        unscaled, in the exact centre of a full-size sheet so the printer driver
        receives correctly-sized media and does not rescale — the on-screen
        preview and the physical print then agree.  The GS output is already
        normalised (upright, /Rotate cleared, media box at the origin), so a
        plain translate + media-box resize is safe here.
        """
        from pypdf import PdfReader, PdfWriter, Transformation
        from pypdf.generic import RectangleObject
        reader = PdfReader(src_path, strict=False)
        writer = PdfWriter()
        for page in reader.pages:
            box = page.mediabox
            x0 = float(box.left);   y0 = float(box.bottom)
            w0 = float(box.width);  h0 = float(box.height)
            tx = (paper_w_pt - w0) / 2.0 - x0
            ty = (paper_h_pt - h0) / 2.0 - y0
            page.add_transformation(Transformation().translate(tx, ty))
            full = RectangleObject([0, 0, paper_w_pt, paper_h_pt])
            page.mediabox = full
            page.cropbox  = full
            writer.add_page(page)
        if not writer.pages:
            raise RuntimeError(tr("Re-centre: keine Seiten."))
        with open(dest_path, "wb") as f:
            writer.write(f)

    def _print_via_gs(self, pages, copies, color_mode, collate, duplex,
                      duplex_edge, colorconv, printer_name, scale_idx,
                      paper_key, orient_idx, hw_margin_mm, report):
        """Full-quality print via Ghostscript + CUPS/lp.

        GS normalises, embeds fonts, applies colour conversion AND pre-scales the
        output to exactly the target paper dimensions.  lp then receives a
        correctly-sized PDF and is told print-scaling=none — no double-scaling.

        For "Fit" and "Shrink" the content is fitted to the *printable area*
        (paper minus the printer's unprintable hardware margin), matching the
        on-screen preview exactly, then re-centred on a full-size sheet so the
        printer never clips content or rescales.
        """
        import subprocess, shutil, tempfile, os

        pw_pt, ph_pt = PrintDialog._PAPER_PTS.get(paper_key, (595.28, 841.89))

        # Determine target paper orientation
        if orient_idx == 2:          # explicit landscape
            pw_pt, ph_pt = ph_pt, pw_pt
        elif orient_idx == 0:        # auto — detect from first selected page
            try:
                uid = self.model.order[pages[0]]
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                import pypdfium2 as pdfium
                with _pdfium_lock:
                    doc = pdfium.PdfDocument(src_path)
                    try:
                        pg = doc[orig]; pdfw = pg.get_width(); pdfh = pg.get_height()
                    finally:
                        doc.close()
                rot0 = self.model.get_rotation(self.model.order[pages[0]])
                if (pdfw > pdfh) != bool(rot0 % 180):   # page is landscape
                    if pw_pt < ph_pt:
                        pw_pt, ph_pt = ph_pt, pw_pt
            except Exception:
                pass
        # orient_idx == 1 → portrait: keep pw_pt < ph_pt as-is

        sub_fd, sub_tmp = tempfile.mkstemp(suffix="_sub.pdf")
        os.close(sub_fd)
        norm_tmp = None
        recenter_tmp = None
        printable_unrecentered = False
        try:
            report(tr("Seiten zusammenstellen… ({count})").format(count=len(pages)))
            skipped = self._write_subset_pdf(pages, sub_tmp)
            print_src = sub_tmp
            cups_fit  = (scale_idx == 0)   # used by both the GS + lp branches

            if shutil.which("gs"):
                norm_fd, norm_tmp = tempfile.mkstemp(suffix="_norm.pdf")
                os.close(norm_fd)
                report(tr("Ghostscript: Normalisierung und Skalierung…"))

                # ── Scaling strategy ──────────────────────────────────────────
                # "Fit" (scale_idx 0) is delegated to CUPS/pdftopdf, which scales
                # each page to the driver's REAL imageable area — the printer's
                # actual margins, exactly like Acrobat's "Fit to Printable Area"
                # (and it adapts per printer). GS just normalises at natural size.
                # "Shrink" (scale_idx 2) pre-fits to our printable-area estimate
                # and re-centres it; "100 %" (scale_idx 1) prints 1:1 on full media.
                margin_pt = (0.0 if hw_margin_mm < 0.5
                             else hw_margin_mm * 72.0 / 25.4)
                fit_to_printable = (scale_idx == 2) and margin_pt > 0.0
                if fit_to_printable:
                    media_w = max(1.0, pw_pt - 2.0 * margin_pt)
                    media_h = max(1.0, ph_pt - 2.0 * margin_pt)
                else:
                    media_w, media_h = pw_pt, ph_pt

                gs_cmd = [
                    "gs", "-dBATCH", "-dNOPAUSE", "-dQUIET",
                    "-sDEVICE=pdfwrite",
                    "-dCompatibilityLevel=1.5",
                    "-dPDFSETTINGS=/printer",
                    "-dEmbedAllFonts=true",
                    "-dSubsetFonts=true",
                    "-dCompressFonts=true",
                    "-dDetectDuplicateImages=true",
                ]
                if not cups_fit:
                    # Fix output media to the fit target (printable area or sheet).
                    gs_cmd += [
                        f"-dDEVICEWIDTHPOINTS={media_w}",
                        f"-dDEVICEHEIGHTPOINTS={media_h}",
                        "-dFIXEDMEDIA",
                    ]

                # Scaling policy
                if cups_fit:
                    pass                # CUPS fits to the imageable area (see lp -o)
                elif scale_idx == 2:    # Shrink only — fit if ANY page exceeds target
                    try:
                        import pypdfium2 as pdfium
                        needs_fit = False
                        with _pdfium_lock:
                            doc = pdfium.PdfDocument(sub_tmp)
                            try:
                                for pi in range(len(doc)):
                                    pg = doc[pi]
                                    if pg.get_width() > media_w + 1 or pg.get_height() > media_h + 1:
                                        needs_fit = True
                                        break
                            finally:
                                doc.close()
                        if needs_fit:
                            gs_cmd.append("-dPDFFitPage")
                        # else: all pages fit — FIXEDMEDIA centres at natural size
                    except Exception:
                        gs_cmd.append("-dPDFFitPage")   # safe fallback
                # scale_idx == 1 (100 %): FIXEDMEDIA without -dPDFFitPage → 1:1

                # Colour handling.
                # "Graustufen" deliberately does NOT convert the PDF here. It
                # used to, and that destroyed the colour in the spooled file:
                # the job was monochrome for good, so a queue that was later
                # re-routed, or settings picked on another machine, could never
                # bring the colour back. Mono is requested as a CUPS job option
                # instead (see print-color-mode below) and the printer does the
                # conversion, exactly as Evince, Chrome and Acrobat do it. The
                # explicit "Farbkonvertierung" choices below stay, because there
                # the user is asking for the data itself to be converted.
                if colorconv == 1:
                    gs_cmd += ["-sColorConversionStrategy=CMYK",
                                "-dProcessColorModel=/DeviceCMYK"]
                elif colorconv == 2:
                    gs_cmd += ["-sColorConversionStrategy=sRGB",
                                "-dProcessColorModel=/DeviceRGB"]
                else:
                    gs_cmd += ["-sColorConversionStrategy=LeaveColorUnchanged"]

                gs_cmd += [f"-sOutputFile={norm_tmp}", sub_tmp]
                r = subprocess.run(gs_cmd, capture_output=True, text=True, errors="replace", timeout=240)
                # Only when a colour conversion was actually asked for, and only
                # once Ghostscript reported success — otherwise there is nothing
                # meaningful to compare against.
                converted = colorconv in (1, 2)
                blackout = (_gs_blacked_out(sub_tmp, norm_tmp)
                            if (converted and r.returncode == 0
                                and os.path.getsize(norm_tmp) > 100)
                            else None)
                if blackout:
                    # Print unconverted rather than print black paper. Same
                    # fallback Ghostscript failing outright already takes.
                    logging.error("print: colour conversion blacked out page(s) %s "
                                  "— printing the unconverted file", blackout)
                    AppState.get().status_message.emit(tr(
                        'Farbumwandlung hat Seite(n) {p0} geschwaerzt — es wird '
                        'ohne Umwandlung gedruckt.').format(
                            p0=", ".join(str(i + 1) for i in blackout)))
                elif r.returncode == 0 and os.path.getsize(norm_tmp) > 100:
                    print_src = norm_tmp
                    # Re-centre the printable-area page on a full-size sheet so
                    # CUPS receives correctly-sized media and never rescales.
                    if fit_to_printable:
                        try:
                            rec_fd, recenter_tmp = tempfile.mkstemp(suffix="_ctr.pdf")
                            os.close(rec_fd)
                            self._recenter_on_paper(norm_tmp, recenter_tmp,
                                                    pw_pt, ph_pt)
                            if os.path.getsize(recenter_tmp) > 100:
                                print_src = recenter_tmp
                            else:
                                printable_unrecentered = True
                        except Exception:
                            import logging
                            logging.warning("Re-centre step failed; letting CUPS "
                                            "fit to printable area", exc_info=True)
                            printable_unrecentered = True
                else:
                    import logging
                    logging.warning("GS normalization failed (rc=%d): %s",
                                    r.returncode, r.stderr[:300])

            # Spool via lp/CUPS
            report(tr("Sende an Drucker…"))
            cmd = ["lp"]
            if printer_name and printer_name not in ("lp", "none"):
                cmd += ["-d", printer_name]
            cmd += ["-n", str(copies)]

            # Scaling: "Fit" → let CUPS fit to the driver's imageable area;
            # everything else was already sized exactly by GS (+ re-centre).
            if cups_fit:
                cmd += ["-o", "fit-to-page"]
            elif print_src in (norm_tmp, recenter_tmp) and not printable_unrecentered:
                cmd += ["-o", "print-scaling=none"]
            elif printable_unrecentered:
                # Re-centre failed: page is printable-area sized — let CUPS fit it
                # to the imageable area (single scaling, centred, no clipping).
                cmd += ["-o", "print-scaling=fit"]
            else:
                # GS unavailable — let CUPS handle scaling
                if scale_idx == 0:
                    cmd += ["-o", "fit-to-page"]
                elif scale_idx == 1:
                    cmd += ["-o", "print-scaling=none"]
                else:
                    cmd += ["-o", "fit-to-page"]

            if orient_idx == 1:
                cmd += ["-o", "orientation-requested=3"]
            elif orient_idx == 2:
                cmd += ["-o", "orientation-requested=4"]

            if duplex:
                # Emit BOTH the IPP attribute AND the PPD driver keyword.
                # Driverless/IPP queues (e.g. the Xerox) honour `sides`; the
                # EPSON/Brother-style PPD filters read the `Duplex` keyword and
                # otherwise fall back to the queue's stored default — which is
                # what made portrait backs come out tumbled (short edge) even
                # though we asked for long edge. Both values are synonyms, and
                # CUPS ignores whichever the driver doesn't understand, so
                # sending both is safe and forces the correct binding edge.
                if duplex_edge == "short":
                    cmd += ["-o", "sides=two-sided-short-edge",
                            "-o", "Duplex=DuplexTumble"]
                else:
                    cmd += ["-o", "sides=two-sided-long-edge",
                            "-o", "Duplex=DuplexNoTumble"]
            # Force the colour mode explicitly so this job overrides the
            # printer's system-wide default (e.g. a queue whose default is
            # monochrome must still print in colour when the user picks "Farbe").
            # print-color-mode is the driver-independent IPP attribute;
            # ColorModel=Gray is kept as a fallback for older PPD-only drivers.
            if color_mode == "mono":
                cmd += ["-o", "print-color-mode=monochrome",
                        "-o", "ColorModel=Gray"]
            elif color_mode == "color":
                cmd += ["-o", "print-color-mode=color"]
            # "auto": send no colour option at all, so the queue's own default
            # applies and the decision can still be made downstream.

            # Collation via standard IPP multiple-document-handling attribute
            if copies > 1:
                mdc = ("separate-documents-collated-copies" if collate
                       else "separate-documents-uncollated-copies")
                cmd += ["-o", f"multiple-document-handling={mdc}"]

            cmd += ["-o", f"media={paper_key}"]
            cmd.append(print_src)

            result = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or result.stdout.strip()
                    or f"lp: exit code {result.returncode}")
            return skipped

        finally:
            for f in [sub_tmp, norm_tmp, recenter_tmp]:
                if f:
                    try: os.unlink(f)
                    except Exception: pass

    def _prerender_for_qt(self, pages, color_mode, scale_idx, orient_idx,
                          paper_key, qt_dpi, hw_margin_mm, report):
        """Rasterise PDF pages via pypdfium2 in background.

        No QPrinter usage here — Qt objects must stay on the GUI thread.
        Dimensions are computed directly from paper points × DPI / 72.

        Returns (rendered_list, skipped_list).
        rendered_list items: (pil_image, page_orient, target_w_px, target_h_px)
        """
        import pypdfium2 as pdfium

        pw_pt, ph_pt = PrintDialog._PAPER_PTS.get(paper_key, (595.28, 841.89))
        full_bleed   = hw_margin_mm < 0.5
        margin_px    = 0 if full_bleed else max(0, int(hw_margin_mm / 25.4 * qt_dpi))

        def _target_dims(landscape):
            w_pt, h_pt = (ph_pt, pw_pt) if landscape else (pw_pt, ph_pt)
            w_px = max(1, int(w_pt * qt_dpi / 72) - 2 * margin_px)
            h_px = max(1, int(h_pt * qt_dpi / 72) - 2 * margin_px)
            return w_px, h_px

        rendered    = []
        skipped     = []
        pdfium_docs: dict = {}

        try:
            for i, pos in enumerate(pages):
                report(tr("Rendere Seite {i} / {total}…").format(i=i + 1, total=len(pages)))
                uid = self.model.order[pos]
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                rot = self.model.get_rotation(uid)
                try:
                    with _pdfium_lock:
                        if src_path not in pdfium_docs:
                            pdfium_docs[src_path] = pdfium.PdfDocument(src_path)
                        pdfpage = pdfium_docs[src_path][orig]
                        pdfw    = pdfpage.get_width()
                        pdfh    = pdfpage.get_height()

                        # Determine per-page orientation
                        if orient_idx == 0:
                            page_is_ls = (pdfw > pdfh) != bool(rot % 180)
                        else:
                            page_is_ls = (orient_idx == 2)
                        page_orient = (QPageLayout.Orientation.Landscape
                                       if page_is_ls
                                       else QPageLayout.Orientation.Portrait)

                        target_w, target_h = _target_dims(page_is_ls)

                        scale_fit = min(target_w / max(pdfw, 1),
                                        target_h / max(pdfh, 1))
                        scale_100 = qt_dpi / 72.0
                        if scale_idx == 0:
                            scale = scale_fit
                        elif scale_idx == 1:
                            scale = scale_100
                        else:
                            scale = min(scale_100, scale_fit)

                        bm  = pdfpage.render(scale=max(0.5, scale))
                        pil = bm.to_pil().convert("RGB")

                    # No convert("L") here either: the QPrinter colour mode
                    # below carries the request, and throwing the colour away in
                    # the raster made this path just as irreversible as the
                    # Ghostscript one.
                    if rot:
                        pil = pil.rotate(-rot, expand=True)

                    rendered.append((pil, page_orient, target_w, target_h))

                except Exception:
                    import logging; logging.exception("Qt render: page %d", pos + 1)
                    skipped.append(pos + 1)
        finally:
            for doc in pdfium_docs.values():
                try: doc.close()
                except Exception: pass

        if not rendered:
            raise RuntimeError(tr("Keine Seiten konnten gerendert werden."))
        return rendered, skipped

    def _qt_send_to_printer(self, rendered, skipped, pages, copies, color_mode,
                             collate, duplex, duplex_edge, printer_name,
                             paper_key, orient_idx):
        """Draw pre-rendered images to QPrinter.  MUST run on the GUI thread."""
        from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
        from PyQt6.QtGui import QPainter

        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            if printer_name and printer_name not in ("lp", "none"):
                info = QPrinterInfo.printerInfo(printer_name)
                if not info.isNull():
                    printer = QPrinter(info, QPrinter.PrinterMode.HighResolution)

            printer.setCopyCount(copies)
            printer.setCollateCopies(collate)
            if duplex:
                printer.setDuplex(
                    QPrinter.DuplexMode.DuplexShortSide if duplex_edge == "short"
                    else QPrinter.DuplexMode.DuplexLongSide)
            if color_mode == "mono":
                printer.setColorMode(QPrinter.ColorMode.GrayScale)
            elif color_mode == "color":
                printer.setColorMode(QPrinter.ColorMode.Color)

            _lp_to_qt = {
                "A4": QPrinter.PageSize.A4,   "A3": QPrinter.PageSize.A3,
                "A5": QPrinter.PageSize.A5,   "Letter": QPrinter.PageSize.Letter,
                "Legal": QPrinter.PageSize.Legal, "B4": QPrinter.PageSize.B4,
                "B5": QPrinter.PageSize.B5,   "Executive": QPrinter.PageSize.Executive,
                "Folio": QPrinter.PageSize.Folio,
            }
            printer.setPageSize(_lp_to_qt.get(paper_key, QPrinter.PageSize.A4))
            if orient_idx == 1:
                printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            elif orient_idx == 2:
                printer.setPageOrientation(QPageLayout.Orientation.Landscape)

            painter = QPainter()
            if not painter.begin(printer):
                raise RuntimeError(tr("QPainter konnte nicht gestartet werden."))
            try:
                first = True
                for pil, page_orient, target_w, target_h in rendered:
                    if orient_idx == 0:
                        printer.setPageOrientation(page_orient)
                    if not first:
                        printer.newPage()
                    first = False

                    pm = pil_to_qpixmap(pil)
                    cx = max(0, int((target_w - pm.width())  / 2))
                    cy = max(0, int((target_h - pm.height()) / 2))
                    painter.drawPixmap(cx, cy, pm)
            finally:
                painter.end()

            self._finish(pages, copies, skipped)

        except Exception as e:
            self._on_print_failed(tr("Qt-Druckfehler: {e}").format(e=e))


# ══════════════════════════════════════════════════════════════════════════════
# PDF TAB
# ══════════════════════════════════════════════════════════════════════════════

class PdfTab(QWidget):
    changed = pyqtSignal()

    def __init__(self, pdf_path, parent=None):
        super().__init__(parent)
        self.pdf_path = pdf_path
        self.model    = None
        self._setup()
        self._load()

    def _setup(self):
        self._stack = QStackedWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._stack, 1)

        # Einzelansicht
        self.single = SinglePageView()
        self.single.page_changed.connect(self._on_page_changed)
        self._stack.addWidget(self.single)
        self._manage_widget = None
        self._manage_panel  = None

    def cancel_render_work(self):
        """Stop everything this tab has on the render queue.

        Closing a tab cancelled its jobs.py work but nothing on _RenderQueue, so
        a pre-render for a document nobody is looking at any more went on holding
        the one render thread — and the pdfium lock with it — while the tab the
        user switched to waited behind it."""
        self.single.stop_background_work()

    def _load(self):
        """Load the document, or raise.

        This used to swallow every failure into a log line, so a corrupt or
        encrypted PDF opened a blank tab with model=None and no explanation —
        and every feature downstream then had to survive a tab with no model.
        Both construction sites report the exception instead."""
        from pypdf import PdfReader
        try:
            reader = PdfReader(self.pdf_path, strict=False)
            encrypted = reader.is_encrypted
            n = len(reader.pages)
        except Exception as e:
            import logging; logging.error(f"PdfTab._load: {e}")
            raise RuntimeError(tr('Die Datei ist beschaedigt oder keine gueltige PDF.\n{p0}')
                               .format(p0=e)) from e
        if encrypted:
            raise RuntimeError(tr(
                "Diese PDF ist passwortgeschützt.\n"
                "Bitte zuerst entsperren (Passwort entfernen), dann erneut öffnen."))
        if n == 0:
            raise RuntimeError(tr("Das PDF enthaelt keine Seiten."))
        self.model = PageModel(n)
        self.single.load(self.pdf_path, self.model)
        AppState.get().page_model   = self.model
        AppState.get().current_page = 0
        _set_active(self.pdf_path, 0)

    def _on_page_changed(self, page_num):
        if self.model:
            AppState.get().current_page = page_num - 1
            _set_active(self.pdf_path, page_num - 1)
            # Slide the pre-render window to keep pages ahead warm
            QTimer.singleShot(200, self.single._prerender_all)

    def _build_manage_once(self):
        """Create PageGrid + ManagePanel once and cache them for the lifetime of the tab."""
        if self._manage_panel is not None:
            return  # already built
        grid = PageGrid(self.model, self.pdf_path)
        grid_scroll = QScrollArea()
        grid_scroll.setWidgetResizable(True)
        grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        grid_scroll.setWidget(grid)

        panel = ManagePanel(self.model, self.pdf_path, grid, parent=self, tab=self)
        panel.hide()
        panel.closed.connect(self._exit_manage)
        grid.order_changed.connect(self.changed.emit)
        grid.order_changed.connect(panel.update_info)
        grid.selection_changed.connect(panel.update_info)

        self._manage_panel = panel
        self._manage_widget = grid_scroll
        self._stack.addWidget(grid_scroll)

    def _enter_manage(self, on_exit=None):
        self._on_manage_exit = on_exit
        self._build_manage_once()
        self._manage_panel.show()
        self._stack.setCurrentWidget(self._manage_widget)

    def _exit_manage(self):
        self._stack.setCurrentWidget(self.single)
        # Restore sidebar / layout via callback
        cb = getattr(self, '_on_manage_exit', None)
        if cb:
            cb()
            self._on_manage_exit = None
        # Jump to the last selected page
        if self.model and self.model.selected:
            last_pos = max(
                pos for pos, uid in enumerate(self.model.order)
                if uid in self.model.selected)
            self.single._current = last_pos
        self.single.refresh()
        self.single._view.setFocus()
        self.changed.emit()

    def _print(self):
        """Oeffnet den vollstaendigen Druckdialog."""
        if not self.model:
            return
        dlg = PrintDialog(self.pdf_path, self.model, self)
        dlg.exec()

    def page_count(self):
        return len(self.model.order) if self.model else 0

    def in_manage_mode(self):
        """True while this tab is showing the page manager rather than the
        single-page preview."""
        return bool(self._manage_widget is not None
                    and self._stack.currentWidget() is self._manage_widget)

    def selected_uids(self):
        """Selected pages in display order — empty when nothing is picked."""
        if not self.model: return []
        return [uid for uid in self.model.order if uid in self.model.selected]

    def save_to(self, out_path, uids=None):
        """Write the document as the page manager shows it. This is what Ctrl+S
        and Datei ▸ Speichern go through.

        `uids` limits the output to those pages, in display order — used by
        Ctrl+Shift+S when pages are picked in the page manager."""
        if not self.model: raise ValueError(tr("Keine PDF geladen."))
        import tempfile
        from pypdf import PdfReader, PdfWriter
        readers = {}
        def _rdr(p):
            if p not in readers: readers[p] = PdfReader(p, strict=False)
            return readers[p]
        writer = PdfWriter()
        wanted = None if uids is None else set(uids)
        for uid in self.model.order:
            if wanted is not None and uid not in wanted: continue
            src_path, orig = self.model.page_source(uid, self.pdf_path)
            reader = _rdr(src_path)
            if orig >= len(reader.pages): continue
            page = reader.pages[orig]
            rot  = self.model.get_rotation(uid)
            if rot: page.rotate(rot)
            writer.add_page(page)
        n = len(writer.pages)
        # Write beside the target and rename over it: saving in place is the
        # normal case here (Ctrl+S), and writing straight into the file we are
        # reading from leaves a truncated PDF behind if anything fails midway.
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".pdf", dir=os.path.dirname(os.path.abspath(out_path)))
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                writer.write(f)
            os.replace(tmp_path, out_path)
        except Exception:
            try: os.unlink(tmp_path)
            except Exception: pass
            raise
        return tr('Gespeichert: {p0} Seiten -> {p1}').format(p0=n, p1=out_path)


# ══════════════════════════════════════════════════════════════════════════════
# MERGE ORDER WIDGET — Datei-Grid wie Seitenverwaltung
# ══════════════════════════════════════════════════════════════════════════════

class FileCard(QFrame):
    """Thumbnail card for one file. Deliberately a near-copy of PageCard: the
    merge view is the page-manager view for files, so cards must have the same
    size, the same selected/unselected look, the same Ctrl-click handling and
    the same multi-drag pixmap."""
    clicked = pyqtSignal(int)

    FILE_ICONS = {
        ".pdf":"📄",".jpg":"🖼",".jpeg":"🖼",".png":"🖼",
        ".tif":"🖼",".tiff":"🖼",".bmp":"🖼",".webp":"🖼",
        ".docx":"📝",".doc":"📝",".xlsx":"📊",".xls":"📊",
        ".pptx":"📊",".ppt":"📊",".odt":"📝",".ods":"📊",
        ".odp":"📊",".rtf":"📝",".pages":"📝"
    }

    def __init__(self, pos, path, pixmap=None, parent=None,
                 card_w=CARD_W, card_h=CARD_H):
        super().__init__(parent)
        self.pos         = pos
        self.display_pos = pos       # same attribute name PageCard uses
        self.path        = path
        self._card_w     = card_w
        self._card_h     = card_h
        self.setFixedSize(card_w+16, card_h+28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected           = False
        self._drag_pos           = None
        self._pending_ctrl_click = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 2)
        layout.setSpacing(2)

        self.img = QLabel()
        self.img.setFixedSize(card_w, card_h)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setStyleSheet(
            f"border:1px solid {_TV['border']};background:{_TV['card_bg']};border-radius:2px;")
        if pixmap is not None:
            self.set_pixmap(pixmap)
        layout.addWidget(self.img)

        num_size = max(9, min(13, card_w // 10))
        self.num = QLabel()
        self.num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.num.setStyleSheet(
            f"color:{_TV['dim']};font-size:{num_size}px;"
            "background:transparent;border:none;")
        layout.addWidget(self.num)
        self._set_label(num_size)
        self.setToolTip(path)

        if pixmap is None:
            self._load_local_preview()
        self._update_style()

    def _set_label(self, num_size):
        """"<n>  <name>", elided to the card width — the position matters for the
        merge order, the name for telling the files apart."""
        from PyQt6.QtGui import QFontMetrics
        f = self.num.font(); f.setPixelSize(num_size); self.num.setFont(f)
        text = f"{self.pos + 1}  {os.path.basename(self.path)}"
        self.num.setText(QFontMetrics(f).elidedText(
            text, Qt.TextElideMode.ElideMiddle, self._card_w))

    def set_pixmap(self, pm):
        if pm is None or pm.isNull(): return
        self.img.setPixmap(pm.scaled(
            self._card_w, self._card_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def set_image(self, image: QImage):
        """Called from the GUI thread with a freshly rendered QImage (same entry
        point as PageCard, so the shared render queue can drive both)."""
        self.set_pixmap(QPixmap.fromImage(image))

    def _load_local_preview(self):
        """Non-PDF files: images render from disk, everything else gets its icon.
        PDF thumbnails come from the shared render queue via FileGrid."""
        ext = os.path.splitext(self.path)[1].lower()
        if ext == ".pdf":
            return
        try:
            if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"):
                self.set_pixmap(QPixmap(self.path))
            else:
                self._set_preview_icon(ext)
        except Exception:
            self._set_preview_icon(ext)

    def _set_preview_icon(self, ext):
        icon = self.FILE_ICONS.get(ext, "📄")
        self.img.setText(icon)
        self.img.setStyleSheet(
            f"border:1px solid {_TV['border']};background:{_TV['card_bg']};"
            f"border-radius:2px;font-size:{max(18, self._card_w // 3)}px;")

    def set_selected(self, sel):
        self._selected = sel
        self._update_style()

    def _update_style(self):
        if self._selected:
            self.setStyleSheet(
                f"QFrame{{background:{_TV['sel_bg']};border:2px solid {_TV['acc']};border-radius:5px;}}")
        else:
            self.setStyleSheet(
                "QFrame{background:transparent;border:2px solid transparent;"
                "border-radius:5px;}")

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_pos = e.position().toPoint()
        ctrl = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        # Ctrl+click on a selected card: defer, so a following drag keeps the
        # whole selection (exactly what PageCard does).
        if ctrl and self._selected:
            self._pending_ctrl_click = True
        else:
            self._pending_ctrl_click = False
            self.clicked.emit(self.pos)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pending_ctrl_click:
            self._pending_ctrl_click = False
            self.clicked.emit(self.pos)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.MouseButton.LeftButton): return
        if self._drag_pos is None: return
        if (e.position().toPoint()-self._drag_pos).manhattanLength() < 12: return
        self._pending_ctrl_click = False

        grid = self.parent()
        while grid and not isinstance(grid, FileGrid):
            grid = grid.parent()
        if not self._selected:
            self.clicked.emit(self.pos)
        is_multi = bool(grid and self._selected and len(grid._selected) > 1)

        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(f"multi:{self.pos}" if is_multi else str(self.pos))
        drag.setMimeData(mime)
        if is_multi and grid:
            pm = QPixmap(self.size()); pm.fill(QColor("#1e3a5a"))
            from PyQt6.QtGui import QPainter as _P, QFont as _F
            p = _P(pm); p.setPen(QColor("#eaeaea"))
            f = _F(); f.setPointSize(11); f.setBold(True); p.setFont(f)
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
                       tr('{p0} Dateien').format(p0=len(grid._selected)))
            p.end()
        else:
            pm = QPixmap(self.size()); self.render(pm)
        drag.setPixmap(pm); drag.setHotSpot(e.position().toPoint())
        drag.exec(Qt.DropAction.MoveAction)


class FileGrid(QWidget):
    """Grid of FileCards — the file-level twin of PageGrid: same zoom, same
    Ctrl/Shift selection, same drag & drop, and PDF thumbnails come off the same
    shared render queue instead of a thread per card."""
    order_changed          = pyqtSignal()
    order_about_to_change  = pyqtSignal()  # fired before a drag reorders the list
    selection_changed      = pyqtSignal(int)   # pos of the card that was clicked

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths          = list(paths)
        self._cards          = []
        self._selected       = set()
        self._last_selected  = -1
        self._last_click_pos = None       # for Shift+click ranges
        self._drop_indicator = -1
        self._card_w         = CARD_W
        self._card_h         = CARD_H
        self._thumb_gen      = 0
        self._thumb_tasks    = []
        self._thumb_signals  = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        self._scroll_connected = False
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rebuild()
        _register_themed(self)

    def _apply_theme(self):
        self._rebuild()

    # ── zoom (same steps and limits as PageGrid) ──────────────────────────────
    def zoom_in(self):
        step = 20 if self._card_w < 300 else 40 if self._card_w < 600 else 80
        self._card_w = min(1400, self._card_w + step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_out(self):
        step = 20 if self._card_w <= 300 else 40 if self._card_w <= 600 else 80
        self._card_w = max(60, self._card_w - step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_reset(self):
        self._card_w = CARD_W; self._card_h = CARD_H
        self._rebuild()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if e.angleDelta().y() > 0 else self.zoom_out()
            e.accept()
        else:
            e.ignore()

    def _render_w(self):
        return _thumb_render_width(max(self._card_w * 2, 200))

    def _rebuild(self):
        if getattr(self, "_rebuilding", False):
            return
        self._rebuilding = True
        try:
            for t in self._thumb_tasks: t.cancel()
            self._thumb_tasks.clear()
            self._thumb_gen += 1
            old_cards = self._cards[:]
            old_pm = {c.path: c.img.pixmap() for c in old_cards
                      if c.img.pixmap() and not c.img.pixmap().isNull()}
            self._cards = []
            render_w = self._render_w()
            for i, path in enumerate(self._paths):
                pm = None
                if os.path.splitext(path)[1].lower() == ".pdf":
                    cached = (_ThumbnailCache.get((path, 0, 0, render_w))
                              or _ThumbnailCache.get_any(path, 0, 0))
                    if cached is not None:
                        pm = QPixmap.fromImage(cached)
                    else:
                        pm = old_pm.get(path)
                card = FileCard(i, path, pm, self, self._card_w, self._card_h)
                card.clicked.connect(self._on_click)
                card.set_selected(i in self._selected)
                self._cards.append(card)
            for c in old_cards:
                c.hide(); c.deleteLater()
            self._relayout()
            QTimer.singleShot(0, self._connect_scroll)
            QTimer.singleShot(0, self._schedule_visible)
        finally:
            self._rebuilding = False

    # ── lazy PDF thumbnails on the shared queue ───────────────────────────────
    def _get_scroll_area(self):
        p = self.parent()
        if p is None: return None
        p = p.parent()
        return p if isinstance(p, QScrollArea) else None

    def _connect_scroll(self):
        if self._scroll_connected: return
        sa = self._get_scroll_area()
        if sa is None: return
        sa.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self._scroll_connected = True

    def _schedule_visible(self, _=None):
        if not self._cards: return
        sa = self._get_scroll_area()
        if sa is not None:
            scroll_y   = sa.verticalScrollBar().value()
            viewport_h = sa.viewport().height() or 600
            y_min = max(0, scroll_y - viewport_h); y_max = scroll_y + 2*viewport_h
        else:
            y_min, y_max = 0, 9_999_999
        per_row = self._per_row()
        cell_h  = self._card_h + 28 + GAP
        row_min = max(0, int((y_min - MARGIN) // cell_h))
        row_max = int((y_max - MARGIN) // cell_h) + 1
        gen = self._thumb_gen
        for i, card in enumerate(self._cards):
            if i // per_row < row_min or i // per_row > row_max: continue
            self._maybe_schedule(i, gen)

    def _maybe_schedule(self, cidx, gen):
        if cidx >= len(self._cards): return
        card = self._cards[cidx]
        if os.path.splitext(card.path)[1].lower() != ".pdf": return
        render_w = self._render_w()
        if _ThumbnailCache.get((card.path, 0, 0, render_w)) is not None:
            return
        self._thumb_tasks = [t for t in self._thumb_tasks if t._active]
        for t in self._thumb_tasks:
            if t._cidx == cidx: return
        task = _ThumbTask(gen, cidx, card.path, 0, 0, render_w, self._thumb_signals)
        self._thumb_tasks.append(task)
        _render_queue.submit(task, 1)

    def _on_thumb_ready(self, gen, cidx, image):
        if gen != self._thumb_gen: return
        if 0 <= cidx < len(self._cards):
            self._cards[cidx].set_image(image)

    # ── layout ────────────────────────────────────────────────────────────────
    def _per_row(self):
        w = self.width() or 800
        return max(1, (w - 2*MARGIN + GAP) // (self._card_w+16+GAP))

    def _relayout(self):
        if not self._cards: self.setMinimumHeight(200); return
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        rows   = (len(self._cards)+pr-1)//pr
        for i, card in enumerate(self._cards):
            card.move(MARGIN+i%pr*cell_w, MARGIN+i//pr*cell_h)
            card.show()
        self.setMinimumHeight(MARGIN+rows*cell_h+MARGIN)
        self.update()

    def resizeEvent(self, e):
        self._relayout(); self._schedule_visible()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_indicator < 0 or not self._cards: return
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        pos    = min(self._drop_indicator, len(self._cards))
        p = QPainter(self)
        if pr == 1:
            y = MARGIN + pos*cell_h - GAP//2
            _paint_drop_marker(p, MARGIN, y - _DROP_THICKNESS/2.0,
                               self._cards[0].width(), horizontal=True)
        else:
            col = pos%pr; row = pos//pr
            x   = MARGIN+col*cell_w-GAP//2
            y   = MARGIN+row*cell_h
            _paint_drop_marker(p, x - _DROP_THICKNESS/2.0, y, self._card_h)
        p.end()

    def _pos_from_point(self, pt):
        # Same rule as PageGrid._pos_from_point, including the past-the-end
        # guard. Clamping the cell index to n-1 first and only then applying the
        # half-cell test made the marker flip between "before" and "after" the
        # last card as the cursor swept across the empty space beyond it.
        if not self._cards: return 0
        n      = len(self._cards)
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        rel_x  = pt.x()-MARGIN; rel_y = pt.y()-MARGIN
        if pr == 1:
            for i in range(n):
                top    = MARGIN + i*cell_h
                bottom = top + self._card_h
                if pt.y() < (top + bottom) // 2:
                    return i
            return n
        col    = max(0, min(rel_x//cell_w, pr-1))
        row    = max(0, rel_y//cell_h)
        pos    = row*pr + col
        if pos >= n:
            return n
        if rel_x - col*cell_w > cell_w//2:
            pos += 1
        return min(pos, n)

    # ── selection (Ctrl toggles, Shift selects a range — as in PageGrid) ──────
    def mousePressEvent(self, e):
        # Click on empty background clears the selection, as in PageGrid. The
        # file grid simply had no handler, so a picked thumbnail could not be
        # unpicked by clicking beside it.
        if e.button() == Qt.MouseButton.LeftButton:
            self.deselect_all()
        super().mousePressEvent(e)

    def _on_click(self, pos):
        mods  = QApplication.keyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if shift and self._last_click_pos is not None:
            lo, hi = sorted((self._last_click_pos, pos))
            self._selected |= set(range(lo, hi+1))
        elif ctrl:
            self._selected ^= {pos}
            self._last_click_pos = pos
        else:
            self._selected = {pos}
            self._last_click_pos = pos
        self._last_selected = pos
        self._update_selection()
        self.selection_changed.emit(pos)

    def _update_selection(self):
        for i, c in enumerate(self._cards):
            c.set_selected(i in self._selected)

    def select_all(self):
        self._selected = set(range(len(self._paths)))
        self._update_selection(); self.selection_changed.emit(self._last_selected)

    def deselect_all(self):
        self._selected.clear(); self._last_selected = -1; self._last_click_pos = None
        self._update_selection(); self.selection_changed.emit(-1)

    def current_path(self):
        if 0 <= self._last_selected < len(self._paths):
            return self._paths[self._last_selected]
        return None

    # ── drag & drop ───────────────────────────────────────────────────────────
    def handle_drop(self, from_pos, to_pos, multi=False):
        self._drop_indicator = -1; self.update()
        # Let the owner snapshot before the list changes, so a drag is undoable
        # like every other reorder.
        self.order_about_to_change.emit()
        if multi:
            picked = [self._paths[i] for i in sorted(self._selected)
                      if 0 <= i < len(self._paths)]
            if not picked: return
            before = sum(1 for i in self._selected if i < to_pos)
            rest   = [p for i, p in enumerate(self._paths) if i not in self._selected]
            ins    = max(0, min(to_pos - before, len(rest)))
            self._paths = rest[:ins] + picked + rest[ins:]
            self._selected = set(range(ins, ins+len(picked)))
            self._last_selected = ins
        else:
            if from_pos == to_pos: return
            p   = self._paths.pop(from_pos)
            ins = to_pos-1 if from_pos < to_pos else to_pos
            ins = max(0, min(ins, len(self._paths)))
            self._paths.insert(ins, p)
            self._selected = {ins}; self._last_selected = ins
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def dragEnterEvent(self, e):
        if e.mimeData().hasText(): e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasText(): return
        e.acceptProposedAction()
        self._drop_indicator = self._pos_from_point(e.position().toPoint())
        self.update()

    def dragLeaveEvent(self, e):
        self._drop_indicator = -1; self.update()

    def dropEvent(self, e):
        if not e.mimeData().hasText(): return
        to = self._drop_indicator
        if to < 0: to = self._pos_from_point(e.position().toPoint())
        self._drop_indicator = -1; self.update()
        text = e.mimeData().text()
        e.acceptProposedAction()
        try:
            if text.startswith("multi:"):
                self.handle_drop(int(text.split(":")[1]), to, multi=True)
            else:
                self.handle_drop(int(text), to)
        except (ValueError, IndexError):
            pass

    # ── operations ────────────────────────────────────────────────────────────
    def remove_selected(self):
        for i in sorted(self._selected, reverse=True):
            if 0<=i<len(self._paths): self._paths.pop(i)
        self._selected.clear(); self._last_selected = -1; self._last_click_pos = None
        self._rebuild(); self.order_changed.emit(); self.selection_changed.emit(-1)

    def move_up(self):
        sel = sorted(self._selected)
        if not sel or sel[0]==0: return
        for i in sel:
            self._paths[i-1], self._paths[i] = self._paths[i], self._paths[i-1]
        self._selected = {i-1 for i in sel}
        self._last_selected = min(self._selected)
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def move_down(self):
        sel = sorted(self._selected, reverse=True)
        if not sel or sel[0]>=len(self._paths)-1: return
        for i in sel:
            self._paths[i], self._paths[i+1] = self._paths[i+1], self._paths[i]
        self._selected = {i+1 for i in sel}
        self._last_selected = max(self._selected)
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def get_paths(self): return list(self._paths)

    def insert_paths(self, at, paths):
        """Insert files at a position and leave them selected — the file-level
        twin of pasting pages into the page manager."""
        paths = [p for p in paths if p]
        if not paths: return
        at = max(0, min(at, len(self._paths)))
        self._paths[at:at] = list(paths)
        self._selected = set(range(at, at + len(paths)))
        self._last_selected  = at
        self._last_click_pos = at
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def set_state(self, paths, selected):
        """Restore a previous list and selection wholesale (undo / redo)."""
        self._paths    = list(paths)
        self._selected = {i for i in selected if 0 <= i < len(self._paths)}
        self._last_selected  = min(self._selected) if self._selected else -1
        self._last_click_pos = self._last_selected if self._selected else None
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def get_selected_info(self):
        if not self._selected: return tr("Keine Auswahl")
        sel = sorted(self._selected)
        if len(sel)==1: return tr('Datei {p0}').format(p0=sel[0] + 1)
        return tr('{p0} Dateien ausgewaehlt').format(p0=len(sel))


class MergeShortcutFilter(QObject):
    """App-level keys for the merge preview.

    Deliberately the same set, and the same mechanics, as ManageShortcutFilter:
    the two views show thumbnails and are meant to answer to the same keys.
    Like that one it stands down for modal dialogs and for text fields, so
    Ctrl+A in the selection box still selects the text."""
    def __init__(self, widget, parent=None):
        super().__init__(parent)
        self.w = widget

    def _live(self):
        return self.w.isVisible() and not self.w._busy

    def eventFilter(self, obj, event):
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        if t == QEvent.Type.ShortcutOverride:
            if not self._live():
                return False
            if isinstance(QApplication.focusWidget(), QLineEdit):
                return False
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier and \
               event.key() in (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_V,
                               Qt.Key.Key_X, Qt.Key.Key_Z, Qt.Key.Key_Y,
                               Qt.Key.Key_D):
                event.accept()
            return False

        if t != QEvent.Type.KeyPress or not self._live():
            return False
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return False

        k     = event.key()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not ctrl:
            self.w._remove(); return True
        if ctrl:
            if k == Qt.Key.Key_A: self.w._grid.select_all();   return True
            if k == Qt.Key.Key_D: self.w._grid.deselect_all(); return True
            if k == Qt.Key.Key_C: self.w._copy();  return True
            if k == Qt.Key.Key_X: self.w._cut();   return True
            if k == Qt.Key.Key_V: self.w._paste(); return True
            if k == Qt.Key.Key_Z and not shift: self.w._undo(); return True
            if (k == Qt.Key.Key_Z and shift) or k == Qt.Key.Key_Y:
                self.w._redo(); return True
        return False


class MergeOrderWidget(QWidget):
    merge_confirmed = pyqtSignal(list)
    open_separately = pyqtSignal(list)
    cancelled       = pyqtSignal()

    # Shared between merge previews, like ManagePanel._shared_clipboard
    _shared_clipboard: list = []

    def __init__(self, file_paths, parent=None):
        super().__init__(parent)
        self._busy        = False
        self.source_paths = list(file_paths)   # what the tab was opened with
        self.tmp_dir      = None               # set by PageViewerPanel
        self._history     = []
        self._redo_stack  = []
        self._key_filter  = None
        self._setup(file_paths)
        self.destroyed.connect(self._cleanup_filter)

    # ── keyboard ─────────────────────────────────────────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        if self._key_filter is None:
            self._key_filter = MergeShortcutFilter(self)
            QApplication.instance().installEventFilter(self._key_filter)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._cleanup_filter()

    def _cleanup_filter(self):
        if getattr(self, "_key_filter", None) is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._key_filter)
            self._key_filter = None

    # ── clipboard / history, mirroring the page manager ──────────────────────
    def _save_history(self):
        self._history.append((self._grid.get_paths(), set(self._grid._selected)))
        del self._history[:-40]
        self._redo_stack.clear()

    def _copy(self):
        picked = [self._grid.get_paths()[i] for i in sorted(self._grid._selected)
                  if 0 <= i < len(self._grid.get_paths())]
        if not picked:
            self.status.setText(tr("Zuerst Dateien auswaehlen.")); return
        MergeOrderWidget._shared_clipboard = picked
        self.status.setText(tr('{p0} Datei(en) kopiert.').format(p0=len(picked)))

    def _cut(self):
        if not self._grid._selected:
            self.status.setText(tr("Zuerst Dateien auswaehlen.")); return
        self._copy()
        self._save_history()
        self._grid.remove_selected()
        self._on_order_changed()

    def _paste(self):
        clip = MergeOrderWidget._shared_clipboard
        if not clip:
            self.status.setText(tr("Zwischenablage ist leer.")); return
        at = (max(self._grid._selected) + 1) if self._grid._selected \
             else len(self._grid.get_paths())
        self._save_history()
        self._grid.insert_paths(at, clip)
        self._on_order_changed()
        self.status.setText(tr('{p0} Datei(en) eingefuegt.').format(p0=len(clip)))

    def _undo(self):
        if not self._history:
            self.status.setText(tr("Nichts rueckgaengig zu machen.")); return
        self._redo_stack.append((self._grid.get_paths(), set(self._grid._selected)))
        paths, sel = self._history.pop()
        self._grid.set_state(paths, sel)
        self._on_order_changed()
        self.status.setText(tr("Rueckgaengig."))

    def _redo(self):
        if not self._redo_stack:
            self.status.setText(tr("Nichts zu wiederholen.")); return
        self._history.append((self._grid.get_paths(), set(self._grid._selected)))
        paths, sel = self._redo_stack.pop()
        self._grid.set_state(paths, sel)
        self._on_order_changed()
        self.status.setText(tr("Wiederhergestellt."))

    def _sep(self):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color:{_TV['border']};margin:3px 0;")
        return f

    def _section(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        layout.addWidget(lbl)

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.setObjectName("secondaryBtn")
        b.clicked.connect(fn)
        b.setMinimumHeight(28)
        return b

    def _setup(self, file_paths):
        # The layout below mirrors ManagePanel exactly (fixed title bar, scrollable
        # sidebar with the same margins/sections/helpers, grid on the right) — this
        # view is "Seiten verwalten" for files, so it should not look like a
        # different program.
        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setStyleSheet(
            f"QSplitter::handle{{background:{_TV['splitter']};width:2px;}}")

        # ── Links: Steuerung wie ManagePanel ─────────────────────────
        self._left_w = QWidget(); self._left_w.setObjectName("mergeLeftW")
        # Wide enough that the primary action still fits at the narrowest the
        # splitter allows — "Zusammenfuehren (n)" needs ~200px of button.
        self._left_w.setMinimumWidth(236)
        ol = QVBoxLayout(self._left_w); ol.setContentsMargins(0,0,0,0); ol.setSpacing(0)

        self._title_w = QWidget(); self._title_w.setObjectName("mergeTitleW")
        self._title_w.setFixedHeight(36)
        tl = QHBoxLayout(self._title_w); tl.setContentsMargins(10, 0, 10, 0)
        self._title_lbl = QLabel(tr("Dateien oeffnen"))
        tl.addWidget(self._title_lbl)
        ol.addWidget(self._title_w)

        self._left_scroll = QScrollArea(); self._left_scroll.setWidgetResizable(True)
        self._left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._left_content = QWidget(); self._left_content.setObjectName("mergeLeftContent")
        ll = QVBoxLayout(self._left_content); ll.setContentsMargins(10, 8, 22, 10); ll.setSpacing(5)
        self._left_scroll.setWidget(self._left_content)
        ol.addWidget(self._left_scroll, 1)

        sel_lbl = QLabel(tr("Auswahl  (z.B. 1, 3, 5-8)"))
        sel_lbl.setObjectName("sectionLabel")
        ll.addWidget(sel_lbl)
        self.sel_edit = QLineEdit()
        self.sel_edit.setPlaceholderText(tr("z.B. 1, 3, 5-8, 12  →  Enter"))
        self.sel_edit.returnPressed.connect(self._apply_sel_edit)
        ll.addWidget(self.sel_edit)
        self._info = QLabel(tr("Keine Auswahl"))
        self._info.setWordWrap(True)
        self._info.setObjectName("dimLabel")
        ll.addWidget(self._info); ll.addWidget(self._sep())

        # Zoom only. The reset button used to be labelled "↺", which is the page
        # manager's rotate-left icon — so it read as "turn this thumbnail", an
        # action that means nothing for a whole file. Same fix as was already
        # made one view over: call it what it is.
        self._section(ll, tr("ANSICHT"))
        zoom_row = QHBoxLayout(); zoom_row.setSpacing(4)
        self._zoom_btns = []
        for text, tip, fn in [
                ("−",   "Thumbnails verkleinern",  lambda: self._grid.zoom_out()),
                ("+",   "Thumbnails vergroessern", lambda: self._grid.zoom_in()),
                ("1:1", "Zoom zuruecksetzen",      lambda: self._grid.zoom_reset())]:
            b = QPushButton(text); b.setFixedSize(32, 26)
            b.setToolTip(tr(tip))
            b.clicked.connect(fn)
            zoom_row.addWidget(b); self._zoom_btns.append(b)
        self._zoom_hint_lbl = QLabel(tr("Thumbnails"))
        zoom_row.addWidget(self._zoom_hint_lbl); zoom_row.addStretch()
        ll.addLayout(zoom_row)
        ll.addWidget(self._sep())

        self._section(ll, tr("AUSWAHL"))
        ll.addWidget(self._btn(tr("Alle auswaehlen  (Strg+A)"),  lambda: self._grid.select_all()))
        ll.addWidget(self._btn(tr("Auswahl aufheben  (Strg+D)"), lambda: self._grid.deselect_all()))
        ll.addWidget(self._sep())

        self._section(ll, tr("REIHENFOLGE"))
        ll.addWidget(self._btn(tr("▲  Hoch"),   self._move_up))
        ll.addWidget(self._btn(tr("▼  Runter"), self._move_down))
        ll.addWidget(self._sep())

        self._section(ll, tr("OPERATIONEN"))
        ll.addWidget(self._btn(tr("Entfernen  (Entf)"),       self._remove))
        ll.addWidget(self._btn(tr("Kopieren  (Strg+C)"),      self._copy))
        ll.addWidget(self._btn(tr("Ausschneiden  (Strg+X)"),  self._cut))
        ll.addWidget(self._btn(tr("Einfuegen  (Strg+V)"),     self._paste))
        ll.addWidget(self._btn(tr("Rueckgaengig  (Strg+Z)"),  self._undo))
        ll.addWidget(self._sep())

        self._section(ll, tr("DATEI-INFO"))
        self._inf_name = QLabel("—"); self._inf_name.setWordWrap(True)
        self._inf_name.setObjectName("currentFileLabel")
        ll.addWidget(self._inf_name)
        self._inf_type = QLabel(""); self._inf_pages = QLabel(""); self._inf_size = QLabel("")
        for w in [self._inf_type, self._inf_pages, self._inf_size]:
            w.setObjectName("dimLabel"); ll.addWidget(w)
        ll.addStretch()

        # ── The two ways out, pinned below the scroll area ───────────────
        # These are what the view exists for, so they must never be scrolled
        # out of reach: at the bottom of the scrolling column they sat below
        # the fold on a standard window and "Zusammenfuehren" was invisible
        # until the sidebar was scrolled.
        self._actions_w = QWidget(); self._actions_w.setObjectName("mergeActionsW")
        al = QVBoxLayout(self._actions_w)
        # No 22px right inset here: that one exists in the scroll area above to
        # clear its scrollbar, and copying it made "Zusammenfuehren (n)" wider
        # than its button.
        al.setContentsMargins(10, 8, 10, 10); al.setSpacing(5)

        self._section(al, tr("OEFFNEN"))
        self._total = QLabel("")
        self._total.setWordWrap(True)
        self._total.setObjectName("dimLabel")
        al.addWidget(self._total)
        self._btn_go = QPushButton(tr("  Zusammenfuehren"))
        self._btn_go.setObjectName("actionBtn")
        self._btn_go.setMinimumHeight(28)
        self._btn_go.clicked.connect(self._confirm)
        al.addWidget(self._btn_go)
        self._btn_single = self._btn(tr("  Einzeln oeffnen"), self._do_open_separately)
        al.addWidget(self._btn_single)
        self._btn_cancel = self._btn(tr("✗  Abbrechen"), self._do_cancel)
        al.addWidget(self._btn_cancel)

        self.status = QLabel(tr("Drag & Drop zum Umsortieren  ·  Strg/Shift zum Mehrfachauswaehlen"))
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:10px;min-height:32px;background:transparent;")
        al.addWidget(self.status)
        ol.addWidget(self._actions_w)
        splitter.addWidget(self._left_w)

        # ── Rechts: FileGrid ─────────────────────────────────────────
        self._right_w = QWidget(); self._right_w.setObjectName("mergeRightW")
        rl = QVBoxLayout(self._right_w); rl.setContentsMargins(0,0,0,0); rl.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid = FileGrid(file_paths)
        self._grid.order_changed.connect(self._on_order_changed)
        self._grid.order_about_to_change.connect(self._save_history)
        self._grid.selection_changed.connect(self._on_select)
        self._scroll.setWidget(self._grid)
        rl.addWidget(self._scroll, 1)
        splitter.addWidget(self._right_w)

        splitter.setSizes([236, 500])
        splitter.setStretchFactor(0,0); splitter.setStretchFactor(1,1)
        root.addWidget(splitter, 1)

        # Keys go through the same app-level filter the page manager uses (see
        # MergeShortcutFilter, installed in showEvent), so this view answers to
        # the same set. It used to register three lone QShortcuts, which is why
        # Ctrl+C / Ctrl+X / Ctrl+V / Ctrl+Z did nothing here while working one
        # view over.
        self._on_order_changed()
        _register_themed(self)
        self._apply_theme()

    def _apply_theme(self):
        t = _TV
        self._left_w.setStyleSheet(
            f"QWidget#mergeLeftW{{background:{t['sidebar_bg']};border-right:1px solid {t['border']};}}")
        self._title_w.setStyleSheet(
            f"QWidget#mergeTitleW{{background:{t['sidebar_bg']};}}")
        self._title_lbl.setStyleSheet(
            f"color:{t['text']};font-size:13px;font-weight:bold;background:transparent;")
        self._left_scroll.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._left_content.setStyleSheet(
            f"QWidget#mergeLeftContent{{background:{t['sidebar_bg']};}}")
        self._actions_w.setStyleSheet(
            f"QWidget#mergeActionsW{{background:{t['sidebar_bg']};"
            f"border-top:1px solid {t['border']};}}")
        self._right_w.setStyleSheet(
            f"QWidget#mergeRightW{{background:{t['viewer_bg']};}}")
        self._scroll.setStyleSheet(
            f"QScrollArea{{background:{t['viewer_bg']};border:none;}}")
        _zs = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:4px;font-size:13px;}}"
               f"QPushButton:hover{{background:{t['hover']};}}")
        for b in getattr(self, "_zoom_btns", []):
            b.setStyleSheet(_zs)
        if hasattr(self, "_zoom_hint_lbl"):
            self._zoom_hint_lbl.setStyleSheet(
                f"color:{t['vdim']};font-size:9px;background:transparent;")
        if hasattr(self, "status"):
            self.status.setStyleSheet(
                f"color:{t['vdim']};font-size:10px;min-height:32px;background:transparent;")

    FILE_KINDS = {
        ".pdf":"PDF",".jpg":"JPEG",".jpeg":"JPEG",".png":"PNG",
        ".tif":"TIFF",".tiff":"TIFF",".bmp":"BMP",".webp":"WebP",
        ".docx":"Word",".doc":"Word",".xlsx":"Excel",".xls":"Excel",
        ".pptx":"PowerPoint",".ppt":"PowerPoint",
        ".odt":"Writer",".ods":"Calc",".odp":"Impress",
        ".rtf":"RTF",".pages":"Pages"
    }

    def _on_order_changed(self):
        n = len(self._grid.get_paths())
        n_conv = sum(1 for p in self._grid.get_paths()
                     if os.path.splitext(p)[1].lower() != ".pdf")
        txt = tr('{p0} Datei(en)').format(p0=n)
        if n_conv: txt += tr("  —  {p0} zu konvertieren").format(p0=n_conv)
        self._total.setText(txt)
        self._btn_go.setText(tr('  Zusammenfuehren  ({p0})').format(p0=n))
        self._btn_single.setText(tr('  Einzeln oeffnen  ({p0})').format(p0=n))

    def _apply_sel_edit(self):
        positions = _parse_positions(self.sel_edit.text(), len(self._grid.get_paths()))
        if positions:
            self._grid._selected = set(positions)
            self._grid._last_selected = min(positions)
            self._grid._last_click_pos = self._grid._last_selected
            self._grid._update_selection()
            self._on_select(self._grid._last_selected)
        else:
            self.update_info()

    def update_info(self):
        """Keep the selection field showing the current selection in compact
        form — the page manager does the same after every selection change."""
        self.sel_edit.blockSignals(True)
        self.sel_edit.setText(_positions_to_str(sorted(i+1 for i in self._grid._selected)))
        self.sel_edit.blockSignals(False)

    def _on_select(self, pos):
        self.update_info()
        path = self._grid.current_path()
        if not path:
            self._inf_name.setText("—"); self._inf_type.setText("")
            self._inf_pages.setText(""); self._inf_size.setText("")
            self._info.setText(tr("Keine Auswahl")); return
        self._info.setText(self._grid.get_selected_info())
        ext = os.path.splitext(path)[1].lower()
        self._inf_name.setText(os.path.basename(path))
        self._inf_type.setText(f"Typ: {self.FILE_KINDS.get(ext, ext.upper().lstrip('.'))}")
        try: self._inf_size.setText(tr('Groesse: {p0:.0f} KB').format(p0=os.path.getsize(path) / 1024))
        except Exception: self._inf_size.setText("")
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                self._inf_pages.setText(tr('Seiten: {p0}').format(p0=len(PdfReader(path, strict=False).pages)))
            except Exception: self._inf_pages.setText(tr("Seiten: ?"))
        else:
            self._inf_pages.setText(tr("Seiten: nach Konvertierung"))
        paths = self._grid.get_paths()
        self._info.setText(tr('Datei {p0} von {p1}').format(p0=pos + 1, p1=len(paths)))

    def _move_up(self):
        if self._grid._selected: self._save_history()
        self._grid.move_up()
        self._on_order_changed()

    def _move_down(self):
        if self._grid._selected: self._save_history()
        self._grid.move_down()
        self._on_order_changed()

    def _remove(self):
        if not self._grid._selected:
            self.status.setText(tr("Zuerst Dateien auswaehlen.")); return
        self._save_history()
        self._grid.remove_selected()
        self._on_order_changed()

    def set_busy(self, busy):
        """Latch the view while a conversion runs. Every button that starts or
        aborts work goes dead, so a double click — or a click on the second
        button while the first one's work is in flight — cannot start a second
        run behind the first one."""
        self._busy = bool(busy)
        for b in (self._btn_go, self._btn_single, self._btn_cancel):
            b.setEnabled(not self._busy)

    def _confirm(self):
        if self._busy:
            return
        paths = self._grid.get_paths()
        import logging; logging.debug(f"MergeOrderWidget._confirm: {paths}")
        if not paths:
            return
        self.set_busy(True)
        self.merge_confirmed.emit(paths)

    def _do_open_separately(self):
        if self._busy:
            return
        paths = self._grid.get_paths()
        if not paths:
            return
        self.set_busy(True)
        self.open_separately.emit(paths)

    def _do_cancel(self):
        if self._busy:
            return
        self.set_busy(True)      # one cancel only — the tab is about to go
        self.cancelled.emit()


# ══════════════════════════════════════════════════════════════════════════════
# GLOBALER KEY-FILTER
# Ctrl+Shift+O → Einzelansicht ↔ Seiten verwalten umschalten
# Esc          → immer zurück zur Einzelansicht
# Tab          → normale Fokus-Navigation zwischen Eingabefeldern (nicht abgefangen)
# ══════════════════════════════════════════════════════════════════════════════

class _ViewerKeyFilter(QObject):
    """
    Globaler QApplication-Event-Filter.
    Ctrl+Shift+O → Viewer einblenden + Einzelansicht ↔ Seiten verwalten
    Escape       → Viewer einblenden + immer zur Einzelansicht
    Tab wird NICHT abgefangen → Standard-Fokus-Traversal der Widgets.
    """
    def __init__(self, viewer_panel):
        super().__init__(viewer_panel)
        self._vp = viewer_panel

    def eventFilter(self, obj, event):
        # Stand down entirely while a modal dialog (print dialog, settings, file
        # picker, message box) is open, so its own widgets get Tab/Escape/zoom
        # keys for normal focus traversal instead of us hijacking them for the
        # background viewer.
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        # ShortcutOverride: Qt fragt Widget ob es die Taste übernehmen will.
        # event.accept() tells Qt "send this as KeyPress, not as shortcut".
        # We MUST return False here so Qt still sees the accepted state and
        # dispatches the subsequent KeyPress — returning True would eat the
        # ShortcutOverride entirely and no KeyPress would follow.
        if t == QEvent.Type.ShortcutOverride:
            k = event.key()
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            # Escape is claimed (exits the manage view). Tab is intentionally
            # NOT claimed anymore so it performs normal focus traversal between
            # input fields — the manage view moved to Ctrl+Shift+O.
            if k == Qt.Key.Key_Escape:
                focused = QApplication.focusWidget()
                if not isinstance(focused, QLineEdit):
                    event.accept()
                    return False
            # Qt reports Ctrl+Shift+Tab as Key_Backtab with Ctrl modifier
            if ctrl and k in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_W,
                              Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                              Qt.Key.Key_Minus, Qt.Key.Key_0, Qt.Key.Key_1):
                event.accept()
                return False
            return False

        if t != QEvent.Type.KeyPress:
            return False

        k    = event.key()
        mods = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # Ctrl+Shift+O → toggle the pages overview (Manage view). Handled BEFORE
        # the text-field guard so it works globally, even while a field is
        # focused. This replaces the old plain-Tab binding, which hijacked Tab
        # everywhere and made normal field-to-field focus traversal impossible.
        if ctrl and shift and k == Qt.Key.Key_O:
            try:
                self._vp._toggle_manage()
            except Exception as exc:
                import logging; logging.error(f"_toggle_manage: {exc}", exc_info=True)
            return True

        # Nie abfangen wenn ein Textfeld fokussiert ist
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return False

        # Ctrl+W → close current tab (like browser / Acrobat)
        if ctrl and k == Qt.Key.Key_W:
            try:
                idx = self._vp.tabs.currentIndex()
                if idx >= 0:
                    self._vp._close_tab(idx)
            except Exception as exc:
                import logging; logging.error(f"_close_tab: {exc}", exc_info=True)
            return True

        # Ctrl+Tab → forward, Ctrl+Shift+Tab (reported as Ctrl+Backtab) → backward
        if ctrl and k == Qt.Key.Key_Tab:
            try:
                self._vp._cycle_tab(forward=True)
            except Exception as exc:
                import logging; logging.error(f"_cycle_tab: {exc}", exc_info=True)
            return True
        if ctrl and k == Qt.Key.Key_Backtab:
            try:
                self._vp._cycle_tab(forward=False)
            except Exception as exc:
                import logging; logging.error(f"_cycle_tab: {exc}", exc_info=True)
            return True

        # ── Zoom shortcuts — work regardless of which widget has focus ──────
        # Ctrl+0=fit/reset, Ctrl+1=actual size, Ctrl++/= zoom in, Ctrl+- zoom out
        if ctrl and k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal,
                          Qt.Key.Key_Minus, Qt.Key.Key_0, Qt.Key.Key_1):
            tab = self._vp._current()
            if tab:
                in_manage = (tab._stack.currentWidget() is not tab.single)
                if in_manage and tab._manage_panel:
                    # Zoom the thumbnail grid
                    grid = tab._manage_panel.grid
                    try:
                        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal): grid.zoom_in()
                        elif k == Qt.Key.Key_Minus:                   grid.zoom_out()
                        elif k == Qt.Key.Key_0:                       grid.zoom_reset()
                    except Exception as exc:
                        import logging; logging.error(f"manage zoom shortcut: {exc}", exc_info=True)
                else:
                    sv = tab.single
                    try:
                        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal): sv._zoom_in()
                        elif k == Qt.Key.Key_Minus:                   sv._zoom_out()
                        elif k == Qt.Key.Key_0:                       sv._zoom_fit()
                        elif k == Qt.Key.Key_1:                       sv._zoom_actual_size()
                    except Exception as exc:
                        import logging; logging.error(f"zoom shortcut: {exc}", exc_info=True)
            return True

        if k == Qt.Key.Key_Escape:
            try:
                self._vp._ensure_single_view()
            except Exception as exc:
                import logging; logging.error(f"_ensure_single_view: {exc}", exc_info=True)
            return True
        return False


# ══════════════════════════════════════════════════════════════════════════════
# HAUPT-VIEWER
# ══════════════════════════════════════════════════════════════════════════════

class PageViewerPanel(QWidget):
    tab_opened = pyqtSignal()  # neuer Tab geöffnet
    tabs_changed = pyqtSignal()  # Tab hinzugefügt/entfernt

    def __init__(self, parent=None):
        super().__init__(parent)
        # Callbacks die MainWindow setzt:
        self.switch_to_viewer   = None   # lambda: main._switch(0)
        self.get_main_stack_idx = None   # lambda: main._stack.currentIndex()
        self.restore_main_idx   = None   # lambda idx: main._switch(idx)
        self.hide_sidebar       = None   # lambda: sidebar.setVisible(False)
        self.show_sidebar       = None   # lambda: sidebar.setVisible(True)
        self._pre_manage_idx    = None   # gespeicherter Stack-Index vor Manage-Modus
        self._setup_ui()
        AppState.get().result_ready.connect(self._open_result_tab)
        # Globaler Tab/Escape-Filter
        self._key_filter = _ViewerKeyFilter(self)
        QApplication.instance().installEventFilter(self._key_filter)
        self.destroyed.connect(
            lambda: QApplication.instance().removeEventFilter(self._key_filter))

    def _toggle_manage(self):
        """
        Tab: Einzelansicht ↔ Seiten verwalten umschalten.
        Beim Hineingehen: aktuellen Stack-Index merken.
        Beim Herausgehen: zum gespeicherten Stack-Index zurückkehren.
        """
        if self._toggle_in_progress:
            return   # block re-entrant calls from rapid key presses
        self._toggle_in_progress = True
        try:
            self._toggle_manage_impl()
        finally:
            self._toggle_in_progress = False

    def _toggle_manage_impl(self):
        tab = self._current()
        # Wenn wir noch nicht im Viewer sind, müssen wir erst prüfen ob wir
        # gerade schon im Manage-Modus sind (Stack-Index 0 aber Manage aktiv)
        currently_in_manage = (tab is not None and
                               tab._stack.currentWidget() is not tab.single)

        if currently_in_manage:
            # → Manage verlassen und zurück zur vorherigen Ansicht
            self._exit_manage_layout()
            tab._exit_manage()
            if self._pre_manage_idx is not None and self.restore_main_idx:
                self.restore_main_idx(self._pre_manage_idx)
            self._pre_manage_idx = None
        else:
            # → Manage betreten: aktuellen Stack-Index speichern, Viewer zeigen
            if self.get_main_stack_idx:
                self._pre_manage_idx = self.get_main_stack_idx()
            if self.switch_to_viewer:
                self.switch_to_viewer()
            tab = self._current()   # nach switch_to_viewer neu holen
            if tab:
                if self.hide_sidebar:
                    self.hide_sidebar()
                show_sb = self.show_sidebar
                def _on_exit():
                    self._exit_manage_layout()
                    if show_sb:
                        show_sb()
                try:
                    tab._enter_manage(on_exit=_on_exit)
                    self._enter_manage_layout(tab._manage_panel)
                    # Give the grid keyboard focus so arrow keys work immediately
                    if tab._manage_widget:
                        tab._manage_widget.setFocus()
                except Exception:
                    import logging, traceback
                    logging.error(traceback.format_exc())
                    if show_sb:
                        show_sb()  # always restore sidebar on failure

    def _ensure_single_view(self):
        """Esc: immer zur Einzelansicht — Manage verlassen, Viewer zeigen, kein Zurück-Sprung."""
        if self.switch_to_viewer:
            self.switch_to_viewer()
        tab = self._current()
        if tab and tab._stack.currentWidget() is not tab.single:
            self._exit_manage_layout()
            tab._exit_manage()
            if self.show_sidebar:
                self.show_sidebar()
        self._pre_manage_idx = None   # Esc bricht den Rücksprung-Pfad ab

    def _cycle_tab(self, forward=True):
        """Ctrl+Tab / Ctrl+Shift+Tab: zum nächsten / vorherigen Tab wechseln."""
        n = self.tabs.count()
        if n < 2:
            return
        cur = self.tabs.currentIndex()
        nxt = (cur + (1 if forward else -1)) % n
        self.tabs.setCurrentIndex(nxt)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Shortcuts
        from PyQt6.QtGui import QKeySequence, QShortcut
        # Ctrl+S / Ctrl+Shift+S belong to the Datei menu actions, which are
        # window-scoped and so fire from a tool panel too. Registering them here
        # as well made both ambiguous, and Qt then delivers neither.
        sc_print = QShortcut(QKeySequence("Ctrl+P"), self)
        sc_print.activated.connect(self._print_current)

        # ── Obere Toolbar ────────────────────────────────────────────────────
        self._top_bar = QWidget()
        self._top_bar.setObjectName("pvTopBar")
        self._top_bar.setFixedHeight(46)
        self._top_bar.setStyleSheet(
            f"QWidget#pvTopBar{{background:{_TV['sidebar_bg']};border-bottom:1px solid {_TV['border']};}}")
        top_bar = self._top_bar
        _register_themed(self)
        tbl = QHBoxLayout(top_bar)
        tbl.setContentsMargins(12, 0, 12, 0)
        tbl.setSpacing(8)

        # One primary action on the left, the document-scoped actions grouped on
        # the right in the *same* weight. "Öffnen" and "Seiten verwalten" used to
        # both be accent-filled, so the bar had two competing primaries pulling
        # at opposite ends with a third, differently-styled button beside one of
        # them.
        open_btn = QPushButton(tr("Öffnen..."))
        open_btn.setObjectName("actionBtn")
        open_btn.setMinimumWidth(_TOP_BTN_W)
        open_btn.clicked.connect(self._open)
        tbl.addWidget(open_btn)

        self._viewer_info = QLabel("")
        self._viewer_info.setObjectName("currentFileLabel")
        tbl.addWidget(self._viewer_info, 1)

        # The shortcut lives in the tooltip, not in the label: spelled out it made
        # this button twice the width of every other one in the app.
        self._manage_btn = QPushButton(tr("Seiten verwalten"))
        self._manage_btn.setObjectName("secondaryBtn")
        self._manage_btn.setToolTip(tr("Seiten verwalten") + "  (Strg+Umschalt+O)")
        self._manage_btn.setMinimumWidth(_TOP_BTN_W)
        self._manage_btn.setEnabled(False)
        self._manage_btn.clicked.connect(self._manage_current)
        tbl.addWidget(self._manage_btn)

        self._print_btn = QPushButton(tr("Drucken"))
        self._print_btn.setObjectName("secondaryBtn")
        self._print_btn.setToolTip(tr("Drucken") + "  (Strg+P)")
        self._print_btn.setMinimumWidth(_TOP_BTN_W)
        self._print_btn.setEnabled(False)
        self._print_btn.clicked.connect(self._print_current)
        tbl.addWidget(self._print_btn)

        layout.addWidget(top_bar)

        self.tabs = QTabWidget()
        self.tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Body: holds [ManagePanel (optional)] + [tabs]
        self._body = QWidget()
        self._body_layout = QHBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addWidget(self.tabs, 1)
        layout.addWidget(self._body, 1)

        self._manage_splitter_widget = None  # QSplitter shown in manage mode
        self._manage_tab             = None  # PdfTab whose panel is in the splitter
        self._toggle_in_progress     = False # reentrancy guard for _toggle_manage

        AppState.get().status_message.connect(self._on_status)

    def _apply_theme(self):
        self._top_bar.setStyleSheet(
            f"QWidget#pvTopBar{{background:{_TV['sidebar_bg']};border-bottom:1px solid {_TV['border']};}}")

    def _on_status(self, msg):
        pass  # Status wird in der Info-Leiste der SinglePageView angezeigt

    # ── Manage-Layout helpers ─────────────────────────────────────────────────

    def _enter_manage_layout(self, panel):
        """Place ManagePanel left of QTabWidget using a splitter."""
        if self._manage_splitter_widget:
            return  # already in manage layout

        panel.show()
        # Remember which tab owns this layout so _exit can detach correctly
        self._manage_tab = self._current()
        # Detach tabs from body, put into splitter alongside panel
        self._body_layout.removeWidget(self.tabs)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(panel)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 800])
        splitter.setStyleSheet(
            f"QSplitter::handle{{background:{_TV['splitter']};width:4px;}}")

        self._body_layout.addWidget(splitter, 1)
        self._manage_splitter_widget = splitter

    def _exit_manage_layout(self):
        """Restore QTabWidget to full body width, remove the splitter."""
        if not self._manage_splitter_widget:
            return
        # Detach panel back to its OWNING tab (not _current() which may have changed)
        tab = self._manage_tab
        if tab and tab._manage_panel:
            tab._manage_panel.setParent(tab)
            tab._manage_panel.hide()
        self._manage_tab = None
        # Detach tabs, then destroy the splitter
        self.tabs.setParent(None)
        self._body_layout.removeWidget(self._manage_splitter_widget)
        self._manage_splitter_widget.deleteLater()
        self._manage_splitter_widget = None
        self._body_layout.addWidget(self.tabs, 1)

    def _current(self):
        w = self.tabs.currentWidget()
        return w if isinstance(w, PdfTab) else None

    def _open(self, path=None):
        from PyQt6.QtWidgets import QMessageBox
        from tools.multi_open import (IMAGE_EXTS, OFFICE_EXTS, PDF_EXT,
                                      file_dialog_filter)
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, tr("Datei oeffnen"), "", file_dialog_filter())
        if not path: return

        # Everything below assumes a readable file. Opening one that vanished
        # between being picked and being read (a stale "recent file", a removed
        # USB stick) used to fall through to the PDF parser and raise inside a
        # slot, which aborts the process rather than showing anything.
        if not os.path.isfile(path):
            QMessageBox.warning(self, tr("Datei nicht gefunden"),
                                tr('Die Datei existiert nicht mehr:\n{p0}').format(p0=path))
            return

        ext = os.path.splitext(path)[1].lower()
        if ext not in PDF_EXT | IMAGE_EXTS | OFFICE_EXTS:
            QMessageBox.warning(
                self, tr("Format nicht unterstuetzt"),
                tr('CopyShop kann "{p0}" nicht oeffnen.').format(p0=ext or "?"))
            return

        if ext in IMAGE_EXTS:
            try:
                import img2pdf, tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
                with open(tmp, "wb") as f:
                    f.write(img2pdf.convert(path))
                path = tmp
            except Exception as e:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, tr("Bild-Konvertierung fehlgeschlagen"), str(e))
                return

        elif ext in OFFICE_EXTS:
            # LibreOffice takes seconds to start, and this runs on the GUI
            # thread. Say what is happening instead of just freezing.
            self._viewer_info.setText(
                tr('Konvertiere {p0} …').format(p0=os.path.basename(path)))
            QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
            QApplication.processEvents()
            try:
                return self._open_office(path)
            finally:
                QApplication.restoreOverrideCursor()
                self._update_toolbar()

        return self._add_pdf_tab(path)

    def _open_office(self, path):
        """Convert an Office/text/vector document via LibreOffice, then open it."""
        from PyQt6.QtWidgets import QMessageBox
        import shutil, subprocess, tempfile, atexit
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            QMessageBox.warning(self, tr("LibreOffice fehlt"),
                tr("LibreOffice wird benoetigt um Office-Dateien zu oeffnen.\n"
                   "Installation: sudo pacman -S libreoffice-still"))
            return None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
            atexit.register(shutil.rmtree, tmp_dir, ignore_errors=True)
            stem = os.path.splitext(os.path.basename(path))[0]
            r = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf",
                 "--outdir", tmp_dir, path],
                capture_output=True, text=True, errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            QMessageBox.warning(self, tr("Konvertierung fehlgeschlagen"),
                tr("LibreOffice hat nicht innerhalb von 120 Sekunden geantwortet."))
            return None
        except Exception as e:
            QMessageBox.warning(self, tr("Office-Konvertierung fehlgeschlagen"), str(e))
            return None
        converted = os.path.join(tmp_dir, stem + ".pdf")
        if not os.path.isfile(converted):
            # LibreOffice benennt manchmal anders — suche erste PDF
            pdfs = [f for f in os.listdir(tmp_dir) if f.endswith(".pdf")]
            if not pdfs:
                QMessageBox.warning(self, tr("Konvertierung fehlgeschlagen"),
                                    (r.stderr or "").strip()[:300]
                                    or tr("LibreOffice hat keine PDF erzeugt."))
                return None
            converted = os.path.join(tmp_dir, pdfs[0])
        return self._add_pdf_tab(converted)

    def _add_pdf_tab(self, path):
        from PyQt6.QtWidgets import QMessageBox
        # A damaged, encrypted or truncated PDF makes this raise. Unhandled in a
        # slot, PyQt takes the whole process down — so a single bad file killed
        # the app instead of reporting one failed open.
        try:
            tab = PdfTab(path)
        except Exception as e:
            import logging; logging.exception("open failed: %s", path)
            QMessageBox.critical(
                self, tr("Datei konnte nicht geoeffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            return
        name = os.path.basename(path)
        disp = name if len(name) <= 22 else name[:19] + "..."
        idx  = self.tabs.addTab(tab, f"  {disp}  ")
        self.tabs.setCurrentIndex(idx)
        AppState.get().open_pdf(path)
        self.tab_opened.emit()
        self.tabs_changed.emit()
        return tab

    def open_file(self, path):
        tab = self._open(path)
        # Persist last opened file for the "reopen on startup" setting — but
        # only when it actually opened. Remembering a file that failed meant the
        # next start reopened it and failed again, every time.
        if tab is None:
            return
        try:
            from PyQt6.QtCore import QSettings
            QSettings("CopyShop", "PDFSuite").setValue("general/last_file", path)
        except Exception:
            pass

    def _open_result_tab(self, path, title):
        # Reached from AppState.result_ready, i.e. from a tool that just wrote a
        # file. If that file is unreadable this raises inside a slot and takes
        # the process with it — the tool's own error handling never sees it.
        try:
            tab = PdfTab(path)
        except Exception as e:
            import logging; logging.exception("result tab failed: %s", path)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(
                self, tr("Ergebnis konnte nicht geoeffnet werden"),
                tr('{p0}\n\n{p1}').format(p0=os.path.basename(path), p1=e))
            return
        disp = title if len(title) <= 22 else title[:19] + "..."
        idx  = self.tabs.addTab(tab, f"  {disp}  ")
        self.tabs.setCurrentIndex(idx)
        self.tab_opened.emit()
        self.tabs_changed.emit()

    def _close_tab(self, idx):
        w = self.tabs.widget(idx)
        # Whatever this tab (or a dialog it owns) put on the pool stops here.
        try:
            from tools.jobs import cancel_owner
            cancel_owner(w)
            for child in w.findChildren(QWidget):
                cancel_owner(child)
        except Exception:
            pass
        if isinstance(w, PdfTab):
            w.cancel_render_work()
            _ThumbnailCache.evict_tab(w.pdf_path)
            _FullPageCache.evict_tab(w.pdf_path)
            # …and the parsed document behind them. A loaded page of a
            # poster-sized PDF is hundreds of megabytes; holding one for a tab
            # that is gone is the largest single thing this app can leak.
            try:
                from tools.render.document_cache import release
                release(w.pdf_path)
            except Exception:
                logging.exception("close: releasing the cached document failed")
        elif isinstance(w, MergeOrderWidget):
            # Closing the preview discards its conversions — unless one is still
            # running, in which case the worker is still writing in there.
            if w.tmp_dir and not w._busy:
                try: shutil.rmtree(w.tmp_dir, ignore_errors=True)
                except Exception: pass
        self.tabs.removeTab(idx)
        self.tabs_changed.emit()

    def get_tab_names(self):
        """Gibt Liste von (idx, name, is_current) für alle PDF-Tabs zurück."""
        result = []
        current = self.tabs.currentIndex()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, PdfTab):
                name = self.tabs.tabText(i).strip()
                result.append((i, name, i == current))
        return result

    def switch_to_tab(self, idx):
        self.tabs.setCurrentIndex(idx)

    def _manage_current(self):
        # Reuse _toggle_manage so the button works as a proper toggle,
        # exiting manage mode when clicked while already inside it.
        self._toggle_manage()

    def show_merge_tab(self, file_paths):
        """Preview for several picked files, shown as a tab in the same style as
        the page manager: sort them, then either merge them into one document or
        open them as separate tabs."""
        import tempfile, logging
        file_paths = [p for p in file_paths if os.path.isfile(p)]
        if not file_paths:
            return
        logging.debug(f"show_merge_tab: {len(file_paths)} Dateien: {file_paths}")

        # A repeat call with the same files is a double click or a re-sent open
        # request, not a second job. Raise the tab that is already open instead
        # of stacking an identical one behind it — that stack was how a fast
        # click ended up merging twice at once.
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, MergeOrderWidget) and w.source_paths == file_paths:
                self.tabs.setCurrentIndex(i)
                return

        widget = MergeOrderWidget(file_paths)   # records file_paths as source_paths
        # One conversion directory per tab. A single panel-wide one was wiped by
        # whichever merge tab was cancelled first, taking the output another tab
        # was still using with it.
        widget.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
        idx = self.tabs.addTab(widget, tr("  📂  Dateien oeffnen  "))
        self.tabs.setCurrentIndex(idx)
        self._manage_btn.setEnabled(False)
        self._print_btn.setEnabled(False)
        self._viewer_info.setText(tr("Dateien sortieren, zusammenfuehren oder einzeln oeffnen"))

        def _on_confirmed(paths):
            import logging
            logging.debug(f"_on_confirmed empfangen: {len(paths)} Dateien")
            self._do_convert_and_merge(paths, widget)

        def _on_separately(paths):
            self._do_convert_and_open(paths, widget)

        def _on_cancelled():
            wi = self.tabs.indexOf(widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            self._update_toolbar()
            try: shutil.rmtree(widget.tmp_dir, ignore_errors=True)
            except Exception: pass

        widget.merge_confirmed.connect(_on_confirmed)
        widget.open_separately.connect(_on_separately)
        widget.cancelled.connect(_on_cancelled)

    def _start_conversion(self, file_paths, merge_widget, on_done):
        """Convert the picked files to PDF in the merge tab's own temp dir and
        hand the results to `on_done(pdfs, failures)`. Shared by merge and
        open-separately.

        The conversion is a plain function on a pool job now. It used to be a
        QThread whose reference the panel had to keep alive by hand, in a set,
        until QThread.finished said the thread had really stopped — get that
        wrong and Qt aborts the process. tools/jobs.py owns the job instead, and
        it is tied to the merge tab so closing the tab stops it.
        """
        from tools.jobs import submit
        from tools.multi_open import convert_files
        self._viewer_info.setText(tr("Konvertiere Dateien..."))
        # Tab-Titel via Widget-Referenz setzen (sicher gegen Index-Shifts)
        wi = self.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.tabs.setTabText(wi, tr("  ⏳  Konvertiere...  "))

        # A file that cannot be converted is dropped from the result, so
        # convert_files hands back which ones and why — the user is told rather
        # than left with a document quietly missing pages.
        return submit(
            lambda job: convert_files(file_paths, merge_widget.tmp_dir, job),
            owner=merge_widget, name="convert-files",
            on_progress=self._viewer_info.setText,
            on_done=lambda result: on_done(result[0], result[1]))

    def _report_conversion_failures(self, failures):
        """Never let files vanish from a merge in silence."""
        if not failures:
            return
        import logging
        from PyQt6.QtWidgets import QMessageBox
        detail = "\n".join(f"{os.path.basename(p)}  —  {m}" for p, m in failures)
        logging.error("conversion failed:\n%s", detail)
        AppState.get().status_message.emit(
            tr('{p0} Datei(en) konnten nicht konvertiert werden').format(p0=len(failures)))
        QMessageBox.warning(
            self, tr("Nicht konvertierte Dateien"),
            tr("Diese Dateien fehlen im Ergebnis:\n\n{p0}").format(p0=detail))

    def _conversion_failed(self, merge_widget, failures=()):
        """No file survived conversion — put the tab back the way it was so the
        user can change the list and try again instead of being stuck."""
        self._viewer_info.setText(tr("Fehler: Keine Dateien konvertiert"))
        wi = self.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.tabs.setTabText(wi, tr("  ✗  Fehler  "))
        merge_widget.set_busy(False)
        self._report_conversion_failures(failures)

    def _do_convert_and_merge(self, file_paths, merge_widget):
        """Konvertiert Dateien und fuegt sie zusammen."""
        import logging
        logging.debug(f"_do_convert_and_merge: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            try:
                from pypdf import PdfWriter, PdfReader
                writer = PdfWriter()
                for path in valid:
                    for page in PdfReader(path, strict=False).pages:
                        writer.add_page(page)
                out = os.path.join(merge_widget.tmp_dir, "zusammengefuehrt.pdf")
                with open(out, "wb") as f:
                    writer.write(f)
            except Exception as e:
                logging.exception("merge failed")
                self._viewer_info.setText(tr('Fehler: {p0}').format(p0=e))
                merge_widget.set_busy(False)
                return
            wi = self.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            self._open_result_tab(out, tr("Zusammengefuehrt"))
            self._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _do_convert_and_open(self, file_paths, merge_widget):
        """"Einzeln oeffnen" — same conversion as the merge, but every file
        becomes its own tab instead of one combined document."""
        import logging
        logging.debug(f"_do_convert_and_open: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            wi = self.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.tabs.removeTab(wi)
            failures = list(failures)
            for path in valid:
                try:
                    self._open(path)
                except Exception as e:
                    logging.exception("open failed: %s", path)
                    failures.append((path, str(e)))
            self._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _update_toolbar(self):
        tab = self._current()
        if tab and isinstance(tab, PdfTab):
            self._manage_btn.setEnabled(True)
            self._print_btn.setEnabled(True)
            self._viewer_info.setText(os.path.basename(tab.pdf_path))
        else:
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText("")

    def _print_current(self):
        tab = self._current()
        if tab: tab._print()

    def _save_current(self):
        """Ctrl+S — aktuellen Tab am Originalpfad speichern."""
        tab = self._current()
        if not tab: return
        try:
            tab.save_to(tab.pdf_path)
            AppState.get().status_message.emit(
                tr('Gespeichert: {p0}').format(p0=os.path.basename(tab.pdf_path)))
        except Exception as e:
            AppState.get().status_message.emit(f"Speicherfehler: {e}")

    def _save_as_current(self):
        """Ctrl+Shift+S — save under a new name.

        With pages picked in the page manager this saves *those pages only*; it
        used to write the whole document and ignore the selection entirely. The
        selection is honoured only while the manager is actually on screen, so a
        selection left behind from an earlier visit cannot silently truncate an
        ordinary Save As."""
        tab = self._current()
        if not tab: return
        uids     = tab.selected_uids() if tab.in_manage_mode() else []
        subset   = bool(uids) and len(uids) < len(tab.model.order)
        stem, ext = os.path.splitext(tab.pdf_path)
        suggested = f"{stem}_auswahl{ext or '.pdf'}" if subset else tab.pdf_path
        title = tr('Auswahl speichern als ({p0} Seiten)').format(p0=len(uids)) \
                if subset else tr("Speichern als")
        path, _ = QFileDialog.getSaveFileName(
            self, title, suggested, tr("PDF Dateien (*.pdf)"))
        if not path: return
        try:
            tab.save_to(path, uids=uids if subset else None)
            name = os.path.basename(path)
            if subset:
                # An export of part of the document — the tab still shows the
                # whole thing, so it keeps its own file.
                AppState.get().status_message.emit(
                    tr('{p0} Seite(n) gespeichert als: {p1}').format(p0=len(uids), p1=name))
                return
            self._retarget_tab(tab, path)
            AppState.get().open_pdf(path)
            AppState.get().status_message.emit(tr('Gespeichert als: {p0}').format(p0=name))
        except Exception as e:
            AppState.get().status_message.emit(f"Speicherfehler: {e}")

    def _retarget_tab(self, tab, path):
        """Point a tab at the file just written for it.

        The model must be re-based, not just re-pathed. save_to() writes the
        pages in display order with rotations baked in, so in the new file page
        i *is* order position i — while src/foreign_src still held indexes into
        the old sources and rotations still asked for a turn already applied.
        Re-pathing alone made a reordered or rotated document show the wrong
        pages after Save As."""
        model = tab.model
        tab.pdf_path        = path
        tab.single.pdf_path = path
        model.src         = {uid: i for i, uid in enumerate(model.order)}
        model.foreign_src = {}
        model.rotations   = {}
        if tab._manage_panel is not None:
            tab._manage_panel.pdf_path = path
            tab._manage_panel.grid.pdf_path = path
        idx  = self.tabs.indexOf(tab)
        name = os.path.basename(path)
        disp = name if len(name) <= 22 else name[:19] + "..."
        if idx >= 0:
            self.tabs.setTabText(idx, f"  {disp}  ")

    def _on_tab_changed(self, idx):
        # ── Always clean up manage layout when switching file tabs ────────────
        # The manage panel belongs to the outgoing tab; if it is still in the
        # splitter we must detach it now, before _current() changes meaning.
        if self._manage_splitter_widget and self._manage_tab is not None:
            old_tab = self._manage_tab   # tab that owns the panel
            # Exit manage mode on the old tab so its stack returns to single view
            if old_tab._stack.currentWidget() is not old_tab.single:
                old_tab._stack.setCurrentWidget(old_tab.single)
                old_tab._on_manage_exit = None  # discard stale callback
            try:
                self._exit_manage_layout()   # detach panel → old tab, destroy splitter
            finally:
                if self.show_sidebar:
                    self.show_sidebar()

        # ── Memory management: freeze outgoing tab, resume incoming tab ────────
        # Cancel pre-render tasks for ALL non-active tabs to stop them burning
        # cache slots that belong to the tab the user is actually looking at.
        active_widget = self.tabs.currentWidget()
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if isinstance(tab, PdfTab) and tab is not active_widget:
                for t in list(tab.single._prerender_tasks):
                    t.cancel()
                tab.single._prerender_tasks.clear()
                # Evict full-page renders for this tab, keeping only its current page
                # (it can re-render quickly when the user switches back)
                _FullPageCache.evict_tab(tab.pdf_path,
                                         keep_page=tab.single._current)
                # Thumbnails are small — let the priority eviction handle those
                # gradually rather than dropping them all at once.

        w = active_widget
        if isinstance(w, PdfTab):
            _set_active(w.pdf_path, w.single._current)
            AppState.get().open_pdf(w.pdf_path)
            AppState.get().page_model   = w.model
            AppState.get().current_page = w.single._current
            self._manage_btn.setEnabled(True)
            self._print_btn.setEnabled(True)
            self._viewer_info.setText(os.path.basename(w.pdf_path))
            w.single._view.setFocus()
        elif isinstance(w, MergeOrderWidget):
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText(tr("Dateien sortieren, zusammenfuehren oder einzeln oeffnen"))
            w.setFocus()
        else:
            self._manage_btn.setEnabled(False)
            self._print_btn.setEnabled(False)
            self._viewer_info.setText("")
        self._sync_sidebar()

    def _sync_sidebar(self):
        """The merge preview brings its own sidebar. The app's tool nav sitting
        next to it made two stacked sidebars, the left one offering tools that
        do not apply to the view — so it steps aside for that tab, exactly as it
        does for the page manager."""
        w = self.tabs.currentWidget()
        if isinstance(w, MergeOrderWidget):
            if self.hide_sidebar: self.hide_sidebar()
        elif self._manage_splitter_widget is None:   # manage mode owns it there
            if self.show_sidebar: self.show_sidebar()
