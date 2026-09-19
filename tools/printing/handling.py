"""Print-dialog imposition: poster tiles and n-up sheets (booklet next).

The dialog already subsets the job and runs Comments & Forms. This module
turns that prepared file into sheet-sized pages so spool.py can send them
as a normal Size job at 100 % — CUPS must not fit or shrink a tile that is
already the sheet.

No Qt. The spooler prints this file. The dialog preview still composites
a simplified tile from the page pixmap — rasterising the tiled PDF
inside the printing-module test process trips a heap fault.
"""
import math
import os

from tools.panels._shared import MM_TO_PT
from tools.panels._imposition import (
    FIT_EPS_PT, form_factory, _flatten_annots, _slot_placement)


def poster_grid(src_w, src_h, paper_w, paper_h, tile_pct):
    """Columns, rows and scale for one source page tiled onto `paper_*`.

    100 % of an A4 page on A4 paper is 1×1; 200 % is 2×2. Overlap is a
    taping allowance on adjacent tiles, not another row or column — a 3 mm
    overlap on that 200 % job is still four sheets.
    """
    scale = max(10.0, float(tile_pct or 100)) / 100.0
    pw = max(float(paper_w), 1e-6)
    ph = max(float(paper_h), 1e-6)
    # FIT_EPS_PT so an A4 page measured as 842 pt on A4 paper at 200 %
    # stays 2×2, not 2×3.
    cols = max(1, math.ceil(float(src_w) * scale / pw - FIT_EPS_PT))
    rows = max(1, math.ceil(float(src_h) * scale / ph - FIT_EPS_PT))
    return cols, rows, scale


def _inward_cut_marks(paper_w, paper_h, length=10.0, inset=6.0):
    """L-shaped crop marks sitting on the tile, not outside it.

    The shared crop-mark helper offsets outward from a trim box, which on a
    poster tile is the paper edge — those strokes would land off the sheet.
    Acrobat draws the ticks on the tile so they survive the cut.
    """
    ops = ["q", "0 0 0 RG", "0.5 w"]
    for cx, cy, dx, dy in (
            (inset, inset, 1, 1),
            (paper_w - inset, inset, -1, 1),
            (inset, paper_h - inset, 1, -1),
            (paper_w - inset, paper_h - inset, -1, -1)):
        ops.append(f"{cx:.2f} {cy:.2f} m {cx + dx * length:.2f} {cy:.2f} l S")
        ops.append(f"{cx:.2f} {cy:.2f} m {cx:.2f} {cy + dy * length:.2f} l S")
    ops.append("Q")
    return ("\n".join(ops)).encode("latin-1")


def _pdf_literal(text):
    s = str(text or "").encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _tile_id(row, col):
    """A1 is top-left, then A2, B1 — Acrobat's poster labels."""
    return f"{chr(65 + (row % 26))}{col + 1}"


def _visual_size(box, rot):
    w = max(float(box[2] - box[0]), 1e-6)
    h = max(float(box[3] - box[1]), 1e-6)
    if int(rot or 0) % 180:
        return h, w
    return w, h


def build_poster_pdf(src, pages, paper_pts, tile_pct, overlap_mm, cut_marks,
                     labels, dest, label_name=""):
    """One source page → N paper-sized tiles. Returns the number of tiles.

    `pages` are 0-based indices into `src`. `paper_pts` is the oriented sheet
    as (width, height); when it is None each page is tiled onto a sheet of
    its own size (100 % then stays one tile). Comments & Forms must already
    have been applied — this only places what it is given.
    """
    from pikepdf import Pdf, Page, Stream, Name, Dictionary

    src_doc = Pdf.open(src)
    _flatten_annots(src_doc)
    n_src = len(src_doc.pages)
    if pages is None:
        pages = list(range(n_src))
    pages = [p for p in pages if isinstance(p, int) and 0 <= p < n_src]
    if not pages:
        src_doc.close()
        raise RuntimeError("poster: no pages to tile")

    overlap_mm = max(0.0, float(overlap_mm or 0.0))
    overlap_pt = overlap_mm * MM_TO_PT
    want_marks = bool(cut_marks)
    want_labels = bool(labels)
    name = label_name or os.path.basename(src)
    form_for = form_factory(src_doc)
    out_doc = Pdf.new()
    font = Dictionary(Type=Name.Font, Subtype=Name.Type1,
                      BaseFont=Name.Helvetica)

    tiled = 0
    try:
        for src_i in pages:
            fx, box, rot = form_for(src_i)
            vw, vh = _visual_size(box, rot)
            if paper_pts:
                paper_w, paper_h = float(paper_pts[0]), float(paper_pts[1])
            else:
                paper_w, paper_h = vw, vh
            cols, rows, scale = poster_grid(vw, vh, paper_w, paper_h, tile_pct)
            step_w = max(paper_w - overlap_pt, paper_w * 0.1)
            step_h = max(paper_h - overlap_pt, paper_h * 0.1)
            scaled_w, scaled_h = vw * scale, vh * scale
            mark_ops = (_inward_cut_marks(paper_w, paper_h)
                        if want_marks else None)

            for row in range(rows):          # row 0 = top of the page
                for col in range(cols):
                    sheet = Page(out_doc.add_blank_page(
                        page_size=(paper_w, paper_h)))
                    xobj = sheet.add_resource(fx, Name.XObject, prefix="Pst")
                    tx = -col * step_w
                    # PDF y grows up; the first row is the top of the poster.
                    ty = paper_h - scaled_h + row * step_h
                    rect = (tx, ty, tx + scaled_w, ty + scaled_h)
                    s, cmx, cmy = _slot_placement(
                        box, rot, rect, fixed_scale=scale)
                    # Clip to the sheet so a neighbour's overlap does not
                    # paint off the tile — printers clip MediaBox, tests
                    # render it.
                    ops = [
                        "q",
                        f"0 0 {paper_w:.4f} {paper_h:.4f} re W n",
                        f"{s:.6f} 0 0 {s:.6f} {cmx:.6f} {cmy:.6f} cm {xobj} Do",
                        "Q",
                    ]
                    sheet.contents_add(
                        Stream(out_doc, ("\n".join(ops) + "\n").encode("latin-1")))
                    if mark_ops is not None:
                        sheet.contents_add(Stream(out_doc, mark_ops))
                    if want_labels:
                        fname = sheet.add_resource(font, Name.Font, prefix="Lbl")
                        tag = f"{name}  {_tile_id(row, col)}"
                        text = (
                            f"q BT {fname} 7 Tf 8 {paper_h - 14:.2f} Td "
                            f"({_pdf_literal(tag)}) Tj ET Q\n"
                        )
                        sheet.contents_add(
                            Stream(out_doc, text.encode("latin-1")))
                    sheet.contents_coalesce()
                    tiled += 1

        out_doc.save(dest)
    finally:
        src_doc.close()
        try:
            out_doc.close()
        except Exception:
            pass
    return tiled


NUP_GRID = {2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}


def nup_grid(count):
    """(cols, rows) for a pages-per-sheet count. 2→2×1, 4→2×2, 6→3×2, …"""
    count = int(count or 4)
    for n, grid in NUP_GRID.items():
        if count <= n:
            return grid
    return (4, 4)


def nup_order(n, cols, rows, order):
    """Slot permutation matching the concept JS `nupOrder`.

    Visual slots are row-major (top-left is 0). The returned list maps
    visual slot i → offset within the sheet's page block.
    """
    n = max(1, int(n))
    order = order or "h"
    if order in ("v", "vr"):
        cells = [r * cols + c for c in range(cols) for r in range(rows)]
    else:
        cells = list(range(n))
    cells = cells[:n]
    if order in ("hr", "vr"):
        cells = list(reversed(cells))
    return cells


def _nup_page_list(n_src, count, order):
    """Source indices (or None) in visual-slot order, padded to whole sheets."""
    cols, rows = nup_grid(count)
    count = cols * rows
    perm = nup_order(count, cols, rows, order)
    n_sheets = max(1, math.ceil(max(n_src, 1) / count))
    out = []
    for s in range(n_sheets):
        start = s * count
        for i in range(count):
            src = start + perm[i]
            out.append(src if src < n_src else None)
    return out


def _nup_params(paper_w, paper_h, cols, rows):
    """Layout tuple `_build_nup` expects, on the dialog's paper.

    Small gutter, not the sidebar tool's crop-mark / custom-sheet UI.
    """
    margin = 4.0 * MM_TO_PT
    gap = 3.0 * MM_TO_PT
    slot_w = (paper_w - 2 * margin - gap * (cols - 1)) / cols
    slot_h = (paper_h - 2 * margin - gap * (rows - 1)) / rows
    if slot_w <= 1.0 or slot_h <= 1.0:
        margin = gap = 1.0 * MM_TO_PT
        slot_w = (paper_w - 2 * margin - gap * (cols - 1)) / cols
        slot_h = (paper_h - 2 * margin - gap * (rows - 1)) / rows
    return (paper_w, paper_h, margin, margin, margin, margin,
            gap, gap, slot_w, slot_h, cols, rows)


def build_nup_pdf(src, pages, paper_pts, count, order, border, auto_rotate,
                  dest):
    """Several source pages per sheet. Returns the number of sheets.

    Wraps `_build_nup` with the dialog's paper size, the concept's slot
    order, an optional hairline around occupied slots, and auto-rotate so
    a landscape page fills a portrait slot (and the other way around).
    Comments & Forms must already have been applied.
    """
    import tempfile
    from pikepdf import Pdf
    from tools.ghostscript import unlink
    from tools.panels._shared import _visible_size
    from tools.panels.nup import _build_nup

    src_doc = Pdf.open(src)
    n_src = len(src_doc.pages)
    if pages is None:
        pages = list(range(n_src))
    pages = [p for p in pages if isinstance(p, int) and 0 <= p < n_src]
    if not pages:
        src_doc.close()
        raise RuntimeError("n-up: no pages to impose")

    # `_build_nup` indexes into `src`. Subset to `pages` so a range job
    # does not pull in pages the operator did not pick.
    work = []
    subset = src
    if pages != list(range(n_src)):
        fd, subset = tempfile.mkstemp(suffix="_nup_sub.pdf")
        os.close(fd)
        work.append(subset)
        out = Pdf.new()
        for p in pages:
            out.pages.append(src_doc.pages[p])
        out.save(subset)
        out.close()
        n_src = len(pages)
    src_doc.close()

    cols, rows = nup_grid(count)
    count = cols * rows
    if paper_pts:
        paper_w, paper_h = float(paper_pts[0]), float(paper_pts[1])
    else:
        with Pdf.open(subset) as d:
            w, h = _visible_size(d.pages[0])
        paper_w, paper_h = w, h
    params = _nup_params(paper_w, paper_h, cols, rows)
    slot_w, slot_h = params[8], params[9]

    rotated = subset
    if auto_rotate and slot_w > 1 and slot_h > 1:
        slot_ls = slot_w > slot_h
        doc = Pdf.open(subset)
        changed = False
        for page in doc.pages:
            vw, vh = _visible_size(page)
            if (vw > vh) != slot_ls:
                page.rotate(90, relative=True)
                changed = True
        if changed:
            fd, rotated = tempfile.mkstemp(suffix="_nup_rot.pdf")
            os.close(fd)
            work.append(rotated)
            doc.save(rotated)
        doc.close()

    src_pages = _nup_page_list(n_src, count, order)
    try:
        _build_nup(rotated, dest, src_pages, params, count,
                   lambda _msg: None, border=bool(border))
    finally:
        unlink(*work)

    with Pdf.open(dest) as out:
        return len(out.pages)

