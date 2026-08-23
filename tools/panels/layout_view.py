"""
Layout — the staging view.

Three things a job usually needs before it goes on paper, in the order they
have to happen: resize the page, arrange pages onto a sheet, add the marks. Each
is a stage you switch on; the preview shows all of them together; one
*Ausführen* applies them in a single pass.

This is not a fourth implementation of anything. Every stage calls the function
the matching tool already calls — `crop_scale_pdf` and `stamp_crop_marks_pdf`
from Zuschneiden/Skalieren, `_build_nup` from N-Up, `_booklet_sides` from
Broschüre — so a result produced here is the result those tools produce, and
they keep their own panels for anyone who wants one step on its own.

What the staging buys, beyond one run instead of three:

  * Cut marks stop being two half-features. In the tools they are a checkbox in
    N-Up *and* a whole mode in Zuschneiden/Skalieren that excludes cropping.
    Here cropping and marks are independent switches.
  * Broschüre gains margins, gaps and crop marks. It shares N-Up's engine — a
    booklet is a 2x1 grid with the pages in folding order — and never had them.
"""
import os
import tempfile

from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QWidget, QLabel,
                             QCheckBox, QComboBox, QSpinBox, QDoubleSpinBox,
                             QGridLayout, QFrame)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush

from tools._base import BasePanel, CurrentFileBar, LogBox, ToolScrollArea
from tools.app_state import AppState
from tools.i18n import tr
from tools.render.caches import _ThumbnailCache
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.document_cache import open_document as _open_pdf
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals
from tools.theme import INK, PAPER, _TV, _register_themed

from tools.panels._cropmarks import _crop_mark_segments
from tools.panels._shared import (MM_TO_PT, PaperFormatSelector, PreviewPane,
                                  _visible_size, row)
from tools.panels.crop_resize import crop_scale_pdf, stamp_crop_marks_pdf
from tools.panels.impose import _booklet_sides
from tools.panels.nup import _build_nup, _full_scale_problem, _nup_slot_rects


MODE_GRID    = 0
MODE_BOOKLET = 1


class Stage(QWidget):
    """One switchable step of the pipeline.

    Two widgets, deliberately not nested. `self` is the switch — a checkbox
    naming the stage — and lives in the 224 px column with the other two
    switches, so the whole pipeline can be read at a glance. `panel` holds the
    controls that switch governs and is placed in the wider options column
    beside it, because a paper-size dropdown and four margin fields do not fit
    in 224 px and were coming out clipped and half-legible.

    A stage that is off shows no panel at all, so the options column only ever
    carries the settings that are actually going to be applied.
    """
    changed = pyqtSignal()

    def __init__(self, title, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.check = QCheckBox(title)
        self.check.setObjectName("stageHead")
        self.check.toggled.connect(self._on_toggled)
        lay.addWidget(self.check)

        # Repeats the stage's name at the top of its controls: the switch that
        # turned them on is in another column, and an unlabelled block of
        # fields belongs to nothing.
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 14)
        pl.setSpacing(6)
        self.heading = QLabel(title)
        self.heading.setObjectName("optGroup")
        pl.addWidget(self.heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(5)
        pl.addLayout(self.body)
        self.panel.setVisible(False)

    def _on_toggled(self, _on):
        self.panel.setVisible(self.check.isChecked())
        self.changed.emit()

    def enabled(self):
        return self.check.isChecked()

    def add(self, w):
        if isinstance(w, (QHBoxLayout, QGridLayout, QVBoxLayout)):
            self.body.addLayout(w)
        else:
            self.body.addWidget(w)
        return w


def _hint(text):
    lb = QLabel(text)
    lb.setObjectName("dimLabel")
    lb.setWordWrap(True)
    return lb


class LayoutPanel(BasePanel):
    TITLE     = "Layout"
    SUBTITLE  = ""
    RUN_LABEL = "Ausführen"

    def __init__(self, parent=None):
        # Page images come from the same cache "Seiten verwalten" and N-Up fill,
        # so nothing is rendered twice and nothing renders on the GUI thread.
        self._dims_cache    = {}
        self._thumb_pending = set()
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        super().__init__(parent)

    # ── construction ─────────────────────────────────────────────────────────
    #
    # Not one widget: this view has no place of its own in the main stack, only
    # a preview (see MainWindow._build, which puts `preview_widget` there) — its
    # staging column is a second tenant of the app's 224px tool column, mounted
    # in and out by MainWindow._switch() exactly as ManagePanel's operations are
    # for "Seiten verwalten". `self` stays alive to own both and never appears
    # in a layout itself.

    def _setup(self):
        self.controls_widget = self._build_controls()
        self.preview_widget  = self._build_preview()
        _register_themed(self)
        self._apply_theme()
        # Settle the options column now that it exists — build_ui ran before
        # _build_preview, so its own call found nothing to hide.
        self._update_preview()

    def _build_controls(self):
        content = QWidget()
        content.setObjectName("layoutControlsW")
        self._controls_content = content
        lay = QVBoxLayout(content)
        lay.setContentsMargins(10, 8, 12, 10)
        lay.setSpacing(4)

        self.file_bar = CurrentFileBar()
        lay.addWidget(self.file_bar)
        lay.addWidget(self._rule())

        self.build_ui(lay)

        lay.addStretch()

        self.log = LogBox(tr("Log..."))
        self.log.setMaximumHeight(60)
        lay.addWidget(self.log)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.build_action_row(btn_row)
        # build_action_row leads with a stretch meant for a much wider sidebar
        # (the 340-480px one build_tool_sidebar gives every other split-view
        # tool). Here Ausführen fills the row instead, Stop keeps a fixed
        # width beside it — same idea as the concept's flex:1 run button.
        btn_row.takeAt(0)
        self.stop_btn.setMinimumWidth(0)
        self.stop_btn.setFixedWidth(56)
        self.run_btn.setMinimumWidth(0)
        btn_row.setStretchFactor(self.run_btn, 1)
        lay.addLayout(btn_row)

        # build_ui() ran before run_btn existed, so its settling call to
        # _update_preview() found nothing to disable yet — without this,
        # Ausführen opens enabled, in the accent colour, until the first
        # checkbox is touched, even though every stage starts off.
        self._update_preview()

        scroll = ToolScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _build_preview(self):
        """The main area: the options for whatever is switched on, and the
        preview of the result."""
        outer = QWidget()
        lay = QHBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        opts = QWidget()
        opts.setObjectName("layoutOptions")
        self._opts_content = opts
        ol = QVBoxLayout(opts)
        ol.setContentsMargins(16, 14, 16, 14)
        ol.setSpacing(0)
        for st in (self.st_crop, self.st_arr, self.st_marks):
            ol.addWidget(st.panel)
        ol.addStretch()

        # Comboboxes size themselves to their widest item; let them shrink to
        # the column instead of setting its width for it.
        for combo in opts.findChildren(QComboBox):
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)

        self._opts_scroll = ToolScrollArea()
        self._opts_scroll.setWidgetResizable(True)
        self._opts_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._opts_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._opts_scroll.setWidget(opts)
        self._opts_scroll.setFixedWidth(300)
        lay.addWidget(self._opts_scroll)

        right_w = self.build_tool_right_panel()
        right_l = right_w.layout()
        self._pane = PreviewPane(self._render_preview, header=tr("Vorschau"))
        self._preview      = self._pane.label
        self._preview_info = self._pane.info
        right_l.addWidget(self._pane)
        lay.addWidget(right_w, 1)
        return outer

    def _apply_theme(self):
        t = _TV
        # No border-right of its own here either — see ManagePanel._apply_theme.
        self._controls_content.setStyleSheet(
            f"QWidget#layoutControlsW{{background:{t['sidebar_bg']};}}")
        self.controls_widget.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._tool_right_w.setStyleSheet(
            f"QWidget#toolRightPanel{{background:{t['viewer_bg']};}}")
        self._preview.setStyleSheet(f"background:{t['card_bg']};")
        self._opts_content.setStyleSheet(
            f"QWidget#layoutOptions{{background:{t['panel_bg']};"
            f"border-right:1px solid {t['border']};}}")
        self._opts_scroll.setStyleSheet(
            f"QScrollArea{{background:{t['panel_bg']};border:none;}}")
        # Full text colour, at the size the rest of the app writes at — these
        # are the pipeline itself, not captions about it, and they were being
        # drawn dim-grey at 11 px as though they were hints. The box is 16 px:
        # it is the only control in the column and has to look pressable.
        for st in (self.st_crop, self.st_arr, self.st_marks):
            st.check.setStyleSheet(
                f"QCheckBox#stageHead{{color:{t['text']};font-size:13px;"
                f"padding:9px 0 9px 2px;spacing:9px;}}"
                f"QCheckBox#stageHead::indicator{{width:16px;height:16px;"
                f"border:1px solid {t['btn_brd']};border-radius:4px;"
                f"background:{t['btn_bg']};}}"
                f"QCheckBox#stageHead::indicator:hover{{border-color:{t['acc']};}}"
                f"QCheckBox#stageHead::indicator:checked{{"
                f"background:{t['acc']};border-color:{t['acc']};}}"
                f"QCheckBox#stageHead:checked{{color:{t['acc']};font-weight:bold;}}")
            st.heading.setStyleSheet(
                f"QLabel#optGroup{{color:{t['acc']};font-size:11px;"
                f"font-weight:bold;letter-spacing:1px;"
                f"border-bottom:1px solid {t['border']};padding-bottom:5px;}}")
        self._pane.refresh()

    def _update_preview(self):
        # build_ui() runs inside _build_controls(), before _setup() has made
        # the pane — and it settles the initial control state, which fires the
        # same handlers a user edit does.
        pane = getattr(self, "_pane", None)
        if pane is not None:
            pane.refresh()
        any_on = (self.st_crop.enabled() or self.st_arr.enabled()
                  or self.st_marks.enabled())
        # Ausführen stays truly inert, not merely styled to look that way,
        # while every stage is off — matching the preview's own "ändert
        # nichts" message instead of just echoing it.
        if hasattr(self, "run_btn"):
            self.run_btn.setEnabled(any_on)
        # With nothing staged there are no settings to show, and 300 px of
        # empty column beside the preview is 300 px the preview should have.
        if hasattr(self, "_opts_scroll"):
            self._opts_scroll.setVisible(any_on)

    def build_ui(self, layout):
        self._syncing = False

        def mm(val=0.0, lo=-500.0, hi=500.0, w=92):
            s = QDoubleSpinBox()
            s.setRange(lo, hi)
            s.setSuffix(" mm")
            s.setDecimals(1)
            s.setValue(val)
            s.setFixedWidth(w)
            s.valueChanged.connect(self._update_preview)
            return s

        def r(label, widget):
            # 68, not build_tool_sidebar's 220 or even the row() default: this
            # column is 224px, not the 340-480px every other split-view tool
            # gets, and the horizontal scrollbar (see _build_controls) is the
            # fallback for whatever a shorter label still cannot buy back.
            return row(label, widget, label_w=92)

        # ── Stage 1 · Zuschneiden / Skalieren ────────────────────────────────
        self.st_crop = Stage(tr("Zuschneiden / Skalieren"))
        self.st_crop.changed.connect(self._update_preview)

        self.crop_fmt = PaperFormatSelector(before=[tr("— Kein —")])
        self.crop_fmt.changed.connect(self._on_crop_format)
        self.st_crop.add(r(tr("Format"), self.crop_fmt))

        # One margin per row, not the two-per-row grid the wide sidebar tools
        # use: two labelled 64px spinboxes side by side do not fit in 224px,
        # and this column already scrolls sideways as a last resort — better
        # not to lean on that for four fields every stage opens with.
        self.ct, self.cb, self.cl, self.cr = mm(), mm(), mm(), mm()
        for label, w in ((tr("Oben"), self.ct), (tr("Unten"), self.cb),
                         (tr("Links"), self.cl), (tr("Rechts"), self.cr)):
            self.st_crop.add(r(label, w))

        self.crop_sync = QCheckBox(tr("Alle Ränder gleich"))
        self.crop_sync.toggled.connect(self._on_crop_sync)
        self.ct.valueChanged.connect(self._sync_crop_margins)
        self.st_crop.add(self.crop_sync)

        self.scale_pct = QDoubleSpinBox()
        self.scale_pct.setRange(5.0, 400.0)
        self.scale_pct.setDecimals(1)
        self.scale_pct.setSuffix(" %")
        self.scale_pct.setValue(100.0)
        self.scale_pct.setSingleStep(5.0)
        self.scale_pct.setFixedWidth(92)
        self.scale_pct.valueChanged.connect(self._on_pct_edited)
        self.st_crop.add(r(tr("Skalierung"), self.scale_pct))

        self.fit_content = QCheckBox(tr("Inhalt einpassen"))
        self.fit_content.toggled.connect(self._update_preview)
        self.keep_ratio = QCheckBox(tr("Proportionen behalten"))
        self.keep_ratio.setChecked(True)
        self.keep_ratio.toggled.connect(self._update_preview)
        self.apply_all = QCheckBox(tr("Alle Seiten"))
        self.apply_all.setToolTip(tr(
            "Aus: nur die in „Seiten verwalten“ ausgewählten Seiten."))
        self.apply_all.toggled.connect(self._update_preview)
        for w in (self.fit_content, self.keep_ratio, self.apply_all):
            self.st_crop.add(w)
        layout.addWidget(self.st_crop)
        layout.addWidget(self._rule())

        # ── Stage 2 · Anordnung ──────────────────────────────────────────────
        self.st_arr = Stage(tr("Anordnung"))
        self.st_arr.changed.connect(self._update_preview)

        self.mode = QComboBox()
        self.mode.addItems([tr("Raster"), tr("Broschüre · Sattelheftung")])
        self.mode.currentIndexChanged.connect(self._on_mode)
        self.st_arr.add(r(tr("Modus"), self.mode))

        self.cols = QSpinBox(); self.cols.setRange(1, 10); self.cols.setValue(2)
        self.rows = QSpinBox(); self.rows.setRange(1, 10); self.rows.setValue(1)
        for s in (self.cols, self.rows):
            s.setFixedWidth(40)
            s.valueChanged.connect(self._update_preview)
        gridrow = QHBoxLayout()
        gridrow.setSpacing(6)
        lb = QLabel(tr("Raster")); lb.setFixedWidth(92)
        gridrow.addWidget(lb)
        gridrow.addWidget(self.cols)
        x = QLabel("×"); x.setObjectName("dimLabel")
        gridrow.addWidget(x)
        gridrow.addWidget(self.rows)
        gridrow.addStretch()
        self._grid_row = gridrow
        self.st_arr.add(gridrow)

        self.src_mode = QComboBox()
        self.src_mode.addItems([tr("Alle Seiten der Reihe nach"),
                                tr("Jede Seite wiederholt")])
        self.src_mode.currentIndexChanged.connect(self._update_preview)
        self._src_row = r(tr("Quelle"), self.src_mode)
        self.st_arr.add(self._src_row)

        self.AUTO_FORMAT = tr("Wie Quellseite × Anordnung  (automatisch)")
        self.sheet_fmt = PaperFormatSelector(after=[self.AUTO_FORMAT])
        self.sheet_fmt.set_format(self.AUTO_FORMAT)
        self.sheet_fmt.changed.connect(self._update_preview)
        self.st_arr.add(r(tr("Blattformat"), self.sheet_fmt))

        self.mt, self.mb = mm(5, lo=0), mm(5, lo=0)
        self.ml, self.mr = mm(5, lo=0), mm(5, lo=0)
        self.gh, self.gv = mm(3, lo=0), mm(3, lo=0)
        # No "Rand " prefix: they sit under the Anordnung heading already, and
        # the repeated word cost more of a 224px row than it explained.
        for label, w in ((tr("Oben"), self.mt), (tr("Unten"), self.mb),
                         (tr("Links"), self.ml), (tr("Rechts"), self.mr),
                         (tr("Abstand H"), self.gh), (tr("Abstand V"), self.gv)):
            self.st_arr.add(r(label, w))

        self.sync_margins = QCheckBox(tr("Alle Ränder gleich"))
        self.sync_gaps    = QCheckBox(tr("Beide Abstände gleich"))
        self.mt.valueChanged.connect(self._sync_sheet_margins)
        self.gh.valueChanged.connect(self._sync_sheet_gaps)
        self.st_arr.add(self.sync_margins)
        self.st_arr.add(self.sync_gaps)

        # Same label N-Up uses, line-broken: unbroken it is the one control
        # in this column wider than 224px, and QCheckBox has no word-wrap of
        # its own. Broken after the same words in either language, so the
        # translation stays the one entry N-Up's checkbox reads too.
        _full_scale_label = tr("Quellseiten in Originalgröße (100 %)").replace(
            " (100", "\n(100")
        self.full_scale = QCheckBox(_full_scale_label)
        self.full_scale.setToolTip(tr(
            "Seiten werden nicht verkleinert. Das Ausgabeformat muss groß "
            "genug sein, sonst meldet das Werkzeug, wie groß es sein müsste."))
        self.full_scale.toggled.connect(self._update_preview)
        self.st_arr.add(self.full_scale)
        layout.addWidget(self.st_arr)
        layout.addWidget(self._rule())

        # ── Stage 3 · Marken ─────────────────────────────────────────────────
        self.st_marks = Stage(tr("Marken"))
        self.st_marks.changed.connect(self._update_preview)

        self.cut_marks = QCheckBox(tr("Schnittmarken"))
        self.cut_marks.setChecked(True)
        self.cut_marks.toggled.connect(self._update_preview)
        self.st_marks.add(self.cut_marks)

        self.mark_fmt = PaperFormatSelector(before=[tr("— Wie Seite —")])
        self.mark_fmt.changed.connect(self._update_preview)
        self.st_marks.add(r(tr("Endformat"), self.mark_fmt))
        self._mark_hint = _hint(tr(
            "Ohne Anordnung: Marken für dieses Endformat, mittig auf der Seite. "
            "Mit Anordnung: an den Rändern der Slots."))
        self.st_marks.add(self._mark_hint)
        layout.addWidget(self.st_marks)

        self._on_mode()

    def _rule(self):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color:{_TV['border']};max-height:1px;")
        return f

    # ── small handlers ───────────────────────────────────────────────────────

    def _on_mode(self, *_):
        booklet = self.mode.currentIndex() == MODE_BOOKLET
        # A booklet is a 2x1 grid whose page order is decided for it, so the
        # grid and source controls have no meaning there.
        for i in range(self._grid_row.count()):
            w = self._grid_row.itemAt(i).widget()
            if w is not None:
                w.setVisible(not booklet)
        for i in range(self._src_row.count()):
            w = self._src_row.itemAt(i).widget()
            if w is not None:
                w.setVisible(not booklet)
        self._update_preview()

    def _on_crop_sync(self, on):
        if on:
            self._set_crop_margins(max(self.ct.value(), self.cb.value(),
                                       self.cl.value(), self.cr.value()))
            self._update_preview()

    def _sync_crop_margins(self, val):
        if self.crop_sync.isChecked() and not self._syncing:
            self._set_crop_margins(val)
            self._update_preview()

    def _set_crop_margins(self, val):
        self._syncing = True
        for w in (self.ct, self.cb, self.cl, self.cr):
            w.blockSignals(True); w.setValue(val); w.blockSignals(False)
        self._syncing = False

    def _sync_sheet_margins(self, _v):
        if self.sync_margins.isChecked():
            for w in (self.mb, self.ml, self.mr):
                w.blockSignals(True); w.setValue(self.mt.value()); w.blockSignals(False)
            self._update_preview()

    def _sync_sheet_gaps(self, _v):
        if self.sync_gaps.isChecked():
            self.gv.blockSignals(True); self.gv.setValue(self.gh.value())
            self.gv.blockSignals(False)
            self._update_preview()

    def _on_crop_format(self):
        """Picking a format writes the four margins that produce it, the way
        Zuschneiden/Skalieren does — so the boxes always say what will happen."""
        size = self.crop_fmt.target_size_pt()
        if size is None:
            self._set_crop_margins(0.0)
            self._update_preview()
            return
        dims = self._page_dims_safe()
        if dims is None:
            self._update_preview()
            return
        _idx, pw, ph, _n = dims
        self._syncing = True
        for w in (self.ct, self.cb, self.cl, self.cr):
            w.blockSignals(True)
        try:
            dw = (pw - size[0]) / MM_TO_PT / 2
            dh = (ph - size[1]) / MM_TO_PT / 2
            self.cl.setValue(dw); self.cr.setValue(dw)
            self.ct.setValue(dh); self.cb.setValue(dh)
        finally:
            for w in (self.ct, self.cb, self.cl, self.cr):
                w.blockSignals(False)
            self._syncing = False
        self._fmt_margins = self._margins_mm()
        self._update_preview()

    def _on_pct_edited(self, val):
        """A typed percentage resizes the page centred, exactly as it does in
        Zuschneiden/Skalieren — and, as there, turns content-fit on, because a
        page at 80 % whose content stayed its old size is not what it means."""
        if self._syncing:
            return
        dims = self._page_dims_safe()
        if dims is None:
            return
        _idx, pw, ph, _n = dims
        self.crop_fmt.reset()
        blocked = self.fit_content.blockSignals(True)
        self.fit_content.setChecked(True)
        self.fit_content.blockSignals(blocked)
        f = val / 100.0
        self._syncing = True
        for w in (self.ct, self.cb, self.cl, self.cr):
            w.blockSignals(True)
        try:
            self.cl.setValue(pw * (1 - f) / MM_TO_PT / 2)
            self.cr.setValue(pw * (1 - f) / MM_TO_PT / 2)
            self.ct.setValue(ph * (1 - f) / MM_TO_PT / 2)
            self.cb.setValue(ph * (1 - f) / MM_TO_PT / 2)
        finally:
            for w in (self.ct, self.cb, self.cl, self.cr):
                w.blockSignals(False)
            self._syncing = False
        self._fmt_margins = None
        self._update_preview()

    # ── geometry, shared by the preview and the run ──────────────────────────

    def _margins_mm(self):
        return (self.ct.value(), self.cb.value(), self.cl.value(), self.cr.value())

    def _crop_margins_pt(self, pw, ph):
        """(top, bottom, left, right) in points for a page of (pw, ph).

        Derived from the chosen format when the four boxes still hold exactly
        what it put there — so a document of mixed page sizes comes out all one
        size and centred, rather than every page losing the first page's
        millimetres. Same rule as Zuschneiden/Skalieren's _effective_margins_pt.
        """
        size = self.crop_fmt.target_size_pt()
        if size is not None and getattr(self, "_fmt_margins", None) == self._margins_mm():
            dw = (pw - size[0]) / 2
            dh = (ph - size[1]) / 2
            return dh, dh, dw, dw
        return (self.ct.value() * MM_TO_PT, self.cb.value() * MM_TO_PT,
                self.cl.value() * MM_TO_PT, self.cr.value() * MM_TO_PT)

    def _staged_page_size(self, pw, ph):
        """The page size the arrangement stage will be given, after cropping."""
        if not self.st_crop.enabled():
            return pw, ph
        t, b, l, r = self._crop_margins_pt(pw, ph)
        return pw - l - r, ph - t - b

    def _layout_params(self, src_pw, src_ph):
        """N-Up's parameter tuple, from this panel's controls. Identical maths
        to NUpPanel._get_layout_params so the two cannot drift."""
        PT = MM_TO_PT
        if self.mode.currentIndex() == MODE_BOOKLET:
            cols, rows = 2, 1
        else:
            cols, rows = self.cols.value(), self.rows.value()
        sheet = self.sheet_fmt.target_size_pt()      # None = automatic
        mt = self.mt.value() * PT; mb = self.mb.value() * PT
        ml = self.ml.value() * PT; mr = self.mr.value() * PT
        gh = self.gh.value() * PT; gv = self.gv.value() * PT
        if sheet is None:
            out_w = src_pw * cols + ml + mr + gh * (cols - 1)
            out_h = src_ph * rows + mt + mb + gv * (rows - 1)
        else:
            out_w, out_h = sheet
        slot_w = (out_w - ml - mr - gh * (cols - 1)) / cols
        slot_h = (out_h - mt - mb - gv * (rows - 1)) / rows
        return out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows

    def _get_target_pages(self):
        """(position, uid) of the pages stage 1 acts on — positions in the
        document as displayed, matching current_pdf()'s flattened snapshot."""
        state = AppState.get()
        model = state.page_model
        if model and model.selected:
            return [(pos, uid) for pos, uid in enumerate(model.order)
                    if uid in model.selected]
        if model and model.order:
            cur = max(0, min(state.current_page, len(model.order) - 1))
            return [(cur, model.order[cur])]
        return []

    # ── page images ──────────────────────────────────────────────────────────

    def _on_thumb_ready(self, *_):
        self._thumb_pending.clear()
        self._pane.refresh()

    def _page_dims_safe(self):
        path = self.current_pdf()
        if not path or not os.path.isfile(path):
            return None
        try:
            return self._page_dims(path)
        except Exception:
            return None

    def _page_dims(self, pdf_path):
        idx = max(0, AppState.get().current_page)
        key = (pdf_path, idx)
        if key in self._dims_cache:
            return self._dims_cache[key]
        with _pdfium_lock:
            doc = _open_pdf(pdf_path)
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
        key = (pdf_path, page_idx, 0, render_w)
        exact = _ThumbnailCache.get(key)
        if exact is not None:
            return exact
        if key not in self._thumb_pending:
            self._thumb_pending.add(key)
            _render_queue.submit(
                _ThumbTask(0, 0, pdf_path, page_idx, 0, render_w,
                           self._thumb_signals), 1)
        return _ThumbnailCache.get_any(pdf_path, page_idx, 0)

    def _staged_pixmap(self, src_pm, pw, ph):
        """`src_pm` after stage 1: either cropped to the new box, or scaled into
        it — the two branches Zuschneiden/Skalieren's preview draws."""
        if not self.st_crop.enabled() or src_pm is None:
            return src_pm
        t, b, l, r = self._crop_margins_pt(pw, ph)
        new_w, new_h = pw - l - r, ph - t - b
        if new_w < 1 or new_h < 1:
            return src_pm
        px_per_pt = src_pm.width() / max(pw, 1e-6)
        out_w = max(1, int(new_w * px_per_pt))
        out_h = max(1, int(new_h * px_per_pt))
        out = QPixmap(out_w, out_h)
        out.fill(QColor(PAPER))
        p = QPainter(out)
        if self.fit_content.isChecked():
            ar = (Qt.AspectRatioMode.KeepAspectRatio if self.keep_ratio.isChecked()
                  else Qt.AspectRatioMode.IgnoreAspectRatio)
            sc = src_pm.scaled(out_w, out_h, ar,
                               Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((out_w - sc.width()) // 2, (out_h - sc.height()) // 2, sc)
        else:
            sx = int(l * px_per_pt)
            sy = int(t * px_per_pt)
            p.drawPixmap(max(0, -sx), max(0, -sy), src_pm,
                         max(0, sx), max(0, sy), out_w, out_h)
        p.end()
        return out

    # ── preview ──────────────────────────────────────────────────────────────

    def _render_preview(self, avail_w, avail_h, zoom):
        pdf_path = self.current_pdf()
        if not pdf_path:
            return None, tr("Keine PDF geoeffnet")
        if not os.path.isfile(pdf_path):
            return None, tr("Datei nicht mehr auffindbar")

        page_idx, pw, ph, n_total = self._page_dims(pdf_path)
        eff_w, eff_h = self._staged_page_size(pw, ph)
        if eff_w < 1.0 or eff_h < 1.0:
            return None, tr("Ränder zu groß — von der Seite bleibt nichts übrig.")

        if not (self.st_crop.enabled() or self.st_arr.enabled()
                or self.st_marks.enabled()):
            info = tr("Nichts aktiv — Ausführen ändert nichts.")
        else:
            info = None

        if self.st_arr.enabled():
            return self._render_sheet(pdf_path, page_idx, n_total,
                                      eff_w, eff_h, pw, ph,
                                      avail_w, avail_h, zoom, info)
        return self._render_single(pdf_path, page_idx, eff_w, eff_h, pw, ph,
                                   avail_w, avail_h, zoom, info)

    def _render_single(self, pdf_path, page_idx, eff_w, eff_h, pw, ph,
                       avail_w, avail_h, zoom, info):
        cs = min(avail_w / eff_w, avail_h / eff_h) * zoom
        cw = max(1, int(eff_w * cs)); chh = max(1, int(eff_h * cs))
        result = QPixmap(cw, chh)
        result.fill(QColor(_TV['card_bg']))
        p = QPainter(result)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(PAPER)))
        p.drawRect(0, 0, cw - 1, chh - 1)

        render_w = max(160, min(900, int(eff_w)))
        img = self._page_image(pdf_path, page_idx, render_w)
        pending = img is None
        if img is not None:
            pm = self._staged_pixmap(QPixmap.fromImage(img), pw, ph)
            sc = pm.scaled(cw, chh, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((cw - sc.width()) // 2, (chh - sc.height()) // 2, sc)

        marks_rect = self._page_marks_rect(eff_w, eff_h)
        if marks_rect is not None:
            p.setPen(QPen(QColor(INK), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments([marks_rect]):
                p.drawLine(int(a * cs), int((eff_h - b) * cs),
                           int(c * cs), int((eff_h - d) * cs))

        p.setPen(QPen(QColor(_TV['acc']), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(0, 0, cw - 1, chh - 1)
        p.end()

        txt = info or f"{eff_w/MM_TO_PT:.0f}×{eff_h/MM_TO_PT:.0f} mm"
        if pending:
            txt += "  ·  " + tr("rendert …")
        return result, txt

    def _page_marks_rect(self, eff_w, eff_h):
        """The rectangle page-level cut marks are drawn on, or None.

        Only when the arrangement stage is off: with it on, the marks belong to
        the slots and N-Up draws them. `— Wie Seite —` marks the page itself,
        which is what an operator means by "mark where this gets cut" when the
        page is already the finished size.
        """
        if not (self.st_marks.enabled() and self.cut_marks.isChecked()):
            return None
        if self.st_arr.enabled():
            return None
        size = self.mark_fmt.target_size_pt()
        tw, th = size if size is not None else (eff_w, eff_h)
        x0 = (eff_w - tw) / 2
        y0 = (eff_h - th) / 2
        return (x0, y0, x0 + tw, y0 + th)

    def _render_sheet(self, pdf_path, page_idx, n_total, eff_w, eff_h, pw, ph,
                      avail_w, avail_h, zoom, info):
        params = self._layout_params(eff_w, eff_h)
        (out_w, out_h, mt, mb, ml, mr, gh, gv, slot_w, slot_h, cols, rows) = params
        if slot_w <= 1.0 or slot_h <= 1.0:
            return None, tr("Abstände zu groß — kein Platz für Inhalt.")
        booklet = self.mode.currentIndex() == MODE_BOOKLET
        full = self.full_scale.isChecked() and not booklet
        if full:
            problem = _full_scale_problem(eff_w, eff_h, params)
            if problem:
                return None, problem

        cs = min(avail_w / out_w, avail_h / out_h) * zoom
        cw = max(1, int(out_w * cs)); chh = max(1, int(out_h * cs))

        n_slot = cols * rows
        if booklet:
            sides = _booklet_sides(n_total)
            slot_pages = sides[0] if sides else [None, None]
        elif self.src_mode.currentIndex() == 1:
            slot_pages = [page_idx] * n_slot
        else:
            slot_pages = [(page_idx + s if page_idx + s < n_total else None)
                          for s in range(n_slot)]

        render_w = max(120, min(800, int(min(slot_w, eff_w))))
        pms = {}
        pending = False
        for pg in {p for p in slot_pages if p is not None}:
            img = self._page_image(pdf_path, pg, render_w)
            if img is None:
                pending = True
                pms[pg] = None
            else:
                pms[pg] = self._staged_pixmap(QPixmap.fromImage(img), pw, ph)

        result = QPixmap(cw, chh)
        result.fill(QColor(_TV['card_bg']))
        p = QPainter(result)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QBrush(QColor(PAPER)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(0, 0, cw - 1, chh - 1)

        for i in range(n_slot):
            col_i = i % cols; row_i = i // cols
            sx = int((ml + col_i * (slot_w + gh)) * cs)
            sy = int((mt + row_i * (slot_h + gv)) * cs)
            sw = max(1, int(slot_w * cs)); sh = max(1, int(slot_h * cs))
            p.setBrush(QBrush(QColor(200, 210, 230, 60)))
            p.setPen(QPen(QColor(120, 140, 180, 120), 1))
            p.drawRect(sx, sy, sw, sh)
            pg = slot_pages[i] if i < len(slot_pages) else None
            pm = pms.get(pg) if pg is not None else None
            if pm is None:
                continue
            scale = 1.0 if full else min(slot_w / eff_w, slot_h / eff_h)
            tw = max(1, int(eff_w * scale * cs))
            th = max(1, int(eff_h * scale * cs))
            sc = pm.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            ox = sx + (sw - sc.width()) // 2
            oy = sy + (sh - sc.height()) // 2
            p.drawPixmap(ox, oy, sc)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(_TV['acc']), 1))
            p.drawRect(ox, oy, sc.width() - 1, sc.height() - 1)

        if self.st_marks.enabled() and self.cut_marks.isChecked():
            p.setPen(QPen(QColor(INK), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments(_nup_slot_rects(params, n_slot)):
                p.drawLine(int(a * cs), int((out_h - b) * cs),
                           int(c * cs), int((out_h - d) * cs))

        p.setPen(QPen(QColor(120, 160, 255, 180), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(0, 0, cw - 1, chh - 1)
        p.end()

        if info:
            txt = info
        elif booklet:
            txt = (f"{out_w/MM_TO_PT:.0f}×{out_h/MM_TO_PT:.0f} mm  |  "
                   + tr("Broschüre"))
        else:
            txt = (f"{out_w/MM_TO_PT:.0f}×{out_h/MM_TO_PT:.0f} mm  |  "
                   f"{cols}×{rows} = {n_slot} Slots")
        if pending:
            txt += "  ·  " + tr("rendert …")
        return result, txt

    # ── the run ──────────────────────────────────────────────────────────────

    def _run_action(self):
        import pikepdf
        src_path = self.require_pdf()

        stages = [self.st_crop.enabled(), self.st_arr.enabled(),
                  self.st_marks.enabled()]
        if not any(stages):
            raise ValueError(tr("Nichts aktiv — bitte zuerst eine Stufe einschalten."))

        out_path = self.save_pdf(tr("Layout speichern als"))
        if not out_path:
            raise ValueError(tr("Kein Ausgabepfad."))

        # Everything the worker needs, read here on the GUI thread.
        with pikepdf.open(src_path) as doc:
            n_total = len(doc.pages)
            if not n_total:
                raise ValueError(tr("Die PDF hat keine Seiten."))
            rep = max(0, min(AppState.get().current_page, n_total - 1))
            src_pw, src_ph = _visible_size(doc.pages[rep])
            sizes = [_visible_size(pg) for pg in doc.pages]

        if self.apply_all.isChecked():
            target_origs = set(range(n_total))
        else:
            pages = self._get_target_pages()
            if not pages:
                raise ValueError(tr("Keine Seiten ausgewaehlt."))
            target_origs = {pos for pos, _ in pages}

        plan = self._build_plan(src_path, out_path, n_total,
                                src_pw, src_ph, sizes, target_origs)
        self.run_async(plan, on_done=self._done,
                       busy_label=tr("Layout läuft …"))
        return None

    def _build_plan(self, src_path, out_path, n_total,
                    src_pw, src_ph, sizes, target_origs):
        """Freeze every widget value into one callable the worker can run.

        Nothing below touches a widget: the stages run on a worker thread, and
        reading a QSpinBox from there is exactly the kind of thing that works
        until it does not.
        """
        do_crop   = self.st_crop.enabled()
        do_arr    = self.st_arr.enabled()
        do_marks  = self.st_marks.enabled() and self.cut_marks.isChecked()
        booklet   = self.mode.currentIndex() == MODE_BOOKLET
        fit       = self.fit_content.isChecked()
        keep      = self.keep_ratio.isChecked()
        margins   = self._crop_margins_pt          # pure arithmetic, no widgets…
        marg_vals = self._margins_mm()             # …but freeze the numbers too
        fmt_size  = self.crop_fmt.target_size_pt()
        fmt_live  = getattr(self, "_fmt_margins", None) == marg_vals
        full      = self.full_scale.isChecked() and not booklet
        src_seq   = self.src_mode.currentIndex() == 0
        mark_size = self.mark_fmt.target_size_pt()
        eff_w, eff_h = self._staged_page_size(src_pw, src_ph)
        params    = self._layout_params(eff_w, eff_h) if do_arr else None
        if do_arr:
            cols, rows = params[10], params[11]
            n_slot = cols * rows
            if params[8] <= 1.0 or params[9] <= 1.0:
                raise ValueError(tr("Abstände zu groß — kein Platz für Inhalt."))
            if full:
                # At 100 % nothing is scaled down, so the *largest* page decides
                # whether the job is possible — measured after cropping.
                mx = my = 0.0
                for w, h in sizes:
                    cw, chh = ((w - margins(w, h)[2] - margins(w, h)[3],
                                h - margins(w, h)[0] - margins(w, h)[1])
                               if do_crop else (w, h))
                    mx = max(mx, cw); my = max(my, chh)
                problem = _full_scale_problem(mx, my, params)
                if problem:
                    raise ValueError(problem)

        def margins_pt(pw, ph):
            """The same rule _crop_margins_pt applies, without the widgets."""
            if fmt_size is not None and fmt_live:
                return ((ph - fmt_size[1]) / 2, (ph - fmt_size[1]) / 2,
                        (pw - fmt_size[0]) / 2, (pw - fmt_size[0]) / 2)
            t, b, l, r = marg_vals
            return t * MM_TO_PT, b * MM_TO_PT, l * MM_TO_PT, r * MM_TO_PT

        tmpdir = tempfile.mkdtemp(prefix="folio_layout_")

        def run(report):
            src = src_path
            steps = []
            if do_crop:
                report(tr("Zuschneiden / Skalieren …"))
                nxt = os.path.join(tmpdir, "1_crop.pdf")
                crop_scale_pdf(src, nxt, target_origs, margins_pt, fit, keep)
                src = nxt
                steps.append(tr("zugeschnitten"))
            if do_arr:
                report(tr("Anordnen …"))
                nxt = os.path.join(tmpdir, "2_arrange.pdf")
                if booklet:
                    sides = _booklet_sides(n_total)
                    src_pages = [i for side in sides for i in side]
                else:
                    if src_seq:
                        src_pages = list(range(n_total))
                    else:
                        src_pages = [p for p in range(n_total) for _ in range(n_slot)]
                    if len(src_pages) == 1:
                        src_pages = src_pages * n_slot
                    while len(src_pages) % n_slot:
                        src_pages.append(None)
                _build_nup(src, nxt, src_pages, params, n_slot, report,
                           crop_marks=do_marks,
                           fixed_scale=1.0 if full else None)
                src = nxt
                steps.append(tr("angeordnet"))
            if do_marks and not do_arr:
                report(tr("Marken setzen …"))
                nxt = os.path.join(tmpdir, "3_marks.pdf")
                tw, th = mark_size if mark_size is not None else (eff_w, eff_h)
                stamp_crop_marks_pdf(src, nxt, target_origs, tw, th)
                src = nxt
                steps.append(tr("markiert"))
            report(tr("Schreibe Datei …"))
            import shutil
            shutil.copyfile(src, out_path)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return out_path, ", ".join(steps)

        return run

    def _done(self, result):
        out_path, steps = result
        self.log.log(tr('Fertig — {p0}.').format(p0=steps))
        self.open_result(out_path, tr("Layout"))
