"""
Alle Tool-Panels v3.3
======================
- Kein QFormLayout mehr — nur QHBoxLayout pro Zeile
- setFixedWidth(220) auf Labels — garantiert sichtbar
- Kein Abschneiden moeglich
"""
import os, io, math, subprocess, shutil, logging
from tools.page_viewer import (_TV, _register_themed, _pdfium_lock,
                               _ThumbnailCache, _render_queue,
                               _ThumbTask, _ThumbSignals, pil_to_qpixmap)
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox,
    QListWidget, QListWidgetItem, QRadioButton, QTextEdit,
    QScrollArea, QWidget, QSlider, QApplication, QFileDialog, QFrame,
    QSizePolicy, QSplitter, QGridLayout
)
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import (QPixmap, QImage, QKeySequence, QShortcut,
                         QPainter, QPen, QColor, QBrush)
from tools.app_state import AppState, theme_color
from tools._base     import BasePanel, FileDropList, make_label, make_separator, LogBox, CurrentFileBar
from tools.i18n      import tr

# ── Shared helpers, now in tools/panels/ ─────────────────────────────────────
# Imported rather than defined here, and re-exported: this module is a shim.
from tools.panels._shared     import (MM_TO_PT, PAPER_SIZES_PT, LABEL_W,
                                      _normalized_page, _inherited_rotate,
                                      _visible_box, _visible_size, _mat_mul,
                                      _display_matrix, row, row2, PreviewPane)
from tools.panels._colour     import _colour_histogram, _hist_stats
from tools.panels._verify     import (_VERIFY_SCALE, _BLACKOUT_LIMIT, _page_luma,
                                      _conversion_damage, _verify_pages_intact)
from tools.panels._cropmarks  import (_crop_mark_segments,
                                      _crop_marks_content_stream)
from tools.panels._imposition import (_ROT_MATRIX, _slot_placement,
                                      _flatten_annots)
from tools.panels.colour_profile import ColourProfilePanel
from tools.panels.impose import ImposePanel, _booklet_sides, _impose_page_size, _build_impose
from tools.panels.merge_split import MergeSplitPanel
from tools.panels.ocr import _run_ocr, tesseract_langs, _page_has_text, _ocr_with_tesseract, OcrPanel
from tools.panels.layers import LayersPanel
from tools.panels.forms import _plain_ink, _flatten_form, FormsPanel
from tools.panels.compress import CompressPanel, _fmt
from tools.panels.preflight import PreflightPanel
from tools.panels.page_numbers import PageNumbersPanel
from tools.panels.img_pdf import ImgPdfPanel






# ══════════════════════════════════════════════════════════════════════════════
# CROP / RESIZE
# ══════════════════════════════════════════════════════════════════════════════
class CropResizePanel(BasePanel):
    TITLE         = "Zuschneiden / Skalieren"
    SUBTITLE      = ""
    RUN_LABEL     = "Ausfuehren"
    OPENS_NEW_TAB = True

    def _setup(self):
        from PyQt6.QtCore import Qt as _Qt
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(_Qt.Orientation.Horizontal)

        splitter.addWidget(self.build_tool_sidebar())

        self._tool_right_w = QWidget()
        self._tool_right_w.setObjectName("toolRightPanel")
        right_w = self._tool_right_w
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(0, 0, 0, 0); right_l.setSpacing(0)

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
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItem(tr("— Kein —"))
        self.fmt_combo.addItems(list(PAPER_SIZES_PT.keys()))
        self.fmt_combo.addItem(tr("Benutzerdefiniert (mm)"))
        self.fmt_combo.currentIndexChanged.connect(self._apply_format)
        fg.addWidget(self.fmt_combo)

        # Custom width × height (mm) — only shown for "Benutzerdefiniert".
        def cust(default):
            s = QDoubleSpinBox(); s.setRange(10, 2000); s.setSuffix(" mm")
            s.setDecimals(1); s.setFixedWidth(90)
            s.setValue(default)                       # set BEFORE connecting
            s.valueChanged.connect(self._apply_format)
            return s
        self.custom_w = cust(210.0)
        self.custom_h = cust(297.0)
        self._custom_row = QHBoxLayout(); self._custom_row.setSpacing(4)
        cw_lbl = QLabel(tr("B")); cw_lbl.setObjectName("dimLabel")
        ch_lbl = QLabel(tr("H")); ch_lbl.setObjectName("dimLabel")
        self._custom_row.addWidget(cw_lbl); self._custom_row.addWidget(self.custom_w)
        self._custom_row.addWidget(ch_lbl); self._custom_row.addWidget(self.custom_h)
        self._custom_row.addStretch()
        self._custom_w_lbl, self._custom_h_lbl = cw_lbl, ch_lbl
        fg.addLayout(self._custom_row)

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
        self.scale_check = QCheckBox(tr("Inhalt skalieren"))
        self.scale_check.setChecked(False)
        self.scale_check.toggled.connect(self._update_preview)
        layout.addWidget(self.scale_check)

        self.keep_ratio = QCheckBox(tr("Proportionen beibehalten"))
        self.keep_ratio.setChecked(True)
        self.keep_ratio.toggled.connect(self._update_preview)
        layout.addWidget(self.keep_ratio)

        self.apply_all = QCheckBox(tr("Alle PDF-Seiten"))
        self.apply_all.setChecked(False)
        self.apply_all.toggled.connect(self._update_preview)
        layout.addWidget(self.apply_all)
        self._sel_info = QLabel("")
        self._sel_info.setObjectName("dimLabel")
        self._sel_info.setWordWrap(True)
        layout.addWidget(self._sel_info)
        self._update_custom_visibility()

    def _apply_theme(self):
        t = _TV
        self._tool_left_w.setStyleSheet(
            f"QWidget#toolLeftPanel{{background:{t['panel_bg']};}}")
        self._tool_right_w.setStyleSheet(
            f"QWidget#toolRightPanel{{background:{t['viewer_bg']};}}")
        self._preview.setStyleSheet(f"background:{t['card_bg']};")
        self._tool_splitter.setStyleSheet(
            f"QSplitter::handle{{background:{t['splitter']};width:2px;}}")

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
        self.fmt_combo.blockSignals(True)
        self.fmt_combo.setCurrentIndex(0)
        self.fmt_combo.blockSignals(False)
        self._update_custom_visibility()
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
        """Chosen target size (w_pt, h_pt) from the Format dropdown, or None for
        '— Kein —'. Custom uses the mm spin boxes."""
        txt = self.fmt_combo.currentText()
        if txt == tr("Benutzerdefiniert (mm)"):
            return self.custom_w.value() * MM_TO_PT, self.custom_h.value() * MM_TO_PT
        return PAPER_SIZES_PT.get(txt)

    def _update_custom_visibility(self):
        show = self.fmt_combo.currentText() == tr("Benutzerdefiniert (mm)")
        for w in (self.custom_w, self.custom_h, self._custom_w_lbl, self._custom_h_lbl):
            w.setVisible(show)

    def _zero_margins(self):
        self._syncing = True
        for w in [self.ct, self.cb2, self.cl2, self.cr]:
            w.blockSignals(True); w.setValue(0.0); w.blockSignals(False)
        self._syncing = False

    def _apply_format(self):
        """React to Format / custom-size / action changes."""
        self._update_custom_visibility()
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
        # self.current_pdf() — not AppState.current_pdf — so the page manager's
        # rotations and reordering are reflected. Reading the file on disk meant
        # a rotated page was measured and previewed in its original orientation.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            return
        try:
            import pypdfium2 as pdfium
            pages    = self._get_target_pages()
            page_idx = pages[0][0] if pages else 0
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
        boxes. Shared by the preview and the run so they can never disagree."""
        size = self._format_margins()
        if size is not None:
            dw = (pw - size[0]) / 2; dh = (ph - size[1]) / 2
            return dh, dh, dw, dw
        return (self.ct.value()  * MM_TO_PT, self.cb2.value() * MM_TO_PT,
                self.cl2.value() * MM_TO_PT, self.cr.value()  * MM_TO_PT)

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
        with _pdfium_lock:
            doc = pdfium.PdfDocument(pdf_path)
            try:
                page = doc[page_idx]
                pw = page.get_width(); ph = page.get_height()
                base_scale = 1400.0 / max(pw, ph, 1.0)   # fixed preview resolution
                base_pil = page.render(scale=base_scale).to_pil()
            finally:
                doc.close()
        self._base_cache = (key, base_pil, pw, ph)
        return base_pil, pw, ph

    def _render_preview(self, avail_w, avail_h, zoom):
        # self.current_pdf() — not AppState.current_pdf — so the page manager's
        # rotations and reordering are reflected. Reading the file on disk meant
        # a rotated page was measured and previewed in its original orientation.
        pdf_path = self.current_pdf()
        if not pdf_path or not os.path.isfile(pdf_path):
            self._sel_info.setText("")
            return None, tr("Keine PDF geoeffnet")
        from PyQt6.QtCore import Qt as _Qt2
        from PIL import Image as PILImage

        pages    = self._get_target_pages()
        page_idx = pages[0][0] if pages else 0
        n_pages  = len(pages)
        if self.apply_all.isChecked(): self._sel_info.setText(tr("Alle Seiten"))
        elif n_pages > 1:             self._sel_info.setText(f"{n_pages} {tr('Seiten')}")
        else:                         self._sel_info.setText(f"{tr('Seite')} {page_idx+1}")

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
        do_scale = self.scale_check.isChecked()

        cs = min(avail_w / new_w, avail_h / new_h) * zoom
        cw_px = max(1, int(new_w * cs))
        ch_px = max(1, int(new_h * cs))

        target_w = max(1, int(pw * cs)); target_h = max(1, int(ph * cs))
        pil = (base_pil if base_pil.size == (target_w, target_h)
               else base_pil.resize((target_w, target_h), PILImage.LANCZOS))

        changed = any([t_pt, b_pt, l_pt, r_pt])

        gw = max(1, int(pw * cs));  gh = max(1, int(ph * cs))
        g_page_x = int(-l_pt * cs); g_page_y = int(-t_pt * cs)

        min_x = min(0, g_page_x); min_y = min(0, g_page_y)
        max_x = max(cw_px, g_page_x + gw); max_y = max(ch_px, g_page_y + gh)
        shift_x = -min_x; shift_y = -min_y
        canvas_w = max(1, max_x - min_x); canvas_h = max(1, max_y - min_y)

        rx = shift_x;          ry = shift_y
        gx = g_page_x + shift_x; gy = g_page_y + shift_y

        result = QPixmap(canvas_w, canvas_h)
        result.fill(QColor("#1a2a40"))
        painter = QPainter(result)

        # Paint the resulting page as paper first. With negative margins
        # ("- erweitern", i.e. adding white space) the added strips belong to the
        # new page but carried no content, so they stayed the dark canvas colour
        # — the white space you were adding was invisible in the preview.
        painter.setPen(_Qt2.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawRect(rx, ry, cw_px, ch_px)

        def draw_ghost():
            painter.setPen(_Qt2.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(100, 140, 220, 45)))
            painter.drawRect(gx, gy, gw - 1, gh - 1)
            ghost_pen = QPen(QColor(120, 160, 255, 200), 1)
            ghost_pen.setStyle(_Qt2.PenStyle.DashLine)
            painter.setPen(ghost_pen)
            painter.setBrush(_Qt2.BrushStyle.NoBrush)
            painter.drawRect(gx, gy, gw - 1, gh - 1)

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
            painter.setBrush(_Qt2.BrushStyle.NoBrush)
            painter.drawRect(rx, ry, max(1, cw_px - 1), max(1, ch_px - 1))
            if changed: draw_ghost()
        else:
            src_x = max(0, int(l_pt * cs)); src_y = max(0, int(t_pt * cs))
            src_w = min(max(1, int(new_w * cs)), pil.width  - src_x)
            src_h = min(max(1, int(new_h * cs)), pil.height - src_y)
            dst_x = rx + max(0, int(-l_pt * cs))
            dst_y = ry + max(0, int(-t_pt * cs))
            pm  = pil_to_qpixmap(pil)
            painter.drawPixmap(dst_x, dst_y, src_w, src_h, pm, src_x, src_y, src_w, src_h)
            painter.setPen(QPen(QColor(_TV['acc']), 2))
            painter.setBrush(_Qt2.BrushStyle.NoBrush)
            painter.drawRect(rx, ry, max(1, cw_px - 1), max(1, ch_px - 1))
            if changed: draw_ghost()

        # Cut-marks-only mode: leave the page as-is and draw crop marks at the
        # chosen size, centred on the page (same marks as the N-Up tool).
        marks_size = self._target_size_pt() if self._marks_only() else None
        if marks_size:
            tw, th = marks_size
            mx0 = (pw - tw) / 2; my0 = (ph - th) / 2
            rect = (mx0, my0, mx0 + tw, my0 + th)
            painter.setPen(QPen(QColor("#000000"), 1)); painter.setBrush(_Qt2.BrushStyle.NoBrush)
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
        import pikepdf as _pik
        src_path = self.require_pdf()
        out = self.save_pdf("PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        if self.apply_all.isChecked():
            from pypdf import PdfReader as _PR
            target_origs = set(range(len(_PR(src_path, strict=False).pages)))
        else:
            target_pages = self._get_target_pages()
            if not target_pages: raise ValueError(tr("Keine Seiten ausgewaehlt."))
            target_origs = {orig for orig, _ in target_pages}

        # Cut-marks-only mode: don't resize the page — just stamp crop marks at
        # the chosen size, centred on each page (shares the N-Up marks helper).
        marks_size = self._target_size_pt() if self._marks_only() else None
        if marks_size:
            tw, th = marks_size
            pdf = _pik.open(src_path); n_changed = 0
            for i, page in enumerate(pdf.pages):
                if i not in target_origs: continue
                # Centre the marks on the *visible* page; on a rotated page the
                # requested size is meant as seen, so it swaps in page space.
                bx0, by0, bx1, by1 = _visible_box(page)
                mw, mh = ((th, tw) if _inherited_rotate(page) in (90, 270)
                          else (tw, th))
                x0 = bx0 + ((bx1 - bx0) - mw) / 2; y0 = by0 + ((by1 - by0) - mh) / 2
                ops = _crop_marks_content_stream([(x0, y0, x0 + mw, y0 + mh)])
                page.contents_add(_pik.Stream(pdf, ops))
                n_changed += 1
            pdf.save(out)
            self.open_result(out, os.path.basename(out))
            return tr('Schnittmarken auf {p0} Seite(n) gesetzt ({p1:.0f}×{p2:.0f} mm).').format(p0=n_changed, p1=tw / MM_TO_PT, p2=th / MM_TO_PT)

        do_scale = self.scale_check.isChecked()
        pdf = _pik.open(src_path)
        n_changed = 0

        def _apply_ctm(pg, m):
            """Prepend `q a b c d e f cm` to the page's content stream."""
            if m == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0): return
            contents = pg.get("/Contents")
            if contents is None: return
            old = (b" ".join(bytes(s.read_bytes()) for s in contents)
                   if isinstance(contents, _pik.Array)
                   else bytes(contents.read_bytes()))
            hdr = ("q %.6f %.6f %.6f %.6f %.4f %.4f cm\n" % m).encode()
            pg["/Contents"] = _pik.Stream(pdf, hdr + old + (chr(10) + "Q").encode())

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

            page.mediabox = _pik.Array([_pik.Real(0),_pik.Real(0),
                                         _pik.Real(new_w),_pik.Real(new_h)])
            # The rotation and the old boxes are now baked into the content —
            # leaving them behind used to hand every later tool (N-Up above all)
            # a page whose declared boxes no longer matched its content, which is
            # what pushed the content off-centre there. /CropBox and /Rotate are
            # inheritable, so they must be overwritten rather than deleted.
            page.obj["/CropBox"] = _pik.Array([_pik.Real(0), _pik.Real(0),
                                               _pik.Real(new_w), _pik.Real(new_h)])
            page.obj["/Rotate"]  = 0
            for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
                if key in page.obj: del page.obj[key]
            n_changed += 1

        pdf.save(out)
        self.open_result(out, os.path.basename(out))
        return tr('{p0} Seite(n) bearbeitet.').format(p0=n_changed)





# ══════════════════════════════════════════════════════════════════════════════
# GRAYSCALE
# ══════════════════════════════════════════════════════════════════════════════




def _grey_retry_page(gs_bin, src, index, report):
    """Convert a single page on its own, for pages the full-document run damaged.

    Isolating the page drops the surrounding transparency groups and shared
    resources that trip Ghostscript up, so this often succeeds where the whole
    document did not. Returns a one-page PDF path, or None."""
    import subprocess, tempfile, pikepdf
    one = grey = None
    try:
        fd, one = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        fd, grey = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        with pikepdf.open(src) as pdf, pikepdf.Pdf.new() as single:
            single.pages.append(pdf.pages[index])
            single.save(one)
        r = subprocess.run(
            [gs_bin, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
             "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
             "-dCompatibilityLevel=1.5", "-dAutoRotatePages=/None",
             "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
             "-dDownsampleMonoImages=false", "-o", grey, one],
            capture_output=True, text=True, errors="replace", timeout=300)
        if r.returncode != 0 or not os.path.getsize(grey):
            return None
        blacked, vanished = _conversion_damage(
            _page_luma(src, index), _page_luma(grey, 0))
        if blacked > _BLACKOUT_LIMIT or vanished > _BLACKOUT_LIMIT:
            return None
        return grey
    except Exception:
        logging.exception("grayscale: single-page retry for page %d failed", index + 1)
        return None
    finally:
        if one:
            try: os.remove(one)
            except OSError: pass


def _grey_vector(gs_bin, src, out, selected, n_pages, report):
    """Convert the `selected` page indices to greyscale LOSSLESSLY and
    VECTOR-BASED with Ghostscript (pdfwrite + ColorConversionStrategy=Gray):
    text stays text, vectors stay vectors, every colour space (RGB/CMYK/ICC/
    spot) is mapped to DeviceGray, and images keep full resolution (no
    downsampling). Pages NOT selected are copied through unchanged. Runs on a
    worker thread (only paths/ints cross the boundary). Returns (out, summary)."""
    import subprocess, tempfile, os, contextlib, pikepdf
    report(tr("Ghostscript: Graustufen-Konvertierung …"))
    fd, grey_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    repaired = {}          # bound before the try: the finally cleans it up
    try:
        cmd = [gs_bin, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
               "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
               "-dCompatibilityLevel=1.5", "-dAutoRotatePages=/None",
               "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
               "-dDownsampleMonoImages=false",
               "-o", grey_tmp, src]
        try:
            # errors="replace": Ghostscript writes its diagnostics in the system
            # locale, and a byte it could not decode used to raise UnicodeDecodeError
            # here — burying the actual failure under a decoding error.
            r = subprocess.run(cmd, capture_output=True, text=True,
                               errors="replace", timeout=900)
        except subprocess.TimeoutExpired:
            raise RuntimeError(tr(
                "Ghostscript hat nach 15 Minuten nicht geantwortet und wurde "
                "abgebrochen. Die PDF ist vermutlich beschädigt oder sehr groß."))
        if r.returncode != 0 or not os.path.exists(grey_tmp) or os.path.getsize(grey_tmp) == 0:
            raise RuntimeError((r.stderr or r.stdout or tr("Ghostscript-Fehler")).strip()[:400])

        # ── Verify before anything is written ────────────────────────────────
        # Ghostscript exits 0 while blacking out a transparency group or a
        # soft-masked image. Nothing in the return code, the stderr or the page
        # count reveals it, and the result only shows up on paper. So every
        # converted page is compared against a greyscale render of the original
        # and no page that failed that comparison is ever written out.
        with pikepdf.open(src) as _s:
            n = min(n_pages, len(_s.pages))
        with pikepdf.open(grey_tmp) as _g:
            n_grey = len(_g.pages)
        convertible = {i for i in selected if i < n and i < n_grey}
        report(tr("Konvertierte Seiten prüfen …"))
        damaged = _verify_pages_intact(src, grey_tmp, convertible, report)

        # Give the damaged ones a second chance on their own — isolating a page
        # drops the surrounding transparency groups that trip Ghostscript up.
        for i in sorted(damaged):
            report(tr('Seite {p0} erneut versuchen …').format(p0=i + 1))
            fixed = _grey_retry_page(gs_bin, src, i, report)
            if fixed:
                repaired[i] = fixed
        for i in repaired:
            damaged.pop(i, None)

        report(tr("Seiten zusammenstellen …"))
        # ExitStack closes whatever was opened even if the second open throws —
        # the old shape opened src_pdf outside the try, so a bad Ghostscript
        # output leaked the source document's handle.
        with contextlib.ExitStack() as stack:
            src_pdf  = stack.enter_context(pikepdf.open(src))
            grey_pdf = stack.enter_context(pikepdf.open(grey_tmp))
            out_pdf  = stack.enter_context(pikepdf.Pdf.new())
            fixed_pdfs = {i: stack.enter_context(pikepdf.open(p))
                          for i, p in repaired.items()}
            # Never index past either document: the scan's page count can be
            # stale, and Ghostscript can return fewer pages than it was given.
            n_conv = missing = 0
            for i in range(n):
                if i in selected:
                    if i in fixed_pdfs:
                        out_pdf.pages.append(fixed_pdfs[i].pages[0]); n_conv += 1
                        continue
                    if i in convertible and i not in damaged:
                        out_pdf.pages.append(grey_pdf.pages[i]); n_conv += 1
                        continue
                    if i not in damaged:
                        missing += 1    # Ghostscript returned fewer pages
                # Anything damaged, missing or unselected keeps the original
                # exactly — a colour page is a nuisance, a black one is a reprint.
                out_pdf.pages.append(src_pdf.pages[i])
            # Save beside the target and rename over it, so a failure part way
            # through cannot leave a half-written PDF for the app to open.
            tmp_fd, out_tmp = tempfile.mkstemp(
                suffix=".pdf", dir=os.path.dirname(os.path.abspath(out)))
            os.close(tmp_fd)
            try:
                out_pdf.save(out_tmp)
                os.replace(out_tmp, out)
            except Exception:
                with contextlib.suppress(OSError): os.remove(out_tmp)
                raise
        msg = (f"{n_conv} {tr('Seite(n) konvertiert (vektorbasiert)')}, "
               f"{n - n_conv} {tr('unveraendert')}")
        if missing:
            msg += "  — " + tr(
                '{p0} Seite(n) konnte Ghostscript nicht umwandeln und blieben farbig.'
            ).format(p0=missing)
        if n < n_pages:
            msg += "  — " + tr('Dokument hat nur {p0} Seiten.').format(p0=n)
        if repaired:
            msg += "  — " + tr(
                '{p0} Seite(n) einzeln nachkonvertiert.').format(p0=len(repaired))
        if damaged:
            # Loud and specific: these pages are still in colour, on purpose,
            # and the operator has to know which ones before the job goes out.
            detail = ", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items()))
            msg += ("\n⚠  " + tr(
                'ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt '
                'und blieben deshalb unveraendert farbig: {p1}').format(
                    p0=len(damaged), p1=detail))
        return out, msg
    finally:
        for _p in repaired.values():
            try: os.remove(_p)
            except OSError: pass
        try: os.remove(grey_tmp)
        except OSError: pass


class GrayscalePanel(BasePanel):
    TITLE         = "Graustufen-Konvertierung"
    SUBTITLE      = "Visuell graue Seiten in echtes DeviceGray umwandeln."
    OPENS_NEW_TAB = True

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(4)
        self._tool_splitter = splitter

        # ── Left: shared standardized sidebar ─────────────────────────────
        splitter.addWidget(self.build_tool_sidebar())

        # ── Right: preview grid ───────────────────────────────────────────
        right_w = QWidget(); right_w.setObjectName("toolRightPanel")
        self._tool_right_w = right_w
        right_layout = QVBoxLayout(right_w)
        right_layout.setContentsMargins(0, 0, 0, 0); right_layout.setSpacing(0)

        zoom_bar = QWidget(); zoom_bar.setFixedHeight(32)
        self._gs_zoombar = zoom_bar
        zbl = QHBoxLayout(zoom_bar); zbl.setContentsMargins(8, 0, 8, 0); zbl.setSpacing(4)
        self._gs_legend_lbls = []
        for color, text in [("#3a8a3a", tr("Grün = konvertiert")),
                             ("#2176ae", tr("Blau = erzwungen")),
                             ("#e67e22", tr("Orange = übersprungen")),
                             ("#c0392b", tr("Rot = Farbe"))]:
            dot = QLabel("■"); dot.setStyleSheet(f"color:{color};font-size:13px;background:transparent;")
            lbl = QLabel(text)
            self._gs_legend_lbls.append(lbl)
            zbl.addWidget(dot); zbl.addWidget(lbl); zbl.addSpacing(6)
        zbl.addStretch()
        self._card_w = 90
        self._preview_cards = []
        self._gs_zoombtns = []
        for txt, fn in [("−", self._zoom_out), ("fit", self._zoom_reset), ("+", self._zoom_in)]:
            zb = QPushButton(txt); zb.setFixedSize(32, 22)
            self._gs_zoombtns.append(zb)
            zb.clicked.connect(fn); zbl.addWidget(zb)
        right_layout.addWidget(zoom_bar)

        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._preview_scroll.wheelEvent = self._preview_wheel
        self._preview_scroll.viewport().installEventFilter(self)

        placeholder = QLabel(tr("← PDF öffnen um Vorschau zu laden"))
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._gs_placeholder = placeholder
        self._preview_scroll.setWidget(placeholder)
        right_layout.addWidget(self._preview_scroll, 1)

        status_bar = QWidget(); status_bar.setFixedHeight(28)
        self._gs_statusbar = status_bar
        sbl = QHBoxLayout(status_bar); sbl.setContentsMargins(12, 0, 12, 0); sbl.setSpacing(20)
        self._status_sw    = QLabel(f"🖤  {tr('SW')}: —")
        self._status_color = QLabel(f"🎨  {tr('Farbe')}: —")
        self._status_total = QLabel(f"{tr('Gesamt')}: —")
        for lbl in [self._status_sw, self._status_color, self._status_total]:
            sbl.addWidget(lbl)
        sbl.addStretch()
        right_layout.addWidget(status_bar)

        splitter.addWidget(right_w)
        splitter.setSizes([400, 800])
        outer.addWidget(splitter)

        # Follow light/dark theme switches (see NUpPanel._apply_theme).
        _register_themed(self)
        self._apply_theme()

    def _apply_theme(self):
        t = _TV
        self._tool_left_w.setStyleSheet(
            f"QWidget#toolLeftPanel{{background:{t['panel_bg']};}}")
        self._tool_right_w.setStyleSheet(
            f"QWidget#toolRightPanel{{background:{t['viewer_bg']};}}")
        self._tool_splitter.setStyleSheet(
            f"QSplitter::handle{{background:{t['splitter']};}}")
        self._gs_zoombar.setStyleSheet(
            f"background:{t['sidebar_bg']};border-bottom:1px solid {t['border']};")
        self._gs_statusbar.setStyleSheet(
            f"background:{t['sidebar_bg']};border-top:1px solid {t['border']};")
        self._preview_scroll.setStyleSheet(
            f"QScrollArea{{background:{t['viewer_bg']};border:none;}}")
        for zb in self._gs_zoombtns:
            zb.setStyleSheet(
                f"QPushButton{{background:{t['panel_bg']};color:{t['text']};"
                f"border:1px solid {t['border']};border-radius:3px;font-size:11px;padding:0;}}"
                f"QPushButton:hover{{background:{t['hover']};}}")
        for lbl in (self._gs_legend_lbls +
                    [self._status_sw, self._status_color, self._status_total]):
            lbl.setStyleSheet(
                f"color:{t['dim']};font-size:11px;background:transparent;")
        ph = getattr(self, '_gs_placeholder', None)
        if ph is not None:
            try:
                ph.setStyleSheet(
                    f"color:{t['dim']};font-size:14px;background:{t['viewer_bg']};")
            except RuntimeError:
                self._gs_placeholder = None   # replaced by cards after a PDF loads
        # The preview grid is built later (once a PDF is scanned), so it has to
        # be re-styled here too — otherwise it keeps the colours that were live
        # when it was built and stays dark after a switch to the light theme.
        box = getattr(self, '_preview_box', None)
        if box is not None:
            try:
                box.setStyleSheet(
                    f"QWidget#greyPreviewBox{{background:{t['viewer_bg']};}}")
                for _f, img_lbl, num_lbl in self._preview_cards:
                    img_lbl.setStyleSheet(f"background:{t['card_bg']};border:none;")
                    num_lbl.setStyleSheet(
                        f"color:{t['dim']};font-size:10px;background:transparent;border:none;")
                self._update_preview_borders()   # restores the status colours
            except RuntimeError:
                self._preview_box = None; self._preview_cards = []

    def eventFilter(self, obj, e):
        if (hasattr(self, '_preview_scroll') and
                obj is self._preview_scroll.viewport() and
                e.type() == QEvent.Type.Wheel):
            self._preview_wheel(e); return True
        return super().eventFilter(obj, e)

    def _zoom_in(self):
        self._card_w = min(1400, self._card_w + 20); self._rezoom()
    def _zoom_out(self):
        self._card_w = max(50, self._card_w - 20); self._rezoom()
    def _zoom_reset(self):
        self._card_w = 90; self._rezoom()

    def _rezoom(self):
        if not self._preview_cards: return
        card_h = int(self._card_w * (127 / 90))
        for frame, img_lbl, num_lbl in self._preview_cards:
            frame.setFixedSize(self._card_w + 12, card_h + 24)
            img_lbl.setFixedSize(self._card_w, card_h)
            pm = img_lbl.property("src_pm")
            if pm:
                img_lbl.setPixmap(pm.scaled(self._card_w, card_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation))
        self._relayout_preview()
        if not hasattr(self, '_zoom_smooth_timer'):
            from PyQt6.QtCore import QTimer as _QT
            self._zoom_smooth_timer = _QT(); self._zoom_smooth_timer.setSingleShot(True)
            self._zoom_smooth_timer.timeout.connect(self._rezoom_smooth)
        self._zoom_smooth_timer.start(180)

    def _rezoom_smooth(self):
        card_h = int(self._card_w * (127 / 90))
        for frame, img_lbl, num_lbl in self._preview_cards:
            pm = img_lbl.property("src_pm")
            if pm:
                img_lbl.setPixmap(pm.scaled(self._card_w, card_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))

    def _preview_wheel(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if e.angleDelta().y() > 0: self._zoom_in()
            else: self._zoom_out()
            e.accept()
        else:
            QScrollArea.wheelEvent(self._preview_scroll, e)

    def build_ui(self, layout):
        self._page_data    = []
        self._grey_pages   = set()
        self._manual_sel   = set()
        self._manual_skip  = set()
        self._last_click   = None
        self._already_grey = set()
        self._scanned_path = ""
        self._scanning     = False
        # Shared thumbnail pipeline — same cache as PageGrid, same render queue
        self._grey_thumb_gen  = 0
        self._grey_thumb_sigs = _ThumbSignals()
        self._grey_thumb_sigs.ready.connect(self._on_grey_thumb_ready)

        mode_grp = QGroupBox(tr("Erkennungs-Modus"))
        mg = QVBoxLayout(mode_grp); mg.setSpacing(6); mg.setContentsMargins(8,10,8,8)
        self.mode_single = QRadioButton(tr("1 farbiger Pixel = Farbseite"))
        self.mode_ratio  = QRadioButton(tr("Nach Anteil farbiger Pixel"))
        self.mode_ratio.setChecked(True)
        self.mode_single.toggled.connect(self._on_mode_changed)
        mg.addWidget(self.mode_single); mg.addWidget(self.mode_ratio)
        layout.addWidget(mode_grp)

        thr_grp = QGroupBox(tr("Farb-Schwellwert"))
        tg = QVBoxLayout(thr_grp); tg.setSpacing(4); tg.setContentsMargins(8,10,8,8)
        tg.addWidget(make_label(tr("Abstand vom Grau pro Pixel:"), dim=True))
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel(tr("Streng")))
        self.thr = QSlider(Qt.Orientation.Horizontal)
        self.thr.setRange(1, 80); self.thr.setValue(20)
        self.thr.setTickInterval(10); self.thr.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.thr_lbl = QLabel("20"); self.thr_lbl.setFixedWidth(28)
        self.thr.valueChanged.connect(self._on_setting_changed)
        thr_row.addWidget(self.thr, 1); thr_row.addWidget(QLabel(tr("Tolerant"))); thr_row.addWidget(self.thr_lbl)
        tg.addLayout(thr_row)
        layout.addWidget(thr_grp)

        ratio_grp = QGroupBox(tr("Mindest-Anteil farbiger Pixel"))
        rg = QVBoxLayout(ratio_grp); rg.setSpacing(4); rg.setContentsMargins(8,10,8,8)
        rg.addWidget(make_label(tr("Ab wieviel % gilt die Seite als Farbseite?"), dim=True))
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(QLabel("0.05%"))
        self.ratio = QSlider(Qt.Orientation.Horizontal)
        self.ratio.setRange(1, 5000); self.ratio.setValue(300)
        self.ratio.setTickInterval(500); self.ratio.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.ratio_lbl = QLabel("1.50%"); self.ratio_lbl.setFixedWidth(44)
        self.ratio.valueChanged.connect(self._on_setting_changed)
        ratio_row.addWidget(self.ratio, 1); ratio_row.addWidget(QLabel("25%")); ratio_row.addWidget(self.ratio_lbl)
        rg.addLayout(ratio_row)
        self._ratio_grp = ratio_grp; ratio_grp.setEnabled(True)
        layout.addWidget(ratio_grp)

        AppState.get().pdf_changed.connect(self._on_pdf_changed)

    def _on_mode_changed(self):
        self._ratio_grp.setEnabled(self.mode_ratio.isChecked())
        if self._page_data: self._reclassify()

    def _on_setting_changed(self, val=None):
        self.thr_lbl.setText(str(self.thr.value()))
        self.ratio_lbl.setText(f"{self.ratio.value() / 200.0:.2f}%")
        if self._page_data: self._reclassify()

    def _on_pdf_changed(self, path):
        if path and self.isVisible():
            QTimer.singleShot(300, self._scan)

    def showEvent(self, e):
        super().showEvent(e)
        if self._scanned_path != self.current_pdf() or not self._page_data:
            QTimer.singleShot(200, self._scan)

    def _reclassify(self):
        thr = self.thr.value(); use_ratio = self.mode_ratio.isChecked()
        min_ratio = self.ratio.value() / 20000.0
        self._grey_pages.clear()
        for i, hist in enumerate(self._page_data):
            max_diff, colour_ratio = _hist_stats(hist, thr)
            is_colour = (colour_ratio >= min_ratio) if use_ratio else (max_diff > thr)
            if not is_colour: self._grey_pages.add(i)
        self._update_preview_borders(); self._update_status_bar()
        self.log.clear_log()
        self.log.log(f"{len(self._grey_pages)} {tr('Seite(n) werden konvertiert')}, "
                     f"{len(self._page_data)-len(self._grey_pages)} {tr('bleiben unveraendert')}")

    def _build_preview(self, n_pages):
        container = QWidget()
        container.setObjectName("greyPreviewBox")
        self._preview_box = container
        container.setStyleSheet(
            f"QWidget#greyPreviewBox{{background:{_TV['viewer_bg']};}}")
        self._preview_scroll.setWidget(container)
        self._preview_cards = []
        CARD_W = self._card_w; CARD_H = int(CARD_W * (127/90)); GAP = 8; MARGIN = 10
        vp_w = self._preview_scroll.viewport().width() or 600
        cols = max(2, (vp_w - 2*MARGIN + GAP) // (CARD_W + 12 + GAP))
        grid = QGridLayout(container)
        grid.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); grid.setSpacing(GAP)
        for i in range(n_pages):
            frame = QFrame(); frame.setFixedSize(CARD_W + 12, CARD_H + 24)
            frame.setStyleSheet(
                f"QFrame{{background:transparent;border:2px solid {_TV['border']};border-radius:5px;}}")
            fl = QVBoxLayout(frame); fl.setContentsMargins(3,3,3,2); fl.setSpacing(2)
            img_lbl = QLabel(); img_lbl.setFixedSize(CARD_W, CARD_H)
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_lbl.setStyleSheet(f"background:{_TV['card_bg']};border:none;")
            fl.addWidget(img_lbl)
            num_lbl = QLabel(str(i+1)); num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num_lbl.setStyleSheet(
                f"color:{_TV['dim']};font-size:10px;background:transparent;border:none;")
            fl.addWidget(num_lbl)
            grid.addWidget(frame, i // cols, i % cols)
            self._preview_cards.append((frame, img_lbl, num_lbl))
            idx = i
            if i not in self._already_grey:
                frame.setCursor(Qt.CursorShape.PointingHandCursor)
                frame.mousePressEvent = lambda e, n=idx: self._toggle_manual(n, e)

    def _relayout_preview(self):
        container = self._preview_scroll.widget()
        if not container or not self._preview_cards: return
        CARD_W = self._card_w; GAP = 8; MARGIN = 10
        vp_w = self._preview_scroll.viewport().width() or 600
        cols = max(2, (vp_w - 2*MARGIN + GAP) // (CARD_W + 12 + GAP))
        layout = container.layout()
        if layout:
            for i, (frame, _, _) in enumerate(self._preview_cards):
                layout.addWidget(frame, i // cols, i % cols)

    def _load_preview_pixmaps_async(self, pdf_path):
        """Load thumbnails via the shared render queue + thumbnail cache.

        Reuses any thumbnails already rendered by the page grid (same cache,
        same render width) so opening Manage Pages first makes Grayscale
        thumbnails appear instantly — and vice versa.
        """
        self._grey_thumb_gen += 1
        gen = self._grey_thumb_gen
        # Use 220 px — matches PageGrid's default render_w (card_w 110 × 2)
        # so both tools share the exact same cache entries.
        RENDER_W = 220
        for i in range(len(self._preview_cards)):
            cached = _ThumbnailCache.get_any(pdf_path, i, 0)
            if cached is not None:
                self._on_grey_thumb_ready(gen, i, cached)
            else:
                task = _ThumbTask(gen, i, pdf_path, i, 0,
                                  RENDER_W, self._grey_thumb_sigs)
                _render_queue.submit(task, 1)

    def _on_grey_thumb_ready(self, gen, cidx, image):
        """GUI-thread callback: paint a newly arrived thumbnail into its card."""
        if gen != self._grey_thumb_gen:
            return   # stale — a newer scan has started
        if cidx >= len(self._preview_cards):
            return
        frame, img_lbl, num_lbl = self._preview_cards[cidx]
        CARD_W = self._card_w; CARD_H = int(CARD_W * (127 / 90))
        pm = QPixmap.fromImage(image)
        img_lbl.setProperty("src_pm", pm)
        img_lbl.setPixmap(pm.scaled(CARD_W, CARD_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _toggle_manual(self, idx, event=None):
        ctrl  = event is not None and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = event is not None and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if shift and self._last_click is not None:
            lo = min(self._last_click, idx); hi = max(self._last_click, idx)
            if ctrl:
                target_add = self._last_click in self._manual_skip
                for i in range(lo, hi+1):
                    self._manual_sel.discard(i)
                    if target_add: self._manual_skip.add(i)
                    else:          self._manual_skip.discard(i)
            else:
                target_add = self._last_click in self._manual_sel
                for i in range(lo, hi+1):
                    self._manual_skip.discard(i)
                    if target_add: self._manual_sel.add(i)
                    else:          self._manual_sel.discard(i)
        else:
            if ctrl:
                self._manual_sel.discard(idx)
                if idx in self._manual_skip: self._manual_skip.discard(idx)
                else:                        self._manual_skip.add(idx)
            else:
                self._manual_skip.discard(idx)
                if idx in self._manual_sel: self._manual_sel.discard(idx)
                else:                       self._manual_sel.add(idx)
            self._last_click = idx
        self._update_preview_borders()
        effective = (self._grey_pages | self._manual_sel) - self._manual_skip
        self.log.clear_log()
        self.log.log(f"{len(effective)} {tr('Seite(n) werden konvertiert')}  "
                     f"(+{len(self._manual_sel)} {tr('erzwungen')}, -{len(self._manual_skip)} {tr('uebersprungen')})")

    def _update_preview_borders(self):
        for i, (frame, _, _) in enumerate(self._preview_cards):
            if i in self._already_grey:
                color = "transparent"
            elif i in self._manual_skip:
                color = "#e67e22"
            elif i in self._manual_sel:
                color = "#2176ae"
            elif i in self._grey_pages:
                color = "#3a8a3a"
            else:
                color = "#c0392b"
            frame.setStyleSheet(f"QFrame{{background:transparent;border:2px solid {color};border-radius:5px;}}")
        self._update_status_bar()

    def _update_status_bar(self):
        if not hasattr(self, '_status_sw') or not self._page_data: return
        n_total = len(self._page_data)
        pages_to_convert = (self._grey_pages | self._manual_sel) - self._manual_skip - self._already_grey
        n_sw = len(self._already_grey | pages_to_convert)
        self._status_sw.setText(f"🖤  {tr('SW')}: {n_sw}")
        self._status_color.setText(f"🎨  {tr('Farbe')}: {n_total - n_sw}")
        self._status_total.setText(f"{tr('Gesamt')}: {n_total}")

    def _scan(self):
        if self._scanning:
            return
        self._scanning = True
        try:
            self._scan_impl()
        finally:
            # Always clear it: an error escaping the scan used to leave the flag
            # set, and then no later scan would ever run for the rest of the
            # session — the tool just quietly stopped updating.
            self._scanning = False

    def _scan_impl(self):
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        self.log.clear_log()
        self._page_data.clear(); self._grey_pages.clear()
        self._manual_sel.clear(); self._manual_skip.clear(); self._last_click = None
        self._already_grey = set()
        self._grey_thumb_gen += 1   # invalidate any in-flight thumb callbacks
        QApplication.processEvents()
        try:
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(src)
            try:
                n = len(doc)
                self._build_preview(n); QApplication.processEvents()
                for i in range(n):
                    QApplication.processEvents()
                    with _pdfium_lock:
                        pil = doc[i].render(scale=1).to_pil().convert("RGB")
                    hist = _colour_histogram(pil)
                    self._page_data.append(hist)
                    md, _ = _hist_stats(hist, self.thr.value())
                    self.log.log(tr('Seite {p0}: max={p1}, farbig={p2:.2f}%').format(
                        p0=i + 1, p1=md, p2=_hist_stats(hist, self.thr.value())[1] * 100))
            finally:
                # Always: a failed render used to leave the document (and its
                # file handle) open for the life of the app.
                doc.close()
            self._reclassify()
            try:
                import pikepdf, re as _re
                with pikepdf.open(src) as pdf:
                  for i, page in enumerate(pdf.pages):
                    res = page.get("/Resources"); cs_names = set()
                    if res:
                        cs_d = res.get("/ColorSpace")
                        if cs_d and isinstance(cs_d, pikepdf.Dictionary):
                            for v in cs_d.values():
                                try: cs_names.add(str(v[0]) if isinstance(v, pikepdf.Array) else str(v))
                                except Exception: pass
                        xobj = res.get("/XObject")
                        if xobj and isinstance(xobj, pikepdf.Dictionary):
                            for v in xobj.values():
                                try:
                                    if v.get("/Subtype") == pikepdf.Name("/Image"):
                                        cs = v.get("/ColorSpace")
                                        if cs: cs_names.add(str(cs[0]) if isinstance(cs, pikepdf.Array) else str(cs))
                                except Exception: pass
                    try:
                        contents = page.get("/Contents"); stream = b""
                        if isinstance(contents, pikepdf.Array):
                            for c in contents: stream += bytes(c.read_bytes())
                        elif contents: stream = bytes(contents.read_bytes())
                        text = stream.decode("latin-1", errors="replace")
                        if _re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+r[gG]\b', text): cs_names.add("/DeviceRGB")
                        if _re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[kK]\b', text): cs_names.add("/DeviceCMYK")
                    except Exception: pass
                    if "/DeviceGray" in cs_names and not any(x in cs_names for x in ("/DeviceRGB","/DeviceCMYK","/CalRGB","/ICCBased")):
                        self._already_grey.add(i)
            except Exception:
                # Only an optimisation — it marks pages that are already
                # DeviceGray so they are left alone. If it fails they simply get
                # converted like any other page, so carry on, but say so rather
                # than swallowing it silently.
                self._already_grey.clear()
                logging.exception("grayscale: colour-space probe failed")
            self._update_preview_borders()
            self._load_preview_pixmaps_async(src)
            self._scanned_path = src
        except Exception as e:
            logging.exception("grayscale scan failed")
            self.log.log(str(e), error=True)

    def _run_action(self):
        src = self.require_pdf()
        if self._scanned_path != src or not self._page_data:
            self._scan()
        if not self._page_data:
            raise ValueError(tr("Bitte zuerst eine PDF öffnen."))
        if not self._grey_pages and not self._manual_sel:
            self._reclassify()
        pages_to_convert = (self._grey_pages | self._manual_sel) - self._manual_skip - self._already_grey
        if not pages_to_convert:
            raise ValueError(tr("Keine Seiten zum Konvertieren ausgewählt."))
        out = self.save_pdf(tr("Graustufen-PDF speichern als"))
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        gs = shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")
        if not gs:
            raise RuntimeError(tr(
                "Ghostscript (gs) nicht gefunden — für verlustfreie, vektorbasierte "
                "Graustufen erforderlich. Bitte 'ghostscript' installieren."))
        n_pages = len(self._page_data)
        sel = set(pages_to_convert)
        # Vector-preserving conversion runs off the UI thread.
        self.run_async(
            lambda report: _grey_vector(gs, src, out, sel, n_pages, report),
            on_done=self._grey_done,
            busy_label="Graustufen …",
        )
        return None

    def _grey_done(self, result):
        out_path, msg = result
        self.log.log(msg)
        self.open_result(out_path, "Graustufen")




























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












def _build_nup(src, out, src_pages, params, n_slot, report, crop_marks=False):
    """Build the N-Up PDF on a worker thread (via BasePanel.run_async).

    Each source page becomes a Form XObject that is scaled to fit its slot,
    keeping its aspect ratio, and centred there. This is dramatically faster than
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
            s, tx, ty = _slot_placement(box, rot, rects[slot_i])
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
    OPENS_NEW_TAB = True

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
        from PyQt6.QtCore import Qt as _Qt
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(_Qt.Orientation.Horizontal)
        self._tool_splitter = splitter

        splitter.addWidget(self.build_tool_sidebar())

        right_w = QWidget(); right_w.setObjectName("toolRightPanel")
        self._tool_right_w = right_w
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(0, 0, 0, 0); right_l.setSpacing(0)

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
        self._tool_left_w.setStyleSheet(
            f"QWidget#toolLeftPanel{{background:{t['panel_bg']};}}")
        self._tool_right_w.setStyleSheet(
            f"QWidget#toolRightPanel{{background:{t['viewer_bg']};}}")
        self._preview.setStyleSheet(f"background:{t['card_bg']};")
        self._tool_splitter.setStyleSheet(
            f"QSplitter::handle{{background:{t['splitter']};width:2px;}}")

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
        self.blank_fill = QCheckBox(tr("Fehlende Positionen mit Leerseiten auffüllen")); self.blank_fill.setChecked(True)
        gl.addWidget(self.blank_fill)
        layout.addWidget(gb)

        ob = QGroupBox(tr("AUSGABEFORMAT")); ol = QVBoxLayout(ob)
        self.out_fmt = QComboBox()
        self.out_fmt.addItems([tr("DIN A4  (210 × 297 mm)"), tr("DIN A3  (297 × 420 mm)"),
                                tr("DIN A5  (148 × 210 mm)"), tr("Letter  (216 × 279 mm)"),
                                tr("Wie Quellseite × Raster  (automatisch)")])
        ol.addLayout(r(tr("Format:"), self.out_fmt))
        self.landscape = QCheckBox(tr("Querformat")); self.landscape.toggled.connect(self._update_preview)
        ol.addWidget(self.landscape)
        def _fmt_changed(idx):
            # The auto format takes its orientation from the source page.
            self.landscape.setEnabled(idx != 4)
            self._update_preview()
        self.out_fmt.currentIndexChanged.connect(_fmt_changed)
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
        fmt_idx = self.out_fmt.currentIndex()
        fmt_map = {0:(210*PT,297*PT), 1:(297*PT,420*PT), 2:(148*PT,210*PT), 3:(216*PT,279*PT)}
        mt = self.margin_t.value() * PT; mb = self.margin_b.value() * PT
        ml = self.margin_l.value() * PT; mr = self.margin_r.value() * PT
        gh = self.gap_h.value() * PT;    gv = self.gap_v.value() * PT
        if fmt_idx == 4:
            # "Wie Quellseite × Raster": size the sheet so that every slot is
            # exactly one source page and the margins/gaps are added *around*
            # them. They used to be carved out of a sheet of src×grid, so asking
            # for a 10 mm margin here shrank the page to ~95% and — because the
            # fit keeps the aspect ratio — actually produced 10 mm at the sides
            # and 14 mm top/bottom, the opposite of "add white space".
            out_w = src_pw * cols + ml + mr + gh * (cols - 1)
            out_h = src_ph * rows + mt + mb + gv * (rows - 1)
        else:
            out_w, out_h = fmt_map[fmt_idx]
            # Only meaningful for the fixed paper sizes — the auto sheet already
            # follows the orientation of the source page.
            if self.landscape.isChecked(): out_w, out_h = out_h, out_w
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
        from PyQt6.QtGui import QBrush as _QB
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

        result = QPixmap(canvas_w, canvas_h); result.fill(QColor("#1a2a40"))
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(_QB(QColor("#f5f5f5"))); painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(0, 0, canvas_w - 1, canvas_h - 1)
        for row_i in range(rows):
            for col_i in range(cols):
                sx = int((ml + col_i * (slot_w + gh)) * cs)
                sy = int((mt + row_i * (slot_h + gv)) * cs)
                sw = max(1, int(slot_w * cs)); sh = max(1, int(slot_h * cs))
                painter.setBrush(_QB(QColor(200, 210, 230, 60)))
                painter.setPen(QPen(QColor(120, 140, 180, 120), 1))
                painter.drawRect(sx, sy, sw, sh)
                pg = slot_pages[row_i * cols + col_i]
                src_pm = pm_by_page.get(pg) if pg is not None else None
                if src_pm is None:
                    continue
                scale   = min(slot_w / src_pw, slot_h / src_ph)
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
            painter.setPen(QPen(QColor("#000000"), 1)); painter.setBrush(Qt.BrushStyle.NoBrush)
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
        import pikepdf as _pik
        src_path = self.require_pdf()
        out_path = self.save_pdf(tr("N-Up PDF speichern als"))
        if not out_path: raise ValueError(tr("Kein Ausgabepfad."))
        with _pik.open(src_path) as _doc:
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
        params = self._get_layout_params(src_pw, src_ph)
        slot_w, slot_h = params[8], params[9]
        if slot_w <= 1.0 or slot_h <= 1.0:
            raise ValueError(tr("Abstände zu groß — kein Platz für Inhalt."))
        if len(src_pages) == 1: src_pages = src_pages * n_slot   # single-page doc → fill the sheet
        if self.blank_fill.isChecked():
            while len(src_pages) % n_slot: src_pages.append(None)

        crop = self.crop_marks.isChecked()
        self.run_async(
            lambda report: _build_nup(src_path, out_path, src_pages, params,
                                      n_slot, report, crop_marks=crop),
            on_done=self._nup_done,
            busy_label=tr("N-Up läuft …"),
        )
        return None

    def _nup_done(self, result):
        out_path, summary = result
        self.log.log(summary)
        self.open_result(out_path, "N-Up")
