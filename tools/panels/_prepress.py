"""
What a PDF brings with it that decides whether it can go on a press.

Read-only: nothing here changes a file. "Druckvorstufenpruefung" turns these
into a report, and the PDF/X export uses the same answers to decide what to
say about what it did — one module so the two can never disagree about
whether a file was ready.

The four questions a print shop actually asks of an incoming file:

  * **Where does it trim?** The TrimBox is where the guillotine goes. No
    TrimBox means no declared trim and no bleed, which is fine for a flyer
    that ends at the paper edge and wrong for anything that bleeds.
  * **Are the fonts in it?** A font that is only referenced gets substituted
    at the RIP, and the substitute has different metrics — the text reflows on
    the plate, not on screen.
  * **Is there enough image resolution?** Below about 300 dpi an image is
    visibly soft in print, and the file cannot be fixed after the fact.
  * **What colour is it in?** RGB has to be separated before it can be
    printed; the question is only whether the shop does it or the customer.
"""
import logging

from tools.i18n import tr
from tools.panels._shared import MM_TO_PT, _inherited_rotate, _visible_box


# A page walk that renders nothing still costs time on a long document, and
# the report is wanted while someone waits at a counter.
MAX_PAGES = 200

# The PDF/X profiles this application can write.
#
# Lives here rather than in the export panel because three places need it —
# the export, the settings dialog that chooses it, and the report that says
# what it will do to a file. Putting it in the panel meant the settings dialog
# importing the panel while the panel imported the settings.
#
# The X-3 string is the PDF 1.3 revision, because that is the version
# Ghostscript's -dPDFX actually writes; claiming the 1.4-based :2003 over a
# 1.3 file would be a version string outrunning its own document.
PDFX_STANDARDS = {
    "x4": ("PDF/X-4", "PDF/X-4 — moderne Pressen, Transparenz bleibt erhalten"),
    "x3": ("PDF/X-3:2002", "PDF/X-3 — aeltere RIPs, Transparenz wird reduziert"),
}
DEFAULT_STANDARD = "x4"


def standard_of(key):
    """(key, version string, label) for a stored setting, defaulting to X-4."""
    key = key if key in PDFX_STANDARDS else DEFAULT_STANDARD
    return (key,) + PDFX_STANDARDS[key]


# Below this an image is visibly soft in print, and no later step can fix it.
# Deliberately not the export's downsampling setting: that one is a *ceiling*
# on detail worth keeping, this is the *floor* under which there is not enough.
# They are different questions and conflating them made the report flag every
# correctly prepared 300 dpi photograph.
MIN_PRESS_DPI = 300


def page_bleed(path):
    """(pages measured, bleed in mm per page, pages with no TrimBox).

    The bleed is the smallest gap between the TrimBox and the edge of the
    visible page — the amount of image that runs past the cut. Measured off
    the visible box rather than the raw MediaBox for the same reason the crop
    and N-Up tools do: a CropBox or a /Rotate otherwise takes the millimetres
    off the wrong edges.
    """
    import pikepdf
    bleeds, untrimmed, measured = [], [], 0
    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PAGES:
                break
            measured += 1
            if "/TrimBox" not in page.obj:
                untrimmed.append(i + 1)
                continue
            x0, y0, x1, y1 = _visible_box(page)
            try:
                t = [float(v) for v in page.obj["/TrimBox"]]
            except (TypeError, ValueError):
                logging.debug("unreadable TrimBox on page %d of %s", i + 1, path)
                untrimmed.append(i + 1)
                continue
            tx0, ty0 = min(t[0], t[2]), min(t[1], t[3])
            tx1, ty1 = max(t[0], t[2]), max(t[1], t[3])
            gap = min(tx0 - x0, ty0 - y0, x1 - tx1, y1 - ty1)
            bleeds.append(max(0.0, gap) / MM_TO_PT)
    return measured, bleeds, untrimmed


def unembedded_fonts(path):
    """Names of fonts the file only references.

    A base-14 font like Helvetica carries no /FontDescriptor at all, which is
    the commonest case: it looks fine in every viewer, because every viewer
    has a Helvetica, and then the RIP picks a different one.
    """
    import pikepdf
    missing = set()

    def check(font):
        descriptor = font.get("/FontDescriptor")
        if descriptor is None:
            for child in font.get("/DescendantFonts") or []:
                check(child)
                return
            missing.add(str(font.get("/BaseFont", "?")).lstrip("/"))
            return
        if not any(k in descriptor for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            missing.add(str(font.get("/BaseFont", "?")).lstrip("/"))

    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PAGES:
                break
            fonts = (page.obj.get("/Resources") or {}).get("/Font")
            for font in (fonts or {}).values():
                try:
                    check(font)
                except Exception:
                    logging.debug("could not read a font on page %d of %s",
                                  i + 1, path, exc_info=True)
    return sorted(missing)


def low_resolution_images(path, min_dpi=300):
    """[(page, effective dpi)] for images placed below `min_dpi`.

    Effective resolution is pixels divided by the size the image is *placed*
    at, not by anything in the image itself: the same 500-pixel logo is 250 dpi
    across two inches and 50 dpi across ten. That placement lives in the
    content stream's transformation matrix, so this walks the stream and tracks
    the CTM through q/Q/cm to find how big each Do actually draws.

    A one-percent tolerance, because an image prepared at exactly the target
    lands a hair under it: a 2480-pixel image on A4 measures 299.96 dpi, and a
    report that flags every correctly-prepared file is a report nobody reads.
    """
    import pikepdf
    limit = min_dpi * 0.99
    findings = []
    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PAGES:
                break
            xobjects = (page.obj.get("/Resources") or {}).get("/XObject")
            if not xobjects:
                continue
            rot = _inherited_rotate(page)
            try:
                worst, _best = _page_image_dpi(page, xobjects, rot)
            except Exception:
                logging.debug("could not measure image resolution on page %d "
                              "of %s", i + 1, path, exc_info=True)
                continue
            if worst is not None and worst < limit:
                findings.append((i + 1, int(round(worst))))
    return findings


def highest_image_dpi(path):
    """The finest image resolution anywhere in `path`, or None if it has none.

    The mirror of low_resolution_images, and asked for the opposite reason:
    that one answers "is there enough detail for the press", this one answers
    "is there more detail than the export was told to keep" — which is how an
    already-conformant file is told apart from one that still needs
    downsampling.
    """
    import pikepdf
    best = None
    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PAGES:
                break
            xobjects = (page.obj.get("/Resources") or {}).get("/XObject")
            if not xobjects:
                continue
            try:
                _worst, page_best = _page_image_dpi(
                    page, xobjects, _inherited_rotate(page))
            except Exception:
                logging.debug("could not measure image resolution on page %d "
                              "of %s", i + 1, path, exc_info=True)
                continue
            if page_best is not None:
                best = page_best if best is None else max(best, page_best)
    return best


def _page_image_dpi(page, xobjects, rot):
    """(lowest, highest) effective dpi of the images `page` draws, or (None, None).

    Effective resolution is pixels divided by the size the image is *placed*
    at, so this walks the content stream tracking the CTM through q/Q/cm."""
    import pikepdf
    # A PDF unit is 1/72 inch, so an image drawn across `w` units at `px`
    # pixels wide is px / (w / 72) dpi.
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    worst = best = None
    for instruction in pikepdf.parse_content_stream(page, "q Q cm Do"):
        op = str(instruction.operator)
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ctm
        elif op == "cm":
            a, b, c, d, e, f = (float(v) for v in instruction.operands)
            m = ctm
            ctm = (a * m[0] + b * m[2], a * m[1] + b * m[3],
                   c * m[0] + d * m[2], c * m[1] + d * m[3],
                   e * m[0] + f * m[2] + m[4], e * m[1] + f * m[3] + m[5])
        elif op == "Do":
            name = str(instruction.operands[0])
            obj = xobjects.get(name)
            if obj is None or obj.get("/Subtype") != pikepdf.Name("/Image"):
                continue
            # The drawn width and height are the lengths of the unit square's
            # edges under the CTM, which is what "placed size" means for an
            # image however it is rotated or sheared.
            drawn_w = (ctm[0] ** 2 + ctm[1] ** 2) ** 0.5
            drawn_h = (ctm[2] ** 2 + ctm[3] ** 2) ** 0.5
            if drawn_w < 1.0 or drawn_h < 1.0:
                continue
            if rot in (90, 270):
                drawn_w, drawn_h = drawn_h, drawn_w
            dpi = min(int(obj.Width) / (drawn_w / 72.0),
                      int(obj.Height) / (drawn_h / 72.0))
            worst = dpi if worst is None else min(worst, dpi)
            best = dpi if best is None else max(best, dpi)
    return worst, best


def transparent_pages(path):
    """Pages that use live transparency.

    Worth knowing before an export, because these are the pages that cost
    something. PDF/X-3 is PDF 1.3, which has no transparency at all, so each
    of them is *flattened*: rendered to pixels and embedded as an image. Two
    consequences an operator should hear about beforehand rather than discover
    afterwards —

      * vector artwork on those pages stops being vector. Everywhere else it
        survives the export untouched and stays sharp at any size; here it
        becomes a raster at the configured resolution.
      * it is slow. Flattening an A0 page means rendering roughly a
        thousand megapixels, and that is where the minutes go.

    Detected from the three ways a PDF says "transparency": a page group
    marked /Transparency, a soft mask, or a graphics state with a constant
    alpha below 1.
    """
    import pikepdf
    found = []
    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= MAX_PAGES:
                break
            try:
                group = page.obj.get("/Group")
                if group is not None and str(group.get("/S", "")) == "/Transparency":
                    found.append(i + 1)
                    continue
                states = (page.obj.get("/Resources") or {}).get("/ExtGState")
                for state in (states or {}).values():
                    soft = state.get("/SMask")
                    if soft is not None and str(soft) != "/None":
                        found.append(i + 1)
                        break
                    if (float(state.get("/ca", 1)) < 1.0
                            or float(state.get("/CA", 1)) < 1.0):
                        found.append(i + 1)
                        break
            except Exception:
                logging.debug("could not read transparency on page %d of %s",
                              i + 1, path, exc_info=True)
    return found


def layer_summary(path):
    """(layers visible by default, layers switched off by default).

    PDF/X has no optional content, so both lists get resolved away on export —
    the visible ones into the page, the others out of the file entirely.
    """
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject
    reader = PdfReader(path, strict=False)
    oc = reader.trailer["/Root"].get("/OCProperties")
    if not oc:
        return [], []
    oc = oc.get_object()
    default = oc.get("/D")
    off_ids = set()
    if default is not None:
        for ref in default.get_object().get("/OFF", ArrayObject()):
            off_ids.add(ref.idnum)
    on, off = [], []
    for ref in oc.get("/OCGs", ArrayObject()):
        name = str(ref.get_object().get("/Name", tr("(unbenannt)")))
        (off if ref.idnum in off_ids else on).append(name)
    return on, off
