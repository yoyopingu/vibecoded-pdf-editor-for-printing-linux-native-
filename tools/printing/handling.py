"""Print-dialog imposition: poster tiles, and later n-up and booklet.

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
