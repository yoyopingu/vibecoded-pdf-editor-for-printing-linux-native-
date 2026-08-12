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
from tools.viewer.panel import _ViewerKeyFilter, PageViewerPanel
from tools.viewer.merge import FileCard, FileGrid, MergeShortcutFilter, MergeOrderWidget
from tools.viewer.manage import ManageShortcutFilter, ManagePanel
from tools.viewer.page_grid import CARD_W, CARD_H, GAP, MARGIN, PageCard, PageGrid
from tools.viewer.single_page import MAX_ZOOM, MIN_ZOOM, SinglePageView
from tools.viewer.printing import _gs_blacked_out, _PrintPreview, _PRINTER_LIST_CACHE, PrintDialog
from tools.viewer.tab import PdfTab
from tools.viewer.canvas import PdfPageCanvas
from tools.viewer.model import _positions_to_str, _parse_positions, PageModel
from tools.viewer.theme import _DARK_TV, _LIGHT_TV, _TV, _TOP_BTN_W, _PREV_BTN, _DROP_THICKNESS, _DROP_HALO, _paint_drop_marker, _theme_panels, _register_themed, set_viewer_theme
from tools.render.queue import _thumb_render_width, _ThumbSignals, _ThumbTask, _RenderQueue, _render_queue, shutdown_render_queue, _PageSignals, _RegionSignals, _RegionRenderTask, _target_scale, _PageRenderTask, _prerender_enabled, apply_performance_settings
from tools.render.images import MAX_RENDER_PX, _SCALE_EPS, _good_enough, pil_to_qpixmap, _render_image, render_page, _rotate_char_boxes
from tools.render.caches import _active_path, _active_page, _set_active, _priority_evict, _ThumbnailCache, _FullPageCache



# Two scales count as the same when they differ by less than this. Below it,
# re-rendering buys nothing the eye can see.









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



# ── Datenmodell ───────────────────────────────────────────────────────────────



# ══════════════════════════════════════════════════════════════════════════════
# EINZELSEITEN-ANSICHT
# Zeigt eine Seite auf volle Breite, springt seitenweise
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# THUMBNAIL-KARTE
# ══════════════════════════════════════════════════════════════════════════════




# ══════════════════════════════════════════════════════════════════════════════
# THUMBNAIL-GRID
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# GLOBALER EVENT-FILTER FUER SHORTCUTS
# Registriert auf QApplication — funktioniert immer unabhaengig vom Fokus
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# VERWALTUNGS-LEISTE
# ══════════════════════════════════════════════════════════════════════════════









# ══════════════════════════════════════════════════════════════════════════════
# PDF TAB
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# MERGE ORDER WIDGET — Datei-Grid wie Seitenverwaltung
# ══════════════════════════════════════════════════════════════════════════════









# ══════════════════════════════════════════════════════════════════════════════
# GLOBALER KEY-FILTER
# Ctrl+Shift+O → Einzelansicht ↔ Seiten verwalten umschalten
# Esc          → immer zurück zur Einzelansicht
# Tab          → normale Fokus-Navigation zwischen Eingabefeldern (nicht abgefangen)
# ══════════════════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════════════════
# HAUPT-VIEWER
# ══════════════════════════════════════════════════════════════════════════════

