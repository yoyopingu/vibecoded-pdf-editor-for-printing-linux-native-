"""
Placing a page into a slot on a sheet. Shared by Broschüre and N-Up.
"""

# A page is "too big" for its slot only past this much of a point, so a sheet
# sized from the very pages it has to hold — where slot and page are the same
# number arrived at by two different routes — is never rejected for a rounding
# difference. A PDF stores its boxes to four decimals, which is enough for two
# A4 pages side by side to measure 420.00000006 mm rather than 420.
FIT_EPS_PT = 0.01


def fits_at_full_scale(page_w, page_h, slot_w, slot_h):
    """Whether a page of this size goes into that slot without being shrunk."""
    return page_w <= slot_w + FIT_EPS_PT and page_h <= slot_h + FIT_EPS_PT


def full_scale_problem(page_w, page_h, slot_w, slot_h, need_w, need_h):
    """Why the pages will not fit at 100 %, as a ready-to-show sentence, or None.

    Shared by N-Up and Broschüre because both place a page into a rectangle
    without scaling it, and both have to refuse the same jobs in the same
    words. Each works out `need_w`/`need_h` — the sheet that would hold them —
    its own way, because a grid and a folded sheet arrive at it differently.
    """
    from tools.i18n import tr
    from tools.panels._shared import MM_TO_PT
    import math
    if fits_at_full_scale(page_w, page_h, slot_w, slot_h):
        return None
    mm = lambda pt: pt / MM_TO_PT
    # Round the requirement up to a whole millimetre, but ignore anything
    # inside the tolerance above — otherwise a job that fits on 420 mm asks
    # the operator for 421.
    up = lambda pt: math.ceil(mm(pt) - mm(FIT_EPS_PT))
    return tr("Bei 100 % ist kein Platz: Seite {p0}×{p1} mm, Feld {p2}×{p3} mm. "
              "Nötig wäre ein Ausgabeformat von mindestens {p4}×{p5} mm.").format(
        p0=round(mm(page_w)), p1=round(mm(page_h)),
        p2=round(mm(max(slot_w, 0))), p3=round(mm(max(slot_h, 0))),
        p4=up(need_w), p5=up(need_h))



_ROT_MATRIX = {0: (1.0, 0.0, 0.0, 1.0), 90:  (0.0, -1.0, 1.0, 0.0),
               180: (-1.0, 0.0, 0.0, -1.0), 270: (0.0, 1.0, -1.0, 0.0)}


def form_factory(src_doc):
    """A memoized page -> Form XObject converter for one open document.

    N-Up and Broschüre both turn each source page into a Form XObject once
    and reuse it across every slot and sheet it is placed in, and both did
    so with the same fourteen-line inner function — this is that function,
    factored out once instead of kept in step by hand in two files.

    Two things are pinned deliberately, in the Form XObject this builds:
      * the BBox is the page's *visible* box (CropBox clipped to MediaBox).
        as_form_xobject would otherwise take TrimBox → CropBox → MediaBox
        verbatim, so a print PDF's bleed TrimBox — or a stale CropBox left
        behind by an earlier crop — decided the layout and pushed the
        content off-centre, showing something different from the preview.
      * /Rotate is applied through our own exact matrix instead of qpdf's,
        which truncates the rotation offset to whole points.

    Returns a `form_for(page_i) -> (form_xobject, visible_box, rotate)`
    function, closed over a cache private to this call — forms from one
    document are never reused for another.
    """
    from pikepdf import Array
    from tools.panels._shared import _inherited_rotate, _visible_box

    forms = {}

    def form_for(page_i):
        if page_i not in forms:
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
            forms[page_i] = (fx, box, rot)
        return forms[page_i]

    return form_for


def _slot_placement(box, rot, rect, fixed_scale=None):
    """Matrix that fits a page into a slot: scale it to fit `rect` keeping its
    aspect ratio, and centre it there. `box` is the page's visible rectangle,
    `rot` its /Rotate. Returns (scale, tx, ty) for a ``s 0 0 s tx ty cm``, applied
    after the rotation matrix — i.e. exactly the placement pikepdf's add_overlay
    performs, but computed in full precision. qpdf writes that matrix rounded to
    five decimals and truncates the rotation offset to a whole point, which left
    the content up to ~1.5pt off-centre in its slot — small, but this is a print
    tool and it showed up as visibly uneven margins.

    `fixed_scale` places the page at that scale instead of fitting it (still
    centred) — what the Broschüre tool does when normalising is switched off."""
    x0, y0, x1, y1 = box
    a, b, c, d = _ROT_MATRIX[rot % 360 if rot % 90 == 0 else 0]
    pts = [(a * x + c * y, b * x + d * y) for x in (x0, x1) for y in (y0, y1)]
    bx0 = min(p[0] for p in pts); bx1 = max(p[0] for p in pts)
    by0 = min(p[1] for p in pts); by1 = max(p[1] for p in pts)
    bw  = max(bx1 - bx0, 1e-6);   bh  = max(by1 - by0, 1e-6)
    rx0, ry0, rx1, ry1 = rect
    s  = min((rx1 - rx0) / bw, (ry1 - ry0) / bh) if fixed_scale is None else fixed_scale
    tx = rx0 + ((rx1 - rx0) - bw * s) / 2.0 - bx0 * s
    ty = ry0 + ((ry1 - ry0) - bh * s) / 2.0 - by0 * s
    return s, tx, ty


def _flatten_annots(doc):
    """Bake annotation appearances (stamps, signatures, filled form fields) into
    the page content.

    Imposition turns every source page into a Form XObject, and an XObject
    carries content only — annotations stay behind on the page that is being
    left behind. Anything the user could see but that lived in an /AP stream
    would silently vanish from the printed sheet."""
    if not any("/Annots" in p.obj for p in doc.pages):
        return
    try:
        doc.flatten_annotations("all")
    except Exception:
        pass          # older qpdf: better an un-flattened page than no output
