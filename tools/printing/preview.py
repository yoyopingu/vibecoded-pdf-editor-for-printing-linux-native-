"""
The page preview beside the print dialog.

Draws the sheet as the printer will produce it: the selected paper, the margins,
the scale, and the page itself rendered into that. It renders off the GUI thread
and takes its settings from the dialog through update_settings.
"""
import io
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QRectF
from PyQt6.QtGui import QPixmap, QColor, QPainter, QPen
from tools.i18n import tr
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.images import pil_to_qpixmap
from tools.viewer.theme import _TV


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
