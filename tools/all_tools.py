"""
Alle Tool-Panels v3.3
======================
- Kein QFormLayout mehr — nur QHBoxLayout pro Zeile
- setFixedWidth(220) auf Labels — garantiert sichtbar
- Kein Abschneiden moeglich
"""
import os, io, math, subprocess, shutil
from tools.page_viewer import (_TV, _register_themed, _pdfium_lock,
                               _ThumbnailCache, _render_queue,
                               _ThumbTask, _ThumbSignals)
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox,
    QListWidget, QListWidgetItem, QRadioButton, QTextEdit,
    QScrollArea, QWidget, QSlider, QApplication, QFileDialog, QFrame,
    QSizePolicy, QSplitter
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QImage, QKeySequence, QShortcut
from tools.app_state import AppState, theme_color
from tools._base     import BasePanel, FileDropList, make_label, make_separator, LogBox, CurrentFileBar
from tools.i18n      import tr

MM_TO_PT = 2.8346456693
PAPER_SIZES_PT = {
    "A0  (841x1189mm)":   (2383.94, 3370.39),
    "A1  (594x841mm)":    (1683.78, 2383.94),
    "A2  (420x594mm)":    (1190.55, 1683.78),
    "A3  (297x420mm)":    (841.89,  1190.55),
    "A4  (210x297mm)":    (595.28,  841.89),
    "A5  (148x210mm)":    (419.53,  595.28),
    "A6  (105x148mm)":    (297.64,  419.53),
    "Letter (216x279mm)": (612.0,   792.0),
    "Legal  (216x356mm)": (612.0,   1008.0),
}
LABEL_W = 220   # Feste Label-Breite — passt alle deutschen Bezeichnungen


def _normalized_page(src_page):
    """Return a copy of src_page with /Rotate baked into content and mediabox corrected."""
    from pypdf import PdfWriter, Transformation as _T
    try:
        rot = int(src_page.get('/Rotate', 0) or 0) % 360
    except Exception:
        rot = 0
    if rot == 0:
        return src_page
    w = PdfWriter()
    w.add_page(src_page)
    p = w.pages[0]
    rw = float(p.mediabox.width)
    rh = float(p.mediabox.height)
    if rot == 90:
        p.add_transformation(_T((0, -1, 1, 0, 0, rw)))
        p.mediabox.lower_left = (0, 0); p.mediabox.upper_right = (rh, rw)
    elif rot == 180:
        p.add_transformation(_T((-1, 0, 0, -1, rw, rh)))
    elif rot == 270:
        p.add_transformation(_T((0, 1, -1, 0, rh, 0)))
        p.mediabox.lower_left = (0, 0); p.mediabox.upper_right = (rh, rw)
    for key in ('/Rotate', '/CropBox', '/BleedBox', '/TrimBox', '/ArtBox'):
        if key in p:
            del p[key]
    return p


def row(label_text: str, widget, stretch=1) -> QHBoxLayout:
    """
    Garantiert sichtbares Label + Eingabefeld.
    setFixedWidth wird von Qt immer respektiert — kein Abschneiden.
    """
    h = QHBoxLayout()
    h.setSpacing(12)
    lbl = QLabel(label_text)
    lbl.setWordWrap(True)
    lbl.setFixedWidth(LABEL_W)
    lbl.setObjectName("dimLabel")
    h.addWidget(lbl)
    h.addWidget(widget, stretch)
    return h


def row2(label_text: str, w1, label2: str, w2) -> QHBoxLayout:
    """Zwei Label+Feld Paare in einer Zeile."""
    h = QHBoxLayout()
    h.setSpacing(12)
    l1 = QLabel(label_text); l1.setFixedWidth(LABEL_W); l1.setObjectName("dimLabel")
    l2 = QLabel(label2);     l2.setFixedWidth(120);     l2.setObjectName("dimLabel")
    h.addWidget(l1); h.addWidget(w1, 1)
    h.addWidget(l2); h.addWidget(w2, 1)
    return h


# ══════════════════════════════════════════════════════════════════════════════
# MERGE / SPLIT
# ══════════════════════════════════════════════════════════════════════════════
class MergeSplitPanel(BasePanel):
    TITLE    = "Zusammenfuehren / Trennen"
    SUBTITLE = "PDFs zusammenfuehren oder in Teile aufteilen."

    def build_ui(self, layout):
        mg = QGroupBox(tr("ZUSAMMENFUEHREN")); ml = QVBoxLayout(mg)
        ml.addWidget(make_label(tr("PDFs hinzufuegen. Reihenfolge per Drag & Drop."), dim=True))
        self.merge_list = FileDropList(); ml.addWidget(self.merge_list)
        br = QHBoxLayout()
        for txt, fn in [(tr("+ Hinzufuegen"), self._add_merge),
                        (tr("- Entfernen"), self.merge_list.remove_selected),
                        (tr("Hoch"), self.merge_list.move_up),
                        (tr("Runter"), self.merge_list.move_down)]:
            b = QPushButton(txt); b.setObjectName("secondaryBtn")
            b.clicked.connect(fn); br.addWidget(b)
        br.addStretch(); ml.addLayout(br)
        mb = QPushButton(tr("  Zusammenfuehren und speichern..."))
        mb.setObjectName("actionBtn"); mb.clicked.connect(self._do_merge); ml.addWidget(mb)
        layout.addWidget(mg)

        sp = QGroupBox(tr("TRENNEN")); sl = QVBoxLayout(sp)
        sl.addWidget(make_label(tr("Verwendet die aktuell im Viewer geoeffnete PDF."), dim=True))
        self.radio_each  = QRadioButton(tr("Jede Seite als eigene Datei"))
        self.radio_range = QRadioButton(tr("Nach Seitenbereichen  (z.B. 1-3, 5, 7-9)"))
        self.radio_n     = QRadioButton(tr("Alle N Seiten"))
        self.radio_each.setChecked(True)
        for r in (self.radio_each, self.radio_range, self.radio_n): sl.addWidget(r)
        self.range_input = QLineEdit(); self.range_input.setPlaceholderText(tr("z.B. 1-3, 5, 7-9"))
        sl.addLayout(row(tr("Seitenbereiche:"), self.range_input))
        self.n_spin = QSpinBox(); self.n_spin.setRange(1, 9999); self.n_spin.setValue(1)
        sl.addLayout(row(tr("Seiten pro Teil:"), self.n_spin))
        spb = QPushButton(tr("  Trennen und in Ordner speichern..."))
        spb.setObjectName("actionBtn"); spb.clicked.connect(self._do_split); sl.addWidget(spb)
        layout.addWidget(sp)

    def build_action_row(self, row_layout): pass

    def _add_merge(self):
        paths = self.pick_pdfs()
        if paths: self.merge_list.add_files(paths)

    def _do_merge(self):
        from pypdf import PdfWriter, PdfReader
        paths = self.merge_list.get_paths()
        if not paths: self.log.log("Mindestens eine PDF hinzufuegen.", error=True); return
        out = self.save_pdf("Zusammengefuehrte PDF speichern als")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            writer = PdfWriter()
            for p in paths:
                for page in PdfReader(p).pages: writer.add_page(page)
            with open(out, "wb") as f: writer.write(f)
            self.log.log(f"{len(paths)} Dateien zusammengefuehrt")
            self.open_result(out, "Zusammengefuehrt")
        except Exception as e: self.log.log(str(e), error=True)

    def _do_split(self):
        from pypdf import PdfReader, PdfWriter
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            reader = PdfReader(src); stem = os.path.splitext(os.path.basename(src))[0]
            n = len(reader.pages); saved = []
            if self.radio_each.isChecked():
                for i, page in enumerate(reader.pages):
                    w = PdfWriter(); w.add_page(page)
                    p = os.path.join(out_dir, f"{stem}_seite{i+1:03d}.pdf")
                    with open(p, "wb") as f: w.write(f); saved.append(p)
            elif self.radio_range.isChecked():
                groups = self._parse_ranges(self.range_input.text(), n)
                for gi, pages in enumerate(groups, 1):
                    w = PdfWriter()
                    for p in pages: w.add_page(reader.pages[p])
                    path = os.path.join(out_dir, f"{stem}_teil{gi:02d}.pdf")
                    with open(path, "wb") as f: w.write(f); saved.append(path)
            else:
                nv = self.n_spin.value(); chunk = 0
                for start in range(0, n, nv):
                    chunk += 1; w = PdfWriter()
                    for i in range(start, min(start+nv, n)): w.add_page(reader.pages[i])
                    path = os.path.join(out_dir, f"{stem}_teil{chunk:03d}.pdf")
                    with open(path, "wb") as f: w.write(f); saved.append(path)
            self.log.log(f"In {len(saved)} Dateien aufgeteilt")
            if saved: self.open_result(saved[0], f"Teil 1 von {len(saved)}")
        except Exception as e: self.log.log(str(e), error=True)

    def _parse_ranges(self, raw, n):
        groups = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "-" in part:
                    a, b = part.split("-", 1)
                    pages = list(range(int(a.strip())-1, int(b.strip())))
                else:
                    pages = [int(part)-1]
            except ValueError:
                continue
            pages = [p for p in pages if 0 <= p < n]
            if pages: groups.append(pages)
        return groups

    def _run_action(self): pass


# ══════════════════════════════════════════════════════════════════════════════
# COMPRESS
# ══════════════════════════════════════════════════════════════════════════════
class CompressPanel(BasePanel):
    TITLE         = "Komprimieren"
    SUBTITLE      = "PDF-Dateigroesse reduzieren. Ergebnis wird als neuer Tab geoeffnet."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        gb = QGroupBox(tr("EINSTELLUNGEN")); gl = QVBoxLayout(gb)
        self.preset = QComboBox()
        self.preset.addItems([tr("Screen  (72 dpi — Web)"),
                              tr("Ebook   (150 dpi — Tablet)"),
                              tr("Drucker (300 dpi — Standard)"),
                              tr("Vordruck (300 dpi — Profidruck)")])
        self.preset.setCurrentIndex(2)
        gl.addLayout(row(tr("Qualitaetsstufe:"), self.preset))
        self.gs_check = QCheckBox(tr("Ghostscript verwenden (empfohlen)"))
        self.gs_check.setChecked(shutil.which("gs") is not None)
        gl.addWidget(self.gs_check)
        if not shutil.which("gs"):
            gl.addWidget(make_label("Ghostscript fehlt — sudo pacman -S ghostscript", dim=True))
        layout.addWidget(gb)

    def build_action_row(self, row_layout):
        row_layout.addStretch()
        self.run_btn = QPushButton(tr("  Komprimieren"))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.clicked.connect(self._safe_run)
        row_layout.addWidget(self.run_btn)

    def _run_action(self):
        src = self.require_pdf()
        out = self.save_pdf("Komprimierte PDF speichern als")
        if not out:
            raise ValueError("Kein Ausgabepfad.")

        preset_map = ["/screen", "/ebook", "/printer", "/prepress"]
        gs_setting = preset_map[self.preset.currentIndex()]

        size_before = os.path.getsize(src)

        if self.gs_check.isChecked() and shutil.which("gs"):
            cmd = [
                "gs", "-o", out,
                "-sDEVICE=pdfwrite",
                f"-dPDFSETTINGS={gs_setting}",
                "-dNOPAUSE", "-dBATCH", "-dQUIET",
                src,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"Ghostscript-Fehler:\n{(r.stderr or r.stdout)[:400]}")
        else:
            import pikepdf
            pdf = pikepdf.open(src)
            pdf.save(out, compress_streams=True, recompress_flate=True)
            pdf.close()

        size_after = os.path.getsize(out)
        ratio = (1 - size_after / size_before) * 100 if size_before else 0
        self.open_result(out, "Komprimiert")
        return (f"Fertig. {_fmt(size_before)} → {_fmt(size_after)}"
                f"  ({ratio:+.1f}%)")

# ══════════════════════════════════════════════════════════════════════════════
# CROP / RESIZE
# ══════════════════════════════════════════════════════════════════════════════
class CropResizePanel(BasePanel):
    TITLE         = "Zuschneiden / Skalieren"
    SUBTITLE      = ""
    OPENS_NEW_TAB = True

    def _setup(self):
        from PyQt6.QtCore import Qt as _Qt
        from tools._base import LogBox
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(_Qt.Orientation.Horizontal)

        left_w = QWidget(); left_w.setMinimumWidth(220); left_w.setMaximumWidth(380)
        left_w.setObjectName("toolLeftPanel")
        self._tool_left_w = left_w
        left_l = QVBoxLayout(left_w)
        left_l.setContentsMargins(10, 12, 10, 10); left_l.setSpacing(8)

        title = QLabel(tr("Zuschneiden / Skalieren"))
        tf = title.font(); tf.setPointSize(13); tf.setBold(True); title.setFont(tf)
        left_l.addWidget(title)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator"); left_l.addWidget(sep)

        self.build_ui(left_l)
        left_l.addStretch()

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setObjectName("separator"); left_l.addWidget(sep2)
        self.log = LogBox(); left_l.addWidget(self.log)

        self.run_btn = QPushButton(tr("Ausfuehren"))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.clicked.connect(self._safe_run)
        left_l.addWidget(self.run_btn)

        sc  = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        sc.activated.connect(self._enter_pressed)
        sc2 = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        sc2.activated.connect(self._enter_pressed)

        splitter.addWidget(left_w)

        self._tool_right_w = QWidget()
        self._tool_right_w.setObjectName("toolRightPanel")
        right_w = self._tool_right_w
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(6, 6, 6, 6); right_l.setSpacing(3)

        # Header row with zoom controls
        hdr_row = QHBoxLayout()
        hdr = QLabel(tr("Vorschau")); hdr.setObjectName("dimLabel")
        hdr_row.addStretch()
        hdr_row.addWidget(hdr)
        hdr_row.addStretch()
        zoom_out_btn = QPushButton("−"); zoom_out_btn.setFixedSize(22, 22); zoom_out_btn.setObjectName("secondaryBtn")
        self._zoom_lbl_prev = QLabel("100%"); self._zoom_lbl_prev.setObjectName("dimLabel")
        self._zoom_lbl_prev.setFixedWidth(38); self._zoom_lbl_prev.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        zoom_in_btn  = QPushButton("+");  zoom_in_btn.setFixedSize(22, 22);  zoom_in_btn.setObjectName("secondaryBtn")
        zoom_rst_btn = QPushButton("⟳"); zoom_rst_btn.setFixedSize(22, 22); zoom_rst_btn.setObjectName("secondaryBtn")
        hdr_row.addWidget(zoom_out_btn); hdr_row.addWidget(self._zoom_lbl_prev)
        hdr_row.addWidget(zoom_in_btn);  hdr_row.addWidget(zoom_rst_btn)
        right_l.addLayout(hdr_row)

        self._preview_zoom = 1.0
        def _zoom_in():
            self._preview_zoom = min(self._preview_zoom * 1.25, 8.0)
            self._zoom_lbl_prev.setText(f"{int(self._preview_zoom*100)}%"); self._update_preview()
        def _zoom_out():
            self._preview_zoom = max(self._preview_zoom / 1.25, 0.2)
            self._zoom_lbl_prev.setText(f"{int(self._preview_zoom*100)}%"); self._update_preview()
        def _zoom_rst():
            self._preview_zoom = 1.0
            self._zoom_lbl_prev.setText("100%"); self._update_preview()
        zoom_in_btn.clicked.connect(_zoom_in); zoom_out_btn.clicked.connect(_zoom_out); zoom_rst_btn.clicked.connect(_zoom_rst)

        self._preview = QLabel()
        self._preview.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._preview.setMouseTracking(True)
        self._preview.installEventFilter(self)
        right_l.addWidget(self._preview, 1)
        self._preview_info = QLabel("")
        self._preview_info.setObjectName("dimLabel")
        self._preview_info.setAlignment(_Qt.AlignmentFlag.AlignCenter)
        right_l.addWidget(self._preview_info)

        self._tool_splitter = splitter
        _register_themed(self)
        self._apply_theme()
        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)
        QTimer.singleShot(400, self._update_preview)

        # Follow the page picked in "Seiten verwalten": the panel is built once
        # and parked in a QStackedWidget, so refresh the preview whenever the
        # active page changes (or a new PDF is opened).
        AppState.get().current_page_changed.connect(lambda *_: self._update_preview())
        AppState.get().pdf_changed.connect(lambda *_: self._update_preview())

    def showEvent(self, event):
        super().showEvent(event)
        # Re-sync the preview to the currently selected page each time the tool
        # becomes visible.
        self._update_preview()

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
        self.fmt_combo.currentIndexChanged.connect(self._set_format)
        fg.addWidget(self.fmt_combo)
        layout.addWidget(fmt_grp)

        # ── Randfelder ───────────────────────────────────────────────
        crop_grp = QGroupBox(tr("Raender  [+ schneiden / - erweitern]"))
        cg = QVBoxLayout(crop_grp); cg.setSpacing(4); cg.setContentsMargins(6,8,6,6)
        for lbl_txt, w in [(tr("Oben"), self.ct), (tr("Unten"), self.cb2),
                             (tr("Links"), self.cl2), (tr("Rechts"), self.cr)]:
            hl = QHBoxLayout(); hl.setSpacing(4)
            lb = QLabel(lbl_txt); lb.setFixedWidth(38)
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
        # Formatauswahl zurücksetzen ohne _set_format auszulösen
        self.fmt_combo.blockSignals(True)
        self.fmt_combo.setCurrentIndex(0)
        self.fmt_combo.blockSignals(False)
        self._update_preview()

    def _sync_values(self, val):
        if self._syncing or not self.sync_cb.isChecked(): return
        self._syncing = True
        for w in [self.ct, self.cb2, self.cl2, self.cr]: w.setValue(val)
        self._syncing = False

    def _set_format(self):
        """Setzt Randwerte sofort wenn Format gewählt wird."""
        target = PAPER_SIZES_PT.get(self.fmt_combo.currentText())
        if not target:
            self._reset_values()
            return
        pdf_path = AppState.get().current_pdf
        if not pdf_path or not os.path.isfile(pdf_path):
            return
        try:
            import pypdfium2 as pdfium
            pages    = self._get_target_pages()
            page_idx = pages[0][0] if pages else 0
            doc  = pdfium.PdfDocument(pdf_path)
            try:
                page = doc[page_idx]
                pw   = page.get_width()
                ph   = page.get_height()
            finally:
                doc.close()
            tw, th = target
            # Exakte Differenz — kein Runden damit Zielformat stimmt
            diff_w_mm = (pw - tw) / MM_TO_PT / 2
            diff_h_mm = (ph - th) / MM_TO_PT / 2
            # Alle vier Werte setzen ohne zwischendurch Preview auszulösen
            for w in [self.ct, self.cb2, self.cl2, self.cr]:
                w.blockSignals(True)
            self.cl2.setValue(diff_w_mm)
            self.cr.setValue(diff_w_mm)
            self.ct.setValue(diff_h_mm)
            self.cb2.setValue(diff_h_mm)
            for w in [self.ct, self.cb2, self.cl2, self.cr]:
                w.blockSignals(False)
            self._update_preview()
        except Exception as ex:
            self.log.log(str(ex), error=True)

    def _get_target_pages(self):
        state = AppState.get(); model = state.page_model
        if model and model.selected:
            return [(model.orig(uid), uid) for uid in model.order if uid in model.selected]
        if model and model.order:
            cur = max(0, min(state.current_page, len(model.order)-1))
            uid = model.order[cur]
            return [(model.orig(uid), uid)]
        return []

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._preview and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                if delta > 0:
                    self._preview_zoom = min(self._preview_zoom * 1.15, 8.0)
                else:
                    self._preview_zoom = max(self._preview_zoom / 1.15, 0.2)
                self._zoom_lbl_prev.setText(f"{int(self._preview_zoom * 100)}%")
                self._update_preview()
                return True
        return super().eventFilter(obj, event)

    def _update_preview(self):
        pdf_path = AppState.get().current_pdf
        if not pdf_path or not os.path.isfile(pdf_path):
            self._preview.setText(tr("Keine PDF geoeffnet"))
            self._sel_info.setText(""); return
        try:
            import pypdfium2 as pdfium, io
            from PyQt6.QtGui import QPainter, QPen, QColor, QBrush
            from PyQt6.QtCore import Qt as _Qt2
            from PIL import Image as PILImage

            pages    = self._get_target_pages()
            page_idx = pages[0][0] if pages else 0
            n_pages  = len(pages)
            if self.apply_all.isChecked(): self._sel_info.setText(tr("Alle Seiten"))
            elif n_pages > 1:             self._sel_info.setText(f"{n_pages} {tr('Seiten')}")
            else:                         self._sel_info.setText(f"{tr('Seite')} {page_idx+1}")

            doc  = pdfium.PdfDocument(pdf_path)
            try:
                page = doc[page_idx]
                pw   = page.get_width(); ph = page.get_height()

                t_pt = self.ct.value()  * MM_TO_PT
                b_pt = self.cb2.value() * MM_TO_PT
                l_pt = self.cl2.value() * MM_TO_PT
                r_pt = self.cr.value()  * MM_TO_PT

                new_w = max(1., pw - l_pt - r_pt)
                new_h = max(1., ph - t_pt - b_pt)
                do_scale = self.scale_check.isChecked()

                zoom    = getattr(self, '_preview_zoom', 1.0)
                avail_w = max(100, self._preview.width()  - 16)
                avail_h = max(100, self._preview.height() - 16)

                cs_fit    = min(avail_w / new_w, avail_h / new_h) * zoom
                cs_orig   = min(avail_w / pw,    avail_h / ph)    * zoom
                cs_render = max(cs_fit, cs_orig)
                cs        = cs_fit

                cw_px = max(1, int(new_w * cs))
                ch_px = max(1, int(new_h * cs))

                with _pdfium_lock:
                    bm  = page.render(scale=cs_render)
                    pil = bm.to_pil()
            finally:
                doc.close()
            if cs_render > cs * 1.01:
                target_w = max(1, int(pw * cs))
                target_h = max(1, int(ph * cs))
                pil = pil.resize((target_w, target_h), PILImage.LANCZOS)

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
                buf = io.BytesIO(); pil.save(buf, "PNG"); buf.seek(0)
                pm  = QPixmap.fromImage(QImage.fromData(buf.read()))
                ar  = Qt.AspectRatioMode.KeepAspectRatio if keep else Qt.AspectRatioMode.IgnoreAspectRatio
                pm_s = pm.scaled(cw_px, ch_px, ar, Qt.TransformationMode.SmoothTransformation)
                draw_x = rx + (cw_px - pm_s.width())  // 2
                draw_y = ry + (ch_px - pm_s.height()) // 2
                painter.drawPixmap(draw_x, draw_y, pm_s)
                painter.setPen(QPen(QColor("#e94560"), 2))
                painter.setBrush(_Qt2.BrushStyle.NoBrush)
                painter.drawRect(draw_x, draw_y, pm_s.width()-1, pm_s.height()-1)
                if changed: draw_ghost()
            else:
                src_x = max(0, int(l_pt * cs)); src_y = max(0, int(t_pt * cs))
                src_w = min(max(1, int(new_w * cs)), pil.width  - src_x)
                src_h = min(max(1, int(new_h * cs)), pil.height - src_y)
                dst_x = rx + max(0, int(-l_pt * cs))
                dst_y = ry + max(0, int(-t_pt * cs))
                buf = io.BytesIO(); pil.save(buf, "PNG"); buf.seek(0)
                pm  = QPixmap.fromImage(QImage.fromData(buf.read()))
                painter.drawPixmap(dst_x, dst_y, src_w, src_h, pm, src_x, src_y, src_w, src_h)
                painter.setPen(QPen(QColor("#e94560"), 2))
                painter.setBrush(_Qt2.BrushStyle.NoBrush)
                painter.drawRect(rx, ry, max(1, cw_px - 1), max(1, ch_px - 1))
                if changed: draw_ghost()

            painter.end()
            self._preview.setPixmap(result)

            parts = [f"{pw/MM_TO_PT:.0f}x{ph/MM_TO_PT:.0f}mm"]
            if any([t_pt, b_pt, l_pt, r_pt]):
                parts.append(f"{new_w/MM_TO_PT:.0f}x{new_h/MM_TO_PT:.0f}mm")
            self._preview_info.setText(" -> ".join(parts))

        except Exception as ex:
            self._preview.setText(f"Vorschau: {ex}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(50, self._update_preview)

    def _run_action(self):
        import pikepdf as _pik
        src_path = self.require_pdf()
        out = self.save_pdf("PDF speichern als")
        if not out: raise ValueError("Kein Ausgabepfad.")

        if self.apply_all.isChecked():
            from pypdf import PdfReader as _PR
            target_origs = set(range(len(_PR(src_path).pages)))
        else:
            target_pages = self._get_target_pages()
            if not target_pages: raise ValueError("Keine Seiten ausgewaehlt.")
            target_origs = {orig for orig, _ in target_pages}

        t_mm = self.ct.value();  b_mm = self.cb2.value()
        l_mm = self.cl2.value(); r_mm = self.cr.value()
        do_scale = self.scale_check.isChecked()
        pdf = _pik.open(src_path)
        n_changed = 0

        for i, page in enumerate(pdf.pages):
            if i not in target_origs: continue
            mb = page.mediabox
            pw = float(mb[2]) - float(mb[0])
            ph = float(mb[3]) - float(mb[1])

            # Neue Seitengröße
            new_w = max(1.0, pw - l_mm*MM_TO_PT - r_mm*MM_TO_PT)
            new_h = max(1.0, ph - t_mm*MM_TO_PT - b_mm*MM_TO_PT)

            # Inhalt-Versatz: -new_left = -l_mm*MM_TO_PT
            tx = -l_mm * MM_TO_PT   # neg. bei Abschneiden, pos. bei Erweitern
            ty = -b_mm * MM_TO_PT

            def _apply_ctm(pg, sx, sy, e, f):
                """Wendet q sx 0 0 sy e f cm auf den Content-Stream an."""
                contents = pg.get("/Contents")
                if contents is None: return
                old = (b" ".join(bytes(s.read_bytes()) for s in contents)
                       if isinstance(contents, _pik.Array)
                       else bytes(contents.read_bytes()))
                hdr = (("q %.6f 0 0 %.6f %.4f %.4f cm" + chr(10)) % (sx, sy, e, f)).encode()
                pg["/Contents"] = _pik.Stream(pdf, hdr + old + (chr(10) + "Q").encode())

            l_pt = l_mm * MM_TO_PT; b_pt = b_mm * MM_TO_PT

            if do_scale:
                keep = self.keep_ratio.isChecked()
                if keep:
                    # Proportionen beibehalten: kleinerer Faktor, zentriert
                    s  = min(new_w/pw, new_h/ph)
                    cx = (new_w - pw*s) / 2
                    cy = (new_h - ph*s) / 2
                    _apply_ctm(page, s, s, cx, cy)
                else:
                    # Strecken: Inhalt füllt neuen Rahmen exakt
                    _apply_ctm(page, new_w/pw, new_h/ph, 0.0, 0.0)
            else:
                # Nur Rahmen verschieben, Inhalt bleibt
                if abs(l_pt) > 0.01 or abs(b_pt) > 0.01:
                    _apply_ctm(page, 1.0, 1.0, -l_pt, -b_pt)
            page.mediabox = _pik.Array([_pik.Real(0),_pik.Real(0),
                                         _pik.Real(new_w),_pik.Real(new_h)])
            n_changed += 1

        pdf.save(out)
        self.open_result(out, os.path.basename(out))
        return f"{n_changed} Seite(n) bearbeitet."

# ══════════════════════════════════════════════════════════════════════════════
# PAGE NUMBERS
# ══════════════════════════════════════════════════════════════════════════════
class PageNumbersPanel(BasePanel):
    TITLE         = "Seitenzahlen"
    SUBTITLE      = "Seitenzahlen auf jede Seite stempeln."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        ob = QGroupBox(tr("EINSTELLUNGEN")); ol = QVBoxLayout(ob)

        self.pos = QComboBox()
        self.pos.addItems([tr("Unten Mitte"), tr("Unten Links"), tr("Unten Rechts"),
                           tr("Oben Mitte"),  tr("Oben Links"),  tr("Oben Rechts")])
        ol.addLayout(row(tr("Position:"), self.pos))

        self.prefix = QLineEdit(); self.prefix.setPlaceholderText("z.B. Seite ")
        ol.addLayout(row(tr("Praefix:"), self.prefix))

        self.suffix = QLineEdit(); self.suffix.setPlaceholderText("z.B.  / {gesamt}")
        ol.addLayout(row(tr("Suffix:"), self.suffix))

        self.start_spin = QSpinBox(); self.start_spin.setRange(0,9999); self.start_spin.setValue(1)
        ol.addLayout(row(tr("Startnummer:"), self.start_spin))

        self.skip_spin = QSpinBox(); self.skip_spin.setRange(0,999); self.skip_spin.setValue(0)
        ol.addLayout(row(tr("Erste N Seiten ueberspringen:"), self.skip_spin))

        self.font_spin = QSpinBox(); self.font_spin.setRange(5,48); self.font_spin.setValue(10)
        ol.addLayout(row(tr("Schriftgroesse (pt):"), self.font_spin))

        self.margin_spin = QSpinBox(); self.margin_spin.setRange(5,100); self.margin_spin.setValue(20)
        ol.addLayout(row(tr("Abstand vom Rand (pt):"), self.margin_spin))

        layout.addWidget(ob)

    def _run_action(self):
        src = self.require_pdf()
        out = self.save_pdf("PDF mit Seitenzahlen speichern als")
        if not out: raise ValueError("Kein Ausgabepfad.")
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        reader = PdfReader(src); writer = PdfWriter()
        n=len(reader.pages); skip=self.skip_spin.value()
        start=self.start_spin.value(); fs=self.font_spin.value()
        margin=self.margin_spin.value(); pos=self.pos.currentText()
        prefix=self.prefix.text(); suffix=self.suffix.text()
        for i, page in enumerate(reader.pages):
            if i < skip: writer.add_page(page); continue
            num   = start + (i-skip)
            label = prefix + str(num) + suffix.replace("{gesamt}", str(n))
            pw=float(page.mediabox.width); ph=float(page.mediabox.height)
            packet = io.BytesIO()
            c = rl_canvas.Canvas(packet, pagesize=(pw,ph))
            c.setFont("Helvetica", fs)
            y = margin if "Unten" in pos else ph-margin-fs
            x = pw/2 if "Mitte" in pos else (margin if "Links" in pos else pw-margin)
            if "Mitte" in pos: c.drawCentredString(x,y,label)
            elif "Links" in pos: c.drawString(x,y,label)
            else: c.drawRightString(x,y,label)
            c.save(); packet.seek(0)
            from pypdf import PdfReader as PR
            page.merge_page(PR(packet).pages[0]); writer.add_page(page)
        with open(out, "wb") as f: writer.write(f)
        self.open_result(out, "Mit Seitenzahlen")
        return f"Seitenzahlen auf {n-skip} Seiten hinzugefuegt"


# ══════════════════════════════════════════════════════════════════════════════
# IMAGE <-> PDF
# ══════════════════════════════════════════════════════════════════════════════
class ImgPdfPanel(BasePanel):
    TITLE    = "Bild zu/von PDF"
    SUBTITLE = "Bilder in PDF umwandeln oder PDF-Seiten als Bilder exportieren."

    def build_ui(self, layout):
        tb = QGroupBox(tr("BILDER  nach  PDF")); tl = QVBoxLayout(tb)
        tl.addWidget(make_label(tr("Bilder hinzufuegen. Reihenfolge per Drag & Drop."), dim=True))
        self.img_list = FileDropList(extensions=("*.png","*.jpg","*.jpeg","*.tiff","*.tif","*.bmp","*.webp"))
        tl.addWidget(self.img_list)
        br = QHBoxLayout()
        for txt, fn in [(tr("+ Bilder"), self._add_imgs), (tr("- Entfernen"), self.img_list.remove_selected),
                        (tr("Hoch"), self.img_list.move_up), (tr("Runter"), self.img_list.move_down)]:
            b = QPushButton(txt); b.setObjectName("secondaryBtn"); b.clicked.connect(fn); br.addWidget(b)
        br.addStretch(); tl.addLayout(br)
        cb = QPushButton(tr("  In PDF umwandeln...")); cb.setObjectName("actionBtn")
        cb.clicked.connect(self._to_pdf); tl.addWidget(cb)
        layout.addWidget(tb)

        fb = QGroupBox(tr("PDF  nach  BILDER  (aktuell geoeffnete PDF)")); fl = QVBoxLayout(fb)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG","JPEG","TIFF"])
        fl.addLayout(row(tr("Ausgabeformat:"), self.fmt))
        self.dpi = QSpinBox(); self.dpi.setRange(72,600); self.dpi.setValue(300); self.dpi.setSuffix(" dpi")
        fl.addLayout(row(tr("Aufloesung:"), self.dpi))
        eb = QPushButton(tr("  Seiten als Bilder exportieren...")); eb.setObjectName("actionBtn")
        eb.clicked.connect(self._to_img); fl.addWidget(eb)
        layout.addWidget(fb)

    def build_action_row(self, r): pass

    def _add_imgs(self):
        paths = self.pick_images()
        if paths: self.img_list.add_files(paths)

    def _to_pdf(self):
        import img2pdf
        paths = self.img_list.get_paths()
        if not paths: self.log.log("Bilder hinzufuegen.", error=True); return
        out = self.save_pdf("Als PDF speichern")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            with open(out, "wb") as f: f.write(img2pdf.convert(paths))
            self.log.log(f"PDF aus {len(paths)} Bildern erstellt")
            self.open_result(out, "Aus Bildern")
        except Exception as e: self.log.log(str(e), error=True)

    def _to_img(self):
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            from pdf2image import convert_from_path
            fmt=self.fmt.currentText().lower(); ext={"jpeg":"jpg"}.get(fmt,fmt)
            pages=convert_from_path(src, dpi=self.dpi.value())
            stem=os.path.splitext(os.path.basename(src))[0]
            for i,img in enumerate(pages,1):
                QApplication.processEvents()
                img.save(os.path.join(out_dir,f"{stem}_s{i:03d}.{ext}"),fmt.upper())
            self.log.log(f"{len(pages)} Bilder exportiert")
        except Exception as e: self.log.log(str(e), error=True)

    def _run_action(self): pass


# ══════════════════════════════════════════════════════════════════════════════
# GRAYSCALE
# ══════════════════════════════════════════════════════════════════════════════
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
        splitter.setStyleSheet(f"QSplitter::handle{{background:{theme_color('LINE')};}}")

        # ── Left: settings ────────────────────────────────────────────────
        left_w = QWidget()
        left_w.setMinimumWidth(240); left_w.setMaximumWidth(340)
        left_w.setStyleSheet(f"background:{theme_color('PANEL')};")
        left_scroll = QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setStyleSheet(f"QScrollArea{{background:{theme_color('PANEL')};border:none;}}")
        left_scroll.setWidget(left_w)

        left_layout = QVBoxLayout(left_w)
        left_layout.setContentsMargins(12, 12, 12, 10); left_layout.setSpacing(8)

        title_lbl = QLabel(tr(self.TITLE))
        tf = title_lbl.font(); tf.setPointSize(13); tf.setBold(True); title_lbl.setFont(tf)
        left_layout.addWidget(title_lbl)
        left_layout.addWidget(make_separator())

        self.file_bar = CurrentFileBar()
        left_layout.addWidget(self.file_bar)
        left_layout.addWidget(make_separator())

        self.build_ui(left_layout)
        left_layout.addStretch()
        left_layout.addWidget(make_separator())

        self.log = LogBox(tr("Log..."))
        left_layout.addWidget(self.log)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        self.build_action_row(btn_row)
        left_layout.addLayout(btn_row)

        splitter.addWidget(left_scroll)

        # ── Right: preview grid ───────────────────────────────────────────
        right_w = QWidget()
        right_w.setStyleSheet(f"background:{theme_color('BG')};")
        right_layout = QVBoxLayout(right_w)
        right_layout.setContentsMargins(0, 0, 0, 0); right_layout.setSpacing(0)

        zoom_bar = QWidget(); zoom_bar.setFixedHeight(32)
        zoom_bar.setStyleSheet(f"background:{theme_color('SIDE')};border-bottom:1px solid {theme_color('LINE')};")
        zbl = QHBoxLayout(zoom_bar); zbl.setContentsMargins(8, 0, 8, 0); zbl.setSpacing(4)
        for color, text in [("#3a8a3a", tr("Grün = konvertiert")),
                             ("#2176ae", tr("Blau = erzwungen")),
                             ("#e67e22", tr("Orange = übersprungen")),
                             ("#c0392b", tr("Rot = Farbe"))]:
            dot = QLabel("■"); dot.setStyleSheet(f"color:{color};font-size:13px;background:transparent;")
            lbl = QLabel(text); lbl.setStyleSheet(f"color:{theme_color('DIM')};font-size:11px;background:transparent;")
            zbl.addWidget(dot); zbl.addWidget(lbl); zbl.addSpacing(6)
        zbl.addStretch()
        self._card_w = 90
        self._preview_cards = []
        for txt, fn in [("−", self._zoom_out), ("fit", self._zoom_reset), ("+", self._zoom_in)]:
            zb = QPushButton(txt); zb.setFixedSize(32, 22)
            zb.setStyleSheet(
                f"QPushButton{{background:{theme_color('PANEL')};color:{theme_color('TEXT')};"
                f"border:1px solid {theme_color('LINE')};border-radius:3px;font-size:11px;padding:0;}}"
                f"QPushButton:hover{{background:{theme_color('HOVER')};}}")
            zb.clicked.connect(fn); zbl.addWidget(zb)
        right_layout.addWidget(zoom_bar)

        self._preview_scroll = QScrollArea()
        self._preview_scroll.setWidgetResizable(True)
        self._preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._preview_scroll.setStyleSheet(f"QScrollArea{{background:{theme_color('BG')};border:none;}}")
        self._preview_scroll.wheelEvent = self._preview_wheel
        self._preview_scroll.viewport().installEventFilter(self)

        placeholder = QLabel(tr("← PDF öffnen um Vorschau zu laden"))
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(f"color:{theme_color('LINE')};font-size:14px;background:{theme_color('BG')};")
        self._preview_scroll.setWidget(placeholder)
        right_layout.addWidget(self._preview_scroll, 1)

        status_bar = QWidget(); status_bar.setFixedHeight(28)
        status_bar.setStyleSheet(f"background:{theme_color('SIDE')};border-top:1px solid {theme_color('LINE')};")
        sbl = QHBoxLayout(status_bar); sbl.setContentsMargins(12, 0, 12, 0); sbl.setSpacing(20)
        self._status_sw    = QLabel(f"🖤  {tr('SW')}: —")
        self._status_color = QLabel(f"🎨  {tr('Farbe')}: —")
        self._status_total = QLabel(f"{tr('Gesamt')}: —")
        for lbl in [self._status_sw, self._status_color, self._status_total]:
            lbl.setStyleSheet(f"color:{theme_color('DIM')};font-size:11px;background:transparent;")
            sbl.addWidget(lbl)
        sbl.addStretch()
        right_layout.addWidget(status_bar)

        splitter.addWidget(right_w)
        splitter.setSizes([280, 800])
        outer.addWidget(splitter)

    def eventFilter(self, obj, e):
        from PyQt6.QtCore import QEvent
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
        if not self._page_data:
            QTimer.singleShot(200, self._scan)

    def _reclassify(self):
        thr = self.thr.value(); use_ratio = self.mode_ratio.isChecked()
        min_ratio = self.ratio.value() / 20000.0
        self._grey_pages.clear()
        for i, (max_diff, colour_ratio) in enumerate(self._page_data):
            is_colour = (colour_ratio >= min_ratio) if use_ratio else (max_diff > thr)
            if not is_colour: self._grey_pages.add(i)
        self._update_preview_borders(); self._update_status_bar()
        self.log.clear_log()
        self.log.log(f"{len(self._grey_pages)} {tr('Seite(n) werden konvertiert')}, "
                     f"{len(self._page_data)-len(self._grey_pages)} {tr('bleiben unveraendert')}")

    def _build_preview(self, n_pages):
        from PyQt6.QtWidgets import QGridLayout
        container = QWidget()
        container.setStyleSheet(f"background:{theme_color('BG')};")
        self._preview_scroll.setWidget(container)
        self._preview_cards = []
        CARD_W = self._card_w; CARD_H = int(CARD_W * (127/90)); GAP = 8; MARGIN = 10
        vp_w = self._preview_scroll.viewport().width() or 600
        cols = max(2, (vp_w - 2*MARGIN + GAP) // (CARD_W + 12 + GAP))
        grid = QGridLayout(container)
        grid.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN); grid.setSpacing(GAP)
        for i in range(n_pages):
            frame = QFrame(); frame.setFixedSize(CARD_W + 12, CARD_H + 24)
            frame.setStyleSheet(f"QFrame{{background:transparent;border:2px solid {theme_color('LINE')};border-radius:5px;}}")
            fl = QVBoxLayout(frame); fl.setContentsMargins(3,3,3,2); fl.setSpacing(2)
            img_lbl = QLabel(); img_lbl.setFixedSize(CARD_W, CARD_H)
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_lbl.setStyleSheet(f"background:{theme_color('PANEL')};border:none;")
            fl.addWidget(img_lbl)
            num_lbl = QLabel(str(i+1)); num_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num_lbl.setStyleSheet(f"color:{theme_color('DIM')};font-size:10px;background:transparent;border:none;")
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
        from PyQt6.QtWidgets import QGridLayout
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
            doc = pdfium.PdfDocument(src); n = len(doc)
            self._build_preview(n); QApplication.processEvents()
            thr = self.thr.value()
            for i in range(n):
                QApplication.processEvents()
                with _pdfium_lock:
                    bm = doc[i].render(scale=1)
                    pil = bm.to_pil().convert("RGB").resize((128, 128))
                pixels = list(pil.getdata()); total = len(pixels)
                diffs = [max(abs(r-g), abs(r-b), abs(g-b)) for r,g,b in pixels]
                max_diff = max(diffs)
                colour_ratio = sum(1 for d in diffs if d > thr) / total
                self._page_data.append((max_diff, colour_ratio))
                self.log.log(f"Seite {i+1}: max={max_diff}, farbig={colour_ratio*100:.2f}%")
            doc.close()
            self._reclassify()
            try:
                import pikepdf, re as _re
                pdf = pikepdf.open(src)
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
                pdf.close()
            except Exception: pass
            self._update_preview_borders()
            self._load_preview_pixmaps_async(src)
        except Exception as e: self.log.log(str(e), error=True)

    def _run_action(self):
        src = self.require_pdf()
        if not self._page_data:
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

        import pikepdf
        import pypdfium2 as pdfium

        RENDER_SCALE = 150 / 72
        n_pages = len(self._page_data)

        doc = pdfium.PdfDocument(src)
        src_pdf = pikepdf.open(src)
        try:
            out_pdf = pikepdf.Pdf.new()
            n_conv = 0

            for i in range(n_pages):
                QApplication.processEvents()
                if i not in pages_to_convert:
                    out_pdf.pages.append(src_pdf.pages[i])
                    continue

                with _pdfium_lock:
                    bm = doc[i].render(scale=RENDER_SCALE)
                    pil_img = bm.to_pil()

                img = pil_img.convert('L')
                img_w, img_h = img.size
                pt_w = img_w / RENDER_SCALE
                pt_h = img_h / RENDER_SCALE

                raw = img.tobytes()
                img_xobj = pikepdf.Stream(out_pdf, raw)
                img_xobj['/Subtype']         = pikepdf.Name('/Image')
                img_xobj['/Width']           = img_w
                img_xobj['/Height']          = img_h
                img_xobj['/ColorSpace']      = pikepdf.Name('/DeviceGray')
                img_xobj['/BitsPerComponent']= 8

                content = f'q {pt_w:.4f} 0 0 {pt_h:.4f} 0 0 cm /Im0 Do Q\n'.encode()
                page = pikepdf.Dictionary(
                    Type=pikepdf.Name('/Page'),
                    MediaBox=pikepdf.Array([0, 0, pikepdf.Real(pt_w), pikepdf.Real(pt_h)]),
                    Resources=pikepdf.Dictionary(
                        XObject=pikepdf.Dictionary(Im0=img_xobj)
                    ),
                    Contents=pikepdf.Stream(out_pdf, content),
                )
                out_pdf.pages.append(pikepdf.Page(page))
                n_conv += 1

            out_pdf.save(out)
        finally:
            src_pdf.close()
            doc.close()

        self.open_result(out, "Graustufen")
        return f"{n_conv} {tr('Seite(n) konvertiert')}, {n_pages - n_conv} {tr('unveraendert')}"


# ══════════════════════════════════════════════════════════════════════════════
# IMPOSE
# ══════════════════════════════════════════════════════════════════════════════
class ImposePanel(BasePanel):
    TITLE         = "Broschüre / Ausschießen"
    SUBTITLE      = "Seiten normalisieren und auf größere Bögen anordnen."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        mb = QGroupBox(tr("MODUS")); ml = QVBoxLayout(mb)
        self.mode = QComboBox()
        self.mode.addItems([
            tr("Broschüre / Sattelheftung  (A4 → A3)"),
            tr("2-up  (zwei Seiten nebeneinander)"),
            tr("N-up Raster  (Spalten × Zeilen)"),
        ])
        self.mode.currentIndexChanged.connect(self._on_mode_changed)
        ml.addLayout(row(tr("Modus:"), self.mode))
        self.cols = QSpinBox(); self.cols.setRange(1, 8); self.cols.setValue(2)
        self.rows_spin = QSpinBox(); self.rows_spin.setRange(1, 8); self.rows_spin.setValue(2)
        self._row_cols = row(tr("Spalten (N-up):"), self.cols)
        self._row_rows = row(tr("Zeilen (N-up):"), self.rows_spin)
        ml.addLayout(self._row_cols)
        ml.addLayout(self._row_rows)
        self.blank = QCheckBox(tr("Fehlende Seiten mit Leerseiten auffüllen"))
        self.blank.setChecked(True); ml.addWidget(self.blank)
        layout.addWidget(mb)

        nb = QGroupBox(tr("SEITEN NORMALISIEREN")); nl = QVBoxLayout(nb)
        nl.addWidget(make_label(tr(
            "Alle Seiten werden zuerst auf dieselbe Größe gebracht\n"
            "(Proportionen bleiben erhalten, Rand wird aufgefüllt)."), dim=True))
        self.norm_check = QCheckBox(tr("Vor dem Ausschießen normalisieren"))
        self.norm_check.setChecked(True); nl.addWidget(self.norm_check)
        self.norm_target = QComboBox()
        self.norm_target.addItems([
            tr("Größte Seite im Dokument  (automatisch)"),
            tr("DIN A4  (210 × 297 mm)"),
            tr("DIN A3  (297 × 420 mm)"),
            tr("DIN A5  (148 × 210 mm)"),
            tr("Letter  (216 × 279 mm)"),
        ])
        nl.addLayout(row(tr("Zielgröße:"), self.norm_target))
        layout.addWidget(nb)
        self._on_mode_changed(0)

    def _on_mode_changed(self, idx):
        nup = (idx == 2)
        for layout in (self._row_cols, self._row_rows):
            for i in range(layout.count()):
                w = layout.itemAt(i).widget()
                if w: w.setVisible(nup)

    def _run_action(self):
        from pypdf import PdfReader, PdfWriter, Transformation
        src = self.require_pdf()
        out = self.save_pdf(tr("Ausgeschossene PDF speichern als"))
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        reader = PdfReader(src)
        pages  = list(reader.pages)
        n_orig = len(pages)
        target_w, target_h = self._get_target_size(pages)
        if self.norm_check.isChecked():
            pages = self._normalize_pages(pages, target_w, target_h)

        mode   = self.mode.currentIndex()
        writer = PdfWriter()
        pw, ph = target_w, target_h

        def place(sheet, src_page, tx, ty, sx=1.0, sy=1.0):
            try:
                sheet.merge_transformed_page(src_page,
                    Transformation().scale(sx, sy).translate(tx=tx, ty=ty))
            except Exception:
                sheet.merge_page(src_page)

        if mode == 0:
            while len(pages) % 4: pages.append(None)
            n = len(pages)
            num_sheets = n // 4
            for k in range(1, num_sheets + 1):
                vs_left  = n - 2*k + 1
                vs_right = 2*k - 2
                rs_left  = 2*k - 1
                rs_right = n - 2*k
                for left_i, right_i in [(vs_left, vs_right), (rs_left, rs_right)]:
                    sheet = writer.add_blank_page(pw * 2, ph)
                    if left_i < n_orig and pages[left_i] is not None:
                        place(sheet, pages[left_i], 0, 0)
                    if right_i < n_orig and pages[right_i] is not None:
                        place(sheet, pages[right_i], pw, 0)
            msg = f"Broschüre: {n} Seiten → {num_sheets * 2} Blätter"

        elif mode == 1:
            if self.blank.isChecked() and len(pages) % 2: pages.append(None)
            for i in range(0, len(pages), 2):
                sheet = writer.add_blank_page(pw * 2, ph)
                if pages[i] is not None: place(sheet, pages[i], 0, 0)
                if i + 1 < len(pages) and pages[i+1] is not None:
                    place(sheet, pages[i+1], pw, 0)
            msg = f"2-up: {math.ceil(len(pages) / 2)} Bögen"

        else:
            cols = self.cols.value(); rows = self.rows_spin.value(); per = cols * rows
            if self.blank.isChecked():
                while len(pages) % per: pages.append(None)
            cell_w = pw / cols; cell_h = ph / rows
            for si in range(0, len(pages), per):
                sheet = writer.add_blank_page(pw, ph)
                for slot in range(per):
                    pi = si + slot
                    if pi >= len(pages) or pages[pi] is None: break
                    place(sheet, pages[pi],
                          (slot % cols) * cell_w,
                          (rows - 1 - slot // cols) * cell_h,
                          1.0 / cols, 1.0 / rows)
            msg = f"{cols}×{rows}-up: {math.ceil(len(pages) / per)} Bögen"

        with open(out, "wb") as f: writer.write(f)
        self.open_result(out, "Ausgeschossen")
        return msg

    def _get_target_size(self, pages):
        idx = self.norm_target.currentIndex()
        sizes = {1: (595.28, 841.89), 2: (841.89, 1190.55),
                 3: (419.53, 595.28), 4: (612.0, 792.0)}
        if idx in sizes: return sizes[idx]
        max_w = max(float(p.mediabox.width)  for p in pages)
        max_h = max(float(p.mediabox.height) for p in pages)
        return max_w, max_h

    def _normalize_pages(self, pages, target_w, target_h):
        from pypdf import PdfWriter, Transformation
        result = []
        for p in pages:
            if p is None: result.append(None); continue
            pw = float(p.mediabox.width); ph = float(p.mediabox.height)
            if pw <= 0 or ph <= 0: result.append(p); continue
            scale = min(target_w / pw, target_h / ph)
            tx = (target_w - pw * scale) / 2
            ty = (target_h - ph * scale) / 2
            w = PdfWriter(); sheet = w.add_blank_page(target_w, target_h)
            try:
                sheet.merge_transformed_page(p,
                    Transformation().scale(scale, scale).translate(tx=tx, ty=ty))
            except Exception:
                sheet.merge_page(p)
            result.append(sheet)
        return result


# ══════════════════════════════════════════════════════════════════════════════
# FORMS
# ══════════════════════════════════════════════════════════════════════════════
class FormsPanel(BasePanel):
    TITLE         = "Formulare / Reduzieren"
    SUBTITLE      = "Formularfelder ausfuellen und einbetten."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        self._fields = {}
        lb=QPushButton(tr("  Felder laden")); lb.setObjectName("secondaryBtn")
        lb.clicked.connect(self._load); layout.addWidget(lb)
        grp=QGroupBox(tr("FORMULARFELDER")); grp_vl=QVBoxLayout(grp); grp_vl.setContentsMargins(4,4,4,4)
        self.form_box=QWidget()
        self.form_vbox=QVBoxLayout(self.form_box); self.form_vbox.setContentsMargins(0,0,0,0)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.form_box); scroll.setMinimumHeight(140)
        grp_vl.addWidget(scroll)
        layout.addWidget(grp)
        self.flatten=QCheckBox(tr("Nach dem Speichern reduzieren (fuer Druck)"))
        self.flatten.setChecked(True); layout.addWidget(self.flatten)

    def _load(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        self._fields.clear()
        while self.form_vbox.count():
            item = self.form_vbox.takeAt(0)
            if item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()
                item.layout().deleteLater()
            elif item.widget():
                item.widget().deleteLater()
        try:
            from pypdf import PdfReader
            fields=PdfReader(src).get_fields()
            if not fields: self.log.log("Keine Formularfelder gefunden."); return
            for name,field in fields.items():
                ft=field.get("/FT",""); val=field.get("/V","")
                if ft=="/Btn":
                    w=QCheckBox(); w.setChecked(str(val) in ("/Yes","/On","Yes","On"))
                else:
                    w=QLineEdit(); w.setText(str(val) if val else "")
                self._fields[name]=w
                self.form_vbox.addLayout(row(name, w))
            self.log.log(f"{len(fields)} Feld(er) geladen.")
        except Exception as e: self.log.log(str(e),error=True)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("Ausgefuelltes Formular speichern als")
        if not out: raise ValueError("Kein Ausgabepfad.")
        from pypdf import PdfReader, PdfWriter
        reader=PdfReader(src); writer=PdfWriter(); writer.append(reader)
        data={name: ("/Yes" if (isinstance(w,QCheckBox) and w.isChecked()) else
                     "/Off" if isinstance(w,QCheckBox) else w.text())
              for name,w in self._fields.items()}
        for page in writer.pages: writer.update_page_form_field_values(page,data)
        if self.flatten.isChecked():
            for page in writer.pages:
                if "/Annots" in page: del page["/Annots"]
        with open(out,"wb") as f: writer.write(f)
        self.open_result(out,"Formular ausgefuellt")
        return f"Formular ausgefuellt ({len(data)} Felder)"


# ══════════════════════════════════════════════════════════════════════════════
# OCR
# ══════════════════════════════════════════════════════════════════════════════
class _OcrWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, str)   # (out_path, summary)
    failed   = pyqtSignal(str)

    def __init__(self, src, out, lang, deskew, skip):
        super().__init__()
        self.src=src; self.out=out; self.lang=lang
        self.deskew=deskew; self.skip=skip

    def run(self):
        try:
            if shutil.which("ocrmypdf"):
                self.progress.emit("Starte ocrmypdf …")
                cmd=["ocrmypdf","--language",self.lang,"--output-type","pdfa"]
                if self.deskew: cmd.append("--deskew")
                if self.skip:   cmd.append("--skip-text")
                cmd+=[self.src, self.out]
                r=subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if r.returncode not in (0, 6):
                    self.failed.emit(r.stderr.strip() or r.stdout.strip()); return
                self.finished.emit(self.out, f"OCR abgeschlossen ({self.lang})")

            elif shutil.which("tesseract"):
                from pdf2image import convert_from_path
                import pytesseract
                self.progress.emit("Rendere Seiten …")
                images = convert_from_path(self.src, dpi=300)
                txt = ""
                for i, img in enumerate(images):
                    self.progress.emit(f"OCR Seite {i+1} / {len(images)} …")
                    txt += f"--- Seite {i+1} ---\n"
                    txt += pytesseract.image_to_string(img, lang=self.lang) + "\n\n"
                out_txt = os.path.splitext(self.out)[0] + ".txt"
                with open(out_txt, "w", encoding="utf-8") as f:
                    f.write(txt)
                self.finished.emit(out_txt, f"OCR abgeschlossen — Text gespeichert ({self.lang})")
            else:
                self.failed.emit(
                    "Kein OCR-Programm gefunden.\n"
                    "Installation:  pip install ocrmypdf --break-system-packages\n"
                    "           oder:  sudo pacman -S tesseract tesseract-data-deu")
        except Exception as e:
            self.failed.emit(str(e))


class OcrPanel(BasePanel):
    TITLE         = "OCR -- Texterkennung"
    SUBTITLE      = "Gescannte PDFs durchsuchbar machen."
    OPENS_NEW_TAB = True

    def __init__(self, parent=None):
        self._worker = None
        self._async_running = False
        super().__init__(parent)

    def build_ui(self, layout):
        has_ocr  = shutil.which("ocrmypdf") is not None
        has_tess = shutil.which("tesseract") is not None
        layout.addWidget(make_label(
            ("✓  ocrmypdf" if has_ocr  else "✗  ocrmypdf fehlt  →  pip install ocrmypdf --break-system-packages") + "\n" +
            ("✓  tesseract" if has_tess else "✗  tesseract fehlt  →  sudo pacman -S tesseract tesseract-data-deu"),
            dim=True))
        ob = QGroupBox(tr("EINSTELLUNGEN")); ol = QVBoxLayout(ob)
        self.lang = QComboBox()
        for name, code in [(tr("Deutsch (deu)"), "deu"), (tr("Englisch (eng)"), "eng"),
                            (tr("Deutsch + Englisch"), "deu+eng"), ("Französisch", "fra")]:
            self.lang.addItem(name, code)
        ol.addLayout(row(tr("Sprache:"), self.lang))
        self.deskew = QCheckBox(tr("Seiten begradigen")); self.deskew.setChecked(True); ol.addWidget(self.deskew)
        self.skip   = QCheckBox(tr("Seiten mit Text ueberspringen")); self.skip.setChecked(True); ol.addWidget(self.skip)
        layout.addWidget(ob)

        # Fortschrittsanzeige
        self.progress_lbl = make_label("", dim=True)
        layout.addWidget(self.progress_lbl)

    def _run_action(self):
        # Wird von _safe_run aufgerufen — wir starten nur den Worker
        # und geben sofort zurück (kein Blockieren)
        src  = self.require_pdf()
        out  = self.save_pdf("OCR-PDF speichern als")
        if not out: raise ValueError("Kein Ausgabepfad.")

        if self._worker and self._worker.isRunning():
            raise RuntimeError("OCR läuft bereits — bitte warten.")

        self._worker = _OcrWorker(
            src, out,
            lang   = self.lang.currentData(),
            deskew = self.deskew.isChecked(),
            skip   = self.skip.isChecked(),
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

        self._async_running = True
        self.progress_lbl.setText("OCR läuft …")
        return None

    def _on_progress(self, msg):
        self.progress_lbl.setText(msg)
        self.log.log(msg)

    def _on_finished(self, out_path, summary):
        self._async_running = False
        self.progress_lbl.setText("Fertig.")
        self.log.log(summary)
        self.run_btn.setEnabled(True)
        if out_path.endswith(".txt"):
            self.log.log(f"Text gespeichert: {out_path}")
        else:
            self.open_result(out_path, "OCR")

    def _on_failed(self, msg):
        self._async_running = False
        self.progress_lbl.setText("Fehler.")
        self.log.log(msg, error=True)
        self.run_btn.setEnabled(True)


# ══════════════════════════════════════════════════════════════════════════════
# PREFLIGHT
# ══════════════════════════════════════════════════════════════════════════════
class PreflightPanel(BasePanel):
    TITLE    = "Druckvorstufenpruefung"
    SUBTITLE = "PDF auf Drucktauglichkeit pruefen."

    def build_ui(self, layout):
        cb=QGroupBox(tr("PRUEFUNGEN")); cl=QVBoxLayout(cb)
        self.size_combo=QComboBox(); self.size_combo.addItem(tr("Beliebige Groesse"))
        self.size_combo.addItems(list(PAPER_SIZES_PT.keys()))
        cl.addLayout(row(tr("Erwartetes Format:"), self.size_combo))
        self.chk_size=QCheckBox(tr("Seitenformat korrekt")); self.chk_size.setChecked(True); cl.addWidget(self.chk_size)
        self.chk_orient=QCheckBox(tr("Einheitliche Ausrichtung")); self.chk_orient.setChecked(True); cl.addWidget(self.chk_orient)
        self.chk_colour=QCheckBox(tr("Farbige Seiten erkennen")); self.chk_colour.setChecked(True); cl.addWidget(self.chk_colour)
        self.chk_enc=QCheckBox(tr("Nicht passwortgeschuetzt")); self.chk_enc.setChecked(True); cl.addWidget(self.chk_enc)
        layout.addWidget(cb)
        layout.addWidget(make_label(tr("Bericht:"), dim=True))
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMinimumHeight(180)
        self.report.setPlaceholderText(tr("Pruefung starten...")); layout.addWidget(self.report)

    def _run_action(self):
        src=self.require_pdf()
        from pypdf import PdfReader; import pypdfium2 as pdfium
        reader=PdfReader(src); doc=pdfium.PdfDocument(src)
        try:
            n=len(reader.pages); issues=[]; oks=[]
            if self.chk_enc.isChecked():
                if reader.is_encrypted: issues.append("PDF ist passwortgeschuetzt")
                else: oks.append("Nicht verschluesselt")
            target=PAPER_SIZES_PT.get(self.size_combo.currentText())
            orients=[]; colour_pages=[]
            for i in range(n):
                QApplication.processEvents()
                page=reader.pages[i]; pw=float(page.mediabox.width); ph=float(page.mediabox.height)
                orients.append("Q" if pw>ph else "H")
                if self.chk_size.isChecked() and target:
                    tw,th=target
                    if not ((abs(pw-tw)<5 and abs(ph-th)<5) or (abs(pw-th)<5 and abs(ph-tw)<5)):
                        issues.append(f"Seite {i+1}: {pw:.0f}x{ph:.0f}pt != {tw:.0f}x{th:.0f}pt")
                if self.chk_colour.isChecked():
                    with _pdfium_lock:
                        bm=doc[i].render(scale=1); pil=bm.to_pil().convert("RGB").resize((64,64))
                    if any(max(abs(r-g),abs(r-b),abs(g-b))>20 for r,g,b in pil.getdata()):
                        colour_pages.append(i+1)
            if self.chk_orient.isChecked() and orients:
                if len(set(orients))>1: issues.append(f"Gemischte Ausrichtungen")
                else: oks.append(f"Einheitlich: {'Hochformat' if orients[0]=='H' else 'Querformat'}")
            if self.chk_colour.isChecked():
                if colour_pages: issues.append(f"Farbseiten: {colour_pages[:10]}")
                else: oks.append("Keine Farbseiten erkannt")
            lines=[f"BERICHT -- {os.path.basename(src)}",f"Seiten: {n}  |  {os.path.getsize(src)//1024} KB",""]
            if issues: lines+=[f"PROBLEME ({len(issues)}):"]+ [f"  x  {x}" for x in issues]+[""]
            else: lines.append("BESTANDEN -- Datei scheint druckfertig.")
            if oks: lines+=["BESTANDEN:"]+[f"  v  {x}" for x in oks]
            self.report.setPlainText("\n".join(lines))
        finally:
            doc.close()
        return f"{'BESTANDEN' if not issues else f'FEHLER ({len(issues)} Problem(e))'}"


# ══════════════════════════════════════════════════════════════════════════════
# LAYERS
# ══════════════════════════════════════════════════════════════════════════════
class LayersPanel(BasePanel):
    TITLE         = "Ebenen (OCG)"
    SUBTITLE      = "Optionale Inhaltsgruppen steuern."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        self._cbs=[]
        tb=QHBoxLayout()
        for txt,fn in [(tr("Ebenen laden"),self._load),(tr("Alle AN"),lambda:self._all(True)),(tr("Alle AUS"),lambda:self._all(False))]:
            b=QPushButton(txt); b.setObjectName("secondaryBtn"); b.clicked.connect(fn); tb.addWidget(b)
        tb.addStretch(); layout.addLayout(tb)
        scroll_content=QWidget(); self.cb_layout=QVBoxLayout(scroll_content)
        self.no_layers=make_label(tr("Keine Ebenen gefunden."),dim=True); self.cb_layout.addWidget(self.no_layers)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(scroll_content); scroll.setMinimumHeight(130)
        layout.addWidget(scroll)
        self.flatten=QCheckBox(tr("Ausgabe reduzieren")); self.flatten.setChecked(True); layout.addWidget(self.flatten)

    def _load(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        for _,cb in self._cbs: cb.deleteLater()
        self._cbs.clear()
        try:
            from pypdf import PdfReader; from pypdf.generic import ArrayObject
            reader=PdfReader(src); root=reader.trailer["/Root"]; oc=root.get("/OCProperties")
            if not oc: self.no_layers.setVisible(True); self.log.log("Keine Ebenen gefunden."); return
            self.no_layers.setVisible(False)
            for ref in oc.get_object().get("/OCGs",ArrayObject()):
                obj=ref.get_object(); name=str(obj.get("/Name","(unbenannt)"))
                cb=QCheckBox("  "+name); cb.setChecked(True)
                self.cb_layout.addWidget(cb); self._cbs.append((ref,cb))
            self.log.log(f"{len(self._cbs)} Ebene(n) gefunden.")
        except Exception as e: self.log.log(str(e),error=True)

    def _all(self,state):
        for _,cb in self._cbs: cb.setChecked(state)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("PDF speichern als")
        if not out: raise ValueError("Kein Ausgabepfad.")
        if self.flatten.isChecked() and shutil.which("gs"):
            cmd=["gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.5",
                 "-dNOPAUSE","-dBATCH","-dQUIET",f"-sOutputFile={out}",src]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=300)
            if r.returncode!=0: raise RuntimeError(r.stderr.strip())
        else:
            from pypdf import PdfReader,PdfWriter
            writer=PdfWriter(); writer.append(PdfReader(src))
            with open(out,"wb") as f: writer.write(f)
        self.open_result(out,"Ebenen reduziert")
        return "Ebenen verarbeitet"


# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PROFILE
# ══════════════════════════════════════════════════════════════════════════════
class ColourProfilePanel(BasePanel):
    TITLE         = "Farbprofil / CMYK"
    SUBTITLE      = "ICC-Profile pruefen und in CMYK umwandeln."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        ib=QPushButton(tr("  Farbprofil pruefen")); ib.setObjectName("secondaryBtn")
        ib.clicked.connect(self._inspect); layout.addWidget(ib)
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMaximumHeight(150)
        self.report.setPlaceholderText(tr("Farbprofil-Info erscheint hier...")); layout.addWidget(self.report)

        cb=QGroupBox(tr("IN CMYK UMWANDELN")); cl=QVBoxLayout(cb)
        cl.addWidget(make_label(tr(
            "Konvertiert via Ghostscript nach DeviceCMYK. "
            "Qualitaetsstufe: Prepress (hoechste Qualitaet, alle Fonts eingebettet)."), dim=True))
        gs_ok = bool(shutil.which("gs"))
        status = tr("✓  Ghostscript verfuegbar") if gs_ok else tr("✗  Ghostscript fehlt  →  sudo pacman -S ghostscript")
        cl.addWidget(make_label(status, dim=True))
        layout.addWidget(cb)

    def _find_icc(self):
        """Sucht das FOGRA39-ICC-Profil. Gibt Pfad zurück oder None."""
        import glob
        candidates = [
            # Von install.sh installiert
            os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/ISOcoated_v2_eci.icc"),
            os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/ISOcoated_v2_300_eci.icc"),
            # System-Pfade
            "/usr/share/color/icc/ISOcoated_v2_eci.icc",
            "/usr/share/color/icc/colord/ISOcoated_v2_eci.icc",
        ]
        # Alle .icc-Dateien im icc-Verzeichnis der App
        app_icc = os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/")
        candidates += glob.glob(os.path.join(app_icc, "*.icc"))
        return next((p for p in candidates if os.path.isfile(p)), None)

    def _inspect(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        try:
            import pikepdf
            CS_MAP={
                "/DeviceRGB":"RGB", "/DeviceCMYK":"CMYK", "/DeviceGray":"Graustufen",
                "/CalRGB":"Kal. RGB", "/CalGray":"Kal. Grau", "/ICCBased":"ICC",
                "/Lab":"CIE Lab", "/Separation":"Sonderfarbe", "/DeviceN":"DeviceN",
            }
            pdf=pikepdf.open(src)
            found=set()

            def _cs_name(obj):
                try:
                    if isinstance(obj, pikepdf.Array): return str(obj[0])
                    return str(obj)
                except Exception: return ""

            def _scan(res):
                if res is None: return
                if "/ColorSpace" in res:
                    cs_dict=res["/ColorSpace"]
                    if isinstance(cs_dict, pikepdf.Dictionary):
                        for v in cs_dict.values():
                            n=_cs_name(v)
                            if n in CS_MAP: found.add(n)
                    else:
                        n=_cs_name(cs_dict)
                        if n in CS_MAP: found.add(n)
                if "/XObject" in res:
                    xobj=res["/XObject"]
                    if isinstance(xobj, pikepdf.Dictionary):
                        for v in xobj.values():
                            try:
                                if v.get("/Subtype")==pikepdf.Name("/Image") and "/ColorSpace" in v:
                                    n=_cs_name(v["/ColorSpace"])
                                    if n in CS_MAP: found.add(n)
                                elif v.get("/Subtype")==pikepdf.Name("/Form"):
                                    if "/Resources" in v: _scan(v["/Resources"])
                            except Exception: pass

            for page in pdf.pages:
                res=page.get("/Resources")
                if res: _scan(res)
                # Content Stream scannen für direkte Farboperatoren (GS-Vektoren)
                if not found or found == {"/DeviceGray"}:
                    try:
                        import re
                        stream_bytes = b""
                        contents = page.get("/Contents")
                        if contents is not None:
                            if isinstance(contents, pikepdf.Array):
                                for c in contents: stream_bytes += bytes(c.read_bytes())
                            else:
                                stream_bytes = bytes(contents.read_bytes())
                        text = stream_bytes.decode("latin-1", errors="replace")
                        if re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[kK]\b', text):
                            found.add("/DeviceCMYK")
                        if re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+r[gG]\b', text):
                            found.add("/DeviceRGB")
                    except Exception: pass
            n_pages = len(pdf.pages)
            pdf.close()

            readable=[CS_MAP.get(c, c) for c in found]
            is_cmyk="/DeviceCMYK" in found
            is_rgb=bool(found & {"/DeviceRGB","/CalRGB","/ICCBased"})
            lines=[
                f"Datei:   {os.path.basename(src)}",
                f"Seiten:  {n_pages}",
                "",
                f"Farbraum: {', '.join(sorted(readable)) if readable else 'nicht erkennbar'}",
                "",
            ]
            if is_cmyk and not is_rgb:
                lines.append("✓  CMYK — druckfertig.")
            elif is_rgb and is_cmyk:
                lines.append("⚠  Gemischt (RGB + CMYK) — vor Profidruck vollständig in CMYK umwandeln.")
            elif is_rgb:
                lines.append("⚠  RGB — vor Profidruck in CMYK umwandeln.")
            else:
                lines.append("ℹ  Farbraum nicht eindeutig erkennbar.")
            self.report.setPlainText("\n".join(lines))
            self.log.log("Pruefung abgeschlossen.")
        except Exception as e:
            self.log.log(str(e), error=True)

    def _run_action(self):
        import subprocess as sp

        src = self.require_pdf()

        if not shutil.which("gs"):
            raise RuntimeError(
                "Ghostscript nicht gefunden.\n"
                "Installation:  sudo pacman -S ghostscript")

        out = self.save_pdf("CMYK-PDF speichern als")
        if not out: raise ValueError("Kein Ausgabepfad angegeben.")

        # Bewährter GS-Befehl für RGB→CMYK ohne ICC-Profil-Problematik.
        # -dEncodeColorImages=false / -dEncodeGrayImages=false verhindert
        # Neukomprimierung und Qualitätsverlust bei Bildern.
        # -dPDFSETTINGS=/prepress: höchste Qualität, Fonts eingebettet.
        cmd = [
            "gs",
            "-o", out,
            "-sDEVICE=pdfwrite",
            "-dPDFSETTINGS=/prepress",
            "-dEncodeColorImages=false",
            "-dEncodeGrayImages=false",
            "-dEncodeMonoImages=false",
            "-sProcessColorModel=DeviceCMYK",
            "-sColorConversionStrategy=CMYK",
            "-sColorConversionStrategyForImages=CMYK",
            src,
        ]

        r = sp.run(cmd, capture_output=True, text=True, timeout=300)

        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip())[:500]
            raise RuntimeError(f"Ghostscript-Fehler:\n{err}")

        # Ergebnis verifizieren
        try:
            import pikepdf
            found_rgb = False
            pdf_out = pikepdf.open(out)
            for page in pdf_out.pages:
                res = page.get("/Resources")
                if not res: continue
                xobj = res.get("/XObject")
                if xobj and isinstance(xobj, pikepdf.Dictionary):
                    for v in xobj.values():
                        try:
                            if v.get("/Subtype") == pikepdf.Name("/Image"):
                                cs = v.get("/ColorSpace")
                                if cs:
                                    name = str(cs[0]) if isinstance(cs, pikepdf.Array) else str(cs)
                                    if name in ("/DeviceRGB", "/CalRGB"):
                                        found_rgb = True
                        except Exception:
                            pass
            pdf_out.close()
            verify = "⚠  Einige RGB-Bilder noch vorhanden (eingebettete Profile)." if found_rgb else "✓  Farbraum erfolgreich in CMYK konvertiert."
        except Exception:
            verify = "(Verifikation nicht möglich)"

        self.open_result(out, "CMYK konvertiert")
        return f"Konvertierung abgeschlossen.\n{verify}"


def _fmt(b):
    if b<1024: return f"{b} B"
    elif b<1024**2: return f"{b/1024:.1f} KB"
    else: return f"{b/1024**2:.2f} MB"


# ══════════════════════════════════════════════════════════════════════════════
# N-UP LAYOUT
# ══════════════════════════════════════════════════════════════════════════════
class _NUpWorker(QThread):
    """Builds the N-Up PDF off the UI thread so large/heavy documents don't
    freeze the window. Mirrors the _OcrWorker / ConvertWorker pattern: only
    plain data crosses the thread boundary (paths, page-index list, numeric
    layout params) — all pypdf objects are created and used inside run()."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(str, str)   # (out_path, summary)
    failed   = pyqtSignal(str)

    def __init__(self, src, out, src_pages, params, rotate_src, n_slot):
        super().__init__()
        self.src = src; self.out = out
        self.src_pages = src_pages
        self.params = params
        self.rotate_src = rotate_src
        self.n_slot = n_slot

    def run(self):
        try:
            from pypdf import PdfReader, PdfWriter, Transformation
            (out_w, out_h, mt, mb, ml, mr, gh, gv,
             slot_w, slot_h, cols, rows) = self.params
            reader = PdfReader(self.src)
            writer = PdfWriter()
            src_pages = self.src_pages
            n_slot   = self.n_slot
            n_sheets = math.ceil(len(src_pages) / n_slot)
            placed   = 0
            for sheet_i in range(n_sheets):
                self.progress.emit(f"{tr('Blatt')} {sheet_i+1} / {n_sheets} …")
                sheet = writer.add_blank_page(out_w, out_h)
                for slot_i in range(n_slot):
                    page_i = sheet_i * n_slot + slot_i
                    if page_i >= len(src_pages): break
                    src_pi = src_pages[page_i]
                    if src_pi is None: continue
                    src_page = _normalized_page(reader.pages[src_pi])
                    pw = float(src_page.mediabox.width); ph = float(src_page.mediabox.height)
                    if self.rotate_src:
                        src_page = _normalized_page(src_page.rotate(90))
                        pw, ph = float(src_page.mediabox.width), float(src_page.mediabox.height)
                    scale = min(slot_w / pw, slot_h / ph)
                    scaled_w = pw * scale; scaled_h = ph * scale
                    col_i = slot_i % cols; row_i = slot_i // cols
                    slot_x = ml + col_i * (slot_w + gh)
                    slot_y = out_h - mt - (row_i + 1) * slot_h - row_i * gv
                    tx = slot_x + (slot_w - scaled_w) / 2
                    ty = slot_y + (slot_h - scaled_h) / 2
                    sheet.merge_transformed_page(src_page,
                        Transformation().scale(scale, scale).translate(tx=tx, ty=ty), over=True)
                    placed += 1
            self.progress.emit(tr("Schreibe Datei …"))
            writer.write(self.out)
            self.finished.emit(
                self.out,
                f"Fertig. {placed} Seiten auf {n_sheets} Blatt ({cols}×{rows}).")
        except Exception as e:
            self.failed.emit(str(e))


class NUpPanel(BasePanel):
    TITLE         = "N-Up Layout"
    SUBTITLE      = "Mehrere Seiten auf einem Blatt — mit Rand- und Abstandssteuerung."
    OPENS_NEW_TAB = True

    def __init__(self, parent=None):
        self._worker = None
        self._async_running = False
        super().__init__(parent)

    def _setup(self):
        from PyQt6.QtCore import Qt as _Qt
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        splitter = QSplitter(_Qt.Orientation.Horizontal)
        splitter.setStyleSheet(f"QSplitter::handle{{background:{theme_color('LINE')};width:2px;}}")

        left_w = QWidget()
        left_w.setMinimumWidth(380)
        left_w.setStyleSheet(f"background:{theme_color('PANEL')};")
        left_scroll_nup = QScrollArea()
        left_scroll_nup.setWidgetResizable(True)
        left_scroll_nup.setFrameShape(QFrame.Shape.NoFrame)
        left_scroll_nup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll_nup.setStyleSheet(f"QScrollArea{{background:{theme_color('PANEL')};border:none;}}")
        left_scroll_nup.setWidget(left_w)
        left_scroll_nup.setMinimumWidth(380)
        left_l = QVBoxLayout(left_w)
        left_l.setContentsMargins(10, 12, 10, 10); left_l.setSpacing(8)

        title_lbl = QLabel(tr("N-Up Layout"))
        tf = title_lbl.font(); tf.setPointSize(13); tf.setBold(True); title_lbl.setFont(tf)
        left_l.addWidget(title_lbl)
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator"); left_l.addWidget(sep)

        self.build_ui(left_l)
        left_l.addStretch()

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setObjectName("separator"); left_l.addWidget(sep2)
        self.log = LogBox(tr("Log...")); left_l.addWidget(self.log)

        self.run_btn = QPushButton(tr("N-Up erstellen"))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.clicked.connect(self._safe_run)
        left_l.addWidget(self.run_btn)

        sc  = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        sc.activated.connect(self._enter_pressed)
        sc2 = QShortcut(QKeySequence(Qt.Key.Key_Enter), self)
        sc2.activated.connect(self._enter_pressed)

        splitter.addWidget(left_scroll_nup)

        right_w = QWidget(); right_w.setStyleSheet(f"background:{theme_color('BG')};")
        right_l = QVBoxLayout(right_w)
        right_l.setContentsMargins(6, 6, 6, 6); right_l.setSpacing(3)

        hdr_row = QHBoxLayout()
        hdr = QLabel(tr("Vorschau (erstes Blatt)")); hdr.setObjectName("dimLabel")
        hdr_row.addStretch(); hdr_row.addWidget(hdr); hdr_row.addStretch()
        zoom_out_btn = QPushButton("−"); zoom_out_btn.setFixedSize(22, 22); zoom_out_btn.setObjectName("secondaryBtn")
        self._zoom_lbl = QLabel("100%"); self._zoom_lbl.setObjectName("dimLabel")
        self._zoom_lbl.setFixedWidth(38); self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_in_btn  = QPushButton("+"); zoom_in_btn.setFixedSize(22, 22); zoom_in_btn.setObjectName("secondaryBtn")
        zoom_rst_btn = QPushButton("⟳"); zoom_rst_btn.setFixedSize(22, 22); zoom_rst_btn.setObjectName("secondaryBtn")
        hdr_row.addWidget(zoom_out_btn); hdr_row.addWidget(self._zoom_lbl)
        hdr_row.addWidget(zoom_in_btn); hdr_row.addWidget(zoom_rst_btn)
        right_l.addLayout(hdr_row)

        self._preview_zoom = 1.0
        def _zi(): self._preview_zoom = min(self._preview_zoom * 1.25, 8.0); self._zoom_lbl.setText(f"{int(self._preview_zoom*100)}%"); self._update_preview()
        def _zo(): self._preview_zoom = max(self._preview_zoom / 1.25, 0.2); self._zoom_lbl.setText(f"{int(self._preview_zoom*100)}%"); self._update_preview()
        def _zr(): self._preview_zoom = 1.0; self._zoom_lbl.setText("100%"); self._update_preview()
        zoom_in_btn.clicked.connect(_zi); zoom_out_btn.clicked.connect(_zo); zoom_rst_btn.clicked.connect(_zr)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._preview.setStyleSheet(f"background:{theme_color('BG')};")
        self._preview.installEventFilter(self)
        right_l.addWidget(self._preview, 1)

        self._preview_info = QLabel("")
        self._preview_info.setObjectName("dimLabel")
        self._preview_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_l.addWidget(self._preview_info)

        splitter.addWidget(right_w)
        splitter.setStretchFactor(0, 0); splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 700])
        outer.addWidget(splitter, 1)
        QTimer.singleShot(400, self._update_preview)

        # Keep the "current page" preview in sync with "Seiten verwalten".
        AppState.get().current_page_changed.connect(lambda *_: self._update_preview())
        AppState.get().pdf_changed.connect(lambda *_: self._update_preview())

    def showEvent(self, event):
        super().showEvent(event)
        self._update_preview()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._preview and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                delta = event.angleDelta().y()
                self._preview_zoom = min(self._preview_zoom * 1.15, 8.0) if delta > 0 else max(self._preview_zoom / 1.15, 0.2)
                self._zoom_lbl.setText(f"{int(self._preview_zoom * 100)}%")
                self._update_preview()
                return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(50, self._update_preview)

    def build_ui(self, layout):
        def mm_spin(val=0):
            s = QDoubleSpinBox()
            s.setRange(0, 500); s.setSuffix(" mm")
            s.setDecimals(1); s.setValue(val); s.setFixedWidth(90)
            s.valueChanged.connect(self._update_preview)
            return s

        sb = QGroupBox(tr("QUELLSEITEN")); sl = QVBoxLayout(sb)
        self.src_combo = QComboBox()
        self.src_combo.addItems([tr("Alle Seiten der Reihe nach"),
                                  tr("Nur aktuelle Seite (wiederholt)"),
                                  tr("Seitenbereich:")])
        self.src_combo.currentIndexChanged.connect(self._on_src_changed)
        self.src_combo.currentIndexChanged.connect(self._update_preview)
        sl.addLayout(row(tr("Quelle:"), self.src_combo))
        self.src_range = QLineEdit(); self.src_range.setPlaceholderText(tr("z.B.  1-3, 5"))
        self.src_range.setEnabled(False)
        self.src_range.textChanged.connect(self._update_preview)
        sl.addLayout(row(tr("Bereich:"), self.src_range))
        layout.addWidget(sb)

        gb = QGroupBox(tr("RASTER")); gl = QVBoxLayout(gb)
        self.cols = QSpinBox(); self.cols.setRange(1, 10); self.cols.setValue(2); self.cols.valueChanged.connect(self._update_preview)
        self.rows = QSpinBox(); self.rows.setRange(1, 10); self.rows.setValue(2); self.rows.valueChanged.connect(self._update_preview)
        gl.addLayout(row(tr("Spalten:"), self.cols))
        gl.addLayout(row(tr("Zeilen:"), self.rows))
        self.rotate_src = QCheckBox(tr("Quellseite um 90° drehen")); self.rotate_src.toggled.connect(self._update_preview)
        gl.addWidget(self.rotate_src)
        self.blank_fill = QCheckBox(tr("Fehlende Positionen mit Leerseiten auffüllen")); self.blank_fill.setChecked(True)
        gl.addWidget(self.blank_fill)
        layout.addWidget(gb)

        ob = QGroupBox(tr("AUSGABEFORMAT")); ol = QVBoxLayout(ob)
        self.out_fmt = QComboBox()
        self.out_fmt.addItems([tr("DIN A4  (210 × 297 mm)"), tr("DIN A3  (297 × 420 mm)"),
                                tr("DIN A5  (148 × 210 mm)"), tr("Letter  (216 × 279 mm)"),
                                tr("Wie Quellseite × Raster  (automatisch)")])
        self.out_fmt.currentIndexChanged.connect(self._update_preview)
        ol.addLayout(row(tr("Format:"), self.out_fmt))
        self.landscape = QCheckBox(tr("Querformat")); self.landscape.toggled.connect(self._update_preview)
        ol.addWidget(self.landscape)
        layout.addWidget(ob)

        ab = QGroupBox(tr("ABSTÄNDE")); al = QVBoxLayout(ab)
        self.margin_t = mm_spin(5); self.margin_b = mm_spin(5)
        self.margin_l = mm_spin(5); self.margin_r = mm_spin(5)
        self.gap_h    = mm_spin(3); self.gap_v    = mm_spin(3)
        al.addLayout(row(tr("Rand oben:"),   self.margin_t))
        al.addLayout(row(tr("Rand unten:"),  self.margin_b))
        al.addLayout(row(tr("Rand links:"),  self.margin_l))
        al.addLayout(row(tr("Rand rechts:"), self.margin_r))
        al.addLayout(row(tr("Abstand H:"),   self.gap_h))
        al.addLayout(row(tr("Abstand V:"),   self.gap_v))
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
        layout.addWidget(ab)

    def _on_src_changed(self, idx):
        self.src_range.setEnabled(idx == 2)

    def _parse_range(self, text, n_pages):
        pages = []
        for part in text.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-", 1)
                try: pages.extend(range(int(a.strip())-1, int(b.strip())))
                except ValueError: pass
            elif part:
                try: pages.append(int(part)-1)
                except ValueError: pass
        return [p for p in pages if 0 <= p < n_pages]

    def _get_layout_params(self, src_pw, src_ph):
        PT   = MM_TO_PT
        cols = self.cols.value(); rows = self.rows.value()
        fmt_idx = self.out_fmt.currentIndex()
        fmt_map = {0:(210*PT,297*PT), 1:(297*PT,420*PT), 2:(148*PT,210*PT), 3:(216*PT,279*PT)}
        if fmt_idx == 4:
            pw, ph = src_pw, src_ph
            if self.rotate_src.isChecked(): pw, ph = ph, pw
            out_w, out_h = pw * cols, ph * rows
        else:
            out_w, out_h = fmt_map[fmt_idx]
        if self.landscape.isChecked(): out_w, out_h = out_h, out_w
        mt = self.margin_t.value() * PT; mb = self.margin_b.value() * PT
        ml = self.margin_l.value() * PT; mr = self.margin_r.value() * PT
        gh = self.gap_h.value() * PT;    gv = self.gap_v.value() * PT
        slot_w = max(1.0, (out_w - ml - mr - gh * (cols-1)) / cols)
        slot_h = max(1.0, (out_h - mt - mb - gv * (rows-1)) / rows)
        return out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows

    def _update_preview(self):
        pdf_path = AppState.get().current_pdf
        if not pdf_path or not os.path.isfile(pdf_path):
            self._preview.setText(tr("Keine PDF geöffnet")); return
        try:
            import pypdfium2 as pdfium
            from PyQt6.QtGui import QPainter, QPen, QColor, QBrush as _QB
            doc  = pdfium.PdfDocument(pdf_path)
            try:
                n = len(doc)
                src_idx = self.src_combo.currentIndex()
                page_idx = min(AppState.get().current_page, n-1) if src_idx == 1 else 0
                page = doc[page_idx]
                src_pw = page.get_width(); src_ph = page.get_height()
                if self.rotate_src.isChecked(): src_pw, src_ph = src_ph, src_pw
                out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows = \
                    self._get_layout_params(src_pw, src_ph)
                zoom = getattr(self, '_preview_zoom', 1.0)
                avail_w = max(100, self._preview.width()  - 16)
                avail_h = max(100, self._preview.height() - 16)
                cs = min(avail_w / out_w, avail_h / out_h) * zoom
                canvas_w = max(1, int(out_w * cs)); canvas_h = max(1, int(out_h * cs))
                render_scale = max(cs, min(avail_w / src_pw, avail_h / src_ph) * zoom)
                with _pdfium_lock:
                    bm = page.render(scale=render_scale)
                    pil_src = bm.to_pil()
            finally:
                doc.close()
            if self.rotate_src.isChecked(): pil_src = pil_src.rotate(90, expand=True)
            result = QPixmap(canvas_w, canvas_h); result.fill(QColor("#1a2a40"))
            painter = QPainter(result)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setBrush(_QB(QColor("#f5f5f5"))); painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(0, 0, canvas_w - 1, canvas_h - 1)
            buf = io.BytesIO(); pil_src.save(buf, "PNG"); buf.seek(0)
            src_pm = QPixmap.fromImage(QImage.fromData(buf.read()))
            for row_i in range(rows):
                for col_i in range(cols):
                    sx = int((ml + col_i * (slot_w + gh)) * cs)
                    sy = int((mt + row_i * (slot_h + gv)) * cs)
                    sw = max(1, int(slot_w * cs)); sh = max(1, int(slot_h * cs))
                    painter.setBrush(_QB(QColor(200, 210, 230, 60)))
                    painter.setPen(QPen(QColor(120, 140, 180, 120), 1))
                    painter.drawRect(sx, sy, sw, sh)
                    scale   = min(slot_w / src_pw, slot_h / src_ph)
                    scaled_w = max(1, int(src_pw * scale * cs))
                    scaled_h = max(1, int(src_ph * scale * cs))
                    off_x = sx + (sw - scaled_w) // 2; off_y = sy + (sh - scaled_h) // 2
                    pm_s = src_pm.scaled(scaled_w, scaled_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    painter.drawPixmap(off_x, off_y, pm_s)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(QColor("#e94560"), 1))
                    painter.drawRect(off_x, off_y, pm_s.width()-1, pm_s.height()-1)
            painter.setPen(QPen(QColor(120, 160, 255, 180), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(0, 0, canvas_w-1, canvas_h-1)
            painter.end()
            self._preview.setPixmap(result)
            self._preview_info.setText(
                f"{out_w/MM_TO_PT:.0f}×{out_h/MM_TO_PT:.0f} mm  |  {cols}×{rows} = {cols*rows} Slots")
        except Exception as ex:
            self._preview.setText(f"Vorschau: {ex}")

    def _run_action(self):
        # Prepare on the UI thread (cheap: reads widget values + first page size),
        # then hand the heavy merge loop to a worker so big PDFs don't freeze the
        # window. Mirrors the OCR panel's async pattern.
        from pypdf import PdfReader
        src_path = self.require_pdf()
        out_path = self.save_pdf(tr("N-Up PDF speichern als"))
        if not out_path: raise ValueError(tr("Kein Ausgabepfad."))
        if self._worker and self._worker.isRunning():
            raise RuntimeError(tr("N-Up läuft bereits — bitte warten."))
        reader  = PdfReader(src_path); n_total = len(reader.pages)
        cols = self.cols.value(); rows = self.rows.value(); n_slot = cols * rows
        src_idx = self.src_combo.currentIndex()
        if src_idx == 0:
            src_pages = list(range(n_total))
        elif src_idx == 1:
            cur = AppState.get().current_page
            src_pages = [cur if cur < n_total else 0]
        else:
            src_pages = self._parse_range(self.src_range.text(), n_total)
            if not src_pages: raise ValueError(tr("Kein gültiger Seitenbereich."))
        sp = reader.pages[src_pages[0]]
        src_pw = float(sp.mediabox.width); src_ph = float(sp.mediabox.height)
        params = self._get_layout_params(src_pw, src_ph)
        slot_w, slot_h = params[8], params[9]
        if slot_w <= 0 or slot_h <= 0:
            raise ValueError(tr("Abstände zu groß — kein Platz für Inhalt."))
        if len(src_pages) == 1: src_pages = src_pages * n_slot
        if self.blank_fill.isChecked():
            while len(src_pages) % n_slot: src_pages.append(None)

        self._worker = _NUpWorker(src_path, out_path, src_pages, params,
                                  self.rotate_src.isChecked(), n_slot)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()
        self._async_running = True
        self.run_btn.setText(tr("N-Up läuft …"))
        self.log.log(tr("N-Up läuft …"))
        return None

    def _on_progress(self, msg):
        self.log.log(msg)

    def _on_finished(self, out_path, summary):
        self._async_running = False
        self.run_btn.setText(tr("N-Up erstellen"))
        self.run_btn.setEnabled(True)
        self.log.log(summary)
        self.open_result(out_path, "N-Up")

    def _on_failed(self, msg):
        self._async_running = False
        self.run_btn.setText(tr("N-Up erstellen"))
        self.run_btn.setEnabled(True)
        self.log.log(msg, error=True)
