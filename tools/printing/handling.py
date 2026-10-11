"""Print-dialog imposition: poster tiles, n-up sheets, booklet spreads.

The dialog already subsets the job and runs Comments & Forms. This module
turns that prepared file into sheet-sized pages so spool.py can send them
as a normal Size job at 100 % — CUPS must not fit or shrink a tile that is
already the sheet.

No Qt. The spooler prints this file. The dialog composites source rasters
using the same sheet geometry and ordering — rasterising the imposed PDF
inside the printing-module test process trips a heap fault.
"""
from dataclasses import dataclass
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
    # `cells` is traversal order, whereas consumers need visual slot -> page.
    # Those are different permutations for a non-square vertical grid (6-up).
    return [cells.index(i) for i in range(n)]


def sheet_size(paper_pts, orient_idx, sizes, mode):
    """Sheet geometry shared by preview and imposition, in displayed points."""
    first = sizes[0]
    if not paper_pts:
        if mode == "booklet":
            w, h = max(sizes, key=lambda wh: wh[0] * wh[1])
            return w * 2, h
        return first
    w, h = paper_pts
    landscape = (mode == "booklet" or orient_idx == 2 or
                 (orient_idx == 0 and first[0] > first[1]))
    return (max(w, h), min(w, h)) if landscape else (min(w, h), max(w, h))


def rotation_to_fit(size, slot):
    """Turn only when it increases the fitted page; square pages stay put."""
    w, h = size
    sw, sh = slot
    return min(sw / h, sh / w) > min(sw / w, sh / h) + 1e-9


@dataclass(frozen=True)
class PreviewSlot:
    page: int
    rect: tuple             # x, y, width, height from the sheet's top left
    rotate: bool = False
    border: tuple = None    # the enclosing n-up cell, if requested


@dataclass(frozen=True)
class PreviewSheet:
    size: tuple
    slots: tuple
    tile: str = ""


def preview_sheets(sizes, paper_pts, orient_idx, mode, opts):
    """Compose source rasters with the same geometry/order as vector output.

    No imposed PDF is rasterised: only the source pages on the visible sheet
    are needed. Page indexes refer to the already ordered print selection.
    """
    if not sizes:
        return []
    sw, sh = sheet_size(paper_pts, orient_idx, sizes, mode)
    sheets = []
    if mode == "poster":
        for i, (w, h) in enumerate(sizes):
            pw, ph = (sw, sh) if paper_pts else (w, h)
            cols, rows, scale = poster_grid(w, h, pw, ph, opts.get("tile_pct", 200))
            overlap = max(0, opts.get("overlap_mm", 0)) * MM_TO_PT
            dx, dy = max(pw - overlap, pw * .1), max(ph - overlap, ph * .1)
            for r in range(rows):
                for c in range(cols):
                    slot = PreviewSlot(i, (-c * dx, -r * dy, w * scale, h * scale))
                    sheets.append(PreviewSheet((pw, ph), (slot,), _tile_id(r, c)))
        return sheets
    if mode == "nup":
        cols, rows = nup_grid(opts.get("count", 4))
        count = cols * rows
        params = _nup_params(sw, sh, cols, rows)
        margin, gap, cw, ch = params[2], params[6], params[8], params[9]
        page_list = _nup_page_list(len(sizes), count, opts.get("order", "h"))
        faces = [page_list[i:i + count] for i in range(0, len(page_list), count)]
        cells = [(margin + c * (cw + gap), margin + r * (ch + gap), cw, ch)
                 for r in range(rows) for c in range(cols)]
        rotate = opts.get("nup_rotate", True)
    else:
        faces = booklet_faces(len(sizes), opts.get("bind", "left"),
                              opts.get("side", "both"), opts.get("sheet_from", 1),
                              opts.get("sheet_to", 0))
        cells = [(0, 0, sw / 2, sh), (sw / 2, 0, sw / 2, sh)]
        rotate = opts.get("booklet_rotate", True)
    for face in faces:
        slots = []
        for i, cell in zip(face, cells):
            if i is None:
                continue
            x, y, cw, ch = cell
            w, h = sizes[i]
            turn = bool(rotate and rotation_to_fit((w, h), (cw, ch)))
            if turn:
                w, h = h, w
            scale = min(cw / w, ch / h)
            rect = (x + (cw - w * scale) / 2, y + (ch - h * scale) / 2,
                    w * scale, h * scale)
            slots.append(PreviewSlot(i, rect, turn,
                                    cell if mode == "nup" and opts.get("border") else None))
        sheets.append(PreviewSheet((sw, sh), tuple(slots)))
    return sheets


def _rotated_for_slots(src, slot, work):
    """Keep rotation separate from fitting; both booklet and n-up always fit."""
    import tempfile
    from pikepdf import Pdf
    from tools.pagebox import displayed_size
    with Pdf.open(src) as doc:
        changed = False
        for page in doc.pages:
            if rotation_to_fit(displayed_size(page), slot):
                page.rotate(90, relative=True)
                changed = True
        if not changed:
            return src
        fd, rotated = tempfile.mkstemp(suffix="_print_rot.pdf")
        os.close(fd)
        work.append(rotated)
        doc.save(rotated)
        return rotated


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
        rotated = _rotated_for_slots(subset, (slot_w, slot_h), work)

    src_pages = _nup_page_list(n_src, count, order)
    try:
        _build_nup(rotated, dest, src_pages, params, count,
                   lambda _msg: None, border=bool(border))
    finally:
        unlink(*work)

    with Pdf.open(dest) as out:
        return len(out.pages)


def booklet_faces(n_pages, bind="left", side="both", sheet_from=1, sheet_to=None):
    """Imposed faces after bind, Acrobat sheet range, and front/back subset.

    One face is [left, right] as 0-based source indexes (None = blank).
    Physical sheets are pairs of faces; `sheet_from`/`sheet_to` are 1-based
    on that list. The order itself is `_booklet_sides` — the Broschüre
    tool's saddle-stitch, not a second copy of it.
    """
    from tools.panels.impose import _booklet_sides
    sides = _booklet_sides(n_pages)
    if (bind or "left") == "right":
        sides = [list(reversed(pair)) for pair in sides]
    n_phys = max(1, (len(sides) + 1) // 2)
    frm = max(1, int(sheet_from or 1))
    to = int(n_phys if sheet_to in (None, 0) else sheet_to)
    frm = min(frm, n_phys)
    to = max(frm, min(to, n_phys))
    picked = []
    for i in range(frm - 1, to):
        front = sides[2 * i] if 2 * i < len(sides) else None
        back = sides[2 * i + 1] if 2 * i + 1 < len(sides) else None
        if side in ("both", "front", None) and front is not None:
            picked.append(front)
        if side in ("both", "back") and back is not None:
            picked.append(back)
    return picked or sides[:1]


def build_booklet_pdf(src, pages, paper_pts, bind, side, sheet_from, sheet_to,
                      auto_rotate, dest):
    """Saddle-stitch 2-up on a landscape sheet. Returns the number of faces.

    The order and the Form-XObject placement are the Broschüre tool's:
    `_booklet_sides` and `_build_impose`. This only sizes the sheet from
    the dialog's paper (landscape of that size, two pages per face),
    applies bind / front-back / sheet range. Fitting is always required for
    booklet output; Automatisch drehen only controls an extra quarter turn.
    """
    import tempfile
    from pikepdf import Pdf
    from tools.ghostscript import unlink
    from tools.panels.impose import _build_impose, _largest_page

    src_doc = Pdf.open(src)
    n_src = len(src_doc.pages)
    if pages is None:
        pages = list(range(n_src))
    pages = [p for p in pages if isinstance(p, int) and 0 <= p < n_src]
    if not pages:
        src_doc.close()
        raise RuntimeError("booklet: no pages to impose")

    work = []
    subset = src
    if pages != list(range(n_src)):
        fd, subset = tempfile.mkstemp(suffix="_bk_sub.pdf")
        os.close(fd)
        work.append(subset)
        out = Pdf.new()
        for p in pages:
            out.pages.append(src_doc.pages[p])
        out.save(subset)
        out.close()
    src_doc.close()
    with Pdf.open(subset) as d:
        n_src = len(d.pages)
        page_w, page_h = _largest_page(d)

    if paper_pts:
        sheet_w, sheet_h = float(paper_pts[0]), float(paper_pts[1])
        if sheet_w < sheet_h:
            sheet_w, sheet_h = sheet_h, sheet_w
    else:
        sheet_w, sheet_h = page_w * 2.0, page_h

    faces = booklet_faces(n_src, bind=bind, side=side,
                          sheet_from=sheet_from, sheet_to=sheet_to)
    half_w = sheet_w / 2.0
    halves = [(0.0, 0.0, half_w, sheet_h), (half_w, 0.0, sheet_w, sheet_h)]
    sheets = [list(zip(face, halves)) for face in faces]
    try:
        if auto_rotate:
            subset = _rotated_for_slots(subset, (half_w, sheet_h), work)
        _build_impose(subset, dest, sheets, sheet_w, sheet_h, True,
                      lambda _msg: None,
                      lambda placed, nsheets: "")
    finally:
        unlink(*work)

    with Pdf.open(dest) as out:
        return len(out.pages)


