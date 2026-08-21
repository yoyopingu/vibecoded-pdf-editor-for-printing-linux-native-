"""
The page preview beside the print dialog.

Draws the sheet as the printer will produce it: the selected paper, the margins,
the scale, and the page itself rendered into that. It renders off the GUI thread
and takes its settings from the dialog through update_settings.
"""
import logging
import io
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import QPixmap, QColor, QPainter, QPen
from tools.i18n import tr
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.theme import _TV


class _PrintPreview(QWidget):
    """Left-side print preview panel — mirrors Acrobat's layout preview."""

    # Delivers a finished background render to the GUI thread. A signal is
    # auto-queued across threads; the previous QTimer.singleShot(0, …) was
    # created ON the render thread, which has no event loop, so it never fired
    # and the preview stayed blank forever.
    _render_ready = pyqtSignal(int, object, float, float)

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
        self._scale_pct  = 100      # what "Originalgrösse" is a % of
        self._paper_key  = "A4"
        self._orient_idx = 0        # 0=auto, 1=portrait, 2=landscape
        self.setObjectName("printPreviewPanel")
        self.setFixedWidth(260)
        self.setStyleSheet(
            f"QWidget#printPreviewPanel{{background:{_TV['sidebar_bg']};}}")

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
        # iconBtn, not secondaryBtn: the latter's padding is 6px by 14px with a
        # 28px minimum height, which on a button fixed at 28x28 pushes the glyph
        # clean outside the box — the two page buttons rendered as empty
        # rectangles. The same mistake is written up at QPushButton#iconBtn in
        # tools/shell/style.py and again beside the zoom buttons in
        # tools/panels/_shared.py; this was the third place with it.
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedSize(46, 28)
        self._prev_btn.setObjectName("iconBtn")
        self._prev_btn.clicked.connect(self._prev_page)
        self._page_lbl = QLabel(tr("Seite 1 / 1"))
        self._page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_lbl.setMinimumWidth(84)
        self._page_lbl.setStyleSheet(
            f"font-size:10px;color:{_TV['dim']};background:transparent;")
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedSize(46, 28)
        self._next_btn.setObjectName("iconBtn")
        self._next_btn.clicked.connect(self._next_page)
        # Together, centred under the sheet. The stretch used to be on the
        # label, which pushed the two buttons out to opposite ends of the
        # sidebar — a pair of controls that belong beside the page number,
        # sitting as far apart as the panel allowed.
        nav.addStretch()
        nav.addWidget(self._prev_btn)
        nav.addWidget(self._page_lbl)
        nav.addWidget(self._next_btn)
        nav.addStretch()
        lyt.addLayout(nav)

        self._render_page()

    # ── Public API called by PrintDialog ──────────────────────────────────────

    def update_settings(self, scale_idx, paper_key, orient_idx, margin_mm,
                        scale_pct=100):
        changed = (self._scale_idx  != scale_idx  or
                   self._scale_pct  != scale_pct  or
                   self._paper_key  != paper_key  or
                   self._orient_idx != orient_idx or
                   self._margin_mm  != margin_mm)
        self._scale_idx  = scale_idx
        self._scale_pct  = scale_pct
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

        import weakref
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
                logging.exception("print preview: background render failed")
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
        """(w_mm, h_mm) for the chosen paper, or None when there is no chosen
        paper to draw.

        The sizes come from the spooler's table rather than a second copy kept
        here. The copy had the same nine entries the spooler's used to, and the
        same answer of A4 for everything else — so choosing SRA3 drew an A4
        sheet in the preview while the job went out as SRA3.
        """
        from tools.printing.spool import paper_size_pt
        size = paper_size_pt(self._paper_key)
        if size is None:
            return None
        pw, ph = size[0] * 25.4 / 72.0, size[1] * 25.4 / 72.0
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
        cw = self._canvas.width()
        ch = self._canvas.height()
        if cw < 20 or ch < 20:
            return

        page_w_mm = self._page_w_pt * 25.4 / 72.0
        page_h_mm = self._page_h_pt * 25.4 / 72.0

        # No chosen paper means no sheet to draw the page against, and nothing
        # here knows what it will land on. Everything the sheet is used for —
        # the white rectangle, the dashed printable-area boundary, the fitting,
        # the warning about edges that will be cut — is a statement about a
        # sheet nobody picked, so none of it is drawn. The page is shown at its
        # own size, which is all that is actually known.
        sheet = self._paper_dims_mm()
        on_a_sheet = sheet is not None
        if on_a_sheet:
            paper_w_mm, paper_h_mm = sheet
            full_bleed = self._margin_mm < 0.5
        else:
            paper_w_mm, paper_h_mm = page_w_mm, page_h_mm
            full_bleed = True          # no margin is known, so none is marked
        m = self._margin_mm
        printable_w = paper_w_mm if full_bleed else max(1.0, paper_w_mm - 2*m)
        printable_h = paper_h_mm if full_bleed else max(1.0, paper_h_mm - 2*m)

        # Compute the scale factor that will actually be applied when printing
        scale_fit  = min(printable_w / max(page_w_mm, 0.001),
                         printable_h / max(page_h_mm, 0.001))
        if not on_a_sheet:
            # Fit and shrink both need a sheet to work against; the percentage
            # is the operator's own number and still means what it says.
            content_scale = (self._scale_pct / 100.0
                             if self._scale_idx == 1 else 1.0)
        elif self._scale_idx == 0:      # Fit
            content_scale = scale_fit
        elif self._scale_idx == 1:      # Originalgrösse, at the chosen %
            content_scale = self._scale_pct / 100.0
        else:                           # Shrink only
            content_scale = min(1.0, scale_fit)

        content_w_mm = page_w_mm * content_scale
        content_h_mm = page_h_mm * content_scale
        overflows = (content_w_mm > printable_w + 0.5 or
                     content_h_mm > printable_h + 0.5)
        # Overflowing the printable area only matters if something is actually
        # out there. Nearly every page has a white border wider than the 3.5 mm
        # a printer cannot reach, so warning on the geometry alone made the
        # preview go red for jobs that print perfectly — and a warning that
        # cries wolf on every file is one nobody reads on the file that
        # deserves it.
        will_clip = on_a_sheet and overflows and self._ink_outside(
            content_w_mm, content_h_mm, printable_w, printable_h)

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

        # The sheet — a drop shadow and white paper — only when there is a
        # sheet. Drawn for "leave it to the printer" it would be a picture of
        # a sheet of some size, and the size it would look like is the page's.
        if on_a_sheet:
            p.fillRect(ox + 3, oy + 3, pw, ph, QColor(0, 0, 0, 60))
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
            p.restore()
            # Mark what will be lost — outside the clip, which is where it is.
            # The tint used to be drawn inside it, so it landed on the printable
            # area instead of on the overhang: the whole visible page went red,
            # saying "this page is a problem" when what is true is "these edges
            # are".
            if will_clip:
                clip_tint = QColor(220, 60, 60, 70)
                left   = QRectF(cx, cy, max(0.0, pr.left() - cx), ch_px)
                right  = QRectF(pr.right(), cy,
                                max(0.0, cx + cw_px - pr.right()), ch_px)
                top    = QRectF(cx, cy, cw_px, max(0.0, pr.top() - cy))
                bottom = QRectF(cx, pr.bottom(), cw_px,
                                max(0.0, cy + ch_px - pr.bottom()))
                for band in (left, right, top, bottom):
                    if band.width() > 0.5 and band.height() > 0.5:
                        p.fillRect(band, clip_tint)
        else:
            # No image yet — grey placeholder
            p.fillRect(content_rect, QColor(200, 200, 200))

        # Margin indicator — dashed line showing the printable-area boundary.
        # There is no printable area to bound without a sheet: the margin comes
        # from the queue's hardware margin for a paper size, and no size was
        # named.
        if on_a_sheet and not full_bleed:
            pen = QPen(QColor(180, 100, 100, 200), 1, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawRect(pr.toRect())

        # Paper border, or — with no paper — the edge of the page itself, so
        # the pixmap does not float unbounded on the canvas.
        p.setPen(QPen(QColor(140, 140, 140), 1))
        if on_a_sheet:
            p.drawRect(ox, oy, pw - 1, ph - 1)
        else:
            p.drawRect(content_rect.toRect().adjusted(0, 0, -1, -1))

        p.end()
        self._canvas.setPixmap(canvas_pm)

        # Info line
        pct = content_scale * 100.0
        if on_a_sheet:
            info = (f"{pct:.0f}%  ·  "
                    f"{page_w_mm:.0f}×{page_h_mm:.0f} mm  →  "
                    f"{paper_w_mm:.0f}×{paper_h_mm:.0f} mm")
        else:
            # Naming a target size here would be inventing one. The page size
            # is known; what it goes onto is the printer's business.
            info = (f"{pct:.0f}%  ·  {page_w_mm:.0f}×{page_h_mm:.0f} mm  ·  "
                    + tr("Papier wie im Drucker eingestellt"))
        self._info_lbl.setText(info)
        self._clip_lbl.setVisible(will_clip)

    def _ink_outside(self, content_w_mm, content_h_mm, printable_w, printable_h):
        """Is there anything drawn in the part that will not print?

        The bands that fall outside the printable area are mapped back onto the
        rendered page and sampled. Anything appreciably darker than white counts
        as ink; a page whose overhang is blank paper is not clipped in any sense
        the operator cares about.

        Sampled on a grid rather than pixel by pixel — this runs on every change
        to the dialog, and a stray dot of colour large enough to matter on paper
        is many pixels across at preview resolution. Errs towards warning: if
        the page cannot be measured, say it clips.
        """
        pm = self._pixmap
        if pm is None or pm.isNull():
            return True
        try:
            img = pm.toImage()
            w, h = img.width(), img.height()
            if w < 4 or h < 4:
                return True
            # How much of the page, as a fraction of its own width and height,
            # sticks out past what the printer can reach. Symmetric: the content
            # is centred, so an overhang appears on both sides.
            over_x = max(0.0, (content_w_mm - printable_w) / 2.0 / max(content_w_mm, 1e-6))
            over_y = max(0.0, (content_h_mm - printable_h) / 2.0 / max(content_h_mm, 1e-6))
            band_x = int(w * over_x)
            band_y = int(h * over_y)
            step = max(1, min(w, h) // 120)
            WHITE = 246          # anything lighter than this is paper

            def dark(x0, x1, y0, y1):
                for y in range(max(0, y0), min(h, y1), step):
                    for x in range(max(0, x0), min(w, x1), step):
                        c = img.pixel(x, y)
                        if (c & 0xFF) < WHITE or ((c >> 8) & 0xFF) < WHITE \
                           or ((c >> 16) & 0xFF) < WHITE:
                            return True
                return False

            if band_x > 0 and (dark(0, band_x, 0, h) or dark(w - band_x, w, 0, h)):
                return True
            if band_y > 0 and (dark(0, w, 0, band_y) or dark(0, w, h - band_y, h)):
                return True
            return False
        except Exception:
            logging.debug("print preview: could not check the overhang",
                          exc_info=True)
            return True

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._redraw()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._redraw)
