"""The page a viewer shows, as opposed to the full sheet stored in the file.

Acrobat's crop, and any other edit that only sets a CropBox, leaves the old
drawing in the content stream and hides it. /Rotate turns the page for
display without moving that drawing. Tools that measure the raw MediaBox, or
that resize the page without clipping, therefore act on the hidden original.

Both pikepdf pages (``.obj``) and pypdf pages (the page is the dictionary)
are accepted. The rectangle is the CropBox clipped to the MediaBox, which is
what the PDF spec says a viewer displays, and what pdfium reports.
"""


def _page_node(page):
    """The page dictionary. pikepdf keeps it on ``.obj``; a pypdf page is it."""
    node = getattr(page, "obj", None)
    return page if node is None else node


def _inherited_rotate(page) -> int:
    """/Rotate, following the page tree. It may live on a /Pages node."""
    node = _page_node(page)
    for _ in range(32):
        try:
            if "/Rotate" in node:
                return int(node["/Rotate"]) % 360
            node = node["/Parent"]
        except Exception:
            break
    return 0


def _box_rect(obj):
    v = [float(x) for x in obj]
    return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))


def _visible_box(page):
    """The rectangle a viewer shows: CropBox clipped to the MediaBox.

    The spec requires that intersection. A missing CropBox, or one that does
    not overlap the MediaBox, means the MediaBox itself — a stale box left
    larger than the page must not become the layout.
    """
    x0, y0, x1, y1 = _box_rect(page.mediabox)
    try:
        cx0, cy0, cx1, cy1 = _box_rect(page.cropbox)
    except Exception:
        return x0, y0, x1, y1
    ix0, iy0 = max(x0, cx0), max(y0, cy0)
    ix1, iy1 = min(x1, cx1), min(y1, cy1)
    if ix1 - ix0 > 1.0 and iy1 - iy0 > 1.0:
        return ix0, iy0, ix1, iy1
    return x0, y0, x1, y1


def displayed_size(page, extra_rot=0):
    """(width, height) as displayed, including a page-manager rotation on top
    of the file's own /Rotate. ``extra_rot`` is degrees, clockwise in the PDF
    sense, the same numbers /Rotate uses."""
    x0, y0, x1, y1 = _visible_box(page)
    w, h = x1 - x0, y1 - y0
    rot = (_inherited_rotate(page) + int(extra_rot or 0)) % 360
    if rot in (90, 270):
        w, h = h, w
    return w, h


def _visible_size(page):
    """(width, height) of the page as it is displayed.

    The visible box with /Rotate applied. This is what pdfium reports, and
    therefore what a preview draws, so layout maths must use it too.
    """
    return displayed_size(page, 0)


def _mat_mul(m, n):
    """Compose two PDF matrices (a b c d e f): apply `m` first, then `n`."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _mat_inv(m):
    """Inverse of a PDF matrix. Raises ValueError if it cannot be inverted."""
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise ValueError("singular page matrix")
    return (d / det, -b / det, -c / det, a / det,
            (c * f - d * e) / det, (b * e - a * f) / det)


def _display_matrix(box, rot):
    """Matrix mapping a page's visible box into display space.

    Origin at (0, 0) and /Rotate applied: the coordinate system a viewer
    shows. A tool can then do its geometry there instead of in raw MediaBox
    coordinates, and map back with the inverse when it has to draw into the
    file.
    """
    x0, y0, x1, y1 = box
    rot = rot % 360
    if rot == 90:
        return (0.0, -1.0, 1.0, 0.0, -y0, x1)
    if rot == 180:
        return (-1.0, 0.0, 0.0, -1.0, x1, y1)
    if rot == 270:
        return (0.0, 1.0, -1.0, 0.0, y1, -x0)
    return (1.0, 0.0, 0.0, 1.0, -x0, -y0)


def add_turned_page(writer, page, rotation):
    """Add `page` to a pypdf writer, turned by `rotation` degrees clockwise.

    The turn goes onto the writer's copy, never onto the reader's page.
    pypdf's rotate() *adds* to the /Rotate it finds, and a reader hands out the
    same page object for every copy of a source page — so turning the reader's
    page for the second copy of a page turned it once more than the first, and
    the document's second page came out a further 90 degrees round than its
    first. add_page clones the page, so every written page keeps its own
    /Rotate.

    `rotation` is the page manager's turn. The file's own /Rotate is kept and
    turned with it, which is what the viewer shows (see displayed_size).
    """
    added = writer.add_page(page)
    if rotation:
        added.rotate(rotation)
    return added


def write_view_pages(writer, sources, rotations=None, check=None):
    """Write the pages a page manager shows into `writer`.

    `sources` is a list of (path, page_index) in display order, `rotations` a
    parallel list of the page manager's turns in degrees (0 for none). The
    written pages are returned, in the same order.

    Why not add_page: it drops the catalog's /AcroForm, so a document with a
    form lost every field as soon as the page manager had been edited — the
    flattened view and Ctrl+S both wrote a file the Formulare tool could no
    longer read. append keeps the form.

    Why one append per file and not one per page: each append call copies the
    reader's resources again unless the translation table survives it, so the
    pages of one file go in with a single call. The output stays the size it
    was, and every written page keeps its own /Rotate — a page the page
    manager copied is a second entry for the same source page, and each copy
    has to be turned on its own (see add_turned_page).
    """
    from pypdf import PdfReader
    if rotations is None:
        rotations = [0] * len(sources)
    if len(rotations) != len(sources):
        raise ValueError("rotations must have one entry per source")
    readers = {}
    written = []
    runs = []                      # [(path, [(index, rotation), ...]), ...]
    for (path, index), rot in zip(sources, rotations):
        if runs and runs[-1][0] == path:
            runs[-1][1].append((index, rot))
        else:
            runs.append((path, [(index, rot)]))
    try:
        for path, items in runs:
            if check is not None:
                check()
            reader = readers.get(path)
            if reader is None:
                reader = PdfReader(path, strict=False)
                readers[path] = reader
                # Once per reader: a second reset would throw away the
                # mapping of the pages already copied and copy the shared
                # resources a second time.
                writer.reset_translation(reader)
            # A stale index (a page that is no longer in the file) is skipped
            # rather than raised: the page manager can hold one after a failed
            # insert, and one missing page must not lose the whole document.
            items = [(i, r) for i, r in items if 0 <= i < len(reader.pages)]
            if not items:
                continue
            before = len(writer.pages)
            writer.append(reader, pages=[i for i, _ in items],
                          import_outline=False)
            for offset, (_, rot) in enumerate(items):
                page = writer.pages[before + offset]
                if rot:
                    page.rotate(rot)
                written.append(page)
    finally:
        for reader in readers.values():
            try:
                reader.close()
            except Exception:
                pass       # a reader that cannot be closed is not a failure
    return written


def _crop_hides_content(page) -> bool:
    """True when the visible page is meaningfully smaller than the MediaBox.

    A rotation alone does not count: viewers already honour /Rotate, and
    rewriting those pages would only change the file, not the picture.
    """
    mx0, my0, mx1, my1 = _box_rect(page.mediabox)
    x0, y0, x1, y1 = _visible_box(page)
    return (mx1 - mx0) - (x1 - x0) > 1.0 or (my1 - my0) - (y1 - y0) > 1.0


def apply_visible_transform(page, pdf, matrix, clip, width, height):
    """Draw only `clip`, through `matrix`, on a new upright page.

    The clip rectangle is in the content's own user space and inside the same
    q/Q as the matrix, so it travels with the drawing. Content Acrobat hid
    outside the CropBox is dropped instead of sliding back onto the new page.
    /Rotate is cleared because the matrix already applied it.
    """
    import pikepdf
    contents = page.get("/Contents")
    if contents is not None:
        if isinstance(contents, pikepdf.Array):
            old = b" ".join(bytes(s.read_bytes()) for s in contents)
        else:
            old = bytes(contents.read_bytes())
        x0, y0, x1, y1 = clip
        a, b, c, d, e, f = matrix
        hdr = (f"q\n{a:.6f} {b:.6f} {c:.6f} {d:.6f} {e:.4f} {f:.4f} cm\n"
               f"{x0:.4f} {y0:.4f} {x1 - x0:.4f} {y1 - y0:.4f} re W n\n").encode()
        page["/Contents"] = pikepdf.Stream(pdf, hdr + old + b"\nQ")
    box = pikepdf.Array([pikepdf.Real(0), pikepdf.Real(0),
                         pikepdf.Real(float(width)), pikepdf.Real(float(height))])
    page.mediabox = box
    page.obj["/CropBox"] = box
    page.obj["/Rotate"] = 0
    for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
        if key in page.obj:
            del page.obj[key]


def materialize_visible(src, dest) -> bool:
    """Rewrite `src` to `dest` so each cropped page is only its visible page.

    Returns False, and writes nothing, when no page hides anything — the
    caller keeps using `src`. Rotation alone is left as /Rotate, which every
    viewer already applies.
    """
    import pikepdf
    with pikepdf.open(src) as pdf:
        changed = False
        for page in pdf.pages:
            if not _crop_hides_content(page):
                continue
            changed = True
            box = _visible_box(page)
            apply_visible_transform(
                page, pdf, _display_matrix(box, _inherited_rotate(page)),
                box, *_visible_size(page))
        if not changed:
            return False
        pdf.save(dest)
    return True
