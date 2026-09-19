"""
What of a PDF's interactive layer goes on paper.

Adobe Acrobat's print dialog exposes this as "Comments & Forms":

    Dokument                    page + form fields, no comments
    Dokument und Markierungen   page + form fields + comments
    Dokument und Stempel        page + form fields + stamps, no other markups
    Nur Formularfelder          filled-in field values only (pre-printed stock)

Those four names are the ones the dialog shows. This module is the policy
behind them, plus the step that makes a filled-in form actually print.

A field's typed value lives in the field dictionary. What a printer, a RIP
or Ghostscript draws is the widget's *appearance stream*. Fillers that set
the value and leave NeedAppearances set — pypdf, pdftk, a lot of web forms
— produce a file Acrobat shows filled and this application used to send to
the printer empty, because Ghostscript does not generate appearances.

Acrobat regenerates them for the print. The same regeneration is done here
before the file is handed to Ghostscript, and annotations the chosen mode
would leave off the paper are removed first so they cannot sneak through.
"""
import logging
import os
import shutil
import tempfile

from tools.ghostscript import unlink
from tools.render.document_cache import PDFIUM_LOCK, open_document as _open_pdf


DOCUMENT = "document"
DOCUMENT_AND_MARKUPS = "document_and_markups"
DOCUMENT_AND_STAMPS = "document_and_stamps"
FORM_FIELDS_ONLY = "form_fields_only"

MODES = (DOCUMENT, DOCUMENT_AND_MARKUPS, DOCUMENT_AND_STAMPS, FORM_FIELDS_ONLY)

# PDF annotation flags, 1-based bits in /F. See ISO 32000 Table 165.
_INVISIBLE = 1 << 0
_HIDDEN    = 1 << 1
_PRINT     = 1 << 2

_WIDGET = "/Widget"
_STAMP = "/Stamp"
_WATERMARK = "/Watermark"
_PRINTERMARK = "/PrinterMark"
_TRAPNET = "/TrapNet"
# Printed with the document itself, not as review comments. Acrobat's
# "Dokument" keeps these; "Nur Formularfelder" does not.
_WITH_DOCUMENT = {_WIDGET, _WATERMARK, _PRINTERMARK, _TRAPNET}
# Never drawn on paper: the popup window of a comment, and link borders.
_NEVER_PRINT = {"/Popup", "/Link"}


def _subtype(annot):
    try:
        return str(annot.get("/Subtype", "") or "")
    except Exception:
        return ""


def _flags(annot):
    try:
        return int(annot.get("/F", 0) or 0)
    except Exception:
        return 0


def annotation_prints(annot, mode=DOCUMENT):
    """Whether this annotation belongs on paper in Acrobat's `mode`.

    Hidden and Invisible never print. Form fields honour their Print flag
    ("Visible but doesn't print" / "Hidden but printable"). Comments and
    stamps included by the chosen mode print even when the author left the
    Print flag off — picking "Dokument und Markierungen" is the operator
    asking for them.
    """
    subtype = _subtype(annot)
    flags = _flags(annot)
    if flags & (_INVISIBLE | _HIDDEN):
        return False
    if subtype in _NEVER_PRINT:
        return False
    if mode == FORM_FIELDS_ONLY:
        return subtype == _WIDGET and bool(flags & _PRINT)
    if subtype == _WIDGET:
        return bool(flags & _PRINT)
    if subtype in _WITH_DOCUMENT:
        return True
    if mode == DOCUMENT:
        return False
    if mode == DOCUMENT_AND_STAMPS:
        return subtype == _STAMP
    return True     # DOCUMENT_AND_MARKUPS: every remaining visible annot


def _iter_annots(page):
    annots = page.get("/Annots")
    if not annots:
        return []
    try:
        return list(annots)
    except Exception:
        return []


def _ensure_form_for_print(pdf):
    """Make sure pdfium can see the widgets we kept, and will regenerate
    their appearances.

    write_subset_pdf copies pages with pypdf's add_page, which keeps the
    widget annotations but drops the catalog's /AcroForm. Without that
    dictionary pdfium reports FORMTYPE_NONE, init_forms does nothing, and
    flattening has no field values to bake in. Rebuilding Fields from the
    widgets that are actually on the pages, and setting NeedAppearances,
    is what makes a stale or missing appearance stream print filled —
    the same regeneration Acrobat does for the print.
    """
    import pikepdf
    widgets = []
    for page in pdf.pages:
        for annot in _iter_annots(page):
            if _subtype(annot) == _WIDGET:
                widgets.append(annot)
    if not widgets:
        acro = pdf.Root.get("/AcroForm")
        if acro is not None:
            acro["/Fields"] = pikepdf.Array([])
        return False
    acro = pdf.Root.get("/AcroForm")
    if acro is None:
        acro = pikepdf.Dictionary()
        pdf.Root["/AcroForm"] = acro
    # Always rewrite Fields from the widgets still on the pages: a
    # no-print field we just stripped from /Annots would otherwise stay
    # listed here, and flatten would bake it back in.
    acro["/Fields"] = pikepdf.Array(widgets)
    acro["/NeedAppearances"] = True
    return True


def _blank_page_content(pdf, page):
    """Drop the page's own drawing, leaving widgets to be flattened on later.

    Acrobat's "Form fields only" is for printing the typed values onto
    pre-printed stock. The field appearances (border, background, value)
    still go down; the underlying form does not.
    """
    page["/Contents"] = pdf.make_stream(b"")


def _filter_print_content(src, dest, mode):
    """Write `src` to `dest` with annotations the mode would not print
    removed, and return whether any form widgets remain."""
    import pikepdf
    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            kept = [a for a in _iter_annots(page) if annotation_prints(a, mode)]
            if kept:
                page["/Annots"] = pikepdf.Array(kept)
            elif "/Annots" in page:
                del page["/Annots"]
            if mode == FORM_FIELDS_ONLY:
                _blank_page_content(pdf, page)
        has_widgets = _ensure_form_for_print(pdf)
        pdf.save(dest)
    return has_widgets


def _bake_appearances(src, dest, printing=True):
    """Flatten widgets into page content via pdfium.

    Requires a form environment, so `src` must already have an /AcroForm
    and NeedAppearances — see _ensure_form_for_print. FLAT_PRINT honours
    the Print flag, which is how "Visible but doesn't print" stays off
    the paper after the category filter has already run. FLAT_NORMALDISPLAY
    matches the screen, which is what imposition wants: bake what the
    operator sees, Print flag or not.
    """
    import pypdfium2.raw as pdfium_c
    flag = pdfium_c.FLAT_PRINT if printing else pdfium_c.FLAT_NORMALDISPLAY
    with PDFIUM_LOCK:
        doc = _open_pdf(src)
        try:
            if not doc.formenv:
                raise RuntimeError("no form environment")
            for i in range(len(doc)):
                page = doc[i]
                try:
                    page.flatten(flag)
                finally:
                    try:
                        page.close()
                    except Exception:
                        logging.debug("print content: page close failed",
                                      exc_info=True)
            doc.save(dest)
        finally:
            try:
                doc.close()
            except Exception:
                logging.debug("print content: document close failed",
                              exc_info=True)


def prepare_print_pdf(src, dest, mode=DOCUMENT):
    """A copy of `src` that prints the way Acrobat would in `mode`.

    Annotations the mode leaves out are removed; form field values are
    baked into the page so Ghostscript and the printer see them. Never
    raises for a content problem: the original is copied through and the
    print still goes out, rather than failing the job over a form.
    """
    if mode not in MODES:
        mode = DOCUMENT
    fd, filtered = tempfile.mkstemp(suffix="_print_filter.pdf")
    os.close(fd)
    try:
        try:
            has_widgets = _filter_print_content(src, filtered, mode)
        except Exception:
            logging.exception("print content: could not filter annotations")
            if src != dest:
                shutil.copyfile(src, dest)
            return dest
        if has_widgets:
            try:
                _bake_appearances(filtered, dest)
                return dest
            except Exception:
                logging.exception("print content: could not bake form appearances")
        if filtered != dest:
            shutil.copyfile(filtered, dest)
        return dest
    finally:
        unlink(filtered)


def prepare_print_page(src, page_index, dest, mode=DOCUMENT):
    """prepare_print_pdf for a single page — the print preview's input."""
    import pikepdf
    fd, one = tempfile.mkstemp(suffix="_print_page.pdf")
    os.close(fd)
    try:
        with pikepdf.open(src) as pdf:
            if page_index < 0 or page_index >= len(pdf.pages):
                raise IndexError(page_index)
            out = pikepdf.new()
            out.pages.append(pdf.pages[page_index])
            out.save(one)
        return prepare_print_pdf(one, dest, mode)
    finally:
        unlink(one)
