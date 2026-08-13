"""
Broschüre / Ausschießen — saddle-stitch imposition: normalise the pages and
lay them out on larger sheets in the order a folded booklet needs.
"""
from PyQt6.QtWidgets import QVBoxLayout, QSpinBox, QComboBox, QGroupBox, QCheckBox
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import _inherited_rotate, _visible_box, _visible_size, row
from tools.panels._imposition import _ROT_MATRIX, _slot_placement, _flatten_annots


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
