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
from PyQt6.QtWidgets import QVBoxLayout, QGroupBox, QCheckBox
from tools.panels.base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import PaperFormatSelector, _visible_size
from tools.panels._imposition import (_slot_placement, _flatten_annots,
                                      form_factory, full_scale_problem)


# ══════════════════════════════════════════════════════════════════════════════
# IMPOSE
# ══════════════════════════════════════════════════════════════════════════════
class ImposePanel(BasePanel):
    TITLE         = "Broschüre / Ausschießen"
    SUBTITLE      = "Seiten in Falzreihenfolge auf größere Bögen ausschießen."

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
