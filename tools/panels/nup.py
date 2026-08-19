"""
N-Up Layout — several source pages to a sheet, with margins, gaps and crop
marks, over a live preview of the sheet.
"""
import os, math
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals
from tools.theme import INK, PAPER, _TV, _register_themed
from PyQt6.QtWidgets import QVBoxLayout, QLabel, QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox, QSplitter, QGridLayout
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush
from tools.app_state import AppState
from tools._base import BasePanel
from tools.i18n import tr
from tools.panels._shared import MM_TO_PT, PaperFormatSelector, _inherited_rotate, _visible_box, _visible_size, row, PreviewPane
from tools.panels._cropmarks import _crop_mark_segments, _crop_marks_content_stream
from tools.panels._imposition import (_ROT_MATRIX, _slot_placement, _flatten_annots,
                                      full_scale_problem)


# ══════════════════════════════════════════════════════════════════════════════
# N-UP LAYOUT
# ══════════════════════════════════════════════════════════════════════════════
def _nup_slot_rects(params, n_slot):
    """The (x0, y0, x1, y1) rectangle (PDF points, origin bottom-left) of every
    slot on a sheet — shared by the renderer, the preview and the crop marks so
    they always agree."""
    (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
    rects = []
    for slot_i in range(n_slot):
        col_i = slot_i % cols; row_i = slot_i // cols
        x0 = ml + col_i * (slot_w + gh)
        y0 = out_h - mt - (row_i + 1) * slot_h - row_i * gv
        rects.append((x0, y0, x0 + slot_w, y0 + slot_h))
    return rects


def _full_scale_problem(max_w, max_h, params):
    """Why pages of `max_w` x `max_h` points will not fit their slots at 100 %,
    or None if they fit.

    The rule and the sentence live in tools/panels/_imposition.py, with the
    placement maths both tools share. What is specific here is the sheet that
    would work: the same arithmetic "Wie Quellseite x Raster" uses, so the size
    the message names is one this tool can actually produce."""
    (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
    return full_scale_problem(
        max_w, max_h, slot_w, slot_h,
        need_w=max_w * cols + ml + mr + gh * (cols - 1),
        need_h=max_h * rows + mt + mb + gv * (rows - 1))


def _build_nup(src, out, src_pages, params, n_slot, report, crop_marks=False,
               fixed_scale=None):
    """Build the N-Up PDF on a worker thread (via BasePanel.run_async).

    Each source page becomes a Form XObject that is scaled to fit its slot,
    keeping its aspect ratio, and centred there — or, with `fixed_scale=1.0`,
    placed at its own size and centred without being resized at all. This is
    dramatically faster than
    pypdf's ``merge_transformed_page`` on vector-heavy pages (which parses +
    decompresses every content stream — ~30s and a 10× larger output for a dense
    4-page file vs ~0.5s here) and keeps the source content compressed. Only
    plain data crosses the thread boundary."""
    from pikepdf import Pdf, Page, Stream, Array, Name
    (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
    src_doc = Pdf.open(src)
    _flatten_annots(src_doc)
    out_doc = Pdf.new()
    rects = _nup_slot_rects(params, n_slot)

    # One Form XObject per used source page, built once and reused across slots
    # and sheets. Two things are pinned deliberately:
    #  * the BBox is the page's *visible* box (CropBox clipped to MediaBox).
    #    as_form_xobject would otherwise take TrimBox → CropBox → MediaBox
    #    verbatim, so a print PDF's bleed TrimBox — or a stale CropBox left
    #    behind by an earlier crop — decided the layout and pushed the content
    #    off-centre, showing something different from the preview.
    #  * /Rotate is applied through our own exact matrix instead of qpdf's, which
    #    truncates the rotation offset to whole points.
    _forms = {}
    def _form_for(page_i):
        if page_i not in _forms:
            page = src_doc.pages[page_i]
            box  = _visible_box(page)
            arr  = Array([float(v) for v in box])
            page.obj["/CropBox"] = arr
            page.obj["/TrimBox"] = arr
            for key in ("/BleedBox", "/ArtBox"):
                if key in page.obj: del page.obj[key]
            rot = _inherited_rotate(page)
            fx  = page.as_form_xobject(handle_transformations=False)
            fx["/Matrix"] = Array(list(_ROT_MATRIX[rot if rot % 90 == 0 else 0]) + [0.0, 0.0])
            _forms[page_i] = (fx, box, rot)
        return _forms[page_i]

    # Pre-build the crop-marks content stream once (same grid on every sheet).
    mark_ops = _crop_marks_content_stream(rects) if crop_marks else None
    n_sheets = math.ceil(len(src_pages) / n_slot)
    placed   = 0
    for sheet_i in range(n_sheets):
        report(f"{tr('Blatt')} {sheet_i+1} / {n_sheets} …")
        sheet = Page(out_doc.add_blank_page(page_size=(out_w, out_h)))
        names = {}   # one resource name per page, even when it fills several slots
        for slot_i in range(n_slot):
            page_i = sheet_i * n_slot + slot_i
            if page_i >= len(src_pages): break
            src_pi = src_pages[page_i]
            if src_pi is None or src_pi >= len(src_doc.pages): continue
            fx, box, rot = _form_for(src_pi)
            s, tx, ty = _slot_placement(box, rot, rects[slot_i],
                                        fixed_scale=fixed_scale)
            if src_pi not in names:
                names[src_pi] = sheet.add_resource(fx, Name.XObject, prefix="NUp")
            name = names[src_pi]
            sheet.contents_add(Stream(out_doc,
                f"q {s:.6f} 0 0 {s:.6f} {tx:.6f} {ty:.6f} cm {name} Do Q\n".encode("latin-1")))
            placed += 1
        if mark_ops is not None:
            sheet.contents_add(Stream(out_doc, mark_ops))
        sheet.contents_coalesce()
    report(tr("Schreibe Datei …"))
    out_doc.save(out)
    return out, tr('Fertig. {p0} Seiten auf {p1} Blatt ({p2}×{p3}).').format(p0=placed, p1=n_sheets, p2=cols, p3=rows)


class NUpPanel(BasePanel):
    TITLE         = "N-Up Layout"
    SUBTITLE      = "Mehrere Seiten auf einem Blatt — mit Rand- und Abstandssteuerung."
    RUN_LABEL     = "N-Up erstellen"

    def __init__(self, parent=None):
        # Shared-render state: the preview pulls page images from the same
        # _ThumbnailCache / _render_queue the "Seiten verwalten" view uses, so a
        # page rendered for one is reused by the other and the render happens off
        # the GUI thread (opening the tool no longer blocks on a synchronous
        # render of a big page).
        self._dims_cache    = {}     # (path, page_idx) -> (clamped_idx, pw, ph, n)
        self._thumb_pending = set()  # {(path, page_idx, rot, render_w)} in flight
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        super().__init__(parent)

    def _on_thumb_ready(self, *_):
        # A requested page image arrived; clear the in-flight set so any pages
        # still missing get re-requested, then redraw.
        self._thumb_pending.clear()
        self._pane.refresh()

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._tool_splitter = splitter

        splitter.addWidget(self.build_tool_sidebar())

        right_w = self.build_tool_right_panel()
        right_l = right_w.layout()

        # Shared preview pane (zoom, Ctrl+wheel, page/pdf/show/resize refresh).
        self._pane = PreviewPane(self._render_preview, header="Vorschau (erstes Blatt)")
        self._preview      = self._pane.label
        self._preview_info = self._pane.info
        right_l.addWidget(self._pane)

        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 700])
        outer.addWidget(splitter, 1)

        # Register so the sidebar/preview follow light/dark theme switches
        # (built-once theme_color() strings would otherwise stay dark).
        _register_themed(self)
        self._apply_theme()

    def _apply_theme(self):
        t = _TV
        self.apply_split_view_theme()
        self._preview.setStyleSheet(f"background:{t['card_bg']};")
        self._tool_splitter.setStyleSheet(
            f"QSplitter::handle{{background:{t['splitter']};width:2px;}}")
        # The preview is a pixmap painted over that label, and it paints its own
        # backdrop from the palette. Restyling the label alone left the previous
        # theme's canvas sitting on top of the new one until something else
        # happened to trigger a render. refresh() no-ops while hidden.
        self._pane.refresh()

    def _update_preview(self):
        # Bridge for this panel's widget-value connections.
        self._pane.refresh()

    def build_ui(self, layout):
        def mm_spin(val=0):
            s = QDoubleSpinBox()
            s.setRange(0, 500); s.setSuffix(" mm")
            s.setDecimals(1); s.setValue(val); s.setFixedWidth(90)
            s.valueChanged.connect(self._update_preview)
            return s

        # N-Up's labels are short, so use a narrow label column — otherwise the
        # global 220px label width squeezes the fields and the sidebar looks
        # cramped.
        def r(label, widget):
            return row(label, widget, label_w=92)

        sb = QGroupBox(tr("QUELLSEITEN")); sl = QVBoxLayout(sb)
        self.src_combo = QComboBox()
        self.src_combo.addItems([tr("Alle Seiten der Reihe nach"),
                                  tr("Jede Seite wiederholt (ein Blatt je Seite)")])
        self.src_combo.currentIndexChanged.connect(self._update_preview)
        sl.addLayout(r(tr("Quelle:"), self.src_combo))
        layout.addWidget(sb)

        gb = QGroupBox(tr("RASTER")); gl = QVBoxLayout(gb)
        self.cols = QSpinBox(); self.cols.setRange(1, 10); self.cols.setValue(2); self.cols.valueChanged.connect(self._update_preview)
        self.rows = QSpinBox(); self.rows.setRange(1, 10); self.rows.setValue(2); self.rows.valueChanged.connect(self._update_preview)
        gl.addLayout(r(tr("Spalten:"), self.cols))
        gl.addLayout(r(tr("Zeilen:"), self.rows))
        # Empty positions are always left blank — there is nothing else a slot
        # with no page could be — so the tick box that used to say so is gone.
        # This one changes something: normally a page is scaled to fill its
        # slot, and with this set it keeps its own size and is only centred.
        self.full_scale = QCheckBox(tr("Quellseiten in Originalgröße (100 %)"))
        self.full_scale.setToolTip(tr(
            "Seiten werden nicht verkleinert. Das Ausgabeformat muss groß "
            "genug sein, sonst meldet das Werkzeug, wie groß es sein müsste."))
        self.full_scale.toggled.connect(self._update_preview)
        gl.addWidget(self.full_scale)
        layout.addWidget(gb)

        ob = QGroupBox(tr("AUSGABEFORMAT")); ol = QVBoxLayout(ob)
        # Same widget as Crop/Scale, so both offer the same paper sizes and both
        # take a custom width x height. "Wie Quellseite × Raster" sits after the
        # sizes because it is not one.
        #
        # Bound here, not on the class: tr() at class scope runs at import, and
        # the language is only loaded once the QApplication exists.
        self.AUTO_FORMAT = tr("Wie Quellseite × Raster  (automatisch)")
        # Querformat comes with it — the selector disables it for the auto
        # entry, whose orientation follows the source page.
        self.out_fmt = PaperFormatSelector(after=[self.AUTO_FORMAT])
        ol.addLayout(r(tr("Format:"), self.out_fmt))
        self.out_fmt.changed.connect(self._update_preview)
        layout.addWidget(ob)

        ab = QGroupBox(tr("ABSTÄNDE")); al = QVBoxLayout(ab)
        self.margin_t = mm_spin(5); self.margin_b = mm_spin(5)
        self.margin_l = mm_spin(5); self.margin_r = mm_spin(5)
        self.gap_h    = mm_spin(3); self.gap_v    = mm_spin(3)
        # Compact 2-column grid (short labels) instead of six full-width rows —
        # keeps the whole sidebar visible without scrolling.
        grid = QGridLayout(); grid.setHorizontalSpacing(8); grid.setVerticalSpacing(4)
        def cell(rr, cc, text, w):
            lb = QLabel(text); lb.setObjectName("dimLabel")
            grid.addWidget(lb, rr, cc); grid.addWidget(w, rr, cc + 1)
        cell(0, 0, tr("Rand oben"),  self.margin_t); cell(0, 2, tr("unten"),  self.margin_b)
        cell(1, 0, tr("Rand links"), self.margin_l); cell(1, 2, tr("rechts"), self.margin_r)
        cell(2, 0, tr("Abstand H"),  self.gap_h);    cell(2, 2, tr("V"),      self.gap_v)
        grid.setColumnStretch(1, 1); grid.setColumnStretch(3, 1)
        al.addLayout(grid)
        self._sync_margins = QCheckBox(tr("Alle Ränder gleich"))
        self._sync_gaps    = QCheckBox(tr("Beide Abstände gleich"))
        def _sync_m(v):
            if self._sync_margins.isChecked():
                for w in [self.margin_b, self.margin_l, self.margin_r]:
                    w.blockSignals(True); w.setValue(self.margin_t.value()); w.blockSignals(False)
                self._update_preview()
        def _sync_g(v):
            if self._sync_gaps.isChecked():
                self.gap_v.blockSignals(True); self.gap_v.setValue(self.gap_h.value()); self.gap_v.blockSignals(False)
                self._update_preview()
        self.margin_t.valueChanged.connect(_sync_m)
        self.gap_h.valueChanged.connect(_sync_g)
        al.addWidget(self._sync_margins); al.addWidget(self._sync_gaps)
        self.crop_marks = QCheckBox(tr("Schnittmarken hinzufügen"))
        self.crop_marks.toggled.connect(self._update_preview)
        al.addWidget(self.crop_marks)
        layout.addWidget(ab)

    def _get_layout_params(self, src_pw, src_ph):
        PT   = MM_TO_PT
        cols = self.cols.value(); rows = self.rows.value()
        sheet = self.out_fmt.target_size_pt()      # None = "Wie Quellseite × Raster"
        mt = self.margin_t.value() * PT; mb = self.margin_b.value() * PT
        ml = self.margin_l.value() * PT; mr = self.margin_r.value() * PT
        gh = self.gap_h.value() * PT;    gv = self.gap_v.value() * PT
        if sheet is None:
            # "Wie Quellseite × Raster": size the sheet so that every slot is
            # exactly one source page and the margins/gaps are added *around*
            # them. They used to be carved out of a sheet of src×grid, so asking
            # for a 10 mm margin here shrank the page to ~95% and — because the
            # fit keeps the aspect ratio — actually produced 10 mm at the sides
            # and 14 mm top/bottom, the opposite of "add white space".
            out_w = src_pw * cols + ml + mr + gh * (cols - 1)
            out_h = src_ph * rows + mt + mb + gv * (rows - 1)
        else:
            out_w, out_h = sheet          # Querformat already applied
        # NOT clamped: a slot can come out zero or negative when the margins and
        # gaps exceed the sheet. Callers must check (the run refuses, the preview
        # says so) — clamping it to 1pt used to squeeze the whole page into a
        # hairline in the corner instead of reporting the problem.
        slot_w = (out_w - ml - mr - gh * (cols-1)) / cols
        slot_h = (out_h - mt - mb - gv * (rows-1)) / rows
        return out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows

    def _page_dims(self, pdf_path):
        """Return (clamped_page_idx, page_w_pt, page_h_pt, n_pages) for the
        representative page (the one picked in "Seiten verwalten"). Only reads
        page dimensions from pdfium (no rasterisation), so it's cheap even for
        huge files; cached per page so repeated previews don't reopen the doc."""
        idx = max(0, AppState.get().current_page)
        key = (pdf_path, idx)
        if key in self._dims_cache:
            return self._dims_cache[key]
        import pypdfium2 as pdfium
        with _pdfium_lock:
            doc = pdfium.PdfDocument(pdf_path)
            try:
                n = len(doc)
                i = min(idx, n - 1)
                pg = doc[i]
                res = (i, pg.get_width(), pg.get_height(), n)
            finally:
                doc.close()
        self._dims_cache[key] = res
        return res

    def _page_image(self, pdf_path, page_idx, render_w):
        """Fetch a rendered image of the page from the SHARED thumbnail cache
        (the same one "Seiten verwalten" fills). Returns immediately with any
        cached image — or None — and submits a render on the shared queue
        (off the GUI thread) so a missing/low-res page gets a crisp render and
        refreshes the preview when ready. Reuses renders across both tools."""
        key = (pdf_path, page_idx, 0, render_w)
        exact = _ThumbnailCache.get(key)
        if exact is not None:
            return exact
        if key not in self._thumb_pending:
            self._thumb_pending.add(key)
            _render_queue.submit(
                _ThumbTask(0, 0, pdf_path, page_idx, 0, render_w, self._thumb_signals), 1)
        # Show any other cached size for this page meanwhile (e.g. a manage-view
        # thumbnail), scaled — so the layout appears instantly instead of blank.
        return _ThumbnailCache.get_any(pdf_path, page_idx, 0)

    def _render_preview(self, avail_w, avail_h, zoom):
        # self.current_pdf() — not AppState.current_pdf — so the page manager's
        # rotations and reordering are reflected. Reading the file on disk meant
        # a rotated page was measured and previewed in its original orientation.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            return None, tr("Keine PDF geöffnet")
        page_idx, src_pw, src_ph, n_total = self._page_dims(pdf_path)
        out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows = \
            self._get_layout_params(src_pw, src_ph)
        if slot_w <= 1.0 or slot_h <= 1.0:
            return None, tr("Abstände zu groß — kein Platz für Inhalt.")
        full_scale = self.full_scale.isChecked()
        if full_scale:
            # Measured on the page the preview is showing, which is the page the
            # sheet was sized from. The run checks every page it will place.
            problem = _full_scale_problem(src_pw, src_ph,
                                          (out_w, out_h, mt, mb, ml, mr, gh, gv,
                                           slot_w, slot_h, cols, rows))
            if problem:
                return None, problem
        cs = min(avail_w / out_w, avail_h / out_h) * zoom
        canvas_w = max(1, int(out_w * cs)); canvas_h = max(1, int(out_h * cs))

        # Which page goes in each slot — this is what differs between the two
        # source modes: sequential packs consecutive pages, "repeat" puts the
        # same page in every slot.
        sequential = self.src_combo.currentIndex() == 0
        n_slot = cols * rows
        if sequential:
            slot_pages = [(page_idx + s if page_idx + s < n_total else None)
                          for s in range(n_slot)]
        else:
            slot_pages = [page_idx] * n_slot

        # Fetch each needed page image from the shared cache (async if missing).
        # Width is fixed to the slot's point size (independent of the display
        # scale), so dragging the sidebar / zooming reuses the cached render
        # instead of triggering a fresh one each time.
        render_w = max(120, min(800, int(min(slot_w, src_pw))))
        pm_by_page = {}; any_pending = False
        for pg in {p for p in slot_pages if p is not None}:
            img = self._page_image(pdf_path, pg, render_w)
            if img is None: any_pending = True
            pm_by_page[pg] = QPixmap.fromImage(img) if img is not None else None

        result = QPixmap(canvas_w, canvas_h); result.fill(QColor(_TV['card_bg']))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(PAPER))); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(0, 0, canvas_w - 1, canvas_h - 1)
        for row_i in range(rows):
            for col_i in range(cols):
                sx = int((ml + col_i * (slot_w + gh)) * cs)
                sy = int((mt + row_i * (slot_h + gv)) * cs)
                sw = max(1, int(slot_w * cs)); sh = max(1, int(slot_h * cs))
                painter.setBrush(QBrush(QColor(200, 210, 230, 60)))
                painter.setPen(QPen(QColor(120, 140, 180, 120), 1))
                painter.drawRect(sx, sy, sw, sh)
                pg = slot_pages[row_i * cols + col_i]
                src_pm = pm_by_page.get(pg) if pg is not None else None
                if src_pm is None:
                    continue
                scale   = 1.0 if full_scale else min(slot_w / src_pw, slot_h / src_ph)
                scaled_w = max(1, int(src_pw * scale * cs))
                scaled_h = max(1, int(src_ph * scale * cs))
                off_x = sx + (sw - scaled_w) // 2; off_y = sy + (sh - scaled_h) // 2
                pm_s = src_pm.scaled(scaled_w, scaled_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(off_x, off_y, pm_s)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(_TV['acc']), 1))
                painter.drawRect(off_x, off_y, pm_s.width()-1, pm_s.height()-1)
        if self.crop_marks.isChecked():
            params_t = (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows)
            painter.setPen(QPen(QColor(INK), 1)); painter.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments(_nup_slot_rects(params_t, cols * rows)):
                painter.drawLine(int(a * cs), int((out_h - b) * cs),
                                 int(c * cs), int((out_h - d) * cs))
        painter.setPen(QPen(QColor(120, 160, 255, 180), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, canvas_w-1, canvas_h-1)
        painter.end()
        info = f"{out_w/MM_TO_PT:.0f}×{out_h/MM_TO_PT:.0f} mm  |  {cols}×{rows} = {cols*rows} Slots"
        if any_pending:
            info += "  ·  " + tr("rendert …")
        return result, info

    def _run_action(self):
        # Prepare on the UI thread (cheap: reads widget values + first page size),
        # then hand the heavy merge loop to a worker via the shared run_async().
        import pikepdf
        src_path = self.require_pdf()
        out_path = self.save_pdf(tr("N-Up PDF speichern als"))
        if not out_path: raise ValueError(tr("Kein Ausgabepfad."))
        with pikepdf.open(src_path) as _doc:
            n_total = len(_doc.pages)
            cols = self.cols.value(); rows = self.rows.value(); n_slot = cols * rows
            if self.src_combo.currentIndex() == 1:
                # Each page repeated to fill its own sheet (n_slot copies per page).
                src_pages = [p for p in range(n_total) for _ in range(n_slot)]
            else:
                # All pages, packed sequentially across the slots.
                src_pages = list(range(n_total))
            # Size the sheet from the same page the preview shows, measured the
            # same way (visible box, /Rotate applied) — reading the raw MediaBox
            # of page 0 made "Wie Quellseite × Raster" disagree with the preview
            # on rotated or cropped pages, e.g. a portrait sheet for landscape
            # content.
            rep = max(0, min(AppState.get().current_page, n_total - 1))
            src_pw, src_ph = _visible_size(_doc.pages[rep])
            # At 100 % nothing is scaled down, so it is the *largest* page that
            # decides whether the job is possible — not the one the sheet was
            # sized from. Measuring them all is a box lookup per page, no
            # rasterising, so it is cheap even on a long document.
            full_scale = self.full_scale.isChecked()
            max_w = max_h = 0.0
            if full_scale:
                for pi in sorted({p for p in src_pages if p is not None}):
                    w, h = _visible_size(_doc.pages[pi])
                    max_w = max(max_w, w); max_h = max(max_h, h)
        params = self._get_layout_params(src_pw, src_ph)
        slot_w, slot_h = params[8], params[9]
        if slot_w <= 1.0 or slot_h <= 1.0:
            raise ValueError(tr("Abstände zu groß — kein Platz für Inhalt."))
        if full_scale:
            problem = _full_scale_problem(max_w, max_h, params)
            if problem:
                raise ValueError(problem)
        if len(src_pages) == 1: src_pages = src_pages * n_slot   # single-page doc → fill the sheet
        # An unused slot is blank paper; there was never anything else it could
        # be. Padding the list only makes that explicit for the last sheet.
        while len(src_pages) % n_slot: src_pages.append(None)

        crop = self.crop_marks.isChecked()
        self.run_async(
            lambda report: _build_nup(src_path, out_path, src_pages, params,
                                      n_slot, report, crop_marks=crop,
                                      fixed_scale=1.0 if full_scale else None),
            on_done=self._nup_done,
            busy_label=tr("N-Up läuft …"),
        )
        return None

    def _nup_done(self, result):
        out_path, summary = result
        self.log.log(summary)
        self.open_result(out_path, "N-Up")
