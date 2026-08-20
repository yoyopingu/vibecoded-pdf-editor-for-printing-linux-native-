"""
Zuschneiden / Skalieren — trim or extend the page, fit it to a paper size,
and set cut marks. Shows the sheet it is about to produce.
"""
import os, gc
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.images import pil_to_qpixmap
from tools.theme import INK, PAPER, _TV, _register_themed
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox, QSplitter
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush
from tools.app_state import AppState
from tools._base import BasePanel
from tools.i18n import tr
from tools.panels._shared import MM_TO_PT, PaperFormatSelector, _inherited_rotate, _visible_box, _visible_size, _mat_mul, _display_matrix, PreviewPane
from tools.panels._cropmarks import _crop_mark_segments, _crop_marks_content_stream


# ══════════════════════════════════════════════════════════════════════════════
# CROP / RESIZE
# ══════════════════════════════════════════════════════════════════════════════
class CropResizePanel(BasePanel):
    TITLE         = "Zuschneiden / Skalieren"
    SUBTITLE      = ""
    RUN_LABEL     = "Ausfuehren"

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        splitter.addWidget(self.build_tool_sidebar())

        right_w = self.build_tool_right_panel()
        right_l = right_w.layout()

        # Shared preview pane — owns zoom controls, Ctrl+wheel, and the refresh
        # wiring (page change / new PDF / on show / on resize). This panel only
        # supplies the render callback below.
        self._pane = PreviewPane(self._render_preview, header="Vorschau")
        self._preview      = self._pane.label   # kept for _apply_theme()
        self._preview_info = self._pane.info
        right_l.addWidget(self._pane)

        self._tool_splitter = splitter
        _register_themed(self)
        self._apply_theme()
        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 800])
        outer.addWidget(splitter, 1)

    def _update_preview(self):
        # Bridge for this panel's widget-value connections; the pane owns the
        # actual render flow and calls back into _render_preview().
        self._pane.refresh()

    def build_ui(self, layout):
        self._syncing = False

        def mm():
            s = QDoubleSpinBox()
            s.setRange(-500, 500); s.setSuffix(" mm"); s.setDecimals(2)
            s.setFixedWidth(100)
            s.valueChanged.connect(self._update_preview)
            s.valueChanged.connect(self._sync_values)
            return s

        self.ct  = mm(); self.cb2 = mm()
        self.cl2 = mm(); self.cr  = mm()

        # ── DIN-Format Schnellauswahl ────────────────────────────────
        fmt_grp = QGroupBox(tr("Format"))
        fg = QVBoxLayout(fmt_grp); fg.setSpacing(4); fg.setContentsMargins(6,8,6,6)
        # Paper size, with the custom width x height row it brings with it.
        # Shared with N-Up so the two offer the same formats — see
        # PaperFormatSelector in tools/panels/_shared.py.
        self.fmt = PaperFormatSelector(before=[tr("— Kein —")])
        self.fmt.changed.connect(self._apply_format)
        fg.addWidget(self.fmt)

        # What the chosen format does: resize the page, or only add cut marks.
        self.fmt_action = QComboBox()
        self.fmt_action.addItems([tr("Auf Format zuschneiden / skalieren"),
                                  tr("Nur Schnittmarken setzen")])
        self.fmt_action.currentIndexChanged.connect(self._apply_format)
        fg.addWidget(self.fmt_action)
        layout.addWidget(fmt_grp)

        # ── Randfelder ───────────────────────────────────────────────
        crop_grp = QGroupBox(tr("Raender  [+ schneiden / - erweitern]"))
        cg = QVBoxLayout(crop_grp); cg.setSpacing(4); cg.setContentsMargins(6,8,6,6)
        _rows = [(tr("Oben"), self.ct), (tr("Unten"), self.cb2),
                 (tr("Links"), self.cl2), (tr("Rechts"), self.cr)]
        # Width from the widest label, not a fixed 38px: that fit the German
        # "Unten" but clipped the English "Bottom" to "Bottor".
        _fm = self.fontMetrics()
        _lw = max(_fm.horizontalAdvance(t) for t, _ in _rows) + 8
        for lbl_txt, w in _rows:
            hl = QHBoxLayout(); hl.setSpacing(4)
            lb = QLabel(lbl_txt); lb.setFixedWidth(_lw)
            hl.addWidget(lb); hl.addWidget(w); hl.addStretch()
            cg.addLayout(hl)

        self.sync_cb = QCheckBox(tr("Alle gleich"))
        self.sync_cb.toggled.connect(self._on_sync_toggle)
        cg.addWidget(self.sync_cb)
        reset_btn = QPushButton(tr("Auf Null setzen"))
        reset_btn.setObjectName("secondaryBtn")
        reset_btn.clicked.connect(self._reset_values)
        cg.addWidget(reset_btn)
        layout.addWidget(crop_grp)

        # ── Skalieroptionen ──────────────────────────────────────────
        # There was no way to say "make this 80%". The tool could only scale as
        # a side effect of changing the page size — pick a Format, or type
        # millimetres into the four margin boxes — so the one control with
        # "skalieren" in its name did nothing at all on its own: the page came
        # out the size it went in, the preview did not move, and the run wrote
        # a copy of the original.
        scale_grp = QGroupBox(tr("Skalieren"))
        sg = QVBoxLayout(scale_grp); sg.setSpacing(4); sg.setContentsMargins(6, 8, 6, 6)

        pct_row = QHBoxLayout(); pct_row.setSpacing(4)
        pct_row.addWidget(QLabel(tr("Skalierung")))
        self.scale_pct = QDoubleSpinBox()
        self.scale_pct.setRange(5.0, 400.0); self.scale_pct.setDecimals(1)
        self.scale_pct.setSuffix(" %"); self.scale_pct.setValue(100.0)
        self.scale_pct.setSingleStep(5.0); self.scale_pct.setFixedWidth(100)
        self.scale_pct.valueChanged.connect(self._update_preview)
        pct_row.addWidget(self.scale_pct); pct_row.addStretch()
        sg.addLayout(pct_row)
        self._pct_hint = QLabel(tr("Seite und Inhalt zusammen. 100 % laesst die Groesse, wie sie ist."))
        self._pct_hint.setObjectName("dimLabel"); self._pct_hint.setWordWrap(True)
        sg.addWidget(self._pct_hint)

        # Renamed from "Inhalt skalieren", which is what the percentage above
        # does. This one decides what happens to the content when the page size
        # changes for another reason: fit it into the new size, or leave it
        # where it is and let the new edges cut.
        self.scale_check = QCheckBox(tr("Inhalt in das neue Format einpassen"))
        self.scale_check.setChecked(False)
        self.scale_check.toggled.connect(self._update_preview)
        sg.addWidget(self.scale_check)

        self.keep_ratio = QCheckBox(tr("Proportionen beibehalten"))
        self.keep_ratio.setChecked(True)
        self.keep_ratio.toggled.connect(self._update_preview)
        sg.addWidget(self.keep_ratio)
        layout.addWidget(scale_grp)

        self.apply_all = QCheckBox(tr("Alle PDF-Seiten"))
        self.apply_all.setChecked(False)
        self.apply_all.toggled.connect(self._update_preview)
        layout.addWidget(self.apply_all)
        self._sel_info = QLabel("")
        self._sel_info.setObjectName("dimLabel")
        self._sel_info.setWordWrap(True)
        layout.addWidget(self._sel_info)

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

    def _on_sync_toggle(self, checked):
        if checked:
            val = max(self.ct.value(), self.cb2.value(),
                      self.cl2.value(), self.cr.value())
            self._syncing = True
            for w in [self.ct, self.cb2, self.cl2, self.cr]: w.setValue(val)
            self._syncing = False
            self._update_preview()

    def _reset_values(self):
        self._syncing = True
        for w in [self.ct, self.cb2, self.cl2, self.cr]: w.setValue(0.0)
        self._syncing = False
        # Formatauswahl zurücksetzen ohne _apply_format auszulösen
        self.fmt.reset()
        self._update_preview()

    def _sync_values(self, val):
        if self._syncing or not self.sync_cb.isChecked(): return
        self._syncing = True
        for w in [self.ct, self.cb2, self.cl2, self.cr]: w.setValue(val)
        self._syncing = False

    def _marks_only(self):
        """True when the Format should only add cut marks, not resize the page."""
        return self.fmt_action.currentIndex() == 1

    def _target_size_pt(self):
        """Chosen target size (w_pt, h_pt), or None for '— Kein —'."""
        return self.fmt.target_size_pt()

    def _zero_margins(self):
        self._syncing = True
        for w in [self.ct, self.cb2, self.cl2, self.cr]:
            w.blockSignals(True); w.setValue(0.0); w.blockSignals(False)
        self._syncing = False

    def _apply_format(self):
        """React to Format / custom-size / action changes."""
        # Nothing to scale when the run is only stamping cut marks, so the
        # control says so rather than being quietly ignored.
        self.scale_pct.setEnabled(not self._marks_only())
        self._pct_hint.setEnabled(not self._marks_only())
        size = self._target_size_pt()
        if size is None or self._marks_only():
            # 'Kein', or marks-only: leave the page uncropped. In marks-only mode
            # the chosen size is used purely to draw cut marks (see preview/output).
            self._zero_margins()
            self._update_preview()
        else:
            self._set_margins_for_size(*size)

    def _set_margins_for_size(self, tw, th):
        """Set the four crop margins so the page is cropped/extended to (tw, th)."""
        # See BasePanel.current_pdf()'s docstring for why this, not
        # AppState.get().current_pdf, is what a preview measures against.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            return
        try:
            import pypdfium2 as pdfium
            pages    = self._get_target_pages()
            page_idx = pages[0][0] if pages else 0
            # Runs on the GUI thread while the render worker may be inside
            # pdfium: constructing/closing the document needs the process-wide
            # lock, exactly like the page access in _render_base_page below.
            with _pdfium_lock:
                doc  = pdfium.PdfDocument(pdf_path)
                try:
                    page = doc[page_idx]
                    pw   = page.get_width(); ph = page.get_height()
                finally:
                    doc.close()
            # Exakte Differenz — kein Runden damit Zielformat stimmt
            diff_w_mm = (pw - tw) / MM_TO_PT / 2
            diff_h_mm = (ph - th) / MM_TO_PT / 2
            for w in [self.ct, self.cb2, self.cl2, self.cr]:
                w.blockSignals(True)
            self.cl2.setValue(diff_w_mm); self.cr.setValue(diff_w_mm)
            self.ct.setValue(diff_h_mm);  self.cb2.setValue(diff_h_mm)
            for w in [self.ct, self.cb2, self.cl2, self.cr]:
                w.blockSignals(False)
            # Remember that these margins came from the format, so the run can
            # re-derive them for every page instead of applying this one page's
            # millimetres to pages of a different size (see _format_margins).
            self._fmt_margins = self._margins_mm()
            self._update_preview()
        except Exception as ex:
            self.log.log(str(ex), error=True)

    def _scale_factor(self):
        """The Skalierung box as a factor, or 1.0 when it does not apply.

        Marks-only mode is "do not resize the page", so the percentage has
        nothing to act on there and is switched off with the rest of it.
        """
        if self._marks_only():
            return 1.0
        return self.scale_pct.value() / 100.0

    def _scales_content(self):
        """Will the content be scaled into the new page size?

        A percentage other than 100 says so by itself: a page at 80% whose
        content stayed its old size is not what anyone means by 80%, it is a
        crop. Shared by the preview and the run so the two cannot disagree.
        """
        return self.scale_check.isChecked() or self._scale_factor() != 1.0

    def _margins_mm(self):
        return (self.ct.value(), self.cb2.value(), self.cl2.value(), self.cr.value())

    def _format_margins(self):
        """(tw, th) when the four margin boxes still hold exactly what the Format
        dropdown put there — meaning "make every page this size", not "take these
        millimetres off every page". The run then re-derives the margins per page,
        so pages that differ from the previewed one still come out at the target
        size and centred instead of inheriting the first page's millimetres."""
        if self._marks_only(): return None
        size = self._target_size_pt()
        if size is None: return None
        if getattr(self, "_fmt_margins", None) != self._margins_mm(): return None
        return size

    def _effective_margins_pt(self, pw, ph):
        """(top, bottom, left, right) in points for a page of size (pw, ph) —
        derived from the chosen Format when one is active, else the four spin
        boxes, and then taken down by the Skalierung percentage. Shared by the
        preview and the run so they can never disagree."""
        size = self._format_margins()
        if size is not None:
            dw = (pw - size[0]) / 2; dh = (ph - size[1]) / 2
            t = b = dh; l = r = dw
        else:
            t, b = self.ct.value()  * MM_TO_PT, self.cb2.value() * MM_TO_PT
            l, r = self.cl2.value() * MM_TO_PT, self.cr.value()  * MM_TO_PT
        factor = self._scale_factor()
        if factor != 1.0:
            # The percentage is the last word on the size: whatever the Format
            # and the margin boxes have made of this page, the result is that
            # page at `factor`. Taken off all four edges equally, so it stays
            # centred on what it came from.
            dw = (pw - l - r) * (1.0 - factor) / 2
            dh = (ph - t - b) * (1.0 - factor) / 2
            t += dh; b += dh; l += dw; r += dw
        return t, b, l, r

    def _get_target_pages(self):
        state = AppState.get(); model = state.page_model
        if model and model.selected:
            return [(model.orig(uid), uid) for uid in model.order if uid in model.selected]
        if model and model.order:
            cur = max(0, min(state.current_page, len(model.order)-1))
            uid = model.order[cur]
            return [(model.orig(uid), uid)]
        return []

    def _base_page(self, pdf_path, page_idx):
        """Render the page once at a fixed base resolution and cache it; returns
        (base_pil, page_w_pt, page_h_pt). pdfium runs only when the page or
        document actually changes — never on resize/zoom/margin edits."""
        key = (pdf_path, page_idx)
        cache = getattr(self, "_base_cache", None)
        if cache is not None and cache[0] == key:
            return cache[1], cache[2], cache[3]
        import pypdfium2 as pdfium
        gc.disable()
        try:
            with _pdfium_lock:
                doc = pdfium.PdfDocument(pdf_path)
                try:
                    page = doc[page_idx]
                    pw = page.get_width(); ph = page.get_height()
                    base_scale = 1400.0 / max(pw, ph, 1.0)   # fixed preview resolution
                    base_pil = page.render(scale=base_scale).to_pil()
                finally:
                    doc.close()
        finally:
            gc.collect(); gc.enable()
        self._base_cache = (key, base_pil, pw, ph)
        return base_pil, pw, ph

    def _render_preview(self, avail_w, avail_h, zoom):
        # See BasePanel.current_pdf()'s docstring for why this, not
        # AppState.get().current_pdf, is what a preview measures against.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            self._sel_info.setText("")
            return None, tr("Keine PDF geoeffnet")
        from PIL import Image as PILImage

        pages    = self._get_target_pages()
        page_idx = pages[0][0] if pages else 0
        n_pages  = len(pages)
        if self.apply_all.isChecked(): self._sel_info.setText(tr("Alle Seiten"))
        elif n_pages > 1:             self._sel_info.setText(f"{n_pages} {tr('Seiten')}")
        else:                         self._sel_info.setText(f"{tr('Seite')} {page_idx+1}")

        if self._marks_only() and self._target_size_pt() is None:
            # "Nur Schnittmarken setzen" needs a size to centre the marks on —
            # picking the mode is not enough on its own. Silently falling
            # through to "— Kein —" (no format, zero margins) used to run the
            # ordinary crop branch instead, at zero margins, and report
            # success on a file that was really just copied unchanged: no
            # marks, no error, nothing to say something was still needed.
            return None, tr("Bitte zuerst ein Format waehlen.")

        # Render the page ONCE at a fixed base resolution (cached per page). Every
        # resize / zoom / margin / format change then only re-scales this cached
        # image (cheap) instead of re-rendering via pdfium — that re-render is what
        # made dragging the sidebar and editing the custom size hang on big PDFs.
        base_pil, pw, ph = self._base_page(pdf_path, page_idx)

        t_pt, b_pt, l_pt, r_pt = self._effective_margins_pt(pw, ph)

        new_w = pw - l_pt - r_pt
        new_h = ph - t_pt - b_pt
        if new_w < 1. or new_h < 1.:
            # Same refusal the run makes — better than drawing a 1pt sliver.
            return None, tr("Ränder zu groß — von der Seite bleibt nichts übrig.")
        do_scale = self._scales_content()

        # Fit everything that gets drawn, not only the page that comes out.
        # Alongside the new page this draws a ghost of the one it came from,
        # and that ghost is larger than the result whenever anything is being
        # cropped away — so scaling to the result alone produced a pixmap
        # bigger than the pane, of which the user saw the middle. (It was worse
        # than that: an oversized pixmap used to drag the label, and therefore
        # the pane, out with it, and the resize brought the render straight
        # back round with more room to overflow. See PreviewPane.)
        ext_w = max(1.0, max(new_w, pw - l_pt) - min(0.0, -l_pt))
        ext_h = max(1.0, max(new_h, ph - t_pt) - min(0.0, -t_pt))
        cs = min(avail_w / ext_w, avail_h / ext_h) * zoom
        cw_px = max(1, int(new_w * cs))
        ch_px = max(1, int(new_h * cs))

        target_w = max(1, int(pw * cs)); target_h = max(1, int(ph * cs))
        pil = (base_pil if base_pil.size == (target_w, target_h)
               else base_pil.resize((target_w, target_h), PILImage.LANCZOS))

        gw = max(1, int(pw * cs));  gh = max(1, int(ph * cs))
        g_page_x = int(-l_pt * cs); g_page_y = int(-t_pt * cs)

        min_x = min(0, g_page_x); min_y = min(0, g_page_y)
        max_x = max(cw_px, g_page_x + gw); max_y = max(ch_px, g_page_y + gh)
        shift_x = -min_x; shift_y = -min_y
        canvas_w = max(1, max_x - min_x); canvas_h = max(1, max_y - min_y)

        rx = shift_x;          ry = shift_y
        gx = g_page_x + shift_x; gy = g_page_y + shift_y

        result = QPixmap(canvas_w, canvas_h)
        result.fill(QColor(_TV['card_bg']))
        painter = QPainter(result)

        def ghost_fill():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(100, 140, 220, 45)))
            painter.drawRect(gx, gy, gw - 1, gh - 1)

        def ghost_outline():
            ghost_pen = QPen(QColor(120, 160, 255, 200), 1)
            ghost_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(ghost_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(gx, gy, gw - 1, gh - 1)

        changed = any([t_pt, b_pt, l_pt, r_pt])

        # The ghost of the page this came from goes underneath, so that only the
        # part of it that is being cut away is tinted. Painted over the top — as
        # it was — it laid a blue film across the result as well, and the whole
        # point of the preview is to show what will come out.
        if changed:
            ghost_fill()

        # Then the resulting page as paper. With negative margins
        # ("- erweitern", i.e. adding white space) the added strips belong to the
        # new page but carried no content, so they stayed the dark canvas colour
        # — the white space you were adding was invisible in the preview.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(PAPER)))
        painter.drawRect(rx, ry, cw_px, ch_px)

        if do_scale:
            keep = self.keep_ratio.isChecked()
            pm  = pil_to_qpixmap(pil)
            ar  = Qt.AspectRatioMode.KeepAspectRatio if keep else Qt.AspectRatioMode.IgnoreAspectRatio
            pm_s = pm.scaled(cw_px, ch_px, ar, Qt.TransformationMode.SmoothTransformation)
            draw_x = rx + (cw_px - pm_s.width())  // 2
            draw_y = ry + (ch_px - pm_s.height()) // 2
            painter.drawPixmap(draw_x, draw_y, pm_s)
            # Outline the new PAGE, not the scaled content: with "Proportionen
            # beibehalten" the content is narrower than the page, and marking the
            # content made it look as if it filled the sheet.
            painter.setPen(QPen(QColor(_TV['acc']), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rx, ry, max(1, cw_px - 1), max(1, ch_px - 1))
            if changed: ghost_outline()
        else:
            src_x = max(0, int(l_pt * cs)); src_y = max(0, int(t_pt * cs))
            src_w = min(max(1, int(new_w * cs)), pil.width  - src_x)
            src_h = min(max(1, int(new_h * cs)), pil.height - src_y)
            dst_x = rx + max(0, int(-l_pt * cs))
            dst_y = ry + max(0, int(-t_pt * cs))
            pm  = pil_to_qpixmap(pil)
            painter.drawPixmap(dst_x, dst_y, src_w, src_h, pm, src_x, src_y, src_w, src_h)
            painter.setPen(QPen(QColor(_TV['acc']), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rx, ry, max(1, cw_px - 1), max(1, ch_px - 1))
            if changed: ghost_outline()

        # Cut-marks-only mode: leave the page as-is and draw crop marks at the
        # chosen size, centred on the page (same marks as the N-Up tool).
        marks_size = self._target_size_pt() if self._marks_only() else None
        if marks_size:
            tw, th = marks_size
            mx0 = (pw - tw) / 2; my0 = (ph - th) / 2
            rect = (mx0, my0, mx0 + tw, my0 + th)
            painter.setPen(QPen(QColor(INK), 1)); painter.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments([rect]):
                painter.drawLine(int(rx + a * cs), int(ry + (ph - b) * cs),
                                 int(rx + c * cs), int(ry + (ph - d) * cs))

        painter.end()

        parts = [f"{pw/MM_TO_PT:.0f}x{ph/MM_TO_PT:.0f}mm"]
        if marks_size:
            parts.append(f"✂ {marks_size[0]/MM_TO_PT:.0f}x{marks_size[1]/MM_TO_PT:.0f}mm")
        elif any([t_pt, b_pt, l_pt, r_pt]):
            parts.append(f"{new_w/MM_TO_PT:.0f}x{new_h/MM_TO_PT:.0f}mm")
        return result, " -> ".join(parts)

    def _run_action(self):
        import pikepdf
        src_path = self.require_pdf()
        if self._marks_only() and self._target_size_pt() is None:
            # Same guard as the preview, checked before the save dialog rather
            # than after: asking where to save an operation that is about to
            # refuse is a wasted round trip, and reporting "1 Seite(n)
            # bearbeitet." on a file that was really just copied unchanged —
            # no marks, no error — is what this looked like before the guard.
            raise ValueError(tr(
                "Bitte zuerst ein Format waehlen, um Schnittmarken zu setzen."))
        out = self.save_pdf("PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        if self.apply_all.isChecked():
            from pypdf import PdfReader
            target_origs = set(range(len(PdfReader(src_path, strict=False).pages)))
        else:
            target_pages = self._get_target_pages()
            if not target_pages: raise ValueError(tr("Keine Seiten ausgewaehlt."))
            target_origs = {orig for orig, _ in target_pages}

        # Cut-marks-only mode: don't resize the page — just stamp crop marks at
        # the chosen size, centred on each page (shares the N-Up marks helper).
        marks_size = self._target_size_pt() if self._marks_only() else None
        if marks_size:
            tw, th = marks_size
            pdf = pikepdf.open(src_path); n_changed = 0
            for i, page in enumerate(pdf.pages):
                if i not in target_origs: continue
                # Centre the marks on the *visible* page; on a rotated page the
                # requested size is meant as seen, so it swaps in page space.
                bx0, by0, bx1, by1 = _visible_box(page)
                mw, mh = ((th, tw) if _inherited_rotate(page) in (90, 270)
                          else (tw, th))
                x0 = bx0 + ((bx1 - bx0) - mw) / 2; y0 = by0 + ((by1 - by0) - mh) / 2
                ops = _crop_marks_content_stream([(x0, y0, x0 + mw, y0 + mh)])
                page.contents_add(pikepdf.Stream(pdf, ops))
                n_changed += 1
            pdf.save(out)
            self.open_result(out, os.path.basename(out))
            return tr('Schnittmarken auf {p0} Seite(n) gesetzt ({p1:.0f}×{p2:.0f} mm).').format(p0=n_changed, p1=tw / MM_TO_PT, p2=th / MM_TO_PT)

        do_scale = self._scales_content()
        pdf = pikepdf.open(src_path)
        n_changed = 0

        def _apply_ctm(pg, m):
            """Prepend `q a b c d e f cm` to the page's content stream."""
            if m == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0): return
            contents = pg.get("/Contents")
            if contents is None: return
            old = (b" ".join(bytes(s.read_bytes()) for s in contents)
                   if isinstance(contents, pikepdf.Array)
                   else bytes(contents.read_bytes()))
            hdr = ("q %.6f %.6f %.6f %.6f %.4f %.4f cm\n" % m).encode()
            pg["/Contents"] = pikepdf.Stream(pdf, hdr + old + (chr(10) + "Q").encode())

        for i, page in enumerate(pdf.pages):
            if i not in target_origs: continue
            # Measure the page the way the preview draws it: the visible box
            # (CropBox clipped to MediaBox) with /Rotate applied. Reading the raw
            # MediaBox instead meant the millimetres came off the wrong edges on
            # any page with a CropBox or a /Rotate, and a MediaBox that did not
            # start at (0,0) shifted the whole content.
            box = _visible_box(page)
            rot = _inherited_rotate(page)
            R   = _display_matrix(box, rot)
            pw, ph = _visible_size(page)

            # With a Format selected this derives the margins from *this* page,
            # so a document with mixed page sizes ends up all one size and
            # centred instead of inheriting the previewed page's millimetres.
            t_pt, b_pt, l_pt, r_pt = self._effective_margins_pt(pw, ph)

            # Neue Seitengröße
            new_w = pw - l_pt - r_pt
            new_h = ph - t_pt - b_pt
            if new_w < 1.0 or new_h < 1.0:
                raise ValueError(tr("Ränder zu groß — von der Seite bleibt nichts übrig."))

            if do_scale:
                if self.keep_ratio.isChecked():
                    # Proportionen beibehalten: kleinerer Faktor, zentriert
                    s = min(new_w/pw, new_h/ph)
                    C = (s, 0.0, 0.0, s, (new_w - pw*s)/2, (new_h - ph*s)/2)
                else:
                    # Strecken: Inhalt füllt neuen Rahmen exakt
                    C = (new_w/pw, 0.0, 0.0, new_h/ph, 0.0, 0.0)
            else:
                # Nur Rahmen verschieben, Inhalt bleibt
                C = (1.0, 0.0, 0.0, 1.0, -l_pt, -b_pt)
            _apply_ctm(page, _mat_mul(R, C))

            page.mediabox = pikepdf.Array([pikepdf.Real(0),pikepdf.Real(0),
                                         pikepdf.Real(new_w),pikepdf.Real(new_h)])
            # The rotation and the old boxes are now baked into the content —
            # leaving them behind used to hand every later tool (N-Up above all)
            # a page whose declared boxes no longer matched its content, which is
            # what pushed the content off-centre there. /CropBox and /Rotate are
            # inheritable, so they must be overwritten rather than deleted.
            page.obj["/CropBox"] = pikepdf.Array([pikepdf.Real(0), pikepdf.Real(0),
                                               pikepdf.Real(new_w), pikepdf.Real(new_h)])
            page.obj["/Rotate"]  = 0
            for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
                if key in page.obj: del page.obj[key]
            n_changed += 1

        pdf.save(out)
        self.open_result(out, os.path.basename(out))
        return tr('{p0} Seite(n) bearbeitet.').format(p0=n_changed)
