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


def _inherited_rotate(page) -> int:
    """/Rotate of a pikepdf page, following the inheritance chain up the page
    tree (it may live on a /Pages node instead of the page itself)."""
    node = page.obj
    for _ in range(32):
        try:
            if "/Rotate" in node:
                return int(node["/Rotate"]) % 360
            node = node["/Parent"]
        except Exception:
            break
    return 0


def _visible_box(page):
    """The rectangle a viewer actually shows for a pikepdf page: its CropBox
    clipped to the MediaBox (the PDF spec requires that intersection), falling
    back to the MediaBox.

    This matters because qpdf's ``add_overlay`` places TrimBox → CropBox →
    MediaBox *as written*, without clipping. A file whose CropBox is stale or
    larger than its MediaBox — e.g. one the Crop tool resized — would otherwise
    be laid out from a box that has nothing to do with the visible page, and the
    content lands off-centre in its slot."""
    def _rect(o):
        v = [float(x) for x in o]
        return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))
    x0, y0, x1, y1 = _rect(page.mediabox)
    try:
        cx0, cy0, cx1, cy1 = _rect(page.cropbox)
    except Exception:
        return x0, y0, x1, y1
    ix0, iy0 = max(x0, cx0), max(y0, cy0)
    ix1, iy1 = min(x1, cx1), min(y1, cy1)
    if ix1 - ix0 > 1.0 and iy1 - iy0 > 1.0:
        return ix0, iy0, ix1, iy1
    return x0, y0, x1, y1


def _visible_size(page):
    """(width, height) of the page as it is displayed — the visible box with
    /Rotate applied. This is what pdfium reports (and therefore what every tool
    preview draws), so layout maths must use it too."""
    x0, y0, x1, y1 = _visible_box(page)
    w, h = x1 - x0, y1 - y0
    return (h, w) if _inherited_rotate(page) in (90, 270) else (w, h)


def _mat_mul(m, n):
    """Compose two PDF matrices (a b c d e f): apply `m` first, then `n`."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1*a2 + b1*c2,        a1*b2 + b1*d2,
            c1*a2 + d1*c2,        c1*b2 + d1*d2,
            e1*a2 + f1*c2 + e2,   e1*b2 + f1*d2 + f2)


def _display_matrix(box, rot):
    """Matrix mapping a page's visible box into display space: origin at (0, 0)
    and /Rotate applied, i.e. the coordinate system the previews (and every
    viewer) show. Lets a tool do its geometry in the same space the user sees
    instead of in raw MediaBox coordinates."""
    x0, y0, x1, y1 = box
    rot = rot % 360
    if rot == 90:   return (0.0, -1.0, 1.0,  0.0, -y0,  x1)
    if rot == 180:  return (-1.0, 0.0, 0.0, -1.0,  x1,  y1)
    if rot == 270:  return (0.0,  1.0, -1.0, 0.0,  y1, -x0)
    return (1.0, 0.0, 0.0, 1.0, -x0, -y0)


def row(label_text: str, widget, stretch=1, label_w: int = LABEL_W) -> QHBoxLayout:
    """
    Garantiert sichtbares Label + Eingabefeld.
    setFixedWidth wird von Qt immer respektiert — kein Abschneiden.
    label_w kann verkleinert werden, wenn die Beschriftungen kurz sind (z.B. in
    schmalen Tool-Seitenleisten), damit das Eingabefeld nicht eingequetscht wird.
    """
    h = QHBoxLayout()
    h.setSpacing(12)
    lbl = QLabel(label_text)
    lbl.setWordWrap(True)
    lbl.setFixedWidth(label_w)
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
# SHARED PREVIEW PANE
# ══════════════════════════════════════════════════════════════════════════════
class PreviewPane(QWidget):
    """Reusable preview area shared by tools that show a rendered PDF page
    (Crop, N-Up, …). Owns the preview label, the zoom controls + state, the
    Ctrl+wheel zoom, and the refresh wiring (page change / new PDF / on show /
    on resize) — so every tool gets identical preview behaviour and a fix lands
    in one place instead of being copy-pasted per panel.

    The owning panel supplies a single render callback:
        render_fn(avail_w:int, avail_h:int, zoom:float) -> (QPixmap | None, str)
    Return (pixmap, info_text) to display a page, or (None, message) to show a
    text placeholder (e.g. "no PDF open"). Exceptions are caught and shown.
    """
    def __init__(self, render_fn, header="Vorschau", parent=None):
        super().__init__(parent)
        self._render_fn = render_fn
        self.zoom = 1.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6); outer.setSpacing(3)

        hdr_row = QHBoxLayout()
        hdr = QLabel(tr(header)); hdr.setObjectName("dimLabel")
        hdr_row.addStretch(); hdr_row.addWidget(hdr); hdr_row.addStretch()
        # iconBtn, not secondaryBtn: the latter's padding leaves no room for the
        # glyph in a 22px square and the buttons come out blank.
        zoom_out_btn = QPushButton("−"); zoom_out_btn.setFixedSize(24, 24); zoom_out_btn.setObjectName("iconBtn")
        self._zoom_lbl = QLabel("100%"); self._zoom_lbl.setObjectName("dimLabel")
        self._zoom_lbl.setFixedWidth(38); self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_in_btn  = QPushButton("+");  zoom_in_btn.setFixedSize(24, 24);  zoom_in_btn.setObjectName("iconBtn")
        zoom_rst_btn = QPushButton("⟳"); zoom_rst_btn.setFixedSize(24, 24); zoom_rst_btn.setObjectName("iconBtn")
        hdr_row.addWidget(zoom_out_btn); hdr_row.addWidget(self._zoom_lbl)
        hdr_row.addWidget(zoom_in_btn);  hdr_row.addWidget(zoom_rst_btn)
        outer.addLayout(hdr_row)
        zoom_out_btn.clicked.connect(lambda: self._set_zoom(self.zoom / 1.25))
        zoom_in_btn.clicked.connect(lambda: self._set_zoom(self.zoom * 1.25))
        zoom_rst_btn.clicked.connect(lambda: self._set_zoom(1.0))

        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.label.setMouseTracking(True)
        self.label.installEventFilter(self)
        outer.addWidget(self.label, 1)

        self.info = QLabel(""); self.info.setObjectName("dimLabel")
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.info)

        # Refresh whenever the active page or document changes.
        AppState.get().current_page_changed.connect(lambda *_: self.refresh())
        AppState.get().pdf_changed.connect(lambda *_: self.refresh())
        QTimer.singleShot(400, self.refresh)

    def _set_zoom(self, z):
        self.zoom = max(0.2, min(z, 8.0))
        self._zoom_lbl.setText(f"{int(self.zoom * 100)}%")
        self.refresh()

    def refresh(self):
        if not self._render_fn:
            return
        # Only render when this tool is actually on screen. The pane subscribes
        # to current_page_changed, which fires on every page turn in the viewer —
        # rendering a hidden preview there would run pdfium on the GUI thread
        # (under _pdfium_lock) on every scroll, starving the viewer's background
        # pre-render and making scrolling stutter. showEvent refreshes it when
        # the tool is opened, so it's always up to date when visible.
        try:
            if not self.isVisible():
                return
            avail_w = max(100, self.label.width()  - 16)
            avail_h = max(100, self.label.height() - 16)
        except RuntimeError:
            # Underlying C++ widget already destroyed (window closing while a
            # queued refresh or an AppState signal is still in flight).
            return
        try:
            pm, info = self._render_fn(avail_w, avail_h, self.zoom)
        except Exception as ex:
            self.label.setText(tr('Vorschau: {p0}').format(p0=ex))
            return
        if pm is None:
            self.label.setText(info or "")
            self.info.setText("")
        else:
            self.label.setPixmap(pm)
            self.info.setText(info or "")

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(50, self.refresh)

    def eventFilter(self, obj, event):
        if obj is self.label and event.type() == QEvent.Type.Wheel:
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                step = 1.15 if event.angleDelta().y() > 0 else (1 / 1.15)
                self._set_zoom(self.zoom * step)
                return True
        return super().eventFilter(obj, event)


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
        if getattr(self, '_merging', False): return
        self._merging = True
        try:
            self._do_merge_impl()
        finally:
            self._merging = False

    def _do_merge_impl(self):
        from pypdf import PdfWriter, PdfReader
        paths = self.merge_list.get_paths()
        if not paths: self.log.log(tr("Mindestens eine PDF hinzufuegen."), error=True); return
        out = self.save_pdf("Zusammengefuehrte PDF speichern als")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            writer = PdfWriter()
            for p in paths:
                for page in PdfReader(p, strict=False).pages: writer.add_page(page)
            with open(out, "wb") as f: writer.write(f)
            self.log.log(tr('{p0} Dateien zusammengefuehrt').format(p0=len(paths)))
            self.open_result(out, tr("Zusammengefuehrt"))
        except Exception as e: self.log.log(str(e), error=True)

    def _do_split(self):
        if getattr(self, '_splitting', False): return
        self._splitting = True
        try:
            self._do_split_impl()
        finally:
            self._splitting = False

    def _do_split_impl(self):
        from pypdf import PdfReader, PdfWriter
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            reader = PdfReader(src, strict=False); stem = os.path.splitext(os.path.basename(src))[0]
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
            self.log.log(tr('In {p0} Dateien aufgeteilt').format(p0=len(saved)))
            if saved: self.open_result(saved[0], tr('Teil 1 von {p0}').format(p0=len(saved)))
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
            raise ValueError(tr("Kein Ausgabepfad."))

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
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
            if r.returncode != 0:
                raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(
                    p0=(r.stderr or r.stdout or f"exit {r.returncode}")[:400]))
            # -dQUIET means a Ghostscript that fails softly says nothing at all;
            # without this the next line raised FileNotFoundError from
            # getsize() instead of reporting that the compression failed.
            if not os.path.exists(out) or os.path.getsize(out) == 0:
                raise RuntimeError(tr(
                    "Ghostscript hat keine Ausgabedatei erzeugt."))
        else:
            import pikepdf
            pdf = pikepdf.open(src)
            try:
                pdf.save(out, compress_streams=True, recompress_flate=True)
            finally:
                pdf.close()

        size_after = os.path.getsize(out)
        ratio = (1 - size_after / size_before) * 100 if size_before else 0
        self.open_result(out, "Komprimiert")
        return (tr('Fertig. {p0} → {p1}  ({p2:+.1f}%)').format(p0=_fmt(size_before), p1=_fmt(size_after), p2=ratio))

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
        pdf_path = AppState.get().current_pdf
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

        self.prefix = QLineEdit(); self.prefix.setPlaceholderText(tr("z.B. Seite "))
        ol.addLayout(row(tr("Praefix:"), self.prefix))

        self.suffix = QLineEdit(); self.suffix.setPlaceholderText(tr("z.B.  / {gesamt}"))
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
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        reader = PdfReader(src, strict=False); writer = PdfWriter()
        n=len(reader.pages); skip=self.skip_spin.value()
        start=self.start_spin.value(); fs=self.font_spin.value()
        margin=self.margin_spin.value(); pos_idx=self.pos.currentIndex()
        prefix=self.prefix.text(); suffix=self.suffix.text()
        for i, page in enumerate(reader.pages):
            if i < skip: writer.add_page(page); continue
            num   = start + (i-skip)
            label = prefix + str(num) + suffix.replace("{gesamt}", str(n))
            pw=float(page.mediabox.width); ph=float(page.mediabox.height)
            packet = io.BytesIO()
            c = rl_canvas.Canvas(packet, pagesize=(pw,ph))
            c.setFont("Helvetica", fs)
            # position by index: 0=Mitte/Unten, 1=Links/Unten, 2=Rechts/Unten,
            #                    3=Mitte/Oben,  4=Links/Oben,  5=Rechts/Oben
            bottom = pos_idx < 3
            h_mode = pos_idx % 3   # 0=center, 1=left, 2=right
            y = margin if bottom else ph - margin - fs
            if h_mode == 0:
                x = pw / 2; c.drawCentredString(x, y, label)
            elif h_mode == 1:
                x = margin; c.drawString(x, y, label)
            else:
                x = pw - margin; c.drawRightString(x, y, label)
            c.save(); packet.seek(0)
            from pypdf import PdfReader as PR
            page.merge_page(PR(packet).pages[0]); writer.add_page(page)
        with open(out, "wb") as f: writer.write(f)
        self.open_result(out, tr("Mit Seitenzahlen"))
        return tr('Seitenzahlen auf {p0} Seiten hinzugefuegt').format(p0=n - skip)


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
        if not paths: self.log.log(tr("Bilder hinzufuegen."), error=True); return
        out = self.save_pdf("Als PDF speichern")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            with open(out, "wb") as f: f.write(img2pdf.convert(paths))
            self.log.log(tr('PDF aus {p0} Bildern erstellt').format(p0=len(paths)))
            self.open_result(out, tr("Aus Bildern"))
        except Exception as e: self.log.log(str(e), error=True)

    def _to_img(self):
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            from pdf2image import convert_from_path
            from pypdf import PdfReader
            fmt=self.fmt.currentText().lower(); ext={"jpeg":"jpg"}.get(fmt,fmt)
            n_pages = len(PdfReader(src, strict=False).pages)
            stem=os.path.splitext(os.path.basename(src))[0]
            for i in range(0, n_pages, 10):
                QApplication.processEvents()
                end = min(i+10, n_pages)
                pages=convert_from_path(src, dpi=self.dpi.value(), first_page=i+1, last_page=end)
                for j, img in enumerate(pages):
                    img.save(os.path.join(out_dir, f"{stem}_s{i+j+1:03d}.{ext}"), fmt.upper())
            self.log.log(tr('{p0} Bilder exportiert').format(p0=n_pages))
        except Exception as e: self.log.log(str(e), error=True)

    def _run_action(self): pass


# ══════════════════════════════════════════════════════════════════════════════
# GRAYSCALE
# ══════════════════════════════════════════════════════════════════════════════
def _colour_histogram(pil_rgb):
    """256-bin histogram of how far each pixel is from neutral grey, where the
    distance is max(|r-g|, |r-b|, |g-b|) — 0 is exactly grey, 255 fully saturated.

    Measured over EVERY pixel of the render. The scan used to squash the page to
    128×128 first and loop over that in Python, which averaged small colour marks
    away: a 1 pt red dot on A4 came out as a distance of 19 instead of 255, so at
    the default threshold of 20 it was invisible and the page was silently
    converted to grey. Losing a red stamp or a coloured logo that way is exactly
    the mistake a copy shop pays for. Pillow does the work in C, so reading every
    pixel is also faster than the old Python loop over the thumbnail."""
    from PIL import ImageChops
    r, g, b = pil_rgb.split()
    d = ImageChops.lighter(
        ImageChops.lighter(ImageChops.difference(r, g), ImageChops.difference(r, b)),
        ImageChops.difference(g, b))
    return d.histogram()


def _hist_stats(hist, thr):
    """(max distance from grey, fraction of pixels above `thr`) for a histogram.

    Derived on demand rather than frozen at scan time: the colour fraction used
    to be computed with whatever the threshold slider happened to be when the
    document was scanned, so moving that slider afterwards changed nothing at all
    in "Nach Anteil farbiger Pixel" mode — the control looked broken because it
    was."""
    total = sum(hist)
    if not total:
        return 0, 0.0
    max_diff = max((b for b, n in enumerate(hist) if n), default=0)
    return max_diff, sum(hist[thr + 1:]) / total


_VERIFY_SCALE   = 0.30    # ~180 px across an A4 page — enough to see a blackout
_BLACKOUT_LIMIT = 0.004   # 0.4 % of the page turning solid black is already wrong


def _page_luma(path, index, scale=_VERIFY_SCALE):
    """Greyscale render of one page, for comparing before against after."""
    import pypdfium2 as pdfium
    with _pdfium_lock:
        doc = pdfium.PdfDocument(path)
        try:
            return doc[index].render(scale=scale).to_pil().convert("L")
        finally:
            doc.close()


def _conversion_damage(ref_l, got_l):
    """(blacked_out, vanished) as fractions of the page.

    `blacked_out` is the share of pixels that were clearly light in the original
    and came back solid black; `vanished` is the reverse — content that was dark
    and is now blank paper. Both are compared against a *greyscale* render of the
    original, so a legitimate colour→grey conversion scores ~0: only gross
    damage registers, not the small colorimetric differences between
    Ghostscript's conversion and Pillow's."""
    from PIL import ImageChops
    if got_l.size != ref_l.size:
        got_l = got_l.resize(ref_l.size)
    light = ref_l.point(lambda v: 255 if v > 160 else 0)
    dark  = ref_l.point(lambda v: 255 if v < 90  else 0)
    now_black = got_l.point(lambda v: 255 if v < 50  else 0)
    now_blank = got_l.point(lambda v: 255 if v > 230 else 0)
    total = ref_l.size[0] * ref_l.size[1] or 1
    hit = lambda a, b: ImageChops.darker(a, b).histogram()[255] / total
    return hit(light, now_black), hit(dark, now_blank)


def _verify_pages_intact(src, cand, pages, report, label=""):
    """Which of `pages` came out damaged in `cand` compared with `src`.

    Ghostscript reports success and exits 0 while blacking out a transparency
    group or a soft-masked image — the failure this exists to catch. It is
    silent, it is invisible until the job is printed, and it is not something a
    return code will ever tell us about, so every converted page is looked at.

    Used by both colour conversions (greyscale and CMYK); `report` may be None.
    Returns {page_index: reason}."""
    bad = {}
    for n, i in enumerate(sorted(pages), 1):
        if report and n % 5 == 1:
            report(tr('Prüfe Seite {p0} / {p1} …{p2}').format(
                p0=n, p1=len(pages), p2=label))
        try:
            blacked, vanished = _conversion_damage(
                _page_luma(src, i), _page_luma(cand, i))
        except Exception:
            # Could not check it — treat as damaged rather than assume it is
            # fine. Silently shipping an unverified page is the whole problem.
            logging.exception("grayscale: verification of page %d failed", i + 1)
            bad[i] = "unverified"
            continue
        if blacked > _BLACKOUT_LIMIT:
            bad[i] = f"{blacked * 100:.1f}% schwarz"
        elif vanished > _BLACKOUT_LIMIT:
            bad[i] = f"{vanished * 100:.1f}% verschwunden"
    return bad


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
        # A saddle-stitched booklet is always a multiple of four pages — a folded
        # sheet carries four of them — so the fill-up option has no meaning there.
        # It used to stay enabled and be silently ignored.
        booklet = (idx == 0)
        self.blank.setEnabled(not booklet)
        self.blank.setToolTip(tr(
            "Eine Broschüre wird immer auf ein Vielfaches von 4 Seiten aufgefüllt.")
            if booklet else "")

    def _run_action(self):
        import pikepdf as _pik
        src = self.require_pdf()
        out = self.save_pdf(tr("Ausgeschossene PDF speichern als"))
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        with _pik.open(src) as doc:
            n = len(doc.pages)
            if not n: raise ValueError(tr("Die PDF hat keine Seiten."))
            pw, ph = _impose_page_size(doc, self.norm_target.currentIndex())

        mode = self.mode.currentIndex()
        pad  = self.blank.isChecked()
        if mode == 0:
            sheet_w, sheet_h = pw * 2, ph
            halves = [(0.0, 0.0, pw, ph), (pw, 0.0, pw * 2, ph)]
            sheets = [list(zip(side, halves)) for side in _booklet_sides(n)]
            summary = lambda placed, nsheets: tr(
                'Broschüre: {p0} Seiten auf {p1} Blattseiten.').format(p0=placed, p1=nsheets)
        elif mode == 1:
            sheet_w, sheet_h = pw * 2, ph
            halves = [(0.0, 0.0, pw, ph), (pw, 0.0, pw * 2, ph)]
            pages = list(range(n))
            if pad:
                while len(pages) % 2: pages.append(None)
            sheets = [list(zip(pages[i:i + 2], halves)) for i in range(0, len(pages), 2)]
            summary = lambda placed, nsheets: tr(
                '2-up: {p0} Seiten auf {p1} Bögen.').format(p0=placed, p1=nsheets)
        else:
            cols = self.cols.value(); rows = self.rows_spin.value(); per = cols * rows
            sheet_w, sheet_h = pw, ph
            cw, chh = pw / cols, ph / rows
            cells = [(c * cw, ph - (r + 1) * chh, (c + 1) * cw, ph - r * chh)
                     for r in range(rows) for c in range(cols)]
            pages = list(range(n))
            if pad:
                while len(pages) % per: pages.append(None)
            sheets = [list(zip(pages[i:i + per], cells)) for i in range(0, len(pages), per)]
            summary = lambda placed, nsheets: tr(
                '{p0}×{p1}-up: {p2} Seiten auf {p3} Bögen.').format(
                    p0=cols, p1=rows, p2=placed, p3=nsheets)

        fit = self.norm_check.isChecked()
        self.run_async(
            lambda report: _build_impose(src, out, sheets, sheet_w, sheet_h,
                                         fit, report, summary),
            on_done=self._impose_done,
            busy_label="Ausschießen …",
        )
        return None

    def _impose_done(self, result):
        out_path, summary = result
        self.log.log(summary)
        self.open_result(out_path, "Ausgeschossen")


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
            fields=PdfReader(src, strict=False).get_fields()
            if not fields: self.log.log(tr("Keine Formularfelder gefunden.")); return
            for name,field in fields.items():
                ft=field.get("/FT",""); val=field.get("/V","")
                if ft=="/Btn":
                    w=QCheckBox(); w.setChecked(str(val) in ("/Yes","/On","Yes","On"))
                else:
                    w=QLineEdit(); w.setText(str(val) if val else "")
                self._fields[name]=w
                self.form_vbox.addLayout(row(name, w))
            self.log.log(tr('{p0} Feld(er) geladen.').format(p0=len(fields)))
        except Exception as e: self.log.log(str(e),error=True)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("Ausgefuelltes Formular speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        from pypdf import PdfReader, PdfWriter
        reader=PdfReader(src, strict=False); writer=PdfWriter(); writer.append(reader)
        data={name: ("/Yes" if (isinstance(w,QCheckBox) and w.isChecked()) else
                     "/Off" if isinstance(w,QCheckBox) else w.text())
              for name,w in self._fields.items()}
        for page in writer.pages: writer.update_page_form_field_values(page,data)
        if self.flatten.isChecked():
            for page in writer.pages:
                if "/Annots" in page: del page["/Annots"]
        with open(out,"wb") as f: writer.write(f)
        self.open_result(out,tr("Formular ausgefuellt"))
        return tr('Formular ausgefuellt ({p0} Felder)').format(p0=len(data))


# ══════════════════════════════════════════════════════════════════════════════
# OCR
# ══════════════════════════════════════════════════════════════════════════════
def _run_ocr(src, out, lang, deskew, skip, report):
    """Run OCR on a worker thread (via BasePanel.run_async). Returns
    (out_path, summary); raises on failure."""
    if shutil.which("ocrmypdf"):
        report(tr("Starte ocrmypdf …"))
        cmd = ["ocrmypdf", "--language", lang, "--output-type", "pdfa"]
        if deskew: cmd.append("--deskew")
        if skip:   cmd.append("--skip-text")
        cmd += [src, out]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=600)
        if r.returncode not in (0, 6):
            # A silent non-zero exit used to raise RuntimeError("") — an error
            # dialog with nothing in it.
            raise RuntimeError(r.stderr.strip() or r.stdout.strip()
                               or tr('ocrmypdf beendet mit Code {p0}').format(p0=r.returncode))
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(tr("OCR hat keine Ausgabedatei erzeugt."))
        return out, tr('OCR abgeschlossen ({p0})').format(p0=lang)

    if shutil.which("tesseract"):
        from pdf2image import convert_from_path
        import pytesseract
        report(tr("Rendere Seiten …"))
        images = convert_from_path(src, dpi=300)
        txt = ""
        for i, img in enumerate(images):
            report(tr('OCR Seite {p0} / {p1} …').format(p0=i + 1, p1=len(images)))
            txt += tr('--- Seite {p0} ---\n').format(p0=i + 1)
            txt += pytesseract.image_to_string(img, lang=lang) + "\n\n"
        out_txt = os.path.splitext(out)[0] + ".txt"
        with open(out_txt, "w", encoding="utf-8") as f:
            f.write(txt)
        return out_txt, tr('OCR abgeschlossen — Text gespeichert ({p0})').format(p0=lang)

    raise RuntimeError(tr(
        "Kein OCR-Programm gefunden.\n"
        "Installation:  pip install ocrmypdf --break-system-packages\n"
        "           oder:  sudo pacman -S tesseract tesseract-data-deu"))


class OcrPanel(BasePanel):
    TITLE         = "OCR -- Texterkennung"
    SUBTITLE      = "Gescannte PDFs durchsuchbar machen."
    OPENS_NEW_TAB = True

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
                            (tr("Deutsch + Englisch"), "deu+eng"), (tr("Französisch"), "fra")]:
            self.lang.addItem(name, code)
        ol.addLayout(row(tr("Sprache:"), self.lang))
        self.deskew = QCheckBox(tr("Seiten begradigen")); self.deskew.setChecked(True); ol.addWidget(self.deskew)
        self.skip   = QCheckBox(tr("Seiten mit Text ueberspringen")); self.skip.setChecked(True); ol.addWidget(self.skip)
        layout.addWidget(ob)

        # Fortschrittsanzeige
        self.progress_lbl = make_label("", dim=True)
        layout.addWidget(self.progress_lbl)

    def _run_action(self):
        # Prep on the UI thread, then hand the work to the shared run_async().
        src  = self.require_pdf()
        out  = self.save_pdf("OCR-PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        lang   = self.lang.currentData()
        deskew = self.deskew.isChecked()
        skip   = self.skip.isChecked()
        self.progress_lbl.setText(tr("OCR läuft …"))
        self.run_async(
            lambda report: _run_ocr(src, out, lang, deskew, skip, report),
            on_done=self._ocr_done,
            on_error=self._ocr_failed,
            on_progress=self._ocr_progress,
        )
        return None

    def _ocr_progress(self, msg):
        self.progress_lbl.setText(msg)
        self.log.log(msg)

    def _ocr_done(self, result):
        out_path, summary = result
        self.progress_lbl.setText(tr("Fertig."))
        self.log.log(summary)
        if out_path.endswith(".txt"):
            self.log.log(tr('Text gespeichert: {p0}').format(p0=out_path))
        else:
            self.open_result(out_path, "OCR")

    def _ocr_failed(self, exc):
        self.progress_lbl.setText(tr("Fehler."))
        self.log.log(str(exc), error=True)


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
        if getattr(self, '_preflighting', False):
            raise RuntimeError(tr("Pruefung laeuft bereits — bitte warten."))
        self._preflighting = True
        try:
            return self._do_preflight()
        finally:
            self._preflighting = False

    def _do_preflight(self):
        src=self.require_pdf()
        from pypdf import PdfReader; import pypdfium2 as pdfium
        reader=PdfReader(src, strict=False); doc=pdfium.PdfDocument(src)
        try:
            n=len(reader.pages); issues=[]; oks=[]
            if self.chk_enc.isChecked():
                if reader.is_encrypted: issues.append(tr("PDF ist passwortgeschuetzt"))
                else: oks.append(tr("Nicht verschluesselt"))
            target=PAPER_SIZES_PT.get(self.size_combo.currentText())
            orients=[]; colour_pages=[]
            for i in range(n):
                QApplication.processEvents()
                page=reader.pages[i]; pw=float(page.mediabox.width); ph=float(page.mediabox.height)
                orients.append("Q" if pw>ph else "H")
                if self.chk_size.isChecked() and target:
                    tw,th=target
                    if not ((abs(pw-tw)<5 and abs(ph-th)<5) or (abs(pw-th)<5 and abs(ph-tw)<5)):
                        issues.append(tr('Seite {p0}: {p1:.0f}x{p2:.0f}pt != {p3:.0f}x{p4:.0f}pt').format(p0=i + 1, p1=pw, p2=ph, p3=tw, p4=th))
                if self.chk_colour.isChecked():
                    with _pdfium_lock:
                        pil=doc[i].render(scale=1).to_pil().convert("RGB")
                    # Every pixel, not a 64x64 squash of the page: averaging the
                    # render first hid small colour marks completely, and this
                    # check reporting "Keine Farbseiten erkannt" for a page that
                    # has them is a preflight that passes a job it should stop.
                    if _hist_stats(_colour_histogram(pil), 20)[0] > 20:
                        colour_pages.append(i+1)
            if self.chk_orient.isChecked() and orients:
                if len(set(orients))>1: issues.append(tr("Gemischte Ausrichtungen"))
                else: oks.append(tr("Einheitlich: {p0}").format(p0=tr('Hochformat') if orients[0]=='H' else tr('Querformat')))
            if self.chk_colour.isChecked():
                if colour_pages: issues.append(f"Farbseiten: {colour_pages[:10]}")
                else: oks.append(tr("Keine Farbseiten erkannt"))
            lines=[f"BERICHT -- {os.path.basename(src)}",tr('Seiten: {p0}  |  {p1} KB').format(p0=n, p1=os.path.getsize(src) // 1024),""]
            if issues: lines+=[f"PROBLEME ({len(issues)}):"]+ [f"  x  {x}" for x in issues]+[""]
            else: lines.append(tr("BESTANDEN -- Datei scheint druckfertig."))
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
            reader=PdfReader(src, strict=False); root=reader.trailer["/Root"]; oc=root.get("/OCProperties")
            if not oc: self.no_layers.setVisible(True); self.log.log(tr("Keine Ebenen gefunden.")); return
            self.no_layers.setVisible(False)
            for ref in oc.get_object().get("/OCGs",ArrayObject()):
                obj=ref.get_object(); name=str(obj.get("/Name","(unbenannt)"))
                cb=QCheckBox("  "+name); cb.setChecked(True)
                self.cb_layout.addWidget(cb); self._cbs.append((ref.idnum, cb))
            self.log.log(tr('{p0} Ebene(n) gefunden.').format(p0=len(self._cbs)))
        except Exception as e: self.log.log(str(e),error=True)

    def _all(self,state):
        for _,cb in self._cbs: cb.setChecked(state)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, NameObject
        reader = PdfReader(src, strict=False)
        writer = PdfWriter(); writer.append(reader)
        if self._cbs:
            root = writer.trailer["/Root"]
            oc = root.get("/OCProperties")
            if oc:
                oc_obj = oc.get_object()
                checked = {idnum for idnum, cb in self._cbs if cb.isChecked()}
                on_arr  = ArrayObject()
                off_arr = ArrayObject()
                for ref in oc_obj.get("/OCGs", ArrayObject()):
                    if ref.idnum in checked:
                        on_arr.append(ref)
                    else:
                        off_arr.append(ref)
                oc_obj[NameObject("/ON")] = on_arr
                if off_arr:
                    oc_obj[NameObject("/OFF")] = off_arr
        with open(out, "wb") as f: writer.write(f)
        if self.flatten.isChecked() and shutil.which("gs"):
            import tempfile
            fd, flat = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
            try:
                cmd=["gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.5",
                     "-dNOPAUSE","-dBATCH","-dQUIET",f"-sOutputFile={flat}",out]
                r=subprocess.run(cmd,capture_output=True, text=True, errors="replace",timeout=300)
                if r.returncode!=0:
                    # -dQUIET: a failing Ghostscript often says nothing, and
                    # RuntimeError("") is an error dialog with no error in it.
                    raise RuntimeError(r.stderr.strip() or r.stdout.strip()
                                       or tr('Ghostscript beendet mit Code {p0}').format(p0=r.returncode))
                if not os.path.exists(flat) or os.path.getsize(flat) == 0:
                    raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))
                os.replace(flat, out)
            finally:
                try: os.remove(flat)
                except OSError: pass
        self.open_result(out,tr("Ebenen verarbeitet"))
        return tr("Ebenen verarbeitet")


# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PROFILE
# ══════════════════════════════════════════════════════════════════════════════
class ColourProfilePanel(BasePanel):
    TITLE         = "Farbprofil / CMYK"
    SUBTITLE      = "ICC-Profile pruefen und in CMYK umwandeln."
    OPENS_NEW_TAB = True

    # (label incl. real profile name + paper/use-case, candidate .icc filenames).
    # The generic option needs no ICC file; the named ones use the matching .icc
    # via Ghostscript when present (drop them in ~/.local/share/copyshop_pdf_suite/icc/).
    CMYK_PROFILES = [
        (tr("Standard (generisch) — universell, ohne ICC-Datei"), None),
        (tr("ISO Coated v2 (FOGRA39) — gestrichenes Papier, EU-Offset-Standard"),
            ("ISOcoated_v2_eci.icc", "ISOcoated_v2_300_eci.icc")),
        (tr("PSO Coated v3 (FOGRA51) — modernes gestrichenes Papier, Premium-Offset"),
            ("PSOcoated_v3.icc",)),
        (tr("PSO Uncoated v3 (FOGRA52) — ungestrichenes/Naturpapier, Bücher & Briefbögen"),
            ("PSOuncoated_v3_FOGRA52.icc", "PSO_Uncoated_ISO12647_eci.icc")),
        (tr("U.S. Web Coated (SWOP) v2 — US-Rollenoffset, Magazine (gestrichen)"),
            ("USWebCoatedSWOP.icc",)),
        (tr("Coated GRACoL 2006 — US-Bogenoffset, hochwertiges gestrichenes Papier"),
            ("GRACoL2006_Coated1v2.icc", "CGATS21_CRPC6.icc")),
    ]

    def build_ui(self, layout):
        ib=QPushButton(tr("  Farbprofil pruefen")); ib.setObjectName("secondaryBtn")
        ib.clicked.connect(self._inspect); layout.addWidget(ib)
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMaximumHeight(150)
        self.report.setPlaceholderText(tr("Farbprofil-Info erscheint hier...")); layout.addWidget(self.report)

        cb=QGroupBox(tr("IN CMYK UMWANDELN")); cl=QVBoxLayout(cb)
        cl.addWidget(make_label(tr(
            "Konvertiert via Ghostscript nach DeviceCMYK. "
            "Qualitaetsstufe: Prepress (hoechste Qualitaet, alle Fonts eingebettet)."), dim=True))
        self.profile_combo = QComboBox()
        for label, cands in self.CMYK_PROFILES:
            self.profile_combo.addItem(tr(label), cands)
        cl.addLayout(row(tr("CMYK-Profil:"), self.profile_combo))
        cl.addWidget(make_label(tr(
            "Benannte Profile nutzen die passende .icc-Datei aus "
            "~/.local/share/copyshop_pdf_suite/icc/ — fehlt sie, wird generisch "
            "konvertiert."), dim=True))
        gs_ok = bool(shutil.which("gs"))
        status = tr("✓  Ghostscript verfuegbar") if gs_ok else tr("✗  Ghostscript fehlt  →  sudo pacman -S ghostscript")
        cl.addWidget(make_label(status, dim=True))
        layout.addWidget(cb)

    def _resolve_icc(self, candidates):
        """Return the path to the first available .icc among `candidates`
        (searching the app icc dir + common system dirs), or None."""
        if not candidates:
            return None
        dirs = [
            os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/"),
            "/usr/share/color/icc/",
            "/usr/share/color/icc/colord/",
        ]
        for name in candidates:
            for d in dirs:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p
        return None

    def _inspect(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        try:
            import pikepdf
            CS_MAP={
                "/DeviceRGB":"RGB", "/DeviceCMYK":"CMYK", "/DeviceGray":"Graustufen",
                "/CalRGB":"Kal. RGB", "/CalGray":"Kal. Grau", "/ICCBased":"ICC",
                "/Lab":"CIE Lab", "/Separation":tr("Sonderfarbe"), "/DeviceN":"DeviceN",
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
                        if re.search(r'[\d.]+\s+[gG]\b', text):
                            found.add("/DeviceGray")
                    except Exception: pass
            n_pages = len(pdf.pages)
            pdf.close()

            readable=[CS_MAP.get(c, c) for c in found]
            is_cmyk="/DeviceCMYK" in found
            is_rgb=bool(found & {"/DeviceRGB","/CalRGB","/ICCBased"})
            lines=[
                tr('Datei:   {p0}').format(p0=os.path.basename(src)),
                tr('Seiten:  {p0}').format(p0=n_pages),
                "",
                tr("Farbraum: {p0}").format(p0=', '.join(sorted(readable)) if readable else tr('nicht erkennbar')),
                "",
            ]
            if is_cmyk and not is_rgb:
                lines.append("✓  CMYK — druckfertig.")
            elif is_rgb and is_cmyk:
                lines.append(tr("⚠  Gemischt (RGB + CMYK) — vor Profidruck vollständig in CMYK umwandeln."))
            elif is_rgb:
                lines.append(tr("⚠  RGB — vor Profidruck in CMYK umwandeln."))
            else:
                lines.append(tr("ℹ  Farbraum nicht eindeutig erkennbar."))
            self.report.setPlainText("\n".join(lines))
            self.log.log(tr("Pruefung abgeschlossen."))
        except Exception as e:
            self.log.log(str(e), error=True)

    def _run_action(self):
        import subprocess as sp

        src = self.require_pdf()

        if not shutil.which("gs"):
            raise RuntimeError(tr(
                "Ghostscript nicht gefunden.\n"
                "Installation:  sudo pacman -S ghostscript"))

        out = self.save_pdf("CMYK-PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad angegeben."))

        # Bewährter GS-Befehl für RGB→CMYK ohne ICC-Profil-Problematik.
        # -dEncodeColorImages=false / -dEncodeGrayImages=false verhindert
        # Neukomprimierung und Qualitätsverlust bei Bildern.
        # -dPDFSETTINGS=/prepress: höchste Qualität, Fonts eingebettet.
        # Selected CMYK target profile (None = generic). Use its .icc if present.
        candidates = self.profile_combo.currentData()
        icc = self._resolve_icc(candidates)
        prof_label = self.profile_combo.currentText().split(" — ")[0]

        # Ghostscript writes to a temp file, never straight to `out`: the result
        # is checked page by page first, exactly as the greyscale conversion is.
        # pdfwrite can black out a transparency group while exiting 0, and for a
        # prepress file nobody notices until it is on press.
        import tempfile, contextlib, pikepdf
        fd, cmyk_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        try:
            cmd = [
                "gs",
                "-o", cmyk_tmp,
                "-sDEVICE=pdfwrite",
                "-dPDFSETTINGS=/prepress",
                "-dEncodeColorImages=false",
                "-dEncodeGrayImages=false",
                "-dEncodeMonoImages=false",
                "-sProcessColorModel=DeviceCMYK",
                "-sColorConversionStrategy=CMYK",
                "-sColorConversionStrategyForImages=CMYK",
            ]
            if icc:
                cmd.append(f"-sOutputICCProfile={icc}")   # convert to this named CMYK space
            cmd.append(src)

            r = sp.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)

            if r.returncode != 0:
                err = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[:500]
                raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(p0=err))
            if not os.path.exists(cmyk_tmp) or os.path.getsize(cmyk_tmp) == 0:
                raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

            with pikepdf.open(src) as _s, pikepdf.open(cmyk_tmp) as _c:
                n_ok = min(len(_s.pages), len(_c.pages))
            damaged = _verify_pages_intact(src, cmyk_tmp, range(n_ok), None)
            if damaged:
                # Keep the untouched original for those pages rather than hand
                # over a file with black rectangles in it.
                with contextlib.ExitStack() as stack:
                    s_pdf = stack.enter_context(pikepdf.open(src))
                    c_pdf = stack.enter_context(pikepdf.open(cmyk_tmp))
                    o_pdf = stack.enter_context(pikepdf.Pdf.new())
                    for i in range(n_ok):
                        o_pdf.pages.append(
                            s_pdf.pages[i] if i in damaged else c_pdf.pages[i])
                    o_pdf.save(out)
            else:
                shutil.copyfile(cmyk_tmp, out)
        finally:
            try: os.remove(cmyk_tmp)
            except OSError: pass

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
            verify = tr("⚠  Einige RGB-Bilder noch vorhanden (eingebettete Profile).") if found_rgb else tr("✓  Farbraum erfolgreich in CMYK konvertiert.")
        except Exception:
            verify = tr("(Verifikation nicht möglich)")
        if damaged:
            verify += "\n⚠  " + tr(
                'ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt '
                'und blieben deshalb unveraendert: {p1}').format(
                    p0=len(damaged),
                    p1=", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items())))

        if icc:
            prof_note = f"Profil: {prof_label}  ({os.path.basename(icc)})"
        elif candidates:
            prof_note = (tr("⚠  Profil '{p0}' nicht installiert — generische CMYK-Konvertierung verwendet.\n   .icc-Datei nach ~/.local/share/copyshop_pdf_suite/icc/ legen.").format(p0=prof_label))
        else:
            prof_note = "Profil: Standard (generisch)"

        self.open_result(out, tr("CMYK konvertiert"))
        return tr('Konvertierung abgeschlossen.\n{p0}\n{p1}').format(p0=prof_note, p1=verify)


def _fmt(b):
    if b<1024: return f"{b} B"
    elif b<1024**2: return f"{b/1024:.1f} KB"
    else: return f"{b/1024**2:.2f} MB"


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


def _crop_mark_segments(rects, length=7.0, gap=2.0):
    """L-shaped crop/cut marks at each rectangle corner: two short line segments
    per corner, offset outward from the trim by `gap` and `length` long (PDF
    points). Returns a flat list of (x0, y0, x1, y1) line segments. Shared by the
    N-Up grid marks and the Crop tool's size marks."""
    segs = []
    for (x0, y0, x1, y1) in rects:
        # Corner marks (L-shaped: a horizontal + a vertical tick at each corner).
        for cx, cy, dx, dy in ((x0, y0, -1, -1), (x1, y0, 1, -1),
                               (x0, y1, -1, 1), (x1, y1, 1, 1)):
            segs.append((cx + dx * gap, cy, cx + dx * (gap + length), cy))  # horizontal
            segs.append((cx, cy + dy * gap, cx, cy + dy * (gap + length)))  # vertical
        # Centre marks: on each side, TWO short lines that run ALONG the edge
        # (in the outer margin). Each sits ~2/3 of the way from the page centre
        # toward its corner — so the two marks are closer to the corner marks
        # than to each other, with a wide clear gap in the middle.
        mx = (x0 + x1) / 2; my = (y0 + y1) / 2
        off_x = (x1 - x0) / 2 * (2.0 / 3.0)   # mark centre offset from the side midpoint
        off_y = (y1 - y0) / 2 * (2.0 / 3.0)
        hl = length / 2.0                     # half the mark length
        # Top & bottom edges → horizontal segments near each corner.
        for yy, dy in ((y1, 1), (y0, -1)):
            oy = yy + dy * gap
            for sx in (mx - off_x, mx + off_x):
                segs.append((sx - hl, oy, sx + hl, oy))
        # Left & right edges → vertical segments near each corner.
        for xx, dx in ((x0, -1), (x1, 1)):
            ox = xx + dx * gap
            for sy in (my - off_y, my + off_y):
                segs.append((ox, sy - hl, ox, sy + hl))
    return segs


def _crop_marks_content_stream(rects, length=7.0, gap=2.0):
    """Build a PDF content stream (bytes) that strokes crop marks for `rects`
    as thin black lines. Appended to a page via `page.contents_add(Stream(...))`.
    Used by both N-Up and the Crop tool so the marks are identical everywhere."""
    ops = ["q", "0 0 0 RG", "0.5 w"]
    for (a, b, c, d) in _crop_mark_segments(rects, length, gap):
        ops.append(f"{a:.2f} {b:.2f} m {c:.2f} {d:.2f} l S")
    ops.append("Q")
    return ("\n".join(ops)).encode("latin-1")


_ROT_MATRIX = {0: (1.0, 0.0, 0.0, 1.0), 90:  (0.0, -1.0, 1.0, 0.0),
               180: (-1.0, 0.0, 0.0, -1.0), 270: (0.0, 1.0, -1.0, 0.0)}


def _slot_placement(box, rot, rect, fixed_scale=None):
    """Matrix that fits a page into a slot: scale it to fit `rect` keeping its
    aspect ratio, and centre it there. `box` is the page's visible rectangle,
    `rot` its /Rotate. Returns (scale, tx, ty) for a ``s 0 0 s tx ty cm``, applied
    after the rotation matrix — i.e. exactly the placement pikepdf's add_overlay
    performs, but computed in full precision. qpdf writes that matrix rounded to
    five decimals and truncates the rotation offset to a whole point, which left
    the content up to ~1.5pt off-centre in its slot — small, but this is a print
    tool and it showed up as visibly uneven margins.

    `fixed_scale` places the page at that scale instead of fitting it (still
    centred) — what the Broschüre tool does when normalising is switched off."""
    x0, y0, x1, y1 = box
    a, b, c, d = _ROT_MATRIX[rot % 360 if rot % 90 == 0 else 0]
    pts = [(a * x + c * y, b * x + d * y) for x in (x0, x1) for y in (y0, y1)]
    bx0 = min(p[0] for p in pts); bx1 = max(p[0] for p in pts)
    by0 = min(p[1] for p in pts); by1 = max(p[1] for p in pts)
    bw  = max(bx1 - bx0, 1e-6);   bh  = max(by1 - by0, 1e-6)
    rx0, ry0, rx1, ry1 = rect
    s  = min((rx1 - rx0) / bw, (ry1 - ry0) / bh) if fixed_scale is None else fixed_scale
    tx = rx0 + ((rx1 - rx0) - bw * s) / 2.0 - bx0 * s
    ty = ry0 + ((ry1 - ry0) - bh * s) / 2.0 - by0 * s
    return s, tx, ty


def _flatten_annots(doc):
    """Bake annotation appearances (stamps, signatures, filled form fields) into
    the page content.

    Imposition turns every source page into a Form XObject, and an XObject
    carries content only — annotations stay behind on the page that is being
    left behind. Anything the user could see but that lived in an /AP stream
    would silently vanish from the printed sheet."""
    if not any("/Annots" in p.obj for p in doc.pages):
        return
    try:
        doc.flatten_annotations("all")
    except Exception:
        pass          # older qpdf: better an un-flattened page than no output


def _booklet_sides(n_pages):
    """Saddle-stitch imposition order for `n_pages` source pages.

    Returns one list per printed sheet side — [left, right] as 0-based source
    page indexes, None for a blank — ordered front, back, front, back … so the
    result prints duplex and folds into a booklet. Padded to a multiple of four
    because a folded sheet always carries four pages."""
    n = n_pages
    while n % 4:
        n += 1
    sides = []
    for k in range(1, n // 4 + 1):
        sides.append([n - 2 * k + 1, 2 * k - 2])     # front of sheet k
        sides.append([2 * k - 1,     n - 2 * k])     # its back
    return [[(i if i < n_pages else None) for i in side] for side in sides]


def _impose_page_size(doc, idx):
    """The size of one booklet page: a fixed paper size, or — for "largest page
    in the document" — the biggest page *as displayed* (visible box, /Rotate
    applied).

    Deliberately the largest single page, not max(width) × max(height) over all
    of them: a document mixing portrait and landscape A4 made those two maxima
    841×841, so every sheet came out square."""
    sizes = {1: (595.28, 841.89), 2: (841.89, 1190.55),
             3: (419.53, 595.28), 4: (612.0, 792.0)}
    if idx in sizes:
        return sizes[idx]
    return max((_visible_size(p) for p in doc.pages),
               key=lambda wh: wh[0] * wh[1], default=(595.28, 841.89))


def _build_impose(src, out, sheets, sheet_w, sheet_h, fit, report, summary):
    """Render an imposition on a worker thread (via BasePanel.run_async).

    `sheets` is a list of sheets, each a list of (source_page_index_or_None,
    slot_rect); `summary(placed, sheets)` builds the finished-message.
    Uses the same Form-XObject engine as _build_nup instead of pypdf's
    merge_transformed_page, which

      * ignored /Rotate, so a page turned in the page manager was imposed
        unturned,
      * measured pages by their raw MediaBox, so a print PDF's bleed decided the
        sheet size (an A4 booklet came out 432 × 303 mm instead of A3) and the
        content sat off-centre,
      * re-encoded every content stream on the GUI thread, freezing the window.
    """
    from pikepdf import Pdf, Page, Stream, Array, Name
    src_doc = Pdf.open(src)
    _flatten_annots(src_doc)
    out_doc = Pdf.new()

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

    placed = 0
    for sheet_i, slots in enumerate(sheets):
        report(f"{tr('Blatt')} {sheet_i + 1} / {len(sheets)} …")
        sheet = Page(out_doc.add_blank_page(page_size=(sheet_w, sheet_h)))
        names = {}
        for page_i, rect in slots:
            if page_i is None or page_i >= len(src_doc.pages):
                continue          # a blank slot: leave the paper empty
            fx, box, rot = _form_for(page_i)
            s, tx, ty = _slot_placement(box, rot, rect,
                                        fixed_scale=None if fit else 1.0)
            if page_i not in names:
                names[page_i] = sheet.add_resource(fx, Name.XObject, prefix="Imp")
            sheet.contents_add(Stream(out_doc,
                f"q {s:.6f} 0 0 {s:.6f} {tx:.6f} {ty:.6f} cm {names[page_i]} Do Q\n"
                .encode("latin-1")))
            placed += 1
        sheet.contents_coalesce()
    report(tr("Schreibe Datei …"))
    out_doc.save(out)
    return out, summary(placed, len(sheets))


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
        pdf_path = AppState.get().current_pdf
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
