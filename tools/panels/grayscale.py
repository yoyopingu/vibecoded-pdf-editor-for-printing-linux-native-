"""
Graustufen — turn visually grey pages into real DeviceGray, one page at a
time, and check each converted page against the original before shipping it.
"""
import os, subprocess, shutil, logging
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals
from tools.theme import STATUS, _TV, _register_themed
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox, QRadioButton, QScrollArea, QWidget, QSlider, QFrame, QSplitter, QGridLayout
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QPixmap
from tools.app_state import AppState
from tools._base import BasePanel, make_label, make_separator
from tools.colorspace import is_grey_only, page_colorspaces, scan_document
from tools.i18n import tr
from tools.panels._colour import _colour_histogram, _hist_stats
from tools.panels._verify import _BLACKOUT_LIMIT, _page_luma, _conversion_damage, _verify_pages_intact


# How long the sliders wait after the handle stops before the document is
# re-classified. Long enough that a drag is one pass rather than forty, short
# enough that letting go and looking at the result feels immediate.
_SETTLE_MS = 120

# The histogram of a page that declares nothing but grey. Such a page cannot
# put a coloured pixel on paper, so it is not rendered at all — see _scan_pages
# — and this stands in its place so that _page_data still has one entry per
# page. _hist_stats reads it as "no pixel is any distance from grey", which is
# what it is.
_NEUTRAL_HIST = [0] * 256

# The preview grid. A card is a thumbnail with its page number under it, on a
# frame that carries the status border; _CARD_PAD is what that frame adds
# around the thumbnail and _CARD_CAPTION what the number takes below it. These
# were written out separately in the three places that lay the grid out, and
# had to agree for the columns to come out right.
_CARD_ASPECT  = 127 / 90        # a page, near enough, at any zoom
_CARD_PAD     = 12
_CARD_CAPTION = 24
_GRID_GAP     = 8
_GRID_MARGIN  = 10


def _card_height(card_w):
    return int(card_w * _CARD_ASPECT)


def _ratio_percent(value):
    """The ratio slider's value as a percentage, written out truthfully.

    The scale runs from 0.005% to 25%, so two decimal places rounded its bottom
    end away to 0.01%. Trailing zeros are dropped instead, which keeps 1.5% and
    25% short and still lets 0.005% say what it is.
    """
    return f"{value / 200.0:.3f}".rstrip("0").rstrip(".") + "%"


def _slider_row(low, slider, high, value_lbl):
    """A slider between the two ends of its scale, with its value on the right.

    One shape for both sliders in this panel; they used to be spelled out
    separately and had drifted apart in spacing and in label width.
    """
    def end(text):
        lbl = QLabel(text)
        lbl.setObjectName("dimLabel")
        return lbl

    h = QHBoxLayout(); h.setSpacing(6)
    h.addWidget(end(low))
    h.addWidget(slider, 1)
    h.addWidget(end(high))
    h.addWidget(value_lbl)
    return h


def _grey_retry_page(gs_bin, src, index, report):
    """Convert a single page on its own, for pages the full-document run damaged.

    Isolating the page drops the surrounding transparency groups and shared
    resources that trip Ghostscript up, so this often succeeds where the whole
    document did not. Returns a one-page PDF path, or None."""
    import tempfile, pikepdf
    one = grey = None
    try:
        fd, one = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        fd, grey = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        with pikepdf.open(src) as pdf, pikepdf.Pdf.new() as single:
            single.pages.append(pdf.pages[index])
            single.save(one)
        r = report.run(
            [gs_bin, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
             "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
             "-dCompatibilityLevel=1.5", "-dAutoRotatePages=/None",
             "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
             "-dDownsampleMonoImages=false", "-o", grey, one],
            text=True, errors="replace", timeout=300)
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
    import tempfile, contextlib, pikepdf
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
            r = report.run(cmd, text=True, errors="replace", timeout=900)
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
        for key, text in [("converted", tr("Grün = konvertiert")),
                          ("forced",    tr("Blau = erzwungen")),
                          ("skipped",   tr("Orange = übersprungen")),
                          ("colour",    tr("Rot = Farbe"))]:
            color = STATUS[key]
            dot = QLabel("■"); dot.setStyleSheet(f"color:{color};font-size:13px;background:transparent;")
            lbl = QLabel(text)
            self._gs_legend_lbls.append(lbl)
            zbl.addWidget(dot); zbl.addWidget(lbl); zbl.addSpacing(6)
        zbl.addStretch()
        self._card_w = 90
        self._preview_cards = []
        self._preview_cols = 0
        # Re-flowing the grid moves every card, and a splitter drag is a stream
        # of resize events, so the width is allowed to settle first.
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.timeout.connect(self._relayout_preview)
        # The zoom scales every card twice: once with a fast transform so the
        # grid follows the wheel, and once smoothly when the wheel stops.
        self._zoom_smooth_timer = QTimer(self)
        self._zoom_smooth_timer.setSingleShot(True)
        self._zoom_smooth_timer.timeout.connect(self._rezoom_smooth)
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
        self._style_ratio_box()
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
                # force: the statuses have not changed, the palette has, so
                # every card needs redrawing even though each says the same.
                self._update_preview_borders(force=True)
            except RuntimeError:
                self._preview_box = None; self._preview_cards = []

    def eventFilter(self, obj, e):
        if (hasattr(self, '_preview_scroll') and
                obj is self._preview_scroll.viewport()):
            if e.type() == QEvent.Type.Wheel:
                self._preview_wheel(e); return True
            if e.type() == QEvent.Type.Resize:
                # Widening the pane used to fit more cards across and nothing
                # noticed: the grid kept the column count it was built with
                # until a zoom happened to rebuild it, so the preview sat in a
                # narrow strip with the rest of the pane empty beside it.
                self._relayout_timer.start(60)
        return super().eventFilter(obj, e)

    def _zoom_in(self):
        self._card_w = min(1400, self._card_w + 20); self._rezoom()
    def _zoom_out(self):
        self._card_w = max(50, self._card_w - 20); self._rezoom()
    def _zoom_reset(self):
        self._card_w = 90; self._rezoom()

    def _rezoom(self):
        if not self._preview_cards: return
        card_h = _card_height(self._card_w)
        for frame, img_lbl, num_lbl in self._preview_cards:
            frame.setFixedSize(self._card_w + _CARD_PAD, card_h + _CARD_CAPTION)
            img_lbl.setFixedSize(self._card_w, card_h)
            pm = img_lbl.property("src_pm")
            if pm:
                img_lbl.setPixmap(pm.scaled(self._card_w, card_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.FastTransformation))
        self._relayout_preview()
        self._zoom_smooth_timer.start(180)

    def _rezoom_smooth(self):
        if not self._preview_cards:
            return
        card_h = _card_height(self._card_w)
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
        # The colour each card's border is painted in right now, so that a
        # re-classification can repaint the ones that changed and leave the
        # rest alone. See _update_preview_borders.
        self._card_colours = []
        # Shared thumbnail pipeline — same cache as PageGrid, same render queue
        self._grey_thumb_gen  = 0
        self._grey_thumb_sigs = _ThumbSignals()
        self._grey_thumb_sigs.ready.connect(self._on_grey_thumb_ready)

        # Dragging a slider re-classifies every page and repaints every card.
        # Measured on a 300-page document that is 132 ms of GUI thread per step
        # of the handle, and a handle emits one step per pixel of travel — so
        # dragging one slider across the sidebar froze the window for five
        # seconds and did the whole job forty times to arrive at one answer.
        # The number beside the handle still follows it exactly; only the work
        # waits for the handle to come to rest.
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._on_setting_changed)
        # One timer for "scan the document again", shared by the two things
        # that ask for it, so a newly opened file cannot start two scans.
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.timeout.connect(self._rescan)

        # One group, because it is one decision. These controls used to sit in
        # three separate boxes — the two modes in one, and the slider that only
        # exists for the second mode two boxes further down — which read as
        # three unrelated settings. The threshold defines what counts as a
        # coloured pixel and feeds both modes, so it comes first; the ratio
        # hangs off the mode that uses it, indented and tied to it by a rule,
        # and greys out with it.
        grp = QGroupBox(tr("Farb-Erkennung"))
        gl = QVBoxLayout(grp); gl.setSpacing(6); gl.setContentsMargins(8, 10, 8, 8)

        gl.addWidget(make_label(tr("Abstand vom Grau pro Pixel:"), dim=True))
        self.thr = QSlider(Qt.Orientation.Horizontal)
        self.thr.setRange(1, 80); self.thr.setValue(20)
        self.thr.setTickInterval(10); self.thr.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.thr_lbl = QLabel("20"); self.thr_lbl.setFixedWidth(28)
        self.thr.valueChanged.connect(self._on_slider_moved)
        gl.addLayout(_slider_row(tr("Streng"), self.thr, tr("Tolerant"), self.thr_lbl))

        gl.addSpacing(2)
        gl.addWidget(make_separator())
        gl.addSpacing(2)
        gl.addWidget(make_label(tr("Wann gilt eine Seite als Farbseite?"), dim=True))
        self.mode_single = QRadioButton(tr("1 farbiger Pixel = Farbseite"))
        self.mode_ratio  = QRadioButton(tr("Nach Anteil farbiger Pixel"))
        self.mode_ratio.setChecked(True)
        self.mode_single.toggled.connect(self._on_mode_changed)
        gl.addWidget(self.mode_single)
        gl.addWidget(self.mode_ratio)

        self._ratio_box = QFrame(); self._ratio_box.setObjectName("subOption")
        rl = QVBoxLayout(self._ratio_box)
        rl.setContentsMargins(14, 2, 0, 4); rl.setSpacing(4)
        rl.addWidget(make_label(tr("Ab wieviel % gilt die Seite als Farbseite?"), dim=True))
        self.ratio = QSlider(Qt.Orientation.Horizontal)
        self.ratio.setRange(1, 5000); self.ratio.setValue(300)
        self.ratio.setTickInterval(500); self.ratio.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.ratio_lbl = QLabel(_ratio_percent(self.ratio.value()))
        self.ratio_lbl.setFixedWidth(48)
        self.ratio.valueChanged.connect(self._on_slider_moved)
        # The ends of the scale are read off the slider rather than written out
        # again beside it. Written out, the low end said 0.05% while the slider
        # could only be set as low as 0.005% — ten times apart, on the control
        # that decides how much colour a page is allowed before it costs a
        # colour click.
        rl.addLayout(_slider_row(_ratio_percent(self.ratio.minimum()), self.ratio,
                                 _ratio_percent(self.ratio.maximum()), self.ratio_lbl))
        gl.addWidget(self._ratio_box)
        self._ratio_box.setEnabled(self.mode_ratio.isChecked())
        layout.addWidget(grp)
        self._style_ratio_box()

        AppState.get().pdf_changed.connect(self._on_pdf_changed)

    def _on_mode_changed(self):
        """A radio was clicked — one click, so apply it straight away."""
        self._ratio_box.setEnabled(self.mode_ratio.isChecked())
        self._style_ratio_box()
        self._on_setting_changed()

    def _style_ratio_box(self):
        """Draw the ratio slider as belonging to the radio above it.

        A rule down its left edge, in the accent colour while that mode is
        chosen and grey when it is not. Written out for both states rather than
        left to a `:disabled` rule, because the application stylesheet gives
        these labels and this slider explicit colours, and an explicit colour
        is not something Qt's disabled palette can take back — setEnabled(False)
        alone left the whole block looking exactly as live as the mode above it.
        """
        t = _TV
        on = self.mode_ratio.isChecked()
        edge = t['acc'] if on else t['vdim']
        text = t['dim'] if on else t['vdim']
        value = t['text'] if on else t['vdim']
        self._ratio_box.setStyleSheet(
            f"QFrame#subOption{{border:none;border-left:2px solid {edge};"
            f"background:transparent;}}"
            f"QFrame#subOption QLabel{{color:{text};background:transparent;}}"
            f"QFrame#subOption QSlider::sub-page:horizontal{{background:{edge};}}"
            f"QFrame#subOption QSlider::handle:horizontal{{background:{edge};}}")
        self.ratio_lbl.setStyleSheet(f"color:{value};background:transparent;")

    def _on_slider_moved(self, _value=None):
        """The handle moved: show the number now, do the work when it stops."""
        self._update_slider_labels()
        self._settle_timer.start(_SETTLE_MS)

    def _update_slider_labels(self):
        self.thr_lbl.setText(str(self.thr.value()))
        self.ratio_lbl.setText(_ratio_percent(self.ratio.value()))

    def _on_setting_changed(self, _value=None):
        """Apply what the controls now say, whatever put them there."""
        self._settle_timer.stop()
        self._update_slider_labels()
        if self._page_data:
            self._reclassify()

    def _on_pdf_changed(self, path):
        if path and self.isVisible():
            self._rescan_timer.start(300)

    def showEvent(self, e):
        super().showEvent(e)
        if self._scanned_path != self.current_pdf() or not self._page_data:
            self._rescan_timer.start(200)

    def _rescan(self):
        """Scan the current document, or come back to it once the tool is free.

        A conversion still running when another document was opened used to end
        up in run_async's "Vorgang laeuft bereits", raised inside a QTimer
        callback where nothing turns it into a log line — so it surfaced as a
        traceback on the unhandled-exception hook, and the new document was
        never scanned at all.
        """
        if self._scanning or getattr(self, "_async_running", False):
            self._rescan_timer.start(1000)
            return
        self._scan()

    def _reclassify(self):
        thr = self.thr.value(); use_ratio = self.mode_ratio.isChecked()
        min_ratio = self.ratio.value() / 20000.0
        self._grey_pages.clear()
        for i, hist in enumerate(self._page_data):
            max_diff, colour_ratio = _hist_stats(hist, thr)
            is_colour = (colour_ratio >= min_ratio) if use_ratio else (max_diff > thr)
            if not is_colour: self._grey_pages.add(i)
        self._update_preview_borders()
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
        CARD_W = self._card_w; CARD_H = _card_height(CARD_W)
        cols = self._preview_cols = self._preview_columns()
        grid = QGridLayout(container)
        grid.setContentsMargins(_GRID_MARGIN, _GRID_MARGIN, _GRID_MARGIN, _GRID_MARGIN)
        grid.setSpacing(_GRID_GAP)
        # Packed into the top-left corner at the spacing asked for. Without
        # this the grid shares the leftover width and height out among its rows
        # and columns, so twelve cards in a large pane drifted apart into a
        # sparse field with a hand's width of nothing between two rows.
        grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for i in range(n_pages):
            frame = QFrame()
            frame.setFixedSize(CARD_W + _CARD_PAD, CARD_H + _CARD_CAPTION)
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
            # Every card takes clicks; which of them may actually be chosen is
            # not known until the scan has run, and _toggle_manual is where
            # that is decided. The cards are built before the scan starts so
            # there is something to look at while it works, so a test here
            # against _already_grey — which is empty at this point, always —
            # was a guard that could never fire.
            frame.mousePressEvent = lambda e, n=i: self._toggle_manual(n, e)
        self._card_colours = [None] * n_pages
        self._set_card_cursors()

    def _set_card_cursors(self):
        """A pointing hand on the cards a click can do something to."""
        for i, (frame, _, _) in enumerate(self._preview_cards):
            frame.setCursor(Qt.CursorShape.ArrowCursor if i in self._already_grey
                            else Qt.CursorShape.PointingHandCursor)

    def _preview_columns(self):
        """How many cards fit across the pane as it is now."""
        vp_w = self._preview_scroll.viewport().width() or 600
        per_card = self._card_w + _CARD_PAD + _GRID_GAP
        return max(2, (vp_w - 2 * _GRID_MARGIN + _GRID_GAP) // per_card)

    def _relayout_preview(self):
        """Re-flow the cards across the width there is now.

        Only when the column count actually changes: re-flowing moves every
        card in the layout, and both the things that ask for it — a zoom step
        and a drag of the splitter — arrive as a stream of events.
        """
        container = self._preview_scroll.widget()
        if not container or not self._preview_cards: return
        cols = self._preview_columns()
        if cols == self._preview_cols:
            return
        self._preview_cols = cols
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
        CARD_W = self._card_w; CARD_H = _card_height(CARD_W)
        pm = QPixmap.fromImage(image)
        img_lbl.setProperty("src_pm", pm)
        img_lbl.setPixmap(pm.scaled(CARD_W, CARD_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _toggle_manual(self, idx, event=None):
        # A page that is already DeviceGray is dropped from the selection by
        # _convert whatever the card says, so choosing it did nothing except
        # write a number into the log that the conversion then disagreed with.
        if idx in self._already_grey:
            return
        ctrl  = event is not None and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        shift = event is not None and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if shift and self._last_click is not None:
            lo = min(self._last_click, idx); hi = max(self._last_click, idx)
            span = [i for i in range(lo, hi + 1) if i not in self._already_grey]
            if ctrl:
                target_add = self._last_click in self._manual_skip
                for i in span:
                    self._manual_sel.discard(i)
                    if target_add: self._manual_skip.add(i)
                    else:          self._manual_skip.discard(i)
            else:
                target_add = self._last_click in self._manual_sel
                for i in span:
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

    def _card_colour(self, i):
        """What page `i`'s border says will happen to it. The order is the
        precedence: an already-grey page is left alone whatever else is set,
        and an explicit choice beats the scan's."""
        if i in self._already_grey: return "transparent"
        if i in self._manual_skip:  return STATUS["skipped"]
        if i in self._manual_sel:   return STATUS["forced"]
        if i in self._grey_pages:   return STATUS["converted"]
        return STATUS["colour"]

    def _update_preview_borders(self, force=False):
        """Repaint the card borders whose colour changed, and only those.

        setStyleSheet re-parses the sheet for the widget it is set on, and at
        ~0.35 ms a card, setting all of them regardless was 108 ms of the
        132 ms a slider step cost on a 300-page document — for a change that
        typically moves two or three cards from red to green. Pass `force` when
        the colours themselves have changed under the cards rather than the
        classification: that is the theme switch, where every card has to be
        redrawn to say the same thing in a different palette.
        """
        if len(self._card_colours) != len(self._preview_cards):
            self._card_colours = [None] * len(self._preview_cards)
        for i, (frame, _, _) in enumerate(self._preview_cards):
            colour = self._card_colour(i)
            if not force and self._card_colours[i] == colour:
                continue
            self._card_colours[i] = colour
            frame.setStyleSheet(
                f"QFrame{{background:transparent;border:2px solid {colour};border-radius:5px;}}")
        self._update_status_bar()

    def _update_status_bar(self):
        if not hasattr(self, '_status_sw') or not self._page_data: return
        n_total = len(self._page_data)
        pages_to_convert = (self._grey_pages | self._manual_sel) - self._manual_skip - self._already_grey
        n_sw = len(self._already_grey | pages_to_convert)
        self._status_sw.setText(f"🖤  {tr('SW')}: {n_sw}")
        self._status_color.setText(f"🎨  {tr('Farbe')}: {n_total - n_sw}")
        self._status_total.setText(f"{tr('Gesamt')}: {n_total}")

    def _scan(self, then=None):
        """Analyse every page, off the GUI thread.

        `then` is called on the GUI thread once the results are in, which is how
        Ausführen gets its classification when nothing has been scanned yet.

        This used to run inline with QApplication.processEvents() between pages.
        Every page is rendered at full size to build its colour histogram —
        about seven seconds each on a heavy vector page, 58 for eight of them,
        and the window could not repaint or be closed for any of it.
        """
        if self._scanning:
            return
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        self.log.clear_log()
        self._page_data.clear(); self._grey_pages.clear()
        self._manual_sel.clear(); self._manual_skip.clear(); self._last_click = None
        self._already_grey = set()
        self._grey_thumb_gen += 1   # invalidate any in-flight thumb callbacks

        # The page count, so the preview cards can go up before the work starts.
        try:
            import pypdfium2 as pdfium
            with _pdfium_lock:
                doc = pdfium.PdfDocument(src)
                try: n = len(doc)
                finally: doc.close()
        except Exception as e:
            logging.exception("grayscale: could not open the document")
            self.log.log(str(e), error=True); return
        self._build_preview(n)

        self._scanning = True
        thr = self.thr.value()
        self.run_async(
            lambda report: _scan_pages(src, thr, report),
            on_done=lambda result: self._scan_done(src, result, then),
            on_error=self._scan_failed,
            busy_label="Seiten analysieren …",
        )

    def _scan_done(self, src, result, then=None):
        self._scanning = False
        hists, already_grey = result
        self._page_data[:] = hists
        self._already_grey = already_grey
        self._set_card_cursors()        # now that it is known which are fixed
        self._reclassify()              # which paints the borders itself
        self._load_preview_pixmaps_async(src)
        self._scanned_path = src
        if then is not None:
            # Next event-loop turn, not straight away: the job's `finished`
            # signal is already queued behind us, and it re-enables the run
            # button. Starting the conversion before that lands would have it
            # re-enabled while the conversion is running.
            QTimer.singleShot(0, then)

    def _scan_failed(self, exc):
        # Always clear it: an error escaping the scan used to leave the flag
        # set, and then no later scan would ever run for the rest of the
        # session — the tool just quietly stopped updating.
        self._scanning = False
        logging.error("grayscale scan failed: %s", exc)
        self.log.log(str(exc), error=True)

    def _run_action(self):
        src = self.require_pdf()
        if self._scanned_path != src or not self._page_data:
            # Nothing analysed yet. Scan first and convert when it comes back —
            # the conversion needs the classification the scan produces.
            self._scan(then=self._convert_after_scan)
            return None
        return self._convert()

    def _convert_after_scan(self):
        try:
            self._convert()
        except (ValueError, RuntimeError) as e:
            # Raised from a timer callback rather than from _run_action, so
            # _safe_run is not there to turn it into a log line.
            self.log.log(str(e), error=True)
        except Exception as e:
            # Nothing may escape from here. This is a QTimer callback, and an
            # exception leaving one goes to the unhandled-exception hook rather
            # than to the user, who is left looking at a tool that did nothing.
            logging.exception("greyscale: conversion after the scan failed")
            self.log.log(str(e), error=True)

    def _convert(self):
        src = self.require_pdf()
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


def _already_grey_pages(src, n, report):
    """Which of the first `n` pages declare nothing but grey.

    The scan is tools/colorspace.py — this used not to look inside Form
    XObjects, so a page produced by N-Up, imposition or merge showed no colour
    spaces at all and was never recognised.

    scan_document reads the whole file through one open; asking page by page
    opened it once per page. A failure here is not fatal: an unrecognised page
    is simply converted along with the rest, which is the safe direction.
    """
    try:
        scan_document(src, should_stop=lambda: report.cancelled)
        return {i for i in range(n) if is_grey_only(page_colorspaces(src, i))}
    except Exception:
        logging.exception("grayscale: colour-space probe failed")
        return set()


def _scan_pages(src, thr, report):
    """Colour histogram of every page, and which pages are already grey.

    The colour-space pass runs first, and the pages it finds are never
    rendered. Rendering a page to measure its pixels is the expensive half of
    this — seconds each on a heavy vector page — while reading what the page
    declares costs a fraction of a millisecond once the file is open. A page
    that declares nothing but DeviceGray cannot put a coloured pixel on paper,
    so rendering it could only confirm what reading it already said. On a
    document that is mostly grey already, which is most of what this tool is
    pointed at, that is the difference between minutes and seconds.

    Plain data only; the panel classifies and draws back on the GUI thread.
    """
    import pypdfium2 as pdfium
    hists = []
    with _pdfium_lock:
        doc = pdfium.PdfDocument(src)
    try:
        with _pdfium_lock:
            n = len(doc)
        already_grey = _already_grey_pages(src, n, report)
        for i in range(n):
            report.check()
            if i in already_grey:
                hists.append(_NEUTRAL_HIST)
                report(tr('Seite {p0}: bereits Graustufen').format(p0=i + 1))
                continue
            with _pdfium_lock:
                page = doc[i]
                try:
                    pil = page.render(scale=1).to_pil().convert("RGB")
                finally:
                    # A loaded pdfium page is not small — hundreds of megabytes
                    # on a poster — and these used to be left to the garbage
                    # collector, so a long document held every page it had
                    # scanned until the scan was over.
                    page.close()
            hist = _colour_histogram(pil)
            hists.append(hist)
            max_diff, ratio = _hist_stats(hist, thr)
            report(tr('Seite {p0}: max={p1}, farbig={p2:.2f}%').format(
                p0=i + 1, p1=max_diff, p2=ratio * 100))
    finally:
        # Always: a failed render used to leave the document (and its file
        # handle) open for the life of the app. Under the lock like every other
        # pdfium call — see tools/render/document_cache.py.
        with _pdfium_lock:
            doc.close()
    return hists, already_grey
