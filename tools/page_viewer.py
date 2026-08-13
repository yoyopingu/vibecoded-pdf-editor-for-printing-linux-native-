"""
Page Viewer — re-export shim.

The viewer lives in tools/viewer/ now, one module per part, and everything that
turns a PDF into pixels lives in tools/render/. This module used to hold all
7,600 lines of both. It stays as the name the rest of the app imports from:

    tools/render/     document_cache  open documents and their parsed pages
                      raster          one page, one bitmap, interruptible
                      region          what to render for a viewport
                      images          whole-page renders and scale primitives
                      caches          thumbnail and full-page LRUs
                      queue           the priority queue and its render tasks

    tools/printing/   dialog          what to print, on what, and how
                      preview         the sheet as the printer will produce it
                      spool           sending it: Ghostscript and lp, or Qt

    tools/viewer/     theme           colours, and the widgets that follow them
                      model           page order, rotation, selection
                      canvas          draws a page, selects text on it
                      single_page     the one-page view and its zoom
                      page_grid       the thumbnails of "Seiten verwalten"
                      manage          the toolbar over that grid
                      merge           the file-level grid, for several files
                      tab             one open document
                      panel           the tab host

What this module does *not* re-export is the private module state those files
keep — _prerender_enabled, _active_path, _active_page, _PRINTER_LIST_CACHE.
Importing one of those here would copy its value once and then quietly go stale
the first time the module that owns it rebinds it; there is no name to import
that stays correct. Read them from the module that owns them.
"""

# ── The rendering engine ─────────────────────────────────────────────────────
from tools.render.caches      import (_set_active, _priority_evict,
                                      _ThumbnailCache, _FullPageCache)
from tools.render.images      import (MAX_RENDER_PX, _SCALE_EPS, _good_enough,
                                      pil_to_qpixmap, _render_image,
                                      _rotate_char_boxes)
from tools.render.queue       import (_thumb_render_width, _ThumbSignals,
                                      _ThumbTask, _RenderQueue, _render_queue,
                                      shutdown_render_queue, _PageSignals,
                                      _RegionSignals, _RegionRenderTask,
                                      _target_scale, _PageRenderTask,
                                      apply_performance_settings)

# libpdfium is not thread-safe, and not only per document: two threads rendering
# two *different* documents corrupt the heap (measurements in
# tools/render/document_cache.py). Every pdfium call in the process is
# serialised through this one lock, so that documents opened ad hoc — by the
# tools, by the print path — are mutually exclusive with the cached ones the
# viewer renders from.
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock

# ── The viewer ───────────────────────────────────────────────────────────────
from tools.viewer.theme       import (_DARK_TV, _LIGHT_TV, _TV, _TOP_BTN_W,
                                      _PREV_BTN, _DROP_THICKNESS, _DROP_HALO,
                                      _paint_drop_marker, _theme_panels,
                                      _register_themed, set_viewer_theme)
from tools.viewer.model       import (_positions_to_str, _parse_positions,
                                      PageModel)
from tools.viewer.canvas      import PdfPageCanvas
from tools.viewer.single_page import MAX_ZOOM, MIN_ZOOM, SinglePageView
from tools.viewer.page_grid   import (CARD_W, CARD_H, GAP, MARGIN, PageCard,
                                      PageGrid)
from tools.viewer.manage      import ManageShortcutFilter, ManagePanel
from tools.viewer.merge       import (FileCard, FileGrid, MergeShortcutFilter,
                                      MergeOrderWidget)
from tools.printing.spool     import _gs_blacked_out
from tools.printing.preview   import _PrintPreview
from tools.printing.dialog    import PrintDialog
from tools.viewer.tab         import PdfTab
from tools.viewer.panel       import _ViewerKeyFilter, PageViewerPanel
