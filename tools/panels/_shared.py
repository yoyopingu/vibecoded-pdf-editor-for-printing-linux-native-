"""
Helpers shared by more than one tool panel: the label/field rows, the
preview pane, the paper sizes and the page geometry.
"""
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QWidget, QComboBox, QDoubleSpinBox,
                             QCheckBox)
from PyQt6.QtCore import Qt, QTimer, QEvent, pyqtSignal
from tools.app_state import AppState
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


class PaperFormatSelector(QWidget):
    """A paper-size dropdown, with a width x height row that appears when the
    user picks "Benutzerdefiniert (mm)".

    Crop/Scale had this and N-Up did not, so the same choice was offered in one
    tool and not the other. One widget now, and the paper list comes from
    PAPER_SIZES_PT so both offer the same formats.

    `before` and `after` are entries that are not paper — "— Kein —", "Wie
    Quellseite × Raster" — placed at either end of the list. target_size_pt()
    returns None when one of them is selected, and special() says which, so the
    panel can do whatever that entry means.

    Querformat belongs here rather than beside it: the orientation is part of
    which sheet you asked for, so target_size_pt() answers with it applied and
    no caller has to remember to swap. It is disabled for the entries that are
    not paper, where there is nothing to turn.
    """

    changed = pyqtSignal()

    def __init__(self, before=(), after=(), width_mm=210.0, height_mm=297.0,
                 parent=None):
        super().__init__(parent)
        self._before = list(before)
        self._after = list(after)
        self._custom = tr("Benutzerdefiniert (mm)")

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        self.combo = QComboBox()
        self.combo.addItems(self._before)
        self.combo.addItems(list(PAPER_SIZES_PT.keys()))
        self.combo.addItem(self._custom)
        self.combo.addItems(self._after)
        self.combo.currentIndexChanged.connect(self._on_combo)
        box.addWidget(self.combo)

        def spin(value):
            s = QDoubleSpinBox()
            s.setRange(10, 2000)
            s.setSuffix(" mm")
            s.setDecimals(1)
            s.setFixedWidth(90)
            s.setValue(value)                     # set BEFORE connecting
            s.valueChanged.connect(lambda _=None: self.changed.emit())
            return s

        self.width_mm = spin(width_mm)
        self.height_mm = spin(height_mm)
        self._w_lbl = QLabel(tr("B")); self._w_lbl.setObjectName("dimLabel")
        self._h_lbl = QLabel(tr("H")); self._h_lbl.setObjectName("dimLabel")
        row = QHBoxLayout(); row.setSpacing(4)
        row.addWidget(self._w_lbl); row.addWidget(self.width_mm)
        row.addWidget(self._h_lbl); row.addWidget(self.height_mm)
        row.addStretch()
        box.addLayout(row)

        self.landscape = QCheckBox(tr("Querformat"))
        self.landscape.toggled.connect(lambda _=None: self.changed.emit())
        box.addWidget(self.landscape)

        self._sync_custom_row()

    # ── what is selected ─────────────────────────────────────────────────────

    def current_text(self):
        return self.combo.currentText()

    def is_custom(self):
        return self.combo.currentText() == self._custom

    def special(self):
        """The selected non-paper entry, or None when a size is selected."""
        txt = self.combo.currentText()
        return txt if txt in self._before or txt in self._after else None

    def target_size_pt(self):
        """(width, height) in points with Querformat applied, or None when a
        non-paper entry is chosen."""
        if self.is_custom():
            size = (self.width_mm.value() * MM_TO_PT,
                    self.height_mm.value() * MM_TO_PT)
        else:
            size = PAPER_SIZES_PT.get(self.combo.currentText())
        if size is None:
            return None
        w, h = size
        return (h, w) if self.landscape.isChecked() else (w, h)

    # ── driving it ───────────────────────────────────────────────────────────

    def set_format(self, text):
        self.combo.setCurrentText(text)

    def set_custom_size(self, width_mm, height_mm):
        self.width_mm.setValue(width_mm)
        self.height_mm.setValue(height_mm)

    def reset(self):
        """Back to the first entry, without telling anyone."""
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)
        self._sync_custom_row()

    def _on_combo(self, _idx):
        self._sync_custom_row()
        self.changed.emit()

    def _sync_custom_row(self):
        show = self.is_custom()
        for w in (self.width_mm, self.height_mm, self._w_lbl, self._h_lbl):
            w.setVisible(show)
        # Nothing to turn when the entry is not a sheet.
        self.landscape.setEnabled(self.special() is None)
