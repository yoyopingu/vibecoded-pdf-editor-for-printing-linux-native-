"""
N-Up Layout — several source pages to a sheet, with margins, gaps and crop
marks, over a live preview of the sheet.
"""
import os, math
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals
from tools.theme import INK, PAPER, _TV, _register_themed
from PyQt6.QtWidgets import QVBoxLayout, QLabel, QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox, QSplitter, QGridLayout
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush
from tools.app_state import AppState
from tools._base import BasePanel
from tools.i18n import tr
from tools.panels._shared import MM_TO_PT, PaperFormatSelector, _visible_size, row, PreviewPane, PreviewPageBar
from tools.panels._cropmarks import _crop_mark_segments, _crop_marks_content_stream
from tools.panels._imposition import (_slot_placement, _flatten_annots,
                                      form_factory, full_scale_problem)
from tools.render.document_cache import page_document
from tools.render.placeholder import paint_page_number, source_page_index
from tools.jobs import submit


def _read_page_dims(pdf_path, idx):
    """Read source geometry from the page tree, before parsing page content.

    The document cache shares an already opened PDF with the viewer. Loading
    the representative page here used to hold up the sheet layout on complex
    pages, even though N-Up only needs its size at this stage.
    """
    return _read_page_dims_many(pdf_path, (idx,))[idx]


def _read_page_dims_many(pdf_path, indices):
    # A visible sheet can contain unlike pages. Read its boxes in one owned
    # metadata job, without parsing page content or opening once per slot.
    with page_document(pdf_path) as doc:
        n = len(doc)
        return {idx: (min(idx, n - 1), *doc.get_page_size(min(idx, n - 1)), n)
                for idx in indices}


# ══════════════════════════════════════════════════════════════════════════════
# N-UP LAYOUT
# ══════════════════════════════════════════════════════════════════════════════
def _nup_sheet_count(n_pages, n_slot, mode):
    return n_pages if mode == 1 else math.ceil(n_pages / n_slot)


def _nup_sheet_pages(n_pages, n_slot, mode, sheet):
    """Share source ordering between preview and export.

    Cut-and-stack deals consecutive runs into separate piles, in reading order
    across the grid. Padding belongs at the end of those runs, not at the end
    of a sequential sheet list, or cutting an uneven job scrambles its order.
    The older modes retain their single-page repeat convenience.
    """
    if mode == 1 or (n_pages == 1 and mode == 0):
        return [sheet] * n_slot
    sheets = _nup_sheet_count(n_pages, n_slot, mode)
    pages = [(slot * sheets + sheet if mode == 2 else sheet * n_slot + slot)
             for slot in range(n_slot)]
    return [page if page < n_pages else None for page in pages]


def _nup_slot_rects(params, n_slot):
    """The (x0, y0, x1, y1) rectangle (PDF points, origin bottom-left) of every
    slot on a sheet. Pages fit inside these cells; crop marks follow the
    actual placed page rectangles instead of the cells' blank padding."""
    (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
    rects = []
    for slot_i in range(n_slot):
        col_i = slot_i % cols; row_i = slot_i // cols
        x0 = ml + col_i * (slot_w + gh)
        y0 = out_h - mt - (row_i + 1) * slot_h - row_i * gv
        rects.append((x0, y0, x0 + slot_w, y0 + slot_h))
    return rects


def _nup_page_rect(width, height, slot, fixed_scale=None):
    """Placed page edges, using the same fit/centering as the PDF transform."""
    scale, x, y = _slot_placement((0.0, 0.0, width, height), 0, slot,
                                fixed_scale=fixed_scale)
    return x, y, x + width * scale, y + height * scale


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
               fixed_scale=None, border=False):
    """Build the N-Up PDF on a worker thread (via BasePanel.run_async).

    Each source page becomes a Form XObject that is scaled to fit its slot,
    keeping its aspect ratio, and centred there — or, with `fixed_scale=1.0`,
    placed at its own size and centred without being resized at all. `border`
    strokes a hairline around each occupied slot (the print dialog's
    Seitenrahmen). Faster than pypdf's ``merge_transformed_page`` on
    vector-heavy pages (which parses + decompresses every content stream —
    ~30s and a 10× larger output for a dense 4-page file vs ~0.5s here) and
    keeps the source content compressed. Only plain data crosses the thread
    boundary."""
    from pikepdf import Pdf, Page, Stream, Name
    (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
    src_doc = Pdf.open(src)
    _flatten_annots(src_doc)
    out_doc = Pdf.new()
    rects = _nup_slot_rects(params, n_slot)

    # One Form XObject per used source page, built once and reused across
    # slots and sheets — see form_factory's docstring for what it pins down.
    _form_for = form_factory(src_doc)

    # Marks depend on each occupied page, not the grid: fitting can leave
    # padding inside a cell, and mixed-size sheets have different trim edges.
    n_sheets = math.ceil(len(src_pages) / n_slot)
    placed   = 0
    for sheet_i in range(n_sheets):
        report(f"{tr('Blatt')} {sheet_i+1} / {n_sheets} …")
        sheet = Page(out_doc.add_blank_page(page_size=(out_w, out_h)))
        mark_rects = []
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
            if border:
                x0, y0, x1, y1 = rects[slot_i]
                sheet.contents_add(Stream(out_doc,
                    (f"q 0 0 0 RG 0.4 w {x0:.2f} {y0:.2f} "
                     f"{x1 - x0:.2f} {y1 - y0:.2f} re S Q\n").encode("latin-1")))
            if crop_marks:
                pw, ph = _visible_size(src_doc.pages[src_pi])
                mark_rects.append(_nup_page_rect(pw, ph, rects[slot_i], fixed_scale))
            placed += 1
        if mark_rects:
            sheet.contents_add(Stream(out_doc, _crop_marks_content_stream(mark_rects)))
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
        self._dims_pending = None
        self._dims_job = None
        self._dims_error = None
        self._thumb_pending = set()  # {(path, page_idx, rot, render_w)} in flight
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        self._sheet_index = 0
        self._sheet_total = 0
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

        # The sheet track navigates the actual output sequence. Keeping its
        # index separate from AppState.current_page lets an operator inspect
        # every imposed sheet without turning the source document.
        self._pane = PreviewPane(
            lambda w, h, z: self._render_preview(w, h, z, async_dims=True),
            page_step=self._step_sheet)
        self._preview      = self._pane.label
        self._preview_info = self._pane.info
        self._sheet_bar = PreviewPageBar('Blatt')
        self._sheet_bar.valueChanged.connect(self._show_sheet)
        self._sheet_status = QLabel()
        self._sheet_status.setObjectName("dimLabel")
        self._pane.add_navigation(self._sheet_bar, self._sheet_status)
        self._set_sheet_range(0)
        right_l.addWidget(self._pane)

        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 700])
        outer.addWidget(splitter, 1)

        # Register so the sidebar/preview follow light/dark theme switches
        # (built-once theme_color() strings would otherwise stay dark).
        _register_themed(self)
        self._apply_theme()
        AppState.get().pdf_changed.connect(self._document_changed)
        AppState.get().current_page_changed.connect(self._source_page_changed)

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

    def _document_changed(self, *_):
        self._dims_cache.clear()
        self._sheet_index = 0
        self._set_sheet_range(0)
        # PreviewPane receives pdf_changed first; redraw after our state reset
        # so switching files cannot leave the old sheet selected.
        self._pane.refresh()

    def _source_page_changed(self, page):
        slots = max(1, self.cols.value() * self.rows.value())
        mode = self.src_combo.currentIndex()
        if mode == 2:
            model = AppState.get().page_model
            sheets = (_nup_sheet_count(len(model.order), slots, mode)
                      if model is not None else self._sheet_total)
            self._sheet_index = page % max(1, sheets)
        else:
            self._sheet_index = page // slots if mode == 0 else page
        self._pane.refresh()

    def _set_sheet_range(self, total):
        self._sheet_total = total
        self._sheet_index = min(self._sheet_index, max(0, total - 1))
        bar = self._sheet_bar
        was_blocked = bar.blockSignals(True)
        bar.setRange(0, max(0, total - 1))
        bar.setPageStep(1)
        bar.setValue(self._sheet_index)
        bar.blockSignals(was_blocked)
        bar.setEnabled(total > 1)
        self._pane.set_page_total(total)
        self._sheet_status.setText(
            tr('Blatt {p0} / {p1}').format(
                p0=self._sheet_index + 1 if total else 0, p1=total))

    def _show_sheet(self, sheet):
        self._sheet_index = sheet
        self._set_sheet_range(self._sheet_total)
        self._pane.refresh()

    def _step_sheet(self, direction):
        if self._sheet_total:
            self._sheet_bar.setValue(max(0, min(self._sheet_total - 1,
                                              self._sheet_index + direction)))

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
                                  tr("Jede Seite wiederholt (ein Blatt je Seite)"),
                                  tr("Schneiden und Stapeln (einseitig)")])
        self.src_combo.setToolTip(tr(
            "Schneiden und Stapeln: Einseitig drucken, Blattreihenfolge beibehalten, "
            "schneiden und die Stapel zeilenweise von links nach rechts "
            "aufeinanderlegen (erster Stapel oben)."))
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
        representative page (the one picked in "Seiten verwalten"). The pane
        requests a cold read in a job, because even reading dimensions can wait
        behind a complex page's render on the process-wide pdfium lock. Direct
        callers reuse the same cache after that first read."""
        idx = max(0, AppState.get().current_page)
        key = (pdf_path, idx)
        if key in self._dims_cache:
            return self._dims_cache[key]
        res = _read_page_dims(pdf_path, idx)
        self._dims_cache[key] = res
        return res

    def _request_page_dims(self, pdf_path, idx):
        indices = (idx,) if isinstance(idx, int) else tuple(sorted(idx))
        key = (pdf_path, indices)
        if self._dims_pending == key:
            return
        if self._dims_job is not None:
            self._dims_job.cancel()
        self._dims_pending = key
        self._dims_error = None

        def ready(result):
            if self._dims_pending != key:
                return
            self._dims_pending = None
            self._dims_cache.update({(pdf_path, page): dims
                                     for page, dims in result.items()})
            self._pane.refresh()

        def failed(error):
            if self._dims_pending != key:
                return
            self._dims_pending = None
            self._dims_error = (key, str(error))
            self._pane.refresh()

        # A quick geometry read can finish before submit returns. Install the
        # callbacks in submit so its result cannot disappear in that gap.
        self._dims_job = submit(
            lambda _job: ({idx: _read_page_dims(pdf_path, idx)} if isinstance(idx, int)
                          else _read_page_dims_many(pdf_path, indices)),
            owner=self, name='nup-preview-dimensions',
            on_done=ready, on_error=failed)

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

    def _render_preview(self, avail_w, avail_h, zoom, async_dims=False):
        # See BasePanel.current_pdf()'s docstring for why this, not
        # AppState.get().current_pdf, is what a preview measures against.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            self._set_sheet_range(0)
            return None, tr("Keine PDF geöffnet")
        idx = max(0, AppState.get().current_page)
        key = (pdf_path, idx)
        if async_dims and key not in self._dims_cache:
            if self._dims_error is not None and self._dims_error[0] == (pdf_path, (idx,)):
                return None, tr('Vorschau: {p0}').format(p0=self._dims_error[1])
            self._request_page_dims(pdf_path, idx)
            pm = QPixmap(max(1, avail_w), max(1, avail_h))
            pm.fill(QColor(PAPER))
            painter = QPainter(pm)
            source = source_page_index(AppState.get().page_model, idx,
                                       pdf_path)
            paint_page_number(painter, pm.rect(), source)
            painter.end()
            return pm, tr('rendert …')
        _page_idx, src_pw, src_ph, n_total = self._page_dims(pdf_path)
        out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows = \
            self._get_layout_params(src_pw, src_ph)
        mode = self.src_combo.currentIndex()
        n_slot = cols * rows
        self._set_sheet_range(_nup_sheet_count(n_total, n_slot, mode))
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
        fit = min(avail_w / out_w, avail_h / out_h)
        cs = self._pane.page_scale(fit, zoom)
        canvas_w = max(1, int(out_w * cs)); canvas_h = max(1, int(out_h * cs))

        slot_pages = _nup_sheet_pages(n_total, n_slot, mode, self._sheet_index)

        occupied = {page for page in slot_pages if page is not None}
        missing = tuple(sorted(page for page in occupied
                               if (pdf_path, page) not in self._dims_cache))
        if missing:
            if async_dims:
                key = (pdf_path, missing)
                if self._dims_error is not None and self._dims_error[0] == key:
                    return None, tr('Vorschau: {p0}').format(p0=self._dims_error[1])
                self._request_page_dims(pdf_path, missing)
            else:
                self._dims_cache.update({(pdf_path, page): dims for page, dims
                                         in _read_page_dims_many(pdf_path, missing).items()})
        params_t = (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows)
        slots = _nup_slot_rects(params_t, n_slot)
        mark_rects = []

        # Fetch each needed page image from the shared cache (async if missing).
        # Width is fixed to the slot's point size (independent of the display
        # scale), so dragging the sidebar / zooming reuses the cached render
        # instead of triggering a fresh one each time.
        render_w = max(120, min(800, int(min(slot_w, src_pw))))
        pm_by_page = {}; any_pending = False
        for pg in occupied:
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
                if pg is None:
                    continue
                dims = self._dims_cache.get((pdf_path, pg))
                # A cold page gets a numbered cell until its own dimensions
                # arrive; never invent trim marks from another page's size.
                if dims is None:
                    any_pending = True
                    page_rect = (slots[row_i * cols + col_i])
                else:
                    _, pw, ph, _ = dims
                    if full_scale:
                        problem = _full_scale_problem(pw, ph, params_t)
                        if problem:
                            painter.end()
                            return None, problem
                    page_rect = _nup_page_rect(pw, ph, slots[row_i * cols + col_i],
                                              1.0 if full_scale else None)
                    mark_rects.append(page_rect)
                x0, y0, x1, y1 = page_rect
                target = QRect(round(x0 * cs), round((out_h - y1) * cs),
                               max(1, round((x1 - x0) * cs)),
                               max(1, round((y1 - y0) * cs)))
                src_pm = pm_by_page.get(pg)
                if src_pm is None or dims is None:
                    source = source_page_index(AppState.get().page_model, pg, pdf_path)
                    paint_page_number(painter, target, source)
                else:
                    painter.drawPixmap(target, src_pm, src_pm.rect())
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(_TV['acc']), 1))
                painter.drawRect(target.adjusted(0, 0, -1, -1))
        if self.crop_marks.isChecked():
            painter.setPen(QPen(QColor(INK), 1)); painter.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments(mark_rects):
                painter.drawLine(int(a * cs), int((out_h - b) * cs),
                                 int(c * cs), int((out_h - d) * cs))
        painter.setPen(QPen(QColor(120, 160, 255, 180), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, canvas_w-1, canvas_h-1)
        painter.end()
        info = (f"{out_w/MM_TO_PT:.0f}×{out_h/MM_TO_PT:.0f} mm  |  "
                + tr('{p0}×{p1} = {p2} Plätze').format(
                    p0=cols, p1=rows, p2=cols * rows))
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
            mode = self.src_combo.currentIndex()
            src_pages = [page
                         for sheet in range(_nup_sheet_count(n_total, n_slot, mode))
                         for page in _nup_sheet_pages(n_total, n_slot, mode, sheet)]
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
