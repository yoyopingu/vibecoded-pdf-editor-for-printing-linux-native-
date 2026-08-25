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
                             QGridLayout, QFrame, QScrollArea)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QBrush

from tools.panels.base import BasePanel, CurrentFileBar, ToolScrollArea
from tools.shell.protokoll import LogAdapter
from tools.app_state import AppState
from tools.i18n import tr
from tools.render.caches import _ThumbnailCache
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.document_cache import open_document as _open_pdf
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals
from tools.theme import INK, PAPER, _TV, _register_themed
from tools.shell.icons import icon

from tools.panels._cropmarks import _crop_mark_segments
from tools.panels._shared import (MM_TO_PT, PaperFormatSelector,
                                  _visible_size, row)
from tools.panels.crop_resize import crop_scale_pdf, stamp_crop_marks_pdf
from tools.panels.impose import _booklet_sides
from tools.panels.nup import _build_nup, _full_scale_problem, _nup_slot_rects


MODE_GRID    = 0
MODE_BOOKLET = 1


class Stage(QWidget):
    """One switchable step of the pipeline.

    `self` is the switch — a full-width clickable *card* — and lives in the
    224 px column with the other two cards, so the whole pipeline can be read
    at a glance. The card carries an icon, the stage's name and a one-line
    description, and a toggle switch on the right. `panel` holds the controls
    that switch governs and is placed in the wider options column beside it,
    because a paper-size dropdown and four margin fields do not fit in 224 px.

    `self.check` is the switch itself, a checkable widget styled as a pill.
    The tests and the run logic read/write it through isChecked()/setChecked(),
    and the card as a whole toggles it too. A stage that is off shows no panel
    at all, so the options column only ever carries the settings that are
    actually going to be applied.
    """
    changed = pyqtSignal()

    def __init__(self, title, desc, icon_name, parent=None):
        super().__init__(parent)
        self.setObjectName("stage")
        self._icon_name = icon_name

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # Icon (left).
        self._icon_lbl = QLabel()
        self._icon_lbl.setObjectName("stageIcon")
        self._icon_lbl.setFixedSize(18, 18)
        self._icon_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self._icon_lbl)

        # Name + description, stacked.
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName("stageTitle")
        self.title_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(self.title_lbl)
        self.desc_lbl = QLabel(desc)
        self.desc_lbl.setObjectName("stageDesc")
        self.desc_lbl.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        col.addWidget(self.desc_lbl)
        lay.addLayout(col, 1)

        # Toggle switch (right).
        self.check = QCheckBox()
        self.check.setObjectName("stageSwitch")
        self.check.setFixedSize(34, 19)
        self.check.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check.toggled.connect(self._on_toggled)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.check, 0, Qt.AlignmentFlag.AlignVCenter)

        # The options block, placed in the wide column by _build_preview.
        self.panel = QWidget()
        self.panel.setObjectName("optCard")
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(13, 12, 13, 12)
        pl.setSpacing(8)

        # Card header: icon + (title + subtitle).
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        self._hdr_icon = QLabel()
        self._hdr_icon.setObjectName("optCardIcon")
        self._hdr_icon.setFixedSize(20, 20)
        hdr.addWidget(self._hdr_icon)
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        self.heading = QLabel(title)
        self.heading.setObjectName("optTitle")
        tcol.addWidget(self.heading)
        self.subtitle = QLabel(desc)
        self.subtitle.setObjectName("optSub")
        tcol.addWidget(self.subtitle)
        hdr.addLayout(tcol, 1)
        pl.addLayout(hdr)

        self.body = QVBoxLayout()
        self.body.setSpacing(5)
        pl.addLayout(self.body)
        self.panel.setVisible(False)

    def _on_toggled(self, _on):
        self.setProperty("on", "true" if self.check.isChecked() else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self._refresh_icon()
        self.panel.setVisible(self.check.isChecked())
        self.changed.emit()

    def _refresh_icon(self):
        """Rebuild the left card icon so it turns accent when the stage is on."""
        t = _TV
        colour = t['acc'] if self.check.isChecked() else t['dim']
        self._icon_lbl.setPixmap(
            icon(self._icon_name, colour=colour, size=18).pixmap(18, 18))

    def apply_theme(self, t, acc_soft):
        self._refresh_icon()
        self._hdr_icon.setPixmap(
            icon(self._icon_name, colour=t['acc'], size=20).pixmap(20, 20))
        self.setStyleSheet(
            f"QWidget#stage{{background:{t['surface_2']};"
            f"border:1px solid {t['border']};border-radius:10px;}}"
            f"QWidget#stage:hover{{border-color:{t['line_strong']};}}"
            f'QWidget#stage[on="true"]{{background:{acc_soft};'
            f"border-color:{t['acc']};}}"
            f"QLabel#stageIcon{{background:transparent;}}"
            f"QLabel#stageTitle{{color:{t['text']};font-size:12px;"
            f"font-weight:600;background:transparent;}}"
            f"QLabel#stageDesc{{color:{t['dim']};font-size:10px;"
            f"background:transparent;}}"
            f"QCheckBox#stageSwitch{{background:transparent;}}"
            f"QCheckBox#stageSwitch::indicator{{width:30px;height:17px;"
            f"border-radius:9px;border:none;"
            f"background:{t['line_strong']};}}"
            f"QCheckBox#stageSwitch::indicator:hover{{"
            f"background:{t['acc']};}}"
            f"QCheckBox#stageSwitch::indicator:checked{{"
            f"background:{t['acc']};}}")
        self.panel.setStyleSheet(
            f"QWidget#optCard{{background:{t['surface_3']};"
            f"border:1px solid {t['border']};border-radius:10px;}}"
            f"QLabel#optCardIcon{{background:transparent;}}"
            f"QLabel#optTitle{{color:{t['text']};font-size:12px;"
            f"font-weight:bold;background:transparent;}}"
            f"QLabel#optSub{{color:{t['dim']};font-size:10px;"
            f"background:transparent;}}")

    def enabled(self):
        return self.check.isChecked()

    def add(self, w):
        if isinstance(w, (QHBoxLayout, QGridLayout, QVBoxLayout)):
            self.body.addLayout(w)
        else:
            self.body.addWidget(w)
        return w

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.check.setChecked(not self.check.isChecked())
            e.accept()
        else:
            super().mousePressEvent(e)


def _hint(text):
    lb = QLabel(text)
    lb.setObjectName("dimLabel")
    lb.setWordWrap(True)
    return lb


class _Sheet(QWidget):
    """One output sheet in the layout view's column: the paper, its slots and
    the page pictures in them, plus a caption naming which pages it holds.

    Paints at fit-width — a column of thumbs, not one zoomed single page — so
    every sheet of a long document reads at a glance. It carries its own
    `slot_pages` list because it cannot paint from a single current page: the
    page that belongs to slot 3 of sheet 4 is not the page the viewer is on.

    Page images land via `set_pixmap(page, pm)` once the shared render queue
    has produced them (see _SheetColumn._schedule_visible); a slot whose page
    is still pending is drawn as an empty well.
    """
    def __init__(self, number, slot_pages, params, eff_w, eff_h, pw, ph,
                 full, booklet, marks_on, parent=None):
        super().__init__(parent)
        self.number     = number
        self.slot_pages = slot_pages
        self.params     = params
        self.eff_w, self.eff_h = eff_w, eff_h
        self.pw, self.ph       = pw, ph
        self.full, self.booklet = full, booklet
        self.marks_on = marks_on
        self._pixmaps = {}
        (self.out_w, self.out_h, self.mt, self.mb, self.ml, self.mr,
         self.gh, self.gv, self.slot_w, self.slot_h, self.cols,
         self.rows) = params
        self.n_slot = self.cols * self.rows
        self._caption = self._build_caption()
        self._recalc()

    def _build_caption(self):
        pages = sorted({p + 1 for p in self.slot_pages if p is not None})
        head = tr("Bogen {p0}").format(p0=self.number)
        if pages:
            head += " · " + tr("Seiten {p0}").format(
                p0=self._format_pages(pages))
        return head

    @staticmethod
    def _format_pages(pages):
        if len(pages) == 1:
            return str(pages[0])
        if pages == list(range(pages[0], pages[-1] + 1)):
            return "{}–{}".format(pages[0], pages[-1])
        return ", ".join(str(x) for x in pages)

    def set_pixmap(self, page, pm):
        self._pixmaps[page] = pm
        self.update()

    def _recalc(self):
        w = self.width()
        margin = 18
        sheet_w = max(10, w - 2 * margin)
        sheet_h = max(10, int(sheet_w * self.out_h / self.out_w))
        self._sheet_w, self._sheet_h = sheet_w, sheet_h
        self._cs = sheet_w / self.out_w
        self.setFixedHeight(10 + sheet_h + 26)

    def resizeEvent(self, e):
        self._recalc()
        super().resizeEvent(e)

    def paintEvent(self, _e):
        t = _TV
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        sheet_w, sheet_h, cs = self._sheet_w, self._sheet_h, self._cs
        x0, y0 = 18, 10
        p.setBrush(QBrush(QColor(PAPER)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(x0, y0, sheet_w - 1, sheet_h - 1)

        for i in range(self.n_slot):
            col_i = i % self.cols; row_i = i // self.cols
            sx = x0 + int((self.ml + col_i * (self.slot_w + self.gh)) * cs)
            sy = y0 + int((self.mt + row_i * (self.slot_h + self.gv)) * cs)
            sw = max(1, int(self.slot_w * cs)); sh = max(1, int(self.slot_h * cs))
            p.setBrush(QBrush(QColor(200, 210, 230, 60)))
            p.setPen(QPen(QColor(120, 140, 180, 120), 1))
            p.drawRect(sx, sy, sw, sh)
            pg = self.slot_pages[i] if i < len(self.slot_pages) else None
            pm = self._pixmaps.get(pg) if pg is not None else None
            if pm is None or pm.isNull():
                continue
            scale = 1.0 if self.full else min(self.slot_w / self.eff_w,
                                              self.slot_h / self.eff_h)
            tw = max(1, int(self.eff_w * scale * cs))
            th = max(1, int(self.eff_h * scale * cs))
            sc = pm.scaled(tw, th, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
            ox = sx + (sw - sc.width()) // 2
            oy = sy + (sh - sc.height()) // 2
            p.drawPixmap(ox, oy, sc)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(t['acc']), 1))
            p.drawRect(ox, oy, sc.width() - 1, sc.height() - 1)

        if self.marks_on:
            p.setPen(QPen(QColor(INK), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for a, b, c, d in _crop_mark_segments(
                    _nup_slot_rects(self.params, self.n_slot)):
                p.drawLine(int(x0 + a * cs), int(y0 + (self.out_h - b) * cs),
                           int(x0 + c * cs), int(y0 + (self.out_h - d) * cs))

        p.setPen(QPen(QColor(120, 160, 255, 180), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(x0, y0, sheet_w - 1, sheet_h - 1)

        p.setPen(QColor(t['dim']))
        f = p.font(); f.setPointSize(8); p.setFont(f)
        p.drawText(x0, y0 + sheet_h + 4, sheet_w, 18,
                   Qt.AlignmentFlag.AlignCenter, self._caption)
        p.end()


class _SheetColumn(QScrollArea):
    """The `.sheetwrap`: a scrollable vertical column of every output sheet.

    Own scrollbar is switched off — the shared navigation rail (mounted into
    the layout view's rail host by MainWindow) is this column's scrollbar, the
    same bargain PageGrid makes in "Seiten verwalten". Sheets are rendered
    lazily for whatever is on screen, exactly like PageGrid._schedule_visible,
    so a 500-page imposition does not queue a render per sheet.
    """
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content.setObjectName("sheetwrap")
        self._lay = QVBoxLayout(self._content)
        self._lay.setContentsMargins(0, 8, 0, 8)
        self._lay.setSpacing(6)
        self._cap = QLabel("")
        self._cap.setObjectName("sheetCap")
        self._cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cap.setWordWrap(True)
        self.setWidget(self._content)
        self._sheets = []
        # Computed state shared by rebuild and the lazy render.
        self._pdf_path = None
        self._pw = self._ph = self._eff_w = self._eff_h = 0
        self._render_w = 160
        self._params = None
        self._booklet = self._full = False
        self.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        # A new file or a picked page can change the sheet sizing; rebuild.
        AppState.get().pdf_changed.connect(lambda *_: self.rebuild())
        AppState.get().current_page_changed.connect(lambda *_: self.rebuild())

    # ── lifecycle ────────────────────────────────────────────────────────────

    def rebuild(self):
        _render_queue.cancel_queued(1)
        panel = self._panel
        panel._thumb_pending.clear()
        while self._lay.count():
            it = self._lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        self._sheets = []
        self._params = None
        self._reset_scroll()

        pdf_path = panel.current_pdf()
        self._pdf_path = pdf_path
        self._cap.setText("")
        if not pdf_path or not os.path.isfile(pdf_path):
            self._cap.setText(tr("Keine PDF geöffnet") if not pdf_path
                              else tr("Datei nicht mehr auffindbar"))
            self._lay.addWidget(self._cap)
            self._lay.addStretch(1)
            return
        try:
            _idx, pw, ph, n_total = panel._page_dims(pdf_path)
        except Exception:
            self._cap.setText(tr("Datei nicht lesbar"))
            self._lay.addWidget(self._cap)
            self._lay.addStretch(1)
            return
        sheets, params, eff_w, eff_h, pw, ph, problem, render_w = \
            panel._compute_sheets(pdf_path, n_total, pw, ph)
        if sheets is None:
            self._cap.setText(problem or tr("Anordnung nicht möglich"))
            self._lay.addWidget(self._cap)
            self._lay.addStretch(1)
            return
        self._pw, self._ph = pw, ph
        self._eff_w, self._eff_h = eff_w, eff_h
        self._render_w = render_w
        self._params = params
        self._booklet = panel.mode.currentIndex() == MODE_BOOKLET
        self._full = (panel.st_arr.enabled() and panel.full_scale.isChecked()
                      and not self._booklet)
        marks_on = panel.st_marks.enabled() and panel.cut_marks.isChecked()

        for number, slot_pages in enumerate(sheets, start=1):
            sheet = _Sheet(number, slot_pages, params, eff_w, eff_h, pw, ph,
                           self._full, self._booklet, marks_on)
            self._lay.addWidget(sheet)
            self._sheets.append(sheet)
        self._cap.setText(self._summary(marks_on))
        self._lay.addWidget(self._cap)
        self._lay.addStretch(1)
        QTimer.singleShot(0, self._schedule_visible)

    def _summary(self, marks_on):
        out_w, out_h = self._params[0], self._params[1]
        parts = [f"{out_w/MM_TO_PT:.0f} × {out_h/MM_TO_PT:.0f} mm"]
        if self._panel.st_arr.enabled():
            cols, rows = self._params[10], self._params[11]
            if self._booklet:
                parts.append(tr("Broschüre 2×1 = 2 Slots"))
            else:
                parts.append(tr("Raster {p0}×{p1} = {p2} Slots").format(
                    p0=cols, p1=rows, p2=cols * rows))
        if self._full:
            parts.append(tr("Originalgröße"))
        parts.append(tr("Schnittmarken an") if marks_on
                     else tr("Schnittmarken aus"))
        return " · ".join(parts)

    def refresh(self):
        """Re-request visible renders after a thumbnail lands (or a theme
        change repaints). Cheaper than rebuild: the sheets stay as they are."""
        self._schedule_visible()

    def showEvent(self, e):
        super().showEvent(e)
        self._schedule_visible()

    def apply_theme(self):
        t = _TV
        self._content.setStyleSheet(
            f"QWidget#sheetwrap{{background:{t['viewer_bg']};}}")
        self.setStyleSheet(
            f"QScrollArea{{background:{t['viewer_bg']};border:none;}}")
        self._cap.setStyleSheet(
            f"QLabel#sheetCap{{color:{t['dim']};font-size:11px;"
            f"padding:6px 12px;}}")
        for sheet in self._sheets:
            sheet.update()

    def _reset_scroll(self):
        bar = self.verticalScrollBar()
        bar.blockSignals(True)
        bar.setValue(0)
        bar.blockSignals(False)

    # ── lazy rendering ───────────────────────────────────────────────────────

    def _schedule_visible(self, _=None):
        """Submit thumb requests for the slots that are (or are near) on screen.
        A one-viewport buffer above and two below, as PageGrid does, so
        scrolling feels instant. Already-cached pages land immediately."""
        panel = self._panel
        if not self._sheets or not self._pdf_path:
            return
        # Only render when actually on screen — rebuild also fires from the
        # AppState page/pdf signals while the viewer (not this view) is showing.
        if not self.isVisible():
            return
        bar = self.verticalScrollBar()
        sy = bar.value()
        vp = self.viewport().height() or 600
        top = sy - vp
        bot = sy + 2 * vp
        visible_sheets = [s for s in self._sheets
                          if not (s.y() + (s.height() or 0) < top or s.y() > bot)]
        # Render each distinct page once, then give the pixmap to every sheet
        # that holds it — a page repeated across sheets/slots would otherwise
        # only ever paint the first occurrence (the old `seen` short-circuit).
        needed = {}
        for sheet in visible_sheets:
            for pg in sheet.slot_pages:
                if pg is not None and pg not in needed:
                    img = panel._page_image(self._pdf_path, pg, self._render_w)
                    needed[pg] = (panel._staged_pixmap(
                        QPixmap.fromImage(img), self._pw, self._ph)
                        if img is not None else None)
        for sheet in visible_sheets:
            for pg in sheet.slot_pages:
                if pg is not None and needed.get(pg) is not None:
                    sheet.set_pixmap(pg, needed[pg])

    def sheets(self):
        return list(self._sheets)


class _SheetRail:
    """Drives the shared navigation rail while the layout sheets are showing.

    The clone of `_GridRail` (tools/viewer/tab.py): the thumb maps onto the
    column's scroll range, the arrows step it, and every scroll pushes the
    position back to the rail. Where the page manager maps the rail onto
    *cards*, this maps it onto *sheets* — the rail reports which sheet, not
    which page, is current.
    """
    def __init__(self, column, panel):
        self._column = column
        self._panel  = panel
        self.single  = None    # the active tab's SinglePageView, set at mount

    def _bar(self):
        return self._column.verticalScrollBar()

    def _single(self):
        if self.single is not None:
            return self.single
        cb = getattr(self._panel, "_single_source", None)
        return cb() if cb is not None else None

    def rail_prev(self):
        self._nudge(-1)

    def rail_next(self):
        self._nudge(1)

    def _nudge(self, direction):
        bar = self._bar()
        step = max(60, int(self._column.viewport().height() * 0.85))
        bar.setValue(bar.value() + direction * step)

    def rail_wheel(self, dy_px):
        bar = self._bar()
        bar.setValue(bar.value() + int(dy_px))

    def rail_go_to(self, sheet):
        n = len(self._column.sheets())
        if not n:
            return
        i = max(0, min(int(sheet) - 1, n - 1))
        self._bar().setValue(max(0, int(self._column.sheets()[i].y()) - 8))

    def rail_drag_to(self, frac):
        bar = self._bar()
        bar.setValue(round(float(frac) * bar.maximum()))

    def rail_prompt_goto(self):
        n = len(self._column.sheets())
        if n <= 0:
            return
        from PyQt6.QtWidgets import QInputDialog
        sheet, ok = QInputDialog.getInt(
            self._column, tr("Gehe zu Bogen"),
            tr('Bogen (1 – {p0}):').format(p0=n),
            self.page(), 1, n)
        if ok:
            self.rail_go_to(sheet)

    def page(self):
        n = len(self._column.sheets())
        if n <= 1:
            return max(1, n)
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        return max(1, min(n, int(round(frac * (n - 1))) + 1))

    def sync(self):
        """Scrollbar position → rail thumb and sheet number."""
        single = self._single()
        if single is None:
            return
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        single.nav_set_document(len(self._column.sheets()), self.page())
        single.nav_set_fraction(frac)


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
        # Resolves the active tab's SinglePageView, for the rail's delegate when
        # the layout view is showing (set by MainWindow._build). None while no
        # tab is open — the sheet column then says so and the rail stays hidden.
        self._single_source = None
        super().__init__(parent)

    def set_single_source(self, cb):
        """Give the panel a callable resolving the active tab's SinglePageView."""
        self._single_source = cb

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

        self.log = LogAdapter()

        # The run row, card-aligned: Ausführen stretches to fill the width, a
        # narrow Stop sits beside it and only appears while a job runs.
        self.run_row = QWidget()
        self.run_row.setObjectName("runRow")
        btn_row = QHBoxLayout(self.run_row)
        btn_row.setContentsMargins(0, 10, 0, 0)
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
        lay.addWidget(self.run_row)

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
        """The main area: the options for whatever is switched on, the column
        of all output sheets, and the rail host that the shared navigation rail
        is reparented into while this view shows (see MainWindow)."""
        outer = QWidget()
        lay = QHBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        opts = QWidget()
        opts.setObjectName("layoutOptions")
        self._opts_content = opts
        ol = QVBoxLayout(opts)
        ol.setContentsMargins(16, 14, 16, 14)
        ol.setSpacing(12)
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

        # The sheet column owns the centre: a lazy scroll of every output sheet.
        self._sheetwrap = _SheetColumn(self)
        lay.addWidget(self._sheetwrap, 1)

        # The narrow host the shared rail is parked in while Layout is showing.
        # Hidden until MainWindow attaches a tab's rail into it.
        self.rail_host = QWidget()
        rh = QVBoxLayout(self.rail_host)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.setSpacing(0)
        self.rail_host.setFixedWidth(40)
        self.rail_host.setVisible(False)
        lay.addWidget(self.rail_host)

        self.sheet_rail = _SheetRail(self._sheetwrap, self)
        return outer

    def _apply_theme(self):
        t = _TV
        # No border-right of its own here either — see ManagePanel._apply_theme.
        self._controls_content.setStyleSheet(
            f"QWidget#layoutControlsW{{background:{t['sidebar_bg']};}}")
        self.controls_widget.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._opts_content.setStyleSheet(
            f"QWidget#layoutOptions{{background:{t['panel_bg']};"
            f"border-right:1px solid {t['border']};}}")
        self._opts_scroll.setStyleSheet(
            f"QScrollArea{{background:{t['panel_bg']};border:none;}}")
        self._sheetwrap.apply_theme()
        # The run row: a hairline above it sets it apart from the cards above,
        # so it reads as the row that acts on all of them.
        self.run_row.setStyleSheet(
            f"QWidget#runRow{{border-top:1px solid {t['border']};}}")
        # The accent-soft ground an on-stage card sits on — the same rgba the
        # shell stylesheet derives for its selected nav items, recomputed here
        # because _TV carries only the solid accent.
        _r, _g, _b = (int(t['acc'][1:][i:i+2], 16) for i in (0, 2, 4))
        acc_soft = f"rgba({_r},{_g},{_b},{0.16})"
        for st in (self.st_crop, self.st_arr, self.st_marks):
            st.apply_theme(t, acc_soft)
        self._sheetwrap.refresh()

    def _update_preview(self):
        # build_ui() runs inside _build_controls(), before _setup() has made
        # the sheet column — and it settles the initial control state, which
        # fires the same handlers a user edit does.
        wrap = getattr(self, "_sheetwrap", None)
        if wrap is not None:
            wrap.rebuild()
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

        # ── Section heading ────────────────────────────────────────────────
        sec = QLabel(tr("Druckstufen"))
        sec.setObjectName("sectionLabel")
        sec.setContentsMargins(2, 4, 0, 2)
        layout.addWidget(sec)

        # ── Stage 1 · Zuschneiden / Skalieren ────────────────────────────────
        self.st_crop = Stage(tr("Zuschneiden / Skalieren"),
                             tr("Format · Ränder · Skalierung"), "crop")
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

        # ── Stage 2 · Anordnung ──────────────────────────────────────────────
        self.st_arr = Stage(tr("Anordnung"),
                            tr("Raster · Broschüre · Blattformat"), "grid")
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

        # ── Stage 3 · Marken ─────────────────────────────────────────────────
        self.st_marks = Stage(tr("Marken"),
                              tr("Schnittmarken · Endformat"), "marks")
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

    def _arrange_sheets(self, n_total, n_slot, booklet, src_seq):
        """The slot-pages list of every output sheet — the one layout the
        preview and the run share, so the column cannot lie about the result.

        This is exactly the chunking `_build_plan.run` feeds `_build_nup`: a
        booklet is one sheet per `_booklet_sides` pair; grid "der Reihe nach"
        packs consecutive pages (filling a single-page document across the
        sheet, padding the last one with blanks); grid "jede Seite wiederholt"
        repeats each page to fill its own sheet."""
        if booklet:
            sides = _booklet_sides(n_total)
            return [list(side) for side in sides] if sides else [[None, None]]
        if src_seq:
            src_pages = list(range(n_total))
        else:
            src_pages = [p for p in range(n_total) for _ in range(n_slot)]
        if len(src_pages) == 1:
            src_pages = src_pages * n_slot
        while len(src_pages) % n_slot:
            src_pages.append(None)
        return [src_pages[i:i + n_slot] for i in range(0, len(src_pages), n_slot)]

    def _compute_sheets(self, pdf_path, n_total, pw, ph):
        """The full sheet set, sized from the representative page.

        Returns ``(sheets, params, eff_w, eff_h, pw, ph, problem, render_w)``.
        On any staging problem `sheets` and `params` are None and `problem`
        carries the sentence (the column shows it); otherwise `problem` is None
        and `sheets` is the list of per-sheet slot_pages."""
        eff_w, eff_h = self._staged_page_size(pw, ph)
        if eff_w < 1.0 or eff_h < 1.0:
            return (None, None, eff_w, eff_h, pw, ph,
                    tr("Ränder zu groß — von der Seite bleibt nichts übrig."), 160)
        if not self.st_arr.enabled():
            # Anordnung is off — the run applies crop/marks 1-up per page, so
            # the preview must show one full-bleed sheet per page, not the
            # 2×1 grid the arrangement controls would otherwise describe.
            sheets = [[i] for i in range(n_total)]
            params = (eff_w, eff_h, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                      eff_w, eff_h, 1, 1)
            render_w = max(120, min(800, int(min(eff_w, eff_h))))
            return sheets, params, eff_w, eff_h, pw, ph, None, render_w
        params = self._layout_params(eff_w, eff_h)
        if params[8] <= 1.0 or params[9] <= 1.0:
            return (None, None, eff_w, eff_h, pw, ph,
                    tr("Abstände zu groß — kein Platz für Inhalt."), 160)
        booklet = self.mode.currentIndex() == MODE_BOOKLET
        full = self.full_scale.isChecked() and not booklet
        if full:
            problem = _full_scale_problem(eff_w, eff_h, params)
            if problem:
                return (None, None, eff_w, eff_h, pw, ph, problem, 160)
        n_slot = params[10] * params[11]
        src_seq = self.src_mode.currentIndex() == 0
        sheets = self._arrange_sheets(n_total, n_slot, booklet, src_seq)
        render_w = max(120, min(800, int(min(params[8], eff_w))))
        return sheets, params, eff_w, eff_h, pw, ph, None, render_w

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
        wrap = getattr(self, "_sheetwrap", None)
        if wrap is not None:
            wrap.refresh()

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
                raise ValueError(tr("Keine Seiten ausgewählt."))
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
                # Same sheet set the preview column shows (see _arrange_sheets),
                # flattened back into _build_nup's packed page list.
                sheets = self._arrange_sheets(n_total, n_slot, booklet, src_seq)
                src_pages = [p for sheet in sheets for p in sheet]
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
        self.log.log(tr('Fertig — {p0}.').format(p0=steps), hold=True)
        self.open_result(out_path, tr("Layout"))
