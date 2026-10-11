"""
Broschüre / Ausschießen — imposition: lay the pages out on larger sheets, in
the order a folded booklet needs.

One job: saddle-stitch imposition. There was a MODUS box offering an "N-up
Raster (Spalten × Zeilen)" mode and a "2-up" mode as well, and both were the
N-Up Layout tool written a second time — without that tool's margins, gaps,
crop marks or preview. 2-up in particular is exactly a 2×1 grid with the pages
in reading order.

What is left is the thing N-Up cannot do: order the pages so the sheets fold
into a booklet. That is not a layout option, it is the whole tool, so there is
nothing to choose a mode between.
"""
from contextlib import ExitStack
from PyQt6 import sip
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QVBoxLayout, QGroupBox, QCheckBox, QSplitter, QLabel
from tools._base import BasePanel, make_label
from tools.app_state import AppState
from tools.i18n import tr
from tools.jobs import submit
from tools.panels._shared import PaperFormatSelector, _visible_size, PreviewPane, PreviewPageBar, MM_TO_PT
from tools.panels._imposition import (_slot_placement, _flatten_annots,
                                      form_factory, full_scale_problem)
from tools.render.caches import cached_screen_page
from tools.render.document_cache import _stat_key
from tools.render.placeholder import paint_page_number
from tools.render.queue import _ThumbSignals, _ThumbTask, _render_queue, _thumb_render_width
from tools.theme import PAPER, _TV, _register_themed


# ══════════════════════════════════════════════════════════════════════════════
# IMPOSE
# ══════════════════════════════════════════════════════════════════════════════
class ImposePanel(BasePanel):
    TITLE         = "Broschüre / Ausschießen"
    SUBTITLE      = "Seiten in Falzreihenfolge auf größere Bögen ausschießen."

    def __init__(self, parent=None):
        self._sheet_index = 0
        self._sides = []
        self._sizes = []
        self._metadata_key = None
        self._pending_key = None
        self._metadata_job = None
        self._metadata_error = None
        self._tasks = {}
        self._generation = 0
        self._signals = _ThumbSignals()
        self._signals.ready.connect(self._image_ready)
        super().__init__(parent)

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._tool_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._tool_splitter.addWidget(self.build_tool_sidebar())
        right = self.build_tool_right_panel()
        self._pane = PreviewPane(
            lambda w, h, z: self._render_preview(w, h, z, async_load=True),
            page_step=self._step_sheet)
        self._preview = self._pane.label
        self._preview_info = self._pane.info
        self._sheet_bar = PreviewPageBar('Blattseite')
        self._sheet_bar.valueChanged.connect(self._show_sheet)
        self._sheet_status = QLabel()
        self._sheet_status.setObjectName('dimLabel')
        self._pane.add_navigation(self._sheet_bar, self._sheet_status)
        self._set_sheet_range()
        right.layout().addWidget(self._pane)
        self._tool_splitter.addWidget(right)
        self._tool_splitter.setStretchFactor(0, 0)
        self._tool_splitter.setStretchFactor(1, 1)
        self._tool_splitter.setSizes([400, 800])
        outer.addWidget(self._tool_splitter)
        _register_themed(self)
        self._apply_theme()
        AppState.get().pdf_changed.connect(self._document_changed)

    def _apply_theme(self):
        self.apply_split_view_theme()
        self._preview.setStyleSheet(f"background:{_TV['card_bg']};")
        self._tool_splitter.setStyleSheet(
            f"QSplitter::handle{{background:{_TV['splitter']};width:2px;}}")
        self._pane.refresh()

    def _cancel_images(self):
        self._generation += 1
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    def _document_changed(self, *_):
        self._cancel_images()
        if self._metadata_job is not None:
            self._metadata_job.cancel()
        self._metadata_key = self._pending_key = self._metadata_error = None
        self._sizes = []
        self._sides = []
        self._sheet_index = 0
        self._set_sheet_range()
        self._pane.refresh()

    def _set_sheet_range(self):
        total = len(self._sides)
        self._sheet_index = min(self._sheet_index, max(0, total - 1))
        blocked = self._sheet_bar.blockSignals(True)
        self._sheet_bar.setRange(0, max(0, total - 1))
        self._sheet_bar.setPageStep(1)
        self._sheet_bar.setValue(self._sheet_index)
        self._sheet_bar.blockSignals(blocked)
        self._sheet_bar.setEnabled(total > 1)
        self._pane.set_page_total(total)
        self._sheet_status.setText(tr('Blatt {p0} / {p1} · {p2}').format(
            p0=self._sheet_index // 2 + 1 if total else 0, p1=total // 2,
            p2=tr('Vorderseite') if self._sheet_index % 2 == 0 else tr('Rückseite')))

    def _show_sheet(self, index):
        self._cancel_images()
        self._sheet_index = index
        self._set_sheet_range()
        self._pane.refresh()

    def _step_sheet(self, direction):
        self._sheet_bar.setValue(max(0, min(len(self._sides) - 1,
                                          self._sheet_index + direction)))

    def _preview_sources(self):
        # Resolve the model directly: a flattened snapshot loses the cache keys
        # of pages already rendered in the viewer and makes large jobs wait.
        state = AppState.get()
        model = state.page_model
        if model is not None:
            return tuple((*model.page_source(uid, state.current_pdf),
                          model.get_rotation(uid)) for uid in model.order)
        return ()

    def _request_sizes(self, sources, key):
        if self._pending_key == key:
            return
        if self._metadata_job is not None:
            self._metadata_job.cancel()
        self._pending_key = key
        self._metadata_error = None

        def ready(sizes):
            if sip.isdeleted(self) or self._pending_key != key:
                return
            self._pending_key = None
            self._metadata_key, self._sizes = key, sizes
            self._pane.refresh()

        def failed(error):
            if sip.isdeleted(self) or self._pending_key != key:
                return
            self._pending_key = None
            self._metadata_error = (key, str(error))
            self._pane.refresh()

        self._metadata_job = submit(
            lambda _job: _booklet_page_sizes(sources), owner=self,
            name='booklet-preview-dimensions', on_done=ready, on_error=failed)

    def _image_ready(self, generation, _slot, _image):
        if generation != self._generation or sip.isdeleted(self):
            return
        self._pane.refresh()

    def _page_image(self, source, size, width):
        # Bound render sizes independently of zoom, and reuse both shared screen
        # caches. Only the two visible pages get work, at foreground priority so
        # switching away from the viewer cannot cancel these as old thumbnails.
        wanted = max(1, min(800, width))
        image, sharp = cached_screen_page(*source, wanted)
        if not sharp:
            base_width = wanted * size[1] / size[0] if source[2] % 180 else wanted
            render_width = _thumb_render_width(base_width, cap=1536)
            key = (*source, render_width)
            task = self._tasks.get(key)
            if task is None or not task._active:
                task = _ThumbTask(self._generation, 0, *source, render_width, self._signals)
                self._tasks[key] = task
                _render_queue.submit(task, 0)
        return image

    def _render_preview(self, avail_w, avail_h, zoom, async_load=False):
        sources = self._preview_sources()
        if not AppState.get().current_pdf or not sources:
            self._sides = []
            self._set_sheet_range()
            return None, tr('Keine PDF geöffnet')
        revisions = tuple((path, _stat_key(path)) for path in sorted({s[0] for s in sources}))
        key = (sources, revisions)
        if key != self._metadata_key:
            self._cancel_images()
            if async_load:
                if self._metadata_error is not None and self._metadata_error[0] == key:
                    return None, tr('Vorschau: {p0}').format(p0=self._metadata_error[1])
                self._request_sizes(sources, key)
                return None, tr('rendert …')
            self._sizes = _booklet_page_sizes(sources)
            self._metadata_key = key
        sizes = self._sizes
        self._sides = _booklet_sides(len(sources))
        self._set_sheet_range()
        page_w, page_h = max(sizes, key=lambda wh: wh[0] * wh[1])
        sheet_w, sheet_h = self.out_fmt.target_size_pt() or (page_w * 2, page_h)
        fit = self.norm_check.isChecked()
        if not fit:
            problem = full_scale_problem(page_w, page_h, sheet_w / 2, sheet_h,
                                         need_w=page_w * 2, need_h=page_h)
            if problem:
                return None, problem
        scale = self._pane.page_scale(min(avail_w / sheet_w, avail_h / sheet_h), zoom)
        pm = QPixmap(max(1, int(sheet_w * scale)), max(1, int(sheet_h * scale)))
        pm.fill(QColor(PAPER))
        painter = QPainter(pm)
        pending = False
        for slot, page in enumerate(self._sides[self._sheet_index]):
            if page is None:
                continue
            pw, ph = sizes[page]
            factor = min(sheet_w / 2 / pw, sheet_h / ph) if fit else 1.0
            width, height = max(1, int(pw * factor * scale)), max(1, int(ph * factor * scale))
            rect = QRect(int((slot + .5) * sheet_w / 2 * scale - width / 2),
                         int(sheet_h * scale / 2 - height / 2), width, height)
            image = self._page_image(sources[page], sizes[page], width)
            if image is None:
                paint_page_number(painter, rect, sources[page][1])
                pending = True
            else:
                painter.drawImage(rect, image)
            painter.setPen(QPen(QColor(_TV['acc']), 1))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
        painter.setPen(QPen(QColor(_TV['dim']), 1, Qt.PenStyle.DashLine))
        painter.drawLine(pm.width() // 2, 0, pm.width() // 2, pm.height())
        painter.end()
        info = f'{sheet_w / MM_TO_PT:.0f}×{sheet_h / MM_TO_PT:.0f} mm'
        if pending:
            info += ' · ' + tr('rendert …')
        return pm, info

    def hideEvent(self, event):
        self._cancel_images()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._cancel_images()
        if self._metadata_job is not None:
            self._metadata_job.cancel()
        self._pending_key = None
        super().closeEvent(event)

    def build_ui(self, layout):
        # The same widget Crop/Scale and N-Up use, so all three offer the same
        # paper list, the same custom width x height and the same Querformat.
        # This tool had a five-entry list of its own that described the *page*
        # and left the sheet implicit at twice its width — so the one number a
        # print shop actually needs, what comes out of the printer, could not
        # be said at all.
        ob = QGroupBox(tr("AUSGABEFORMAT")); ol = QVBoxLayout(ob)
        self.AUTO_FORMAT = tr("Wie Quellseite × 2  (automatisch)")
        self.out_fmt = PaperFormatSelector(after=[self.AUTO_FORMAT])
        self.out_fmt.set_format(self.AUTO_FORMAT)
        self.out_fmt.changed.connect(lambda: self._pane.refresh())
        ol.addWidget(self.out_fmt)
        ol.addWidget(make_label(tr(
            "Der Bogen, auf den gedruckt wird. Zwei Seiten liegen nebeneinander\n"
            "darauf — A3 quer nimmt also zwei A4-Seiten auf."), dim=True))
        layout.addWidget(ob)

        nb = QGroupBox(tr("SEITEN NORMALISIEREN")); nl = QVBoxLayout(nb)
        nl.addWidget(make_label(tr(
            "Alle Seiten werden auf ihre Hälfte des Bogens gebracht\n"
            "(Proportionen bleiben erhalten, Rand wird aufgefüllt)."), dim=True))
        self.norm_check = QCheckBox(tr("Vor dem Ausschießen normalisieren"))
        self.norm_check.setChecked(True); nl.addWidget(self.norm_check)
        self.norm_check.toggled.connect(lambda: self._pane.refresh())
        layout.addWidget(nb)

    def _run_action(self):
        import pikepdf
        src = self.require_pdf()
        out = self.save_pdf(tr("Ausgeschossene PDF speichern als"))
        if not out: raise ValueError(tr("Kein Ausgabepfad."))

        with pikepdf.open(src) as doc:
            n = len(doc.pages)
            if not n: raise ValueError(tr("Die PDF hat keine Seiten."))
            page_w, page_h = _largest_page(doc)

        sheet = self.out_fmt.target_size_pt()      # None = "Wie Quellseite × 2"
        if sheet is None:
            sheet_w, sheet_h = page_w * 2, page_h
        else:
            sheet_w, sheet_h = sheet               # Querformat already applied
        half_w = sheet_w / 2.0
        halves = [(0.0, 0.0, half_w, sheet_h), (half_w, 0.0, sheet_w, sheet_h)]

        fit = self.norm_check.isChecked()
        if not fit:
            # Without normalising, each page goes on at its own size. Before the
            # sheet could be chosen it was always twice the page, so that always
            # fitted; now it need not, and clipped content is not something to
            # discover after printing.
            problem = full_scale_problem(page_w, page_h, half_w, sheet_h,
                                         need_w=page_w * 2, need_h=page_h)
            if problem:
                raise ValueError(problem)

        sheets = [list(zip(side, halves)) for side in _booklet_sides(n)]
        summary = lambda placed, nsheets: tr(
            'Broschüre: {p0} Seiten auf {p1} Blattseiten.').format(p0=placed, p1=nsheets)

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


def _booklet_page_sizes(sources):
    """Read only page-tree geometry, without parsing or rasterising content.

    Open each source once, including inserted pages from other files. This
    avoids waiting for pdfium's global lock just to size a numbered placeholder.
    """
    import pikepdf
    with ExitStack() as stack:
        docs = {path: stack.enter_context(pikepdf.open(path))
                for path in {source[0] for source in sources}}
        sizes = []
        for path, page, rotation in sources:
            w, h = _visible_size(docs[path].pages[page])
            sizes.append((h, w) if rotation % 180 else (w, h))
        return sizes


def _largest_page(doc):
    """The biggest page in the document *as displayed* (visible box, /Rotate
    applied) — what "Wie Quellseite × 2" sizes the sheet from, and what has to
    fit its half when normalising is off.

    Deliberately the largest single page, not max(width) × max(height) over all
    of them: a document mixing portrait and landscape A4 made those two maxima
    841×841, so every sheet came out square.

    The fixed paper sizes this used to also answer with are gone — those are
    the sheet now, and the sheet is chosen with the same PaperFormatSelector
    the other tools use, which offers the full paper list and a custom size."""
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
    from pikepdf import Pdf, Page, Stream, Name
    src_doc = Pdf.open(src)
    _flatten_annots(src_doc)
    out_doc = Pdf.new()

    # See form_factory's docstring for what it pins down about the Form
    # XObject it builds (visible box vs. TrimBox, our own rotation matrix).
    _form_for = form_factory(src_doc)

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
