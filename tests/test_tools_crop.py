"""
Tools Crop.
"""
import os
from pypdf import PdfReader
from PIL import Image
import pypdfium2 as pdfium
from tools.panels.crop_resize import CropResizePanel
from tools.panels._cropmarks import _crop_mark_segments
from tests.support import (FX, MM, _TMP, _brightest, _ink_margins, _nup,
                           _open, _sync_async)


def test_crop_format_modes():
    _open(FX["normal"])
    def run(action_idx, fmt_setter):
        p = CropResizePanel(); p.apply_all.setChecked(True)
        out = os.path.join(_TMP, "crop.pdf")
        p.save_pdf = lambda *a, **k: out
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path)
        p.fmt_action.setCurrentIndex(action_idx); fmt_setter(p); p._run_action()
        mb = PdfReader(out).pages[0].mediabox
        return round(float(mb.width)/MM), round(float(mb.height)/MM), out
    from tools.i18n import tr
    custom = lambda p: (p.fmt.set_format(tr("Benutzerdefiniert (mm)")),
                        p.fmt.set_custom_size(150, 200))
    w, h, out = run(1, custom)                      # marks-only: not resized
    assert (w, h) == (210, 297) and _brightest(out, 0) >= 250
    w, h, _ = run(0, custom)                         # scale: resized to custom
    assert abs(w-150) <= 1 and abs(h-200) <= 1
    w, h, _ = run(0, lambda p: p.fmt.set_format("A5  (148x210mm)"))
    assert abs(w-148) <= 1 and abs(h-210) <= 1


def test_crop_mark_geometry():
    segs = _crop_mark_segments([(100, 100, 300, 500)])    # mx=200, corners 100/300
    assert len(segs) == 16, f"expected 16 (8 corner + 8 centre), got {len(segs)}"
    top = [s for s in segs if abs(s[1]-s[3]) < 0.01 and s[1] > 500]   # horizontal centre marks
    centres = sorted(set(round((s[0]+s[2])/2, 1) for s in top))
    assert len(centres) == 2
    d_corner = centres[0] - 100; d_each = centres[1] - centres[0]
    assert d_corner < d_each, "centre marks must be closer to corners than to each other"


def test_marks_only_without_a_format_refuses_instead_of_silently_copying():
    """"Nur Schnittmarken setzen" needs a target size to centre the marks on,
    and picking the mode is not enough by itself — the Format dropdown still
    has to be moved off "— Kein —" separately. It used to fall through to the
    ordinary crop branch at zero margins instead: no marks, no error, and
    "1 Seite(n) bearbeitet." in the log, which reads as success on a file that
    was really just copied unchanged. Selecting the mode is the one thing
    anyone reaching for crop marks does first, so this was the whole feature
    looking broken."""
    from tools.i18n import tr
    _open(FX["normal"])
    p = CropResizePanel()
    p.fmt_action.setCurrentIndex(1)          # "Nur Schnittmarken setzen"
    assert p.fmt.current_text() == tr("— Kein —"), "fixture assumes no format picked yet"

    pm, info = p._render_preview(500, 650, 1.0)
    assert pm is None, "the preview drew a page with no target size to mark"
    assert "Format" in info, f"the preview did not say a format was needed: {info!r}"

    out = os.path.join(_TMP, "marks_no_format.pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a result was opened for an operation that refused"))
    try:
        p._run_action()
        raise AssertionError("no format, no size — this should have refused")
    except ValueError as e:
        assert "Format" in str(e)
    assert not os.path.exists(out), "a file was written despite refusing"

    # The moment a real format is picked, the same mode works.
    p.fmt.set_format("A5  (148x210mm)")
    pm, info = p._render_preview(500, 650, 1.0)
    assert pm is not None and "✂" in info, f"marks mode did not recover: {info!r}"


def _qpix_to_pil(pm):
    from PyQt6.QtGui import QImage
    img = pm.toImage().convertToFormat(QImage.Format.Format_RGB32)
    b = img.bits(); b.setsize(img.sizeInBytes())
    return Image.frombytes("RGBA", (img.width(), img.height()), bytes(b), "raw", "BGRA").convert("RGB")


def test_crop_format_applies_per_page():
    """A Format picked from the dropdown means "make every page this size". With
    mixed page sizes the run used to apply the *previewed* page's millimetres to
    all of them, so the other pages came out a different size and off-centre."""
    _open(FX["mixed"])
    p = CropResizePanel(); p.apply_all.setChecked(True)
    p.scale_check.setChecked(True); p.keep_ratio.setChecked(True)
    p.fmt.set_format("A5  (148x210mm)")
    out = os.path.join(_TMP, "crop_fmt.pdf")
    p.save_pdf = lambda *a, **k: out; p.open_result = lambda path, t="": None
    p._run_action()
    d = pdfium.PdfDocument(out)
    try:
        for i in range(3):
            w, h = d[i].get_width()/MM, d[i].get_height()/MM
            assert abs(w-148) < 1 and abs(h-210) < 1, f"page {i+1} is {w:.0f}x{h:.0f} mm, not A5"
    finally:
        d.close()
    for i in range(3):
        L, R, B, Tp = _ink_margins(out, i)
        assert abs(L-R) < 0.4 and abs(B-Tp) < 0.4, f"page {i+1} not centred"
    # a manual edit after picking the format must still win
    p2 = CropResizePanel(); p2.apply_all.setChecked(True); p2.scale_check.setChecked(False)
    p2.fmt.set_format("A5  (148x210mm)"); p2.ct.setValue(30.0)
    out2 = os.path.join(_TMP, "crop_fmt2.pdf")
    p2.save_pdf = lambda *a, **k: out2; p2.open_result = lambda path, t="": None
    p2._run_action()
    d = pdfium.PdfDocument(out2); h = d[0].get_height()/MM; d.close()
    assert abs(h - (297-30-43.5)) < 1, f"manual margin ignored (height {h:.0f} mm)"


def test_crop_rejects_over_trim():
    """Trimming away more than the page must be reported, not clamped to a 1 pt
    page with the content dumped somewhere off it."""
    _open(FX["framed"])
    p = CropResizePanel(); p.apply_all.setChecked(True)
    p.cl2.setValue(150.0); p.cr.setValue(150.0)          # 300 mm off a 210 mm page
    p.save_pdf = lambda *a, **k: os.path.join(_TMP, "never2.pdf")
    p.open_result = lambda path, t="": None
    try:
        p._run_action()
        raise AssertionError("no error for trimming away the whole page")
    except ValueError:
        pass
    pm, info = p._render_preview(400, 400, 1.0)
    assert pm is None and "Seite" in info, f"preview should explain the problem, got {info!r}"


def test_crop_preview_shows_added_whitespace():
    """Negative margins ("- erweitern") add white space; the preview has to paint
    the added strip as paper. It used to stay the dark canvas colour, so the
    white space being added was invisible."""
    _open(FX["framed"])
    p = CropResizePanel(); p.apply_all.setChecked(True); p.scale_check.setChecked(False)
    for w in (p.ct, p.cb2, p.cl2, p.cr): w.setValue(-15.0)
    pm, _ = p._render_preview(500, 650, 1.0)
    im = _qpix_to_pil(pm); px = im.load()
    # Match the outline against the *live* accent rather than a literal colour —
    # this used to hardcode the old red and broke the moment the accent changed.
    from tools.theme import _TV
    ar, ag, ab = (int(_TV["acc"].lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    hits = [(x, y) for y in range(im.height) for x in range(im.width)
            if abs(px[x, y][0]-ar) < 40 and abs(px[x, y][1]-ag) < 40
            and abs(px[x, y][2]-ab) < 40]
    assert hits, "no page outline in the preview"
    x0 = min(q[0] for q in hits); y0 = min(q[1] for q in hits); y1 = max(q[1] for q in hits)
    probe = px[x0+4, (y0+y1)//2]          # inside the added left strip
    assert min(probe) > 200, f"added margin is {probe}, expected white paper"


def test_crop_leaves_consistent_boxes():
    """The Crop tool must not leave the old CropBox/Rotate behind: that is what
    handed N-Up a page whose declared box no longer matched its content."""
    import pikepdf
    _open(FX["framed_cropbox"])
    p = CropResizePanel(); p.apply_all.setChecked(True)
    p.scale_check.setChecked(False)
    for w, v in ((p.ct, 5.0), (p.cb2, 5.0), (p.cl2, 5.0), (p.cr, 5.0)): w.setValue(v)
    out = os.path.join(_TMP, "crop_boxes.pdf")
    p.save_pdf = lambda *a, **k: out; p.open_result = lambda path, t="": None
    p._run_action()
    with pikepdf.open(out) as pdf:
        pg = pdf.pages[0]
        mb = [float(v) for v in pg.mediabox]
        cb = [float(v) for v in pg.cropbox]
        assert mb == cb, f"CropBox {cb} left inconsistent with MediaBox {mb}"
        assert "/TrimBox" not in pg.obj and int(pg.obj.get("/Rotate", 0)) == 0
        # visible page = the old CropBox (190x277 mm) minus 2x5 mm
        assert abs(mb[2]-mb[0] - (190-10)*MM) < 1 and abs(mb[3]-mb[1] - (277-10)*MM) < 1, mb
    # and the cropped file now goes through N-Up centred
    out2, _ = _nup(out, margins=20.0, name="crop_then_nup.pdf")
    L, R, B, Tp = _ink_margins(out2)
    assert abs(L-R) < 0.4 and abs(B-Tp) < 0.4, f"crop→N-Up not centred: {L:.2f} {R:.2f} {B:.2f} {Tp:.2f}"


def test_crop_trims_the_edge_you_see():
    """On a /Rotate 90 page, "5 mm from the left" must come off the left edge as
    displayed — the maths used to run in unrotated MediaBox space, so it took the
    millimetres off the wrong side."""
    _open(FX["framed_rot90"])
    d = pdfium.PdfDocument(FX["framed_rot90"]); w0, h0 = d[0].get_width(), d[0].get_height(); d.close()
    p = CropResizePanel(); p.apply_all.setChecked(True); p.scale_check.setChecked(False)
    p.cl2.setValue(30.0)                                  # 30 mm off the left
    out = os.path.join(_TMP, "crop_rot.pdf")
    p.save_pdf = lambda *a, **k: out; p.open_result = lambda path, t="": None
    p._run_action()
    d = pdfium.PdfDocument(out); w1, h1 = d[0].get_width(), d[0].get_height(); d.close()
    assert abs(w1 - (w0 - 30*MM)) < 1 and abs(h1 - h0) < 1, \
        f"expected {w0-30*MM:.0f}x{h0:.0f}, got {w1:.0f}x{h1:.0f}"
    # the frame's left edge is gone, the other three are still there
    L, R, B, Tp = _ink_margins(out)
    assert R < 0.5 and B < 0.5 and Tp < 0.5, f"wrong edges trimmed: {L:.2f} {R:.2f} {B:.2f} {Tp:.2f}"


def test_crop_format_can_be_landscape():
    """Querformat turns the chosen sheet, in Crop/Scale as in N-Up. It lives on
    the shared format widget, so neither tool can have it without the other."""
    def sheet(landscape):
        _open(FX["framed"])
        p = CropResizePanel(); p.log.log = lambda *a, **k: None
        p.fmt.set_format("A5  (148x210mm)")
        p.fmt.landscape.setChecked(landscape)
        out = os.path.join(_TMP, f"crop_landscape_{landscape}.pdf")
        p.save_pdf = lambda *a, **k: out
        cap = {}
        p.open_result = lambda path, t="": cap.update(p=path)
        _sync_async(p); p._run_action()
        d = pdfium.PdfDocument(cap.get("p", out))
        w, h = d[0].get_width(), d[0].get_height()
        d.close()
        return w, h

    pw, ph = sheet(False)
    lw, lh = sheet(True)
    assert ph > pw, f"A5 upright should be portrait, got {pw:.0f}x{ph:.0f}"
    assert lw > lh, f"Querformat should give landscape, got {lw:.0f}x{lh:.0f}"
    assert abs(lw - ph) < 1 and abs(lh - pw) < 1, \
        "Querformat did something other than swap the two sides"


def test_querformat_is_off_for_entries_that_are_not_a_sheet():
    """'— Kein —' in Crop and 'Wie Quellseite × Raster' in N-Up are not paper,
    so there is nothing to turn and the box says so."""
    from tools.panels.nup import NUpPanel
    from tools.i18n import tr
    c = CropResizePanel()
    c.fmt.set_format(tr("— Kein —"))
    assert not c.fmt.landscape.isEnabled()
    c.fmt.set_format("A4  (210x297mm)")
    assert c.fmt.landscape.isEnabled()

    n = NUpPanel()
    n.out_fmt.set_format(n.AUTO_FORMAT)
    assert not n.out_fmt.landscape.isEnabled()
    n.out_fmt.set_format("A4  (210x297mm)")
    assert n.out_fmt.landscape.isEnabled()


def test_a_scale_percentage_actually_scales():
    """There was no way to say "make this 80%".

    The tool could only scale as a side effect of changing the page size — pick
    a Format, or type millimetres into the four margin boxes — so the one
    control with "skalieren" in its name did nothing whatever on its own: the
    page came out the size it went in, the preview did not move, and the run
    wrote a copy of the original into a new tab.
    """
    from pypdf import PdfReader
    _open(FX["normal"])
    src_w = float(PdfReader(FX["normal"]).pages[0].mediabox.width)
    src_h = float(PdfReader(FX["normal"]).pages[0].mediabox.height)

    for pct in (50.0, 80.0, 200.0):
        p = CropResizePanel(); p.apply_all.setChecked(True)
        p.log.log = lambda *a, **k: None
        p.scale_pct.setValue(pct)
        out = os.path.join(_TMP, f"scale_{int(pct)}.pdf")
        p.save_pdf = lambda *a, **k: out
        p.open_result = lambda *a, **k: None
        p._run_action()
        page = PdfReader(out).pages[0]
        got_w = float(page.mediabox.width); got_h = float(page.mediabox.height)
        assert abs(got_w - src_w * pct / 100) < 0.5, f"{pct}%: width {got_w}"
        assert abs(got_h - src_h * pct / 100) < 0.5, f"{pct}%: height {got_h}"
        # The content has to come with it, and stay text rather than be
        # rasterised into place.
        assert "PAGE 1" in (page.extract_text() or ""), f"{pct}%: text lost"
        assert _ink_box(out, 0) is not None, f"{pct}%: the page came out blank"
    return "50%, 80% and 200% all land on the page size they name"


def test_scale_pct_tracks_format_and_can_override_it():
    """The Skalierung box used to be a fully independent multiplier: it kept
    showing whatever number it was last set to no matter what Format or
    margins did to the page, so a page already cropped to A5 still showed
    "100 %" — a live lie about the size the tool was actually producing.

    It should instead be a readout of the real combined size (auto-updating
    whenever Format/margins change) that is still directly editable: typing a
    new value overrides the Format/margins with a centred resize to that
    percentage of the original page, exactly the way picking a Format
    overrides hand-typed margins.
    """
    from tools.i18n import tr
    _open(FX["normal"])            # A4: 210x297mm
    p = CropResizePanel()

    p.fmt.set_format("A5  (148x210mm)")
    pm, _info = p._render_preview(500, 650, 1.0)
    assert pm is not None
    expected = 100.0 * ((148 * 210) / (210 * 297)) ** 0.5
    assert abs(p.scale_pct.value() - expected) < 0.5, \
        f"Skalierung did not track the A5 format: {p.scale_pct.value()}"

    # Typing a new percentage overrides the format with a centred resize of
    # the *original* page, not a further shrink of the A5 crop.
    p.scale_pct.setValue(50.0)
    assert p.fmt.current_text() == tr("— Kein —"), \
        "a hand-typed percentage should drop the stale Format selection"
    p.apply_all.setChecked(True)
    out = os.path.join(_TMP, "scale_pct_override.pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: None
    p._run_action()
    mb = PdfReader(out).pages[0].mediabox
    w_mm = float(mb.width) / MM; h_mm = float(mb.height) / MM
    assert abs(w_mm - 105) <= 1, w_mm      # 50% of 210mm
    assert abs(h_mm - 148.5) <= 1, h_mm    # 50% of 297mm


def _ink_box(path, i):
    import pypdfium2 as pdfium
    from PIL import ImageOps
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=1, fill_color=(255, 255, 255, 255)).to_pil().convert("L")
    d.close()
    return ImageOps.invert(im).getbbox()


def test_scaling_moves_the_ink_by_the_same_factor():
    """The page size alone proves nothing — a page half the size with the
    content left where it was is a crop, not a scale."""
    _open(FX["normal"])
    boxes = {}
    for pct in (100.0, 50.0):
        p = CropResizePanel(); p.apply_all.setChecked(True)
        p.log.log = lambda *a, **k: None
        p.scale_pct.setValue(pct)
        out = os.path.join(_TMP, f"scale_ink_{int(pct)}.pdf")
        p.save_pdf = lambda *a, **k: out
        p.open_result = lambda *a, **k: None
        p._run_action()
        boxes[pct] = _ink_box(out, 0)
    full, half = boxes[100.0], boxes[50.0]
    assert full and half, boxes
    wide_full = full[2] - full[0]; wide_half = half[2] - half[0]
    assert abs(wide_half / wide_full - 0.5) < 0.06, \
        f"ink is {wide_half}px wide at 50%, {wide_full}px at 100%"
    return f"ink {wide_full}px -> {wide_half}px"


def test_the_preview_fits_the_pane_it_is_drawn_for():
    """The preview drew the ghost of the original page beside the new one but
    scaled only the new one to fit, so anything cropped away produced a pixmap
    larger than the pane.

    That was not merely clipped. A QLabel showing a pixmap reports it as the
    label's minimum size, so the oversized render dragged the label — and the
    pane — out with it, and the pane's resize brought the render straight back
    round with more room to overflow into. One margin typed into Crop took an
    889x761 label and a 526x745 render to 2638x3729 and 3958x5597 within thirty
    turns of the event loop, by which point nothing of the preview was on
    screen. Which is what "it doesn't show a preview" looked like.
    """
    _open(FX["normal"])
    p = CropResizePanel()
    avail_w, avail_h = 500, 650
    cases = {
        "unchanged":  lambda: None,
        "cropped":    lambda: (p.ct.setValue(40.0), p.cl2.setValue(30.0)),
        "extended":   lambda: [w.setValue(-20.0) for w in (p.ct, p.cb2, p.cl2, p.cr)],
        "scaled 50%": lambda: p.scale_pct.setValue(50.0),
        "scaled 200%": lambda: p.scale_pct.setValue(200.0),
    }
    for name, setup in cases.items():
        for w in (p.ct, p.cb2, p.cl2, p.cr): w.setValue(0.0)
        p.scale_pct.setValue(100.0)
        setup()
        pm, _info = p._render_preview(avail_w, avail_h, 1.0)
        assert pm is not None, f"{name}: no preview at all"
        assert pm.width() <= avail_w and pm.height() <= avail_h, \
            f"{name}: {pm.width()}x{pm.height()} does not fit {avail_w}x{avail_h}"
    return f"{len(cases)} states, all inside the pane"


def test_an_oversized_preview_does_not_drag_the_pane_out_with_it():
    """The other half of the same fault, and the half that made it runaway.

    A QLabel showing a pixmap reports that pixmap as its minimum size. So a
    render wider than the pane grew the label, which grew the pane, whose
    resizeEvent refreshed the preview with more room, which produced a wider
    render again — and around. The pixmap must not get a say in how big the
    label is; it is clipped to the space there is, which is also what zooming
    past the pane should do.
    """
    from tests.support import _app, _settle, _spin
    _open(FX["normal"])
    p = CropResizePanel(); p.resize(1200, 800); p.show()
    _settle(p, lambda: p._pane.label.width() > 100 and p._pane.label.height() > 100,
            tries=300)
    label = p._pane.label
    started = (label.width(), label.height())
    assert started[0] > 100 and started[1] > 100, f"the pane never opened: {started}"

    # Zoom well past the pane — the deliberate way to get a pixmap bigger than
    # the label — and then let the event loop settle.
    p._pane._set_zoom(6.0)
    _spin(40)
    _app.processEvents()
    grew = (label.width(), label.height())
    assert grew == started, \
        f"the label followed its pixmap out: {started} -> {grew}"
    assert label.pixmap().width() > label.width(), \
        "the test never produced an oversized render"
    return f"6x zoom, label still {started[0]}x{started[1]}"


def _numbered_pdf(name, n):
    """A document whose pages say which page they are, so the output can be
    checked against the page that was actually asked for."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    path = os.path.join(_TMP, name)
    c = canvas.Canvas(path, pagesize=A4)
    for i in range(n):
        c.setFont("Helvetica", 90)
        c.drawCentredString(300, 400, f"P{i+1}")
        c.showPage()
    c.save()
    return path


def test_cropping_follows_the_page_manager_after_pages_are_deleted():
    """The tool measures and writes against the flattened view of the document
    — the page manager's order, not the file on disk — but it picked its pages
    by their *original* number in that file. The two agree only while the view
    is untouched, which is why this worked most of the time.

    Delete the first five pages of eight and the run went looking for pages 5,
    6 and 7 of a document that now had three. Nothing matched, nothing was
    cropped, and the file it saved was a copy of its input.
    """
    from tools.app_state import AppState
    src = _numbered_pdf("del_src.pdf", 8)
    _open(src)
    model = AppState.get().page_model
    model.selected = {model.order[i] for i in range(5)}
    model.delete_selected()
    assert len(model.order) == 3, "fixture did not delete what it meant to"

    p = CropResizePanel(); p.log.log = lambda *a, **k: None
    p.apply_all.setChecked(False)
    model.selected = set(model.order)
    p.fmt.set_format("A5  (148x210mm)")

    out = os.path.join(_TMP, "del_out.pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: None
    p._run_action()

    sizes = [(round(float(pg.mediabox.width) / MM),
              round(float(pg.mediabox.height) / MM))
             for pg in PdfReader(out).pages]
    assert len(sizes) == 3, f"expected the three surviving pages, got {sizes}"
    for w, h in sizes:
        assert abs(w - 148) <= 1 and abs(h - 210) <= 1, \
            f"a page came out {w}x{h}mm — the run left the file untouched"
    return "all three surviving pages cropped, none skipped"


def test_cropping_acts_on_the_page_that_is_selected_not_the_one_beneath_it():
    """Worse than doing nothing: with the pages reordered, picking a page
    cropped a different one and said it had succeeded.

    Reverse a six-page document and select what is now the first page — the
    original page 6. The run took its number, 5, and applied it to position 5
    of the displayed document, which is the original page 1. The wrong page
    came back resized, the right one untouched, and the log said one page had
    been processed.
    """
    from tools.app_state import AppState
    from tests.support import _page_labels
    src = _numbered_pdf("reorder_src.pdf", 6)
    _open(src)
    model = AppState.get().page_model
    model.order.reverse()
    model.selected = {model.order[0]}          # displayed first = original P6

    p = CropResizePanel(); p.log.log = lambda *a, **k: None
    p.apply_all.setChecked(False)
    p.fmt.set_format("A5  (148x210mm)")
    out = os.path.join(_TMP, "reorder_out.pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: None
    p._run_action()

    labels = _page_labels(out)
    cropped = [labels[i] for i, pg in enumerate(PdfReader(out).pages)
               if abs(float(pg.mediabox.width) / MM - 148) <= 1]
    assert cropped == ["P6"], \
        f"cropped {cropped}, but the selected page was P6 (labels: {labels})"
    return "the selected page is the one that changes"


def test_the_preview_still_draws_when_the_view_is_shorter_than_the_file():
    """The same mixed-up numbering reached the preview, which asked the
    flattened document for a page number that only existed in the original —
    so the pane went blank at exactly the moment the run was about to do
    nothing, and neither said why."""
    from tools.app_state import AppState
    src = _numbered_pdf("preview_src.pdf", 8)
    _open(src)
    model = AppState.get().page_model
    model.selected = {model.order[i] for i in range(5)}
    model.delete_selected()
    model.selected = {model.order[-1]}         # the last page still showing

    p = CropResizePanel(); p.log.log = lambda *a, **k: None
    pm, info = p._render_preview(500, 650, 1.0)
    assert pm is not None, f"the preview drew nothing: {info!r}"
    return "the preview draws against the document that is on screen"
