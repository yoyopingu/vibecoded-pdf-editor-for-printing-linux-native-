"""Printing: forms behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _form_pdf, _print_dialog


def _dark_box(path, box, scale=2, forms=False):
    """Dark pixels in a PDF-space box (x, y_from_bottom, w, h) in points."""
    d = pdfium.PdfDocument(path)
    if forms:
        d.init_forms()
    try:
        pil = d[0].render(scale=scale, fill_color=(255, 255, 255, 255),
                          may_draw_forms=forms).to_pil().convert("L")
    finally:
        d.close()
    x, y, w, h = box
    x0, y0 = int(x * scale), int((842 - y - h) * scale)
    crop = pil.crop((x0, y0, x0 + int(w * scale), y0 + int(h * scale)))
    return sum(1 for v in crop.get_flattened_data() if v < 200)


_FIELD_BOX = (60, 700, 300, 24)
_LABEL_BOX = (60, 770, 120, 20)


def test_print_dialog_offers_acrobat_comments_and_forms():
    """Acrobat's print dialog has a Comments & Forms combo that decides
    whether form fields, comments and stamps go on paper. Without it this
    app always sent the raw PDF, and filled fields that only existed as
    a /V value printed blank."""
    from tools.printing.content import (
        DOCUMENT, DOCUMENT_AND_MARKUPS, DOCUMENT_AND_STAMPS, FORM_FIELDS_ONLY)
    tab, dlg = _print_dialog(1, "comments_forms.pdf")
    try:
        assert dlg.comments_combo.count() == 4
        modes = [dlg.comments_combo.itemData(i)
                 for i in range(dlg.comments_combo.count())]
        assert modes == [DOCUMENT, DOCUMENT_AND_MARKUPS,
                         DOCUMENT_AND_STAMPS, FORM_FIELDS_ONLY]
        assert dlg.comments_forms_mode() == DOCUMENT, \
            "Acrobat opens on Document — page plus fields, no comments"
    finally:
        dlg.close(); tab.deleteLater(); _app.processEvents()
    return "Comments & Forms combo, default Document"


def test_a_filled_form_prints_its_values_not_empty_boxes():
    """Ghostscript does not honour NeedAppearances. A form filled by pypdf
    (or pdftk, or a browser) carries the typed value in /V and asks the
    viewer to draw it; sending that file to the printer produced empty
    fields. The print path now bakes the appearances in first, the way
    Acrobat does for the print.
    """
    from tools.printing.content import DOCUMENT, prepare_print_pdf
    src = _form_pdf("filled_needapp.pdf", regenerate=True)
    empty = _form_pdf("empty_needapp.pdf", value=None)
    out = os.path.join(_TMP, "filled_prepared.pdf")
    prepare_print_pdf(src, out, DOCUMENT)

    # Without a form environment — what Ghostscript and the printer see.
    printed = _dark_box(out, _FIELD_BOX, forms=False)
    blank = _dark_box(empty, _FIELD_BOX, forms=False)
    shown = _dark_box(src, _FIELD_BOX, forms=True)
    assert printed > blank + 500, (
        f"prepared print has {printed} dark px in the field, empty form "
        f"has {blank} — the typed value was not baked in")
    assert printed >= shown * 0.7, (
        f"printed field ({printed}) is much lighter than the on-screen "
        f"one ({shown}) — flattening dropped the value")
    return f"filled value survives print prep ({printed} dark px)"


def test_stale_appearances_are_regenerated_for_print():
    """NeedAppearances false and an appearance stream that still shows the
    empty field: Acrobat regenerates for the print. So do we, by forcing
    NeedAppearances before flattening."""
    from tools.printing.content import DOCUMENT, prepare_print_pdf
    src = _form_pdf("stale_ap.pdf", regenerate=False)
    out = os.path.join(_TMP, "stale_prepared.pdf")
    prepare_print_pdf(src, out, DOCUMENT)
    printed = _dark_box(out, _FIELD_BOX, forms=False)
    empty = _dark_box(_form_pdf("stale_empty.pdf", value=None),
                      _FIELD_BOX, forms=False)
    assert printed > empty + 500, (
        f"stale appearance was kept ({printed} vs empty {empty})")
    return f"stale AP regenerated ({printed} dark px)"


def test_form_fields_only_drops_the_page_and_keeps_the_value():
    """Acrobat's 'Form fields only' prints the typed values onto blank
    paper — for filling in a pre-printed original. The label next to the
    field is page content and must go; the value must stay."""
    from tools.printing.content import FORM_FIELDS_ONLY, prepare_print_pdf
    src = _form_pdf("fields_only.pdf")
    out = os.path.join(_TMP, "fields_only_out.pdf")
    prepare_print_pdf(src, out, FORM_FIELDS_ONLY)
    label = _dark_box(out, _LABEL_BOX, forms=False)
    field = _dark_box(out, _FIELD_BOX, forms=False)
    assert label < 80, f"page content was left on the sheet ({label} dark px)"
    assert field > 500, f"the filled value was lost ({field} dark px)"
    return "form fields only: label gone, value kept"


def test_visible_but_does_not_print_stays_off_the_paper():
    """Acrobat's field visibility 'Visible but doesn't print' is the Print
    flag off. Document mode still has to honour it, or a 'do not print
    this box' field lands on every copy."""
    import pikepdf
    from tools.printing.content import DOCUMENT, prepare_print_pdf
    src = _form_pdf("noprint_src.pdf")
    flagged = os.path.join(_TMP, "noprint_flagged.pdf")
    with pikepdf.open(src) as pdf:
        for a in pdf.pages[0]["/Annots"]:
            a["/F"] = 0          # no Print bit
        pdf.save(flagged)
    out = os.path.join(_TMP, "noprint_out.pdf")
    prepare_print_pdf(flagged, out, DOCUMENT)
    field = _dark_box(out, _FIELD_BOX, forms=False)
    # Border and value both gone; only anti-alias crumbs at most.
    assert field < 80, f"a no-print field still landed on the page ({field})"
    label = _dark_box(out, _LABEL_BOX, forms=False)
    assert label > 80, "the page itself was dropped with the field"
    return "Visible but doesn't print is honoured"


def test_document_mode_drops_comments_and_keeps_fields():
    """Acrobat's default 'Dokument' prints form fields and leaves review
    comments off the paper."""
    import pikepdf
    from tools.printing.content import (
        DOCUMENT, DOCUMENT_AND_MARKUPS, annotation_prints)

    src = _form_pdf("with_comment.pdf")
    commented = os.path.join(_TMP, "with_comment_annot.pdf")
    with pikepdf.open(src) as pdf:
        annot = pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Text,
            Rect=[400, 700, 430, 730],
            F=4,
            Contents="bitte prüfen",
        )
        pdf.pages[0]["/Annots"].append(pdf.make_indirect(annot))
        pdf.save(commented)

    with pikepdf.open(commented) as pdf:
        kinds = {str(a.get("/Subtype")): a for a in pdf.pages[0]["/Annots"]}
        assert "/Widget" in kinds and "/Text" in kinds
        assert annotation_prints(kinds["/Widget"], DOCUMENT)
        assert not annotation_prints(kinds["/Text"], DOCUMENT)
        assert annotation_prints(kinds["/Text"], DOCUMENT_AND_MARKUPS)
    return "Dokument keeps fields, drops comments"


def test_a_checked_box_prints_checked():
    """Checkboxes are widgets too. A ticked box whose appearance was never
    generated used to print empty, same as a text field."""
    from pypdf import PdfWriter
    from tools.printing.content import DOCUMENT, prepare_print_pdf
    src = os.path.join(_TMP, "check_src.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.acroForm.checkbox(name="ok", x=60, y=700, size=18, checked=False,
                        buttonStyle="check", borderWidth=1)
    c.showPage(); c.save()
    filled = os.path.join(_TMP, "check_filled.pdf")
    w = PdfWriter(clone_from=src)
    w.update_page_form_field_values(
        w.pages[0], {"ok": "/Yes"}, auto_regenerate=True)
    with open(filled, "wb") as f:
        w.write(f)
    out = os.path.join(_TMP, "check_out.pdf")
    prepare_print_pdf(filled, out, DOCUMENT)
    empty = _dark_box(src, (60, 700, 18, 18), forms=False)
    printed = _dark_box(out, (60, 700, 18, 18), forms=False)
    assert printed > empty + 20, (
        f"checked box printed as empty ({printed} vs unchecked {empty})")
    return f"checked box prints ({printed} dark px vs {empty} empty)"
