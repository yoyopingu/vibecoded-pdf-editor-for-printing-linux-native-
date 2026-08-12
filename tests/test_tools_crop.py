"""
Tools Crop.
"""
import os
from pypdf import PdfReader
from PIL import Image
import pypdfium2 as pdfium
import tools.all_tools as T
from tests.support import FX, MM, _TMP, _brightest, _ink_margins, _nup, _open


def test_crop_format_modes():
    _open(FX["normal"])
    def run(action_idx, fmt_setter):
        p = T.CropResizePanel(); p.apply_all.setChecked(True)
        out = os.path.join(_TMP, "crop.pdf")
        p.save_pdf = lambda *a, **k: out
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path)
        p.fmt_action.setCurrentIndex(action_idx); fmt_setter(p); p._run_action()
        mb = PdfReader(out).pages[0].mediabox
        return round(float(mb.width)/MM), round(float(mb.height)/MM), out
    from tools.i18n import tr
    custom = lambda p: (p.fmt_combo.setCurrentText(tr("Benutzerdefiniert (mm)")),
                        p.custom_w.setValue(150), p.custom_h.setValue(200))
    w, h, out = run(1, custom)                      # marks-only: not resized
    assert (w, h) == (210, 297) and _brightest(out, 0) >= 250
    w, h, _ = run(0, custom)                         # scale: resized to custom
    assert abs(w-150) <= 1 and abs(h-200) <= 1
    w, h, _ = run(0, lambda p: p.fmt_combo.setCurrentText("A5  (148x210mm)"))
    assert abs(w-148) <= 1 and abs(h-210) <= 1


def test_crop_mark_geometry():
    segs = T._crop_mark_segments([(100, 100, 300, 500)])    # mx=200, corners 100/300
    assert len(segs) == 16, f"expected 16 (8 corner + 8 centre), got {len(segs)}"
    top = [s for s in segs if abs(s[1]-s[3]) < 0.01 and s[1] > 500]   # horizontal centre marks
    centres = sorted(set(round((s[0]+s[2])/2, 1) for s in top))
    assert len(centres) == 2
    d_corner = centres[0] - 100; d_each = centres[1] - centres[0]
    assert d_corner < d_each, "centre marks must be closer to corners than to each other"


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
    p = T.CropResizePanel(); p.apply_all.setChecked(True)
    p.scale_check.setChecked(True); p.keep_ratio.setChecked(True)
    p.fmt_combo.setCurrentText("A5  (148x210mm)")
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
    p2 = T.CropResizePanel(); p2.apply_all.setChecked(True); p2.scale_check.setChecked(False)
    p2.fmt_combo.setCurrentText("A5  (148x210mm)"); p2.ct.setValue(30.0)
    out2 = os.path.join(_TMP, "crop_fmt2.pdf")
    p2.save_pdf = lambda *a, **k: out2; p2.open_result = lambda path, t="": None
    p2._run_action()
    d = pdfium.PdfDocument(out2); h = d[0].get_height()/MM; d.close()
    assert abs(h - (297-30-43.5)) < 1, f"manual margin ignored (height {h:.0f} mm)"


def test_crop_rejects_over_trim():
    """Trimming away more than the page must be reported, not clamped to a 1 pt
    page with the content dumped somewhere off it."""
    _open(FX["framed"])
    p = T.CropResizePanel(); p.apply_all.setChecked(True)
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
    p = T.CropResizePanel(); p.apply_all.setChecked(True); p.scale_check.setChecked(False)
    for w in (p.ct, p.cb2, p.cl2, p.cr): w.setValue(-15.0)
    pm, _ = p._render_preview(500, 650, 1.0)
    im = _qpix_to_pil(pm); px = im.load()
    # Match the outline against the *live* accent rather than a literal colour —
    # this used to hardcode the old red and broke the moment the accent changed.
    from tools.page_viewer import _TV
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
    p = T.CropResizePanel(); p.apply_all.setChecked(True)
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
    p = T.CropResizePanel(); p.apply_all.setChecked(True); p.scale_check.setChecked(False)
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
