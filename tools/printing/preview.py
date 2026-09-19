"""
The page preview beside the print dialog.

Draws the sheet as the printer will produce it: the selected paper, the margins,
the scale, and the page itself rendered into that. It renders off the GUI thread
and takes its settings from the dialog through update_settings.
"""
import logging
import io
import os
import tempfile
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import QPixmap, QColor, QPainter, QPen, QFont
from tools.i18n import tr
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.theme import _TV
from tools.render.document_cache import open_document as _open_pdf
from tools.ghostscript import unlink
from tools.printing.content import DOCUMENT, prepare_print_page


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
        self._comments_forms = DOCUMENT
        self._handling = "size"
        self._handling_opts = {}
        self.setObjectName("printPreviewPanel")
        self.setFixedWidth(316)
        # Nav arrows match the concept `.icon` (28×24, no filled chrome).
        # objectName stays iconBtn so they keep the global type, but the
        # panel sheet drops the filled border the rest of the app wants.
        self.setStyleSheet(
            f"QWidget#printPreviewPanel{{background:{_TV['sidebar_bg']};}}"
            f"QPushButton#iconBtn{{background:transparent;color:{_TV['dim']};"
            f"border:none;border-radius:4px;padding:0;"
            f"min-width:28px;max-width:28px;width:28px;"
            f"min-height:24px;max-height:24px;height:24px;"
            f"font-size:13px;}}"
            f"QPushButton#iconBtn:hover{{background:{_TV['hover']};"
            f"color:{_TV['text']};}}"
            f"QPushButton#iconBtn:disabled{{background:transparent;"
            f"color:{_TV['vdim']};}}")

        lyt = QVBoxLayout(self)
        lyt.setContentsMargins(18, 22, 18, 14)
        lyt.setSpacing(4)

        self._canvas = QLabel()
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setMinimumHeight(240)
        lyt.addWidget(self._canvas, 1)

        # One caption under the sheet (concept `#meta`). Clip warning shares
        # this line rather than sitting on a second label of its own.
        self._info_lbl = QLabel("")
        self._info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_lbl.setWordWrap(True)
        self._info_lbl.setStyleSheet(
            f"font-size:11px;color:{_TV['dim']};background:transparent;")
        lyt.addWidget(self._info_lbl)

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
        self._prev_btn.setFixedSize(28, 24)
        self._prev_btn.setObjectName("iconBtn")
        self._prev_btn.clicked.connect(self._prev_page)
        self._page_lbl = QLabel(tr("Seite 1 / 1"))
        self._page_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._page_lbl.setMinimumWidth(88)
        self._page_lbl.setStyleSheet(
            f"font-size:11px;color:{_TV['dim']};background:transparent;")
        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedSize(28, 24)
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
                        scale_pct=100, comments_forms=DOCUMENT,
                        handling="size", handling_opts=None):
        content_changed = self._comments_forms != comments_forms
        handling = handling or "size"
        opts = dict(handling_opts or {})
        mode_changed = (self._handling != handling or
                        self._handling_opts != opts)
        changed = (self._scale_idx  != scale_idx  or
                   self._scale_pct  != scale_pct  or
                   self._paper_key  != paper_key  or
                   self._orient_idx != orient_idx or
                   self._margin_mm  != margin_mm)
        if self._handling != handling:
            self._current = 0
        self._scale_idx  = scale_idx
        self._scale_pct  = scale_pct
        self._paper_key  = paper_key
        self._orient_idx = orient_idx
        self._margin_mm  = margin_mm
        self._comments_forms = comments_forms or DOCUMENT
        self._handling = handling
        self._handling_opts = opts
        if content_changed:
            # Comments & Forms changes what is *on* the page, so the bitmap
            # has to be made again. Paper and scale only change how that
            # bitmap is placed on the sheet.
            self._render_page()
        elif changed or mode_changed:
            n = self._nav_count()
            if self._current >= n:
                self._current = max(0, n - 1)
            self._update_nav_label()
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
            if self._handling == "size":
                self._render_page()
            else:
                self._update_nav_label()
                self._redraw()

    def _next_page(self):
        if self._current < self._nav_count() - 1:
            self._current += 1
            if self._handling == "size":
                self._render_page()
            else:
                self._update_nav_label()
                self._redraw()

    def _poster_tiles(self):
        pct = self._handling_opts.get("tile_pct", 200)
        if pct >= 180:
            return 4
        if pct >= 120:
            return 2
        return 1

    def _nav_count(self):
        n = len(self._pages)
        h = self._handling
        if h == "poster":
            return max(1, self._poster_tiles())
        if h == "nup":
            c = max(1, int(self._handling_opts.get("count") or 4))
            return max(1, (n + c - 1) // c)
        if h == "booklet":
            sig = max(1, (n + 3) // 4)
            frm = int(self._handling_opts.get("sheet_from") or 1)
            to = int(self._handling_opts.get("sheet_to") or sig)
            frm = max(1, min(frm, sig))
            to = max(frm, min(to, sig))
            return to - frm + 1
        return n

    def _update_nav_label(self):
        count = self._nav_count()
        if count <= 0:
            self._page_lbl.setText("—")
            self._prev_btn.setEnabled(False)
            self._next_btn.setEnabled(False)
            return
        if self._current >= count:
            self._current = count - 1
        i = self._current + 1
        h = self._handling
        if h == "poster":
            self._page_lbl.setText(
                tr("Kachel {p0} / {p1}").format(p0=i, p1=count))
        elif h in ("nup", "booklet"):
            self._page_lbl.setText(
                tr("Bogen {p0} / {p1}").format(p0=i, p1=count))
        else:
            n = len(self._pages)
            total = len(self._model.order)
            pos = self._pages[self._current] if self._pages else 0
            if n == total:
                self._page_lbl.setText(
                    tr('Seite {p0} / {p1}').format(p0=pos + 1, p1=total))
            else:
                self._page_lbl.setText(
                    tr('Seite {p0}   ({p1} / {p2} ausgewählt)').format(
                        p0=pos + 1, p1=self._current + 1, p2=n))
        self._prev_btn.setEnabled(self._current > 0)
        self._next_btn.setEnabled(self._current < count - 1)

    def _render_page(self):
        self._render_token += 1
        token = self._render_token
        n     = len(self._pages)
        total = len(self._model.order)
        if n == 0:
            self._update_nav_label()
            self._pixmap = None
            self._redraw()
            return
        if self._handling == "size":
            if self._current >= n:
                self._current = n - 1
            pos = self._pages[self._current]
        else:
            pos = self._pages[0]
        self._update_nav_label()
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

        comments_forms = self._comments_forms

        def _bg(job):
            prepared = None
            try:
                fd, prepared = tempfile.mkstemp(suffix="_preview.pdf")
                os.close(fd)
                try:
                    prepare_print_page(src_path, orig, prepared, comments_forms)
                    render_path, render_index = prepared, 0
                except Exception:
                    logging.debug("print preview: could not prepare page; "
                                  "rendering the original", exc_info=True)
                    render_path, render_index = src_path, orig
                with _pdfium_lock:
                    doc = _open_pdf(render_path)
                    try:
                        page = doc[render_index]
                        pw_pt = page.get_width()
                        ph_pt = page.get_height()
                        render_scale = 240.0 / max(pw_pt, ph_pt, 1)
                        # optimize_mode="print" honours the Print flag, so
                        # the preview matches the paper rather than the
                        # screen — Acrobat's print preview does the same.
                        bm  = page.render(scale=render_scale,
                                          optimize_mode="print")
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
            finally:
                unlink(prepared)
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
            if self._handling == "booklet" and paper_w_mm < paper_h_mm:
                paper_w_mm, paper_h_mm = paper_h_mm, paper_w_mm
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

        if self._handling != "size":
            self._draw_handling(p, QRectF(ox, oy, pw, ph))
        else:
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

        self._set_caption(on_a_sheet, will_clip, page_w_mm, page_h_mm,
                          paper_w_mm, paper_h_mm)

    def _set_caption(self, on_a_sheet, will_clip, page_w_mm, page_h_mm,
                     paper_w_mm, paper_h_mm):
        h = self._handling
        opts = self._handling_opts
        if h == "poster":
            pct = opts.get("tile_pct", 200)
            tiles = self._poster_tiles()
            info = tr("Poster · {pct} % · Kachel {a}/{b}").format(
                pct=pct, a=self._current + 1, b=tiles)
            ov = opts.get("overlap_mm") or 0
            if ov:
                info += tr(" · Überlappung {ov} mm").format(ov=ov)
        elif h == "nup":
            n = opts.get("count") or 4
            order = opts.get("order") or "h"
            order_lbl = {
                "h": tr("Horizontal"), "hr": tr("Horizontal umgekehrt"),
                "v": tr("Vertikal"), "vr": tr("Vertikal umgekehrt"),
            }.get(order, order)
            info = tr("{n} Seiten / Bogen · {order}").format(
                n=n, order=order_lbl)
        elif h == "booklet":
            bind = tr("Links") if opts.get("bind") == "left" else tr("Rechts")
            info = tr("Broschüre · Bindung {bind}").format(bind=bind)
        else:
            if self._scale_idx == 0:
                scale_lbl = tr("Anpassen")
            elif self._scale_idx == 1:
                scale_lbl = f"{self._scale_pct:g} %"
            else:
                scale_lbl = tr("Verkleinern")
            if on_a_sheet:
                info = (f"{scale_lbl} · {self._paper_key}  ·  "
                        f"{page_w_mm:.0f}×{page_h_mm:.0f} mm  →  "
                        f"{paper_w_mm:.0f}×{paper_h_mm:.0f} mm")
            else:
                info = f"{scale_lbl} · " + tr(
                    "Papier wie im Drucker eingestellt")
            if will_clip:
                warn = tr("Inhalt wird beschnitten")
                self._info_lbl.setTextFormat(Qt.TextFormat.RichText)
                self._info_lbl.setText(
                    f'{info}  ·  <span style="color:{_TV["acc"]};'
                    f'font-weight:700">{warn}</span>')
                return
        self._info_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._info_lbl.setText(info)

    def _draw_handling(self, p, sheet):
        h = self._handling
        if h == "poster":
            self._draw_poster(p, sheet)
        elif h == "nup":
            self._draw_nup(p, sheet)
        elif h == "booklet":
            self._draw_booklet(p, sheet)

    def _draw_page_into(self, p, rect, empty=False, tag=""):
        """Composite the last rendered page (or a blank slot) into `rect`."""
        r = rect.toRect()
        if empty or self._pixmap is None or self._pixmap.isNull():
            p.fillRect(r, QColor(243, 245, 248))
        else:
            scaled = self._pixmap.scaled(
                max(1, r.width()), max(1, r.height()),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(r, scaled)
        if tag:
            p.setPen(QPen(QColor(26, 58, 104)))
            font = QFont()
            font.setPixelSize(max(8, min(14, r.height() // 8)))
            font.setBold(True)
            p.setFont(font)
            p.drawText(r.adjusted(4, 2, -4, 0),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       tag)

    def _draw_poster(self, p, sheet):
        tiles = self._poster_tiles()
        cols = 2 if tiles == 4 else tiles
        rows = 2 if tiles == 4 else 1
        idx = self._current
        col = idx % cols
        row = idx // cols
        p.save()
        p.setClipRect(sheet)
        # Zoom the page so this tile is one cell of a cols×rows grid.
        tw, th = sheet.width() * cols, sheet.height() * rows
        dest = QRectF(sheet.x() - col * sheet.width(),
                      sheet.y() - row * sheet.height(), tw, th)
        self._draw_page_into(p, dest, tag="")
        p.restore()
        if self._handling_opts.get("cut_marks"):
            p.setPen(QPen(QColor(192, 57, 43), 1))
            x0, y0 = sheet.x() + 6, sheet.y() + 6
            x1, y1 = sheet.right() - 6, sheet.bottom() - 6
            for x, y, dx, dy in (
                    (x0, y0, 10, 10), (x1, y0, -10, 10),
                    (x0, y1, 10, -10), (x1, y1, -10, -10)):
                p.drawLine(int(x), int(y), int(x + dx), int(y))
                p.drawLine(int(x), int(y), int(x), int(y + dy))
        if self._handling_opts.get("labels"):
            names = ("A1", "A2", "B1", "B2")
            tag = names[idx] if idx < 4 else f"T{idx + 1}"
            p.setPen(QPen(QColor(192, 57, 43)))
            font = QFont()
            font.setPixelSize(10)
            font.setBold(True)
            p.setFont(font)
            p.drawText(int(sheet.x() + 8), int(sheet.y() + 18), tag)

    def _nup_order(self, n, cols, rows, order):
        cells = []
        if order in ("v", "vr"):
            for c in range(cols):
                for r in range(rows):
                    cells.append(r * cols + c)
        else:
            cells = list(range(n))
        if order in ("hr", "vr"):
            cells.reverse()
        return cells

    def _draw_nup(self, p, sheet):
        n = int(self._handling_opts.get("count") or 4)
        grid = {2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}
        cols, rows = grid.get(n, (2, 2))
        order = self._nup_order(n, cols, rows,
                                self._handling_opts.get("order") or "h")
        border = self._handling_opts.get("border", True)
        gap = 3
        pad = 6
        slot_w = (sheet.width() - 2 * pad - (cols - 1) * gap) / cols
        slot_h = (sheet.height() - 2 * pad - (rows - 1) * gap) / rows
        start = self._current * n
        src_n = len(self._pages)
        for i in range(n):
            r, c = divmod(i, cols)
            # Slot i is visual row-major; the page comes from `order`.
            x = sheet.x() + pad + c * (slot_w + gap)
            y = sheet.y() + pad + r * (slot_h + gap)
            slot = QRectF(x, y, slot_w, slot_h)
            page_i = start + order[i]
            empty = page_i >= src_n
            tag = "" if empty else str(self._pages[page_i] + 1)
            self._draw_page_into(p, slot, empty=empty, tag=tag)
            if border and not empty:
                p.setPen(QPen(QColor(197, 206, 216), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(slot.toRect())

    def _draw_booklet(self, p, sheet):
        n = len(self._pages)
        frm = int(self._handling_opts.get("sheet_from") or 1)
        sheet_i = self._current + frm  # 1-based signature index
        left_sel = n - (sheet_i - 1)   # 1-based index into the selection
        right_sel = sheet_i
        if self._handling_opts.get("bind") == "right":
            left_sel, right_sel = right_sel, left_sel
        mid = sheet.center().x()
        gap = 0
        left_r = QRectF(sheet.x(), sheet.y(), mid - sheet.x() - gap,
                        sheet.height())
        right_r = QRectF(mid + gap, sheet.y(),
                         sheet.right() - mid - gap, sheet.height())

        def _slot(sel, rect):
            if sel < 1 or sel > n:
                self._draw_page_into(p, rect, empty=True)
            else:
                tag = str(self._pages[sel - 1] + 1)
                self._draw_page_into(p, rect, tag=tag)

        _slot(left_sel, left_r)
        _slot(right_sel, right_r)
        p.setPen(QPen(QColor(138, 151, 168), 1, Qt.PenStyle.DashLine))
        p.drawLine(int(mid), int(sheet.y()), int(mid), int(sheet.bottom()))

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
