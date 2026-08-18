"""
Graustufen — turn visually grey pages into real DeviceGray, one page at a
time, and check each converted page against the original before shipping it.
"""
import os, subprocess, logging, gc
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _render_queue, _ThumbTask, _ThumbSignals, _thumb_render_width
from tools.theme import STATUS, _TV, _register_themed
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QGroupBox, QRadioButton, QScrollArea, QWidget, QSlider, QFrame, QSplitter, QGridLayout
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QPixmap
from tools.app_state import AppState
from tools._base import BasePanel, make_label
from tools.colorspace import document_colorspaces, is_grey_only, page_colorspaces
from tools.ghostscript import (failed, ghostscript_binary, page_range_flags,
                               run_chunked, unlink)
from tools.i18n import tr
from tools.panels._colour import _colour_histogram, _hist_stats
from tools.pageverify import BLACKOUT_LIMIT, conversion_damage
from tools.panels._verify import _page_luma, _verify_pages_intact


# Default card size — kept in sync with tools/viewer/page_grid.py CARD_W/CARD_H.
# Panels may not import from viewer (layering), so the constants are duplicated
# here; change both together.
_CARD_W = 187
_CARD_H = 264

# Scale for the colour-detection scan. Full size (1.0) used to be the only way
# to catch a half-point colour mark — a 128-pixel squash averaged it away — but
# pdfium's anti-aliased renderer catches it at 0.3 too (verified by
# test_greyscale_detects_a_tiny_colour_mark). 0.4 gives margin while cutting
# pixel count ~6x vs scale=1, which is the difference between seconds and
# minutes on a 150-page book of heavy vector pages. The histogram itself is
# size-independent (PIL ImageChops in C).
_SCAN_SCALE = 0.4

# Where both detection modes start. 20 is what the whole tool used to share,
# and it is the value test_greyscale_detects_a_tiny_colour_mark is calibrated
# against — a half-point red mark on A4 has to survive it.
_DEFAULT_THR = 20

# What the sidebar opens at, matching crop_resize.py and nup.py. The width and
# the minimum are build_tool_sidebar's to decide, not this tool's — sharing the
# sidebar is the point of it. This only stops the splitter from drifting: with
# no stretch factors it handed the sidebar 465 px of the window rather than the
# 400 it was asked for, so the preview grid — the half of the window this tool
# exists to show — opened 65 px narrower than in the tools beside it.
_SIDEBAR_W = 400


def _grey_cmd(gs_bin, src, dest, first=None, last=None):
    """The lossless vector greyscale conversion, as a command.

    Three call sites ran these same eleven flags — the subset conversion, the
    batch retry and the single-page retry — each with its own copy. A flag
    corrected in one of them was a flag still wrong in the other two, and the
    three are meant to differ only in which pages they are given.
    """
    return [gs_bin, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
            "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
            "-dCompatibilityLevel=1.5", "-dAutoRotatePages=/None",
            "-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
            "-dDownsampleMonoImages=false",
            *page_range_flags(first, last),
            "-o", dest, src]


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
        r = report.run(_grey_cmd(gs_bin, one, grey),
                       text=True, errors="replace", timeout=300)
        if r.returncode != 0 or not os.path.getsize(grey):
            return None
        blacked, vanished = conversion_damage(
            _page_luma(src, index), _page_luma(grey, 0))
        if blacked > BLACKOUT_LIMIT or vanished > BLACKOUT_LIMIT:
            return None
        return grey
    except Exception:
        logging.exception("grayscale: single-page retry for page %d failed", index + 1)
        return None
    finally:
        unlink(one)


def _grey_vector(gs_bin, src, out, selected, n_pages, report):
    """Convert the `selected` page indices to greyscale LOSSLESSLY and
    VECTOR-BASED with Ghostscript (pdfwrite + ColorConversionStrategy=Gray):
    text stays text, vectors stay vectors, every colour space (RGB/CMYK/ICC/
    spot) is mapped to DeviceGray, and images keep full resolution (no
    downsampling). Pages NOT selected are copied through unchanged. Runs on a
    worker thread (only paths/ints cross the boundary). Returns (out, summary).

    Only the *selected* pages are handed to Ghostscript, not the whole
    document. A 145-page book where 92 pages need greying and 53 stay colour
    used to spend 15 s in Ghostscript processing all 145; the subset takes
    under 3 s. The unselected pages are copied through unchanged by pikepdf in
    the assembly step — they never go near Ghostscript."""
    import tempfile, contextlib, pikepdf
    report(tr("Ghostscript: Graustufen-Konvertierung …"))
    fd, grey_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    fd, sub_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    # Bound before the try: the finally cleans all of them up, and batch_grey
    # used to be left unbound on the paths that never reached the batch retry
    # — which the cleanup covered by catching NameError.
    repaired = {}
    batch_tmp = batch_grey = None
    try:
        sel = sorted(selected)
        # Extract only the pages that need conversion. On a heavy 150-page
        # document where half the pages are colour (and stay colour), this
        # alone cuts the Ghostscript run from 15 s to 3 s — and the colour
        # pages never risk being altered by the colour conversion either.
        with pikepdf.open(src) as pdf, pikepdf.Pdf.new() as sub:
            for i in sel:
                sub.pages.append(pdf.pages[i])
            sub.save(sub_tmp)

        # The subset goes through in several concurrent runs where it is long
        # enough to be worth it. Ghostscript is single-threaded per document,
        # so the pages of one book were converted one core at a time no matter
        # how many the machine had; the ranges here are of sub_tmp, which is
        # already only the pages that need converting.
        try:
            # errors="replace": Ghostscript writes its diagnostics in the system
            # locale, and a byte it could not decode used to raise UnicodeDecodeError
            # here — burying the actual failure under a decoding error.
            r = failed(run_chunked(
                report,
                lambda dest, first, last: _grey_cmd(gs_bin, sub_tmp, dest, first, last),
                grey_tmp, len(sel), timeout=900))
        except subprocess.TimeoutExpired:
            raise RuntimeError(tr(
                "Ghostscript hat nach 15 Minuten nicht geantwortet und wurde "
                "abgebrochen. Die PDF ist vermutlich beschädigt oder sehr groß."))
        if r is not None:
            raise RuntimeError((r.stderr or r.stdout or tr("Ghostscript-Fehler")).strip()[:400])
        if not os.path.exists(grey_tmp) or os.path.getsize(grey_tmp) == 0:
            raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

        # orig_to_sub maps an original page index to its position in the
        # subset, so verify and assembly can index the Ghostscript output
        # (which only contains the converted pages) back to the original.
        orig_to_sub = {orig: pos for pos, orig in enumerate(sel)}

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
        convertible = {orig for orig, pos in orig_to_sub.items()
                       if orig < n and pos < n_grey}
        report(tr("Konvertierte Seiten prüfen …"))
        damaged = {}
        batch_repaired = {}   # orig_idx -> position in batch_grey
        # Hold the lock for the entire verify + retry phase. The verify and
        # retry functions each acquire the lock internally — RLock re-enters
        # without deadlock — but the render worker is blocked for the whole
        # phase, which is what eliminates the 10 s of contention that stretched
        # this from 7 s to 18 s. The user is watching a progress bar during a
        # conversion; a 5 s viewer freeze is fine there. gc.disable ensures
        # pdfium finalizers never fire on the GC thread (see _scan_pages).
        gc.disable()
        with _pdfium_lock:
            try:
                # Verify needs to compare each converted page against the
                # ORIGINAL of that same page. grey_tmp only contains the
                # selected pages (positions 0..N-1), and so does sub_tmp —
                # page pos in sub_tmp is the original of page pos in grey_tmp.
                # Comparing against `src` directly would line up the WRONG
                # original page whenever the selection isn't an identity map
                # (e.g. sel={5,10,20} would compare original 0 vs converted 5),
                # which can let a blacked-out page ship silently.
                sub_to_orig = {pos: orig for orig, pos in orig_to_sub.items()}
                sub_convertible = {orig_to_sub[i] for i in convertible}
                sub_damaged = _verify_pages_intact(sub_tmp, grey_tmp, sub_convertible, report)
                # Map damaged results back to original page indices
                damaged = {sub_to_orig[pos]: reason for pos, reason in sub_damaged.items()}

                # Give the damaged ones a second chance. Isolating a page drops the
                # surrounding transparency groups that trip Ghostscript up. Rather than
                # spawning a separate Ghostscript process for every damaged page (23
                # pages × 0.3 s each = 7 s of process overhead), all damaged pages are
                # extracted into one temp PDF and converted in a single Ghostscript run.
                # Pages that are STILL damaged after the batch get an individual retry.
                if damaged:
                    report(tr("Beschaedigte Seiten isoliert nachkonvertieren …"))
                    fd, batch_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
                    fd, batch_grey = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
                    try:
                        damaged_sorted = sorted(damaged)
                        with pikepdf.open(src) as pdf, pikepdf.Pdf.new() as batch:
                            for i in damaged_sorted:
                                batch.pages.append(pdf.pages[i])
                            batch.save(batch_tmp)
                        r = report.run(_grey_cmd(gs_bin, batch_tmp, batch_grey),
                                       text=True, errors="replace", timeout=900)
                        if r.returncode == 0 and os.path.exists(batch_grey) and os.path.getsize(batch_grey) > 0:
                            # batch_tmp positions align 1:1 with batch_grey
                            # positions (both built from damaged_sorted order) —
                            # see the sub_tmp comment above for why we can't
                            # compare against `src` directly here.
                            batch_damaged = _verify_pages_intact(
                                batch_tmp, batch_grey, set(range(len(damaged_sorted))), report)
                            for pos in range(len(damaged_sorted)):
                                if pos not in batch_damaged:
                                    orig = damaged_sorted[pos]
                                    batch_repaired[orig] = pos
                            for i in batch_repaired:
                                damaged.pop(i, None)
                    except Exception:
                        logging.exception("grayscale: batch retry failed")
                    finally:
                        unlink(batch_tmp)

                # Still-damaged pages get an individual retry as a last resort.
                for i in sorted(damaged):
                    report(tr('Seite {p0} erneut versuchen …').format(p0=i + 1))
                    fixed = _grey_retry_page(gs_bin, src, i, report)
                    if fixed:
                        repaired[i] = fixed
                for i in repaired:
                    damaged.pop(i, None)
            finally:
                gc.collect(); gc.enable()

        report(tr("Seiten zusammenstellen …"))
        # ExitStack closes whatever was opened even if the second open throws —
        # the old shape opened src_pdf outside the try, so a bad Ghostscript
        # output leaked the source document's handle.
        with contextlib.ExitStack() as stack:
            src_pdf  = stack.enter_context(pikepdf.open(src))
            grey_pdf = stack.enter_context(pikepdf.open(grey_tmp))
            batch_grey_pdf = (stack.enter_context(pikepdf.open(batch_grey))
                             if batch_repaired else None)
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
                    if i in batch_repaired and batch_grey_pdf is not None:
                        out_pdf.pages.append(batch_grey_pdf.pages[batch_repaired[i]]); n_conv += 1
                        continue
                    if i in convertible and i not in damaged:
                        out_pdf.pages.append(grey_pdf.pages[orig_to_sub[i]]); n_conv += 1
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
        if batch_repaired or repaired:
            total_retry = len(batch_repaired) + len(repaired)
            msg += "  — " + tr(
                '{p0} Seite(n) einzeln nachkonvertiert.').format(p0=total_retry)
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
        unlink(*repaired.values(), grey_tmp, sub_tmp, batch_grey)


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
        self._card_w = _CARD_W
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
        # Same two lines nup.py has, and for the same reason: without them the
        # splitter shares out the extra width instead of giving it to the
        # preview, so the sidebar opened wider than it asked for and grew
        # further every time the window did.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([_SIDEBAR_W, 10_000])
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
        self._card_w = _CARD_W; self._rezoom()

    def _rezoom(self):
        if not self._preview_cards: return
        card_h = int(self._card_w * (_CARD_H / _CARD_W))
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
            self._zoom_smooth_timer = QTimer(); self._zoom_smooth_timer.setSingleShot(True)
            self._zoom_smooth_timer.timeout.connect(self._rezoom_smooth)
        self._zoom_smooth_timer.start(180)

    def _rezoom_smooth(self):
        card_h = int(self._card_w * (_CARD_H / _CARD_W))
        for frame, img_lbl, num_lbl in self._preview_cards:
            pm = img_lbl.property("src_pm")
            if pm:
                img_lbl.setPixmap(pm.scaled(self._card_w, card_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
        # Zoom settled: if the card is now wider than the cached render, the
        # preview is an upscaled blur — re-load at a width that fits so it
        # sharpens. _thumb_render_width snaps to 128 px steps, so this only
        # re-renders when the zoom crossed a real ladder rung.
        if self._preview_cards and self._scanned_path:
            needed = _thumb_render_width(max(self._card_w * 2, 200))
            if needed > self._grey_render_w:
                self._load_preview_pixmaps_async(self._scanned_path)

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
        # render width the current preview thumbs are at (zoom-aware; see
        # _load_preview_pixmaps_async). Tracked so a zoom-in that outgrows the
        # cached render re-submits at the wider width instead of staying blurry.
        self._grey_render_w   = 0
        self._grey_thumb_tasks = []

        mode_grp = QGroupBox(tr("Erkennungs-Modus"))
        mg = QVBoxLayout(mode_grp); mg.setSpacing(6); mg.setContentsMargins(8,10,8,8)
        self.mode_single = QRadioButton(tr("1 farbiger Pixel = Farbseite"))
        self.mode_ratio  = QRadioButton(tr("Nach Anteil farbiger Pixel"))
        self.mode_ratio.setChecked(True)
        self.mode_single.toggled.connect(self._on_mode_changed)
        mg.addWidget(self.mode_single); mg.addWidget(self.mode_ratio)
        layout.addWidget(mode_grp)

        # One slider, but a value per mode. The number does a different job in
        # each — "the page is colour if any pixel is this far from grey" versus
        # "a pixel this far from grey counts towards the percentage" — so a
        # position tuned for one mode was quietly re-deciding the other. On a
        # page tinted over a fifth of its area, moving this alone flipped it
        # between colour and grey with the percentage untouched.
        self._thr_by_mode = {"single": _DEFAULT_THR, "ratio": _DEFAULT_THR}
        self._thr_mode = "ratio"          # the mode checked below

        thr_grp = QGroupBox(tr("Farb-Schwellwert"))
        tg = QVBoxLayout(thr_grp); tg.setSpacing(4); tg.setContentsMargins(8,10,8,8)
        self._thr_hint = make_label("", dim=True)
        self._thr_hint.setWordWrap(True)
        tg.addWidget(self._thr_hint)
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel(tr("Streng")))
        self.thr = QSlider(Qt.Orientation.Horizontal)
        self.thr.setRange(1, 80); self.thr.setValue(_DEFAULT_THR)
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
        # The end captions are what the slider actually means: value/20000, so
        # 1 is 0.005 % and 5000 is 25 %. The left one used to read 0.05 %, ten
        # times what the slider does there.
        ratio_row.addWidget(QLabel("0.005%"))
        self.ratio = QSlider(Qt.Orientation.Horizontal)
        self.ratio.setRange(1, 5000); self.ratio.setValue(300)
        self.ratio.setTickInterval(500); self.ratio.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.ratio_lbl = QLabel("1.50%"); self.ratio_lbl.setFixedWidth(44)
        self.ratio.valueChanged.connect(self._on_setting_changed)
        ratio_row.addWidget(self.ratio, 1); ratio_row.addWidget(QLabel("25%")); ratio_row.addWidget(self.ratio_lbl)
        rg.addLayout(ratio_row)
        self._ratio_grp = ratio_grp; ratio_grp.setEnabled(True)
        layout.addWidget(ratio_grp)

        self._sync_threshold_hint()
        AppState.get().pdf_changed.connect(self._on_pdf_changed)

    def _sync_threshold_hint(self):
        """Say what the threshold does in the mode that is switched on.

        The two readings are genuinely different questions, and naming the one
        in force is what makes it obvious that the slider is per-mode rather
        than a single setting that mysteriously moves on its own.
        """
        self._thr_hint.setText(
            tr("Ab welchem Abstand ein Pixel als farbig zaehlt:")
            if self.mode_ratio.isChecked() else
            tr("Abstand vom Grau pro Pixel:"))

    def _on_mode_changed(self):
        # Park the slider under the mode that was using it, then bring in the
        # incoming mode's own value. Without this the two modes shared one
        # number and each retuned the other behind the user's back.
        self._thr_by_mode[self._thr_mode] = self.thr.value()
        self._thr_mode = "ratio" if self.mode_ratio.isChecked() else "single"
        self._ratio_grp.setEnabled(self.mode_ratio.isChecked())
        blocked = self.thr.blockSignals(True)
        self.thr.setValue(self._thr_by_mode[self._thr_mode])
        self.thr.blockSignals(blocked)
        self.thr_lbl.setText(str(self.thr.value()))
        self._sync_threshold_hint()
        if self._page_data: self._reclassify()

    def _on_setting_changed(self, val=None):
        self.thr_lbl.setText(str(self.thr.value()))
        self.ratio_lbl.setText(f"{self.ratio.value() / 200.0:.2f}%")
        self._thr_by_mode[self._thr_mode] = self.thr.value()
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
        CARD_W = self._card_w; CARD_H = int(CARD_W * (_CARD_H/_CARD_W)); GAP = 8; MARGIN = 10
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

        The render width tracks the current zoom: a thumbnail wide enough to
        fill the card at 2x is asked for, snapped onto the queue's 128 px
        ladder via _thumb_render_width. Zooming in past the cached width
        re-runs this (from _rezoom) so the preview sharpens instead of staying
        a blurry upscaled 220 px render forever.
        """
        self._grey_thumb_gen += 1
        gen = self._grey_thumb_gen
        # Drop any still-queued tasks from the previous width and cancel the
        # in-flight ones so a fast zoom scroll does not stack 145 renders.
        _render_queue.cancel_queued(1)
        for t in self._grey_thumb_tasks:
            t.cancel()
        self._grey_thumb_tasks = []
        RENDER_W = _thumb_render_width(max(self._card_w * 2, 200))
        self._grey_render_w = RENDER_W
        for i in range(len(self._preview_cards)):
            cached = _ThumbnailCache.get((pdf_path, i, 0, RENDER_W))
            if cached is None:
                cached = _ThumbnailCache.get_at_least(pdf_path, i, 0, RENDER_W)
            if cached is not None:
                self._on_grey_thumb_ready(gen, i, cached)
            else:
                task = _ThumbTask(gen, i, pdf_path, i, 0,
                                  RENDER_W, self._grey_thumb_sigs)
                self._grey_thumb_tasks.append(task)
                _render_queue.submit(task, 1)

    def _on_grey_thumb_ready(self, gen, cidx, image):
        """GUI-thread callback: paint a newly arrived thumbnail into its card."""
        if gen != self._grey_thumb_gen:
            return   # stale — a newer scan has started
        if cidx >= len(self._preview_cards):
            return
        frame, img_lbl, num_lbl = self._preview_cards[cidx]
        CARD_W = self._card_w; CARD_H = int(CARD_W * (_CARD_H / _CARD_W))
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
                color = STATUS["skipped"]
            elif i in self._manual_sel:
                color = STATUS["forced"]
            elif i in self._grey_pages:
                color = STATUS["converted"]
            else:
                color = STATUS["colour"]
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
        self._load_preview_pixmaps_async(src)

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
        self._reclassify()
        self._update_preview_borders()
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

        # Drop the viewer's pending thumbnail/pre-render tasks so the render
        # worker is not competing for the pdfium lock during the conversion.
        # Each verify render and retry render releases the lock between pages,
        # and the render worker seizes those gaps to render a 500 ms thumbnail
        # — stretching a 7 s conversion into 18 s. Cancelling P1/P2 tasks
        # leaves P0 (the visible page) intact but clears the thumbnail flood.
        _render_queue.cancel_queued(1)
        _render_queue.cancel_queued(2)
        # Also cancel the render worker's currently-running thumbnail, if any —
        # cancel_queued only drops the heap, not the in-flight task. The worker
        # checks _active between render steps and bails out when it flips.
        with _render_queue._cond:
            if _render_queue._running is not None:
                _render_queue._running.cancel()

        gs = ghostscript_binary()
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


def _scan_pages(src, thr, report):
    """Colour histogram of every page, and which pages are already grey.

    Plain data only; the panel classifies and draws back on the GUI thread.
    """
    import pypdfium2 as pdfium
    hists = []
    # PIL images from to_pil() can end up in reference cycles; Python's cyclic
    # GC runs at allocation thresholds on whatever thread happens to allocate —
    # including the render worker, which is NOT under our lock. If it collects a
    # cycled PIL image whose ImagingCore still holds the pdfium bitmap buffer,
    # the buffer's weakref finalizer fires FPDFBitmap_Destroy on the GC thread
    # without the lock, racing the render worker's own pdfium call → heap
    # corruption → SIGSEGV/SIGABRT. Disabling cyclic GC for the scan forces
    # every pdfium object to be freed by refcounting at statement end (always
    # inside the `with _pdfium_lock`), never by a surprise GC pass.
    gc.disable()
    try:
        # PdfDocument(src) is FPDF_LoadDocument and close() is FPDF_CloseDocument —
        # pdfium calls like any other, so both need the process-wide lock. This
        # document has a broken xref (the file that exposed it did), which makes
        # loading slow and widens the race against the render worker: constructing
        # it unlocked corrupted the heap and took the whole app down with a
        # malloc abort, with no Python traceback to show for it.
        with _pdfium_lock:
            doc = pdfium.PdfDocument(src)
        try:
            n = len(doc)
            for i in range(n):
                report.check()
                with _pdfium_lock:
                    pil = doc[i].render(scale=_SCAN_SCALE).to_pil().convert("RGB")
                hist = _colour_histogram(pil)
                hists.append(hist)
                max_diff, ratio = _hist_stats(hist, thr)
                report(tr('Seite {p0}: max={p1}, farbig={p2:.2f}%').format(
                    p0=i + 1, p1=max_diff, p2=ratio * 100))
        finally:
            # Always: a failed render used to leave the document (and its file
            # handle) open for the life of the app.
            with _pdfium_lock:
                doc.close()
    finally:
        gc.collect()   # under no lock — safe: all pdfium objects are already closed
        gc.enable()

    already_grey = set()
    try:
        # Which pages are grey already, so they are left alone. The scan is
        # tools/colorspace.py — this used not to look inside Form XObjects, so a
        # page produced by N-Up, imposition or merge showed no colour spaces at
        # all and was never recognised.
        #
        # document_colorspaces first: it opens the file once and caches every
        # page on the way past, so the page_colorspaces calls below are all
        # cache hits. Asking page by page opened the document once per page.
        document_colorspaces(src)
        for i in range(n):
            if is_grey_only(page_colorspaces(src, i)):
                already_grey.add(i)
    except Exception:
        # Only an optimisation — it marks pages that are already DeviceGray so
        # they are left alone. If it fails they simply get converted like any
        # other page, so carry on, but say so rather than swallowing it.
        already_grey.clear()
        logging.exception("grayscale: colour-space probe failed")
    return hists, already_grey
