"""
Placing a page into a slot on a sheet. Shared by Broschüre and N-Up.
"""


_ROT_MATRIX = {0: (1.0, 0.0, 0.0, 1.0), 90:  (0.0, -1.0, 1.0, 0.0),
               180: (-1.0, 0.0, 0.0, -1.0), 270: (0.0, 1.0, -1.0, 0.0)}


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
