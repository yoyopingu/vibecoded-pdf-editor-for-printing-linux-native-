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

# The visible-page geometry lives in tools.pagebox so the print path and the
# viewer can use it without importing a panel. These names stay here: every
# tool already imports them from this module.
from tools.pagebox import (  # noqa: E402
    _display_matrix, _inherited_rotate, _mat_mul, _visible_box, _visible_size,
)


def _paper_sizes_pt():
    """The sizes to offer, keyed by the label shown in a dropdown.

    Built from tools/paper.py, which is the one list the operator edits in
    Einstellungen. This used to be a table of its own with nine entries, so
    the tools reached A0 and had never heard of SRA3 — a job could be printed
    on SRA3 and not cropped to it.
    """
    from tools.paper import label, sizes
    return {label(name): size for name, size in sizes().items()}
LABEL_W = 220   # Feste Label-Breite — passt alle deutschen Bezeichnungen


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
        self._rendering = False

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
        # A QLabel showing a pixmap reports that pixmap as its minimum size, and
        # that closed a loop that ate the preview: a render wider than the pane
        # grew the label, which grew the pane, whose resizeEvent refreshed the
        # preview with more room, which produced a wider render again. Typing one
        # margin into Crop was enough to start it — the pixmap covers the ghost
        # of the original page, which is by definition larger than the cropped
        # one — and it ran until the preview was thousands of pixels across and
        # nothing of it was on screen. Measured from an 889x761 label and a
        # 526x745 render: 2638x3729 and 3958x5597 within thirty event loops.
        #
        # The pixmap must not get a say in how big the label is. It is clipped
        # to the space there is, which is also what zooming past the pane should
        # do.
        self.label.setMinimumSize(1, 1)
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
        if not self._render_fn or self._rendering:
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
        # Not re-entrant: setting the pixmap can lay the pane out again, and a
        # layout pass that comes back round here would render on top of itself.
        self._rendering = True
        try:
            pm, info = self._render_fn(avail_w, avail_h, self.zoom)
        except Exception as ex:
            self.label.setText(tr('Vorschau: {p0}').format(p0=ex))
            return
        finally:
            self._rendering = False
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
    tools/paper.py so both offer the same formats — and the same ones the
    print dialog offers, edited in one place in Einstellungen.

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
        self.combo.addItems(list(_paper_sizes_pt().keys()))
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
            size = _paper_sizes_pt().get(self.combo.currentText())
        if size is None:
            return None
        w, h = size
        return (h, w) if self.landscape.isChecked() else (w, h)

    # ── driving it ───────────────────────────────────────────────────────────

    def set_format(self, text):
        """Pick a format by its label, or by its bare name.

        setCurrentText does nothing at all when the string is not in the list,
        so a label that changed shape left every caller silently on whatever
        was selected before — the size check downstream then had nothing to
        check against and passed. Matching the name as well means the label
        can be rewritten without taking callers with it.
        """
        idx = self.combo.findText(text)
        if idx < 0:
            for i in range(self.combo.count()):
                entry = self.combo.itemText(i)
                if entry == text or entry.split("  (")[0] == text:
                    idx = i
                    break
        if idx >= 0:
            self.combo.setCurrentIndex(idx)

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
