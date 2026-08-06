#!/usr/bin/env python3
"""
CopyShop regression suite — self-contained, dependency-free (no pytest needed).

Run with the app's venv so PyQt6 / pypdfium2 / pikepdf / reportlab are present:

    ~/.local/share/copyshop_pdf_suite/venv/bin/python tests/test_copyshop.py

It tests the SOURCE tree (this repo), generates its own fixture PDFs in a temp
dir, and prints a PASS/FAIL summary. Exit code 0 = all passed.

Covers the behaviour built up over time: panel construction, the encrypted-PDF
guard, the shared async action base, the preview visibility guard, the Crop
format/cut-marks modes, crop-mark geometry, N-Up source modes + preview + marks,
and the (vector, lossless) greyscale conversion.
"""
import os, sys, hashlib, time, tempfile, shutil

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QStackedWidget, QLabel
from PyQt6.QtCore import QTimer
_app = QApplication(sys.argv)

from pypdf import PdfReader, PdfWriter
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image

from tools.app_state import AppState
from tools.page_viewer import PageModel, _ThumbnailCache
import tools.all_tools as T
# Imported up here on purpose: main pulls in PyQt6.QtNetwork, and loading that
# extension module later — with the render threads already running — segfaults.
import main as MAIN
from tools._base import BasePanel, CurrentFileBar, LogBox, ToolScrollArea

MM = 2.8346456693
_TMP = tempfile.mkdtemp(prefix="copyshop_tests_")


# ── fixtures ──────────────────────────────────────────────────────────────────
def _make_fixtures():
    W, H = A4
    # normal: 5 pages, black text
    p = os.path.join(_TMP, "normal.pdf")
    c = canvas.Canvas(p, pagesize=A4)
    for i in range(5):
        c.setFont("Helvetica", 48); c.drawString(80, 700, f"PAGE {i+1}"); c.showPage()
    c.save()
    # single page
    s = os.path.join(_TMP, "single.pdf")
    c = canvas.Canvas(s, pagesize=A4); c.drawString(80, 700, "ONLY"); c.showPage(); c.save()
    # colour: 3 pages, coloured shapes + text, page 3 has transparency
    col = os.path.join(_TMP, "color.pdf")
    c = canvas.Canvas(col, pagesize=A4)
    for pg in range(3):
        c.setFillColorRGB(1, 0, 0); c.rect(60, H-200, 220, 120, fill=1, stroke=0)
        c.setFillColorRGB(0, 0.4, 1); c.setFont("Helvetica", 46); c.drawString(60, H-300, f"WORD{pg+1}")
        if pg == 2:
            try: c.setFillAlpha(0.4)
            except Exception: pass
            c.setFillColorRGB(0, 1, 0); c.rect(120, 200, 300, 200, fill=1, stroke=0); c.setFillAlpha(1)
        c.showPage()
    c.save()
    # framed: content reaching every page edge, so the ink bounding box of a
    # rendered page *is* its visible box — that is what the placement tests
    # measure. Three variants of the same page: plain, with a CropBox inset by
    # 10 mm, and with /Rotate 90.
    fr = os.path.join(_TMP, "framed.pdf")
    c = canvas.Canvas(fr, pagesize=A4)
    c.setLineWidth(3); c.rect(1.5, 1.5, W-3, H-3)
    c.setLineWidth(1); c.line(0, H/2, W, H/2); c.line(W/2, 0, W/2, H)
    c.setFont("Helvetica", 40); c.drawCentredString(W/2, H/2+10, "CENTER")
    c.showPage(); c.save()
    import pikepdf
    box = os.path.join(_TMP, "framed_cropbox.pdf")
    with pikepdf.open(fr) as pdf:
        pdf.pages[0].obj["/CropBox"] = pikepdf.Array(
            [10*MM, 10*MM, float(W)-10*MM, float(H)-10*MM])
        pdf.save(box)
    rot = os.path.join(_TMP, "framed_rot90.pdf")
    with pikepdf.open(fr) as pdf:
        pdf.pages[0].obj["/Rotate"] = 90
        pdf.save(rot)
    # mixed page sizes: page 2 is neither A4 nor the same aspect ratio
    mix = os.path.join(_TMP, "mixed.pdf")
    c = canvas.Canvas(mix, pagesize=A4)
    for pw, ph in ((W, H), (500.0, 700.0), (W, H)):
        c.setPageSize((pw, ph))
        c.setLineWidth(3); c.rect(1.5, 1.5, pw-3, ph-3)
        c.setFont("Helvetica", 30); c.drawCentredString(pw/2, ph/2, "X")
        c.showPage()
    c.save()
    # encrypted (from normal)
    enc = os.path.join(_TMP, "encrypted.pdf")
    with pikepdf.open(p) as pdf:
        pdf.save(enc, encryption=pikepdf.Encryption(owner="o", user="u"))
    # an image for Image→PDF
    img = os.path.join(_TMP, "img.png")
    Image.new("RGB", (400, 600), (40, 90, 160)).save(img)
    # booklet: 32 pages, each a unique flat grey so a half of an imposed sheet
    # identifies the source page it came from, plus a black bar along the bottom
    # edge so a lost /Rotate is visible as the bar landing on the wrong side.
    bk = os.path.join(_TMP, "booklet32.pdf")
    c = canvas.Canvas(bk, pagesize=A4)
    for i in range(32):
        c.setFillGray(_BK_GREY(i)); c.rect(0, 0, W, H, fill=1, stroke=0)
        c.setFillGray(0.0);         c.rect(0, 0, W, H * 0.05, fill=1, stroke=0)
        c.showPage()
    c.save()
    return {"normal": p, "single": s, "color": col, "encrypted": enc, "image": img,
            "framed": fr, "framed_cropbox": box, "framed_rot90": rot, "mixed": mix,
            "booklet32": bk}


def _BK_GREY(i):
    """Grey level identifying source page `i` of the booklet fixture."""
    return 0.90 - 0.02 * i


def _booklet_halves(path):
    """Decode an imposed booklet: one (left, right) tuple per sheet, each entry
    the 1-based source page that landed there or "blank"."""
    d = pdfium.PdfDocument(path)
    out = []
    for pg in d:
        im = pg.render(scale=0.25).to_pil().convert("L")
        w, h = im.size
        row = []
        for k in (0, 1):
            half = im.crop((w * k // 2, 0, w * (k + 1) // 2, h))
            hw, hh = half.size
            patch = half.crop((int(hw*.3), int(hh*.3), int(hw*.7), int(hh*.7)))
            mean = sum(patch.get_flattened_data()) / (patch.size[0]*patch.size[1]) / 255.0
            if mean > 0.94:
                row.append("blank"); continue
            best = min(range(32), key=lambda j: abs(_BK_GREY(j) - mean))
            row.append(best + 1 if abs(_BK_GREY(best) - mean) < 0.008 else f"?{mean:.3f}")
        out.append(tuple(row))
    d.close()
    return out


def _impose(path, open_first=True, **kw):
    """Run the Broschüre tool on `path` and return the output file.

    `open_first=False` keeps the PageModel AppState already carries — the point
    of the page-manager test is that the tool follows that model."""
    if open_first:
        _open(path)
    p = T.ImposePanel(); _sync_async(p)
    for attr, val in kw.items():
        w = getattr(p, attr)
        (w.setCurrentIndex if hasattr(w, "setCurrentIndex") else w.setChecked)(val)
    o = os.path.join(_TMP, f"impose_{os.path.basename(path)}")
    p.save_pdf = lambda *a, **k: o
    cap = {}; p.open_result = lambda path_, t="": cap.update(p=path_)
    p._run_action()
    return cap["p"]


FX = {}


def _open(path):
    """Point AppState at a PDF with a matching PageModel."""
    n = len(PdfReader(path, strict=False).pages) if "encrypt" not in path else 5
    st = AppState.get(); st.open_pdf(path)
    try:
        st.page_model = PageModel(len(PdfReader(path).pages))
    except Exception:
        st.page_model = PageModel(n)
    st.current_page = 0
    return st


def _sync_async(panel):
    """Make run_async run synchronously for deterministic tests."""
    panel.run_async = lambda work, on_done, **k: on_done(work(lambda m: None))


def _page_has_colour(path, i):
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=1, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
    px = list(im.get_flattened_data()); d.close()
    return any(abs(r-g) > 12 or abs(g-b) > 12 for r, g, b in px)


def _ink_margins(path, i=0, scale=4):
    """(left, right, bottom, top) white space around the ink of page `i`, in mm.
    Used to check that content really is centred in the sheet."""
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=scale, fill_color=(255, 255, 255, 255)).to_pil().convert("L")
    d.close()
    from PIL import ImageOps
    bb = ImageOps.invert(im).getbbox()
    assert bb is not None, f"{path} page {i+1} is blank"
    l, t, r, b = bb                     # r/b are exclusive
    W, H = im.size
    return (l/scale/MM, (W-r)/scale/MM, (H-b)/scale/MM, t/scale/MM)


def _nup(src, *, cols=1, rows=1, fmt=0, margins=20.0, gaps=0.0, name="nup_c.pdf"):
    """Run the real N-Up panel over `src` and return the output path."""
    _open(src)
    p = T.NUpPanel()
    p.cols.setValue(cols); p.rows.setValue(rows)
    p.out_fmt.setCurrentIndex(fmt)
    for w in (p.margin_t, p.margin_b, p.margin_l, p.margin_r): w.setValue(margins)
    p.gap_h.setValue(gaps); p.gap_v.setValue(gaps)
    out = os.path.join(_TMP, name); p.save_pdf = lambda *a, **k: out
    cap = {}; p.open_result = lambda path, t="": cap.update(p=path); _sync_async(p)
    p._run_action()
    return cap["p"], p


def _brightest(path, i):
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=1, fill_color=(255, 255, 255, 255)).to_pil().convert("L")
    px = list(im.get_flattened_data()); d.close()
    return max(px)


# ── tests ───────────────────────────────────────────────────────────────────
def test_panels_construct():
    names = [n for n in dir(T) if n.endswith("Panel") and isinstance(getattr(T, n), type)]
    built = 0
    for n in names:
        getattr(T, n)(); built += 1
    assert built >= 10, f"only {built} panels built"


def test_encryption_guard():
    _open(FX["encrypted"])
    p = T.CropResizePanel()
    try:
        p.require_pdf(); raise AssertionError("encrypted PDF was not rejected")
    except ValueError as e:
        assert "passwortgesch" in str(e).lower()


def test_run_async_base():
    class P(BasePanel):
        def build_ui(self, layout): pass
    p = P(); res = {}; progs = []
    p.run_async(lambda report: (report("a"), report("b"), 42)[-1],
                on_done=lambda r: res.update(done=r), on_progress=progs.append,
                busy_label="x")
    assert p._async_running and not p.run_btn.isEnabled()
    for _ in range(400):
        _app.processEvents()
        if "done" in res: break
        time.sleep(0.005)
    assert res.get("done") == 42 and progs == ["a", "b"]
    assert not p._async_running and p.run_btn.isEnabled()


def test_preview_visibility_guard():
    _open(FX["color"])
    nup = T.NUpPanel(); nup.src_combo.setCurrentIndex(0)
    stack = QStackedWidget(); other = QStackedWidget()
    stack.addWidget(nup); stack.addWidget(other)
    stack.setCurrentWidget(other); stack.resize(900, 600); stack.show(); _app.processEvents()
    AppState.get().current_page = 1; _app.processEvents()
    pm = nup._preview.pixmap()
    assert pm is None or pm.isNull(), "hidden preview rendered (would starve viewer scroll)"


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


def test_nup_source_modes():
    _open(FX["normal"])      # 5 pages
    def sheets(mode):
        p = T.NUpPanel(); p.cols.setValue(2); p.rows.setValue(2); p.src_combo.setCurrentIndex(mode)
        out = os.path.join(_TMP, f"nup{mode}.pdf"); p.save_pdf = lambda *a, **k: out
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path); _sync_async(p)
        p._run_action(); return len(PdfReader(cap["p"]).pages)
    assert sheets(0) == 2, "sequential 5p 2x2 should be 2 sheets"
    assert sheets(1) == 5, "repeat-each-page should be 5 sheets"


def test_nup_preview_differs_by_mode():
    _open(FX["normal"])
    nup = T.NUpPanel(); nup.cols.setValue(2); nup.rows.setValue(2)
    nup.resize(900, 600); nup.show(); _app.processEvents()
    def h(mode):
        nup.src_combo.setCurrentIndex(mode)
        for _ in range(600):
            nup._pane.refresh(); _app.processEvents()
            if "rendert" not in nup._preview_info.text(): break
            time.sleep(0.005)
        nup._pane.refresh(); _app.processEvents()
        pm = nup._preview.pixmap(); img = pm.toImage(); ptr = img.bits(); ptr.setsize(img.sizeInBytes())
        return hashlib.md5(bytes(ptr)).hexdigest()
    assert h(0) != h(1), "preview identical for both source modes"


def test_nup_crop_marks():
    _open(FX["normal"])
    A4w, A4h = A4; ml = mt = 14.0; gap = 8.5
    sw = (A4w-2*ml-gap)/2; sh = (A4h-2*mt-gap)/2
    params = (A4w, A4h, mt, mt, ml, ml, gap, gap, sw, sh, 2, 2)
    nomark = os.path.join(_TMP, "nm.pdf"); withm = os.path.join(_TMP, "wm.pdf")
    T._build_nup(FX["normal"], nomark, [0, 1, 2, 3], params, 4, lambda m: None, crop_marks=False)
    T._build_nup(FX["normal"], withm, [0, 1, 2, 3], params, 4, lambda m: None, crop_marks=True)
    def left_margin_dark(path):
        d = pdfium.PdfDocument(path); im = d[0].render(scale=2).to_pil().convert("L"); d.close()
        return min(im.crop((0, 0, int(ml*2), im.size[1])).get_flattened_data())
    assert left_margin_dark(nomark) > 200 and left_margin_dark(withm) < 80


def test_nup_centres_content():
    """The placed page must sit exactly in the middle of its slot — whatever
    boxes or /Rotate the source carries. A stale/inset CropBox or a rotation used
    to shift it by millimetres because the layout was measured on the MediaBox
    while qpdf placed the TrimBox."""
    for fx in ("framed", "framed_cropbox", "framed_rot90"):
        out, _ = _nup(FX[fx], margins=20.0, name=f"centre_{fx}.pdf")
        L, R, B, Tp = _ink_margins(out)
        assert abs(L-R) < 0.4 and abs(B-Tp) < 0.4, \
            f"{fx}: not centred — L={L:.2f} R={R:.2f} B={B:.2f} T={Tp:.2f} mm"
        assert min(L, R, B, Tp) >= 19.5, f"{fx}: content inside the 20 mm margin"


def test_nup_auto_format_keeps_margins():
    """"Wie Quellseite × Raster" must add the margins around the page instead of
    scaling the page down inside them — 10 mm requested is 10 mm on all four
    sides, not 10 mm at the sides and 14 mm top/bottom."""
    for fx in ("framed", "framed_rot90"):
        out, _ = _nup(FX[fx], fmt=4, margins=10.0, name=f"auto_{fx}.pdf")
        for m in _ink_margins(out):
            assert abs(m - 10.0) < 0.4, f"{fx}: margins {_ink_margins(out)} != 10 mm"
        d = pdfium.PdfDocument(FX[fx]); sw, sh = d[0].get_width(), d[0].get_height(); d.close()
        d = pdfium.PdfDocument(out);    ow, oh = d[0].get_width(), d[0].get_height(); d.close()
        assert abs(ow - (sw + 20*MM)) < 1 and abs(oh - (sh + 20*MM)) < 1, \
            f"{fx}: sheet {ow:.0f}x{oh:.0f} should be the page plus 2x10 mm"


def test_nup_rejects_impossible_margins():
    """Margins wider than the sheet must be reported, not silently squeezed into
    a 1 pt slot in the corner."""
    _open(FX["framed"])
    p = T.NUpPanel()
    p.out_fmt.setCurrentIndex(0)                      # A4
    p.margin_l.setValue(120.0); p.margin_r.setValue(120.0)
    p.save_pdf = lambda *a, **k: os.path.join(_TMP, "never.pdf")
    _sync_async(p)
    try:
        p._run_action()
        raise AssertionError("no error for margins wider than the sheet")
    except ValueError:
        pass
    pm, info = p._render_preview(400, 400, 1.0)
    assert pm is None and "Platz" in info, f"preview should explain the problem, got {info!r}"


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


def _click(grid, pos, mod):
    from PyQt6.QtWidgets import QApplication as _QA
    orig = _QA.keyboardModifiers
    _QA.keyboardModifiers = staticmethod(lambda: mod)
    try:
        grid._on_click(pos)
    finally:
        _QA.keyboardModifiers = orig


def test_merge_view_matches_manage_view():
    """The multi-file merge view is "Seiten verwalten" for files. Its cards and
    its selection behaviour must be the same — they used to be a separate
    implementation with single-select only, no zoom and boxed cards."""
    from PyQt6.QtCore import Qt as _Qt
    from tools.page_viewer import PageGrid, FileGrid, MergeOrderWidget, CARD_W
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    model = PageModel(4)
    pgrid = PageGrid(model, FX["normal"])
    fgrid = FileGrid(paths)

    NONE  = _Qt.KeyboardModifier.NoModifier
    CTRL  = _Qt.KeyboardModifier.ControlModifier
    SHIFT = _Qt.KeyboardModifier.ShiftModifier
    for mod, pos in [(NONE, 0), (CTRL, 2), (CTRL, 2), (SHIFT, 3), (NONE, 1), (SHIFT, 3), (CTRL, 0)]:
        _click(pgrid, pos, mod); _click(fgrid, pos, mod)
        pages = {i for i, u in enumerate(model.order) if u in model.selected}
        assert pages == fgrid._selected, \
            f"after {mod}+click {pos}: pages {sorted(pages)} vs files {sorted(fgrid._selected)}"
    pgrid.select_all(); fgrid.select_all()
    assert len(fgrid._selected) == 4, "select all"
    pgrid.deselect_all(); fgrid.deselect_all()
    assert not fgrid._selected, "deselect all"

    pc, fc = pgrid._cards[0], fgrid._cards[0]
    assert pc.size() == fc.size(), f"card size {pc.size()} vs {fc.size()}"
    assert pc.styleSheet() == fc.styleSheet(), "unselected card styling differs"
    pc.set_selected(True); fc.set_selected(True)
    assert pc.styleSheet() == fc.styleSheet(), "selected card styling differs"

    # zoom, like the page grid
    w0 = fgrid._card_w; fgrid.zoom_in()
    assert fgrid._card_w > w0 and fgrid._cards[0].width() == fgrid._card_w + 16
    fgrid.zoom_reset(); assert fgrid._card_w == CARD_W

    # the sidebar offers the same sections
    mw = MergeOrderWidget(paths)
    labels = {w.text() for w in mw.findChildren(QLabel) if w.objectName() == "sectionLabel"}
    for section in ("ANSICHT", "AUSWAHL", "REIHENFOLGE", "OPERATIONEN"):
        assert any(section in l for l in labels), f"{section} missing from the merge sidebar"
    assert hasattr(mw, "sel_edit") and hasattr(mw, "status")


def test_merge_view_reorders_and_removes():
    from tools.page_viewer import MergeOrderWidget
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    mw = MergeOrderWidget(paths); g = mw._grid
    g._selected = {0, 1}; g._last_selected = 0
    g.move_down()
    assert g.get_paths()[:3] == [paths[2], paths[0], paths[1]], "move down"
    assert g._selected == {1, 2}, "selection must follow the move"
    g.move_up()
    assert g.get_paths()[0] == paths[0], "move up"
    # dragging one card of a multi-selection moves the whole selection
    g2 = MergeOrderWidget(paths)._grid
    g2._selected = {0, 1}; g2._last_selected = 0
    g2.handle_drop(0, 3, multi=True)
    assert g2.get_paths() == [paths[2], paths[0], paths[1], paths[3]], g2.get_paths()
    # selection field parses ranges like the page manager
    mw.sel_edit.setText("1, 3-4"); mw._apply_sel_edit()
    assert g._selected == {0, 2, 3} and mw.sel_edit.text() == "1, 3-4"
    g._selected = {3}; g.remove_selected()
    assert len(g.get_paths()) == 3 and paths[3] not in g.get_paths()


def test_merge_tab_end_to_end():
    """Open the merge tab, reorder, confirm — the result must be one PDF in the
    order shown, replacing the merge tab."""
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget
    paths = [FX["normal"], FX["single"], FX["framed"]]      # 5 + 1 + 1 pages
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    for _ in range(30): _app.processEvents(); time.sleep(0.01)
    w = vp.tabs.currentWidget()
    assert isinstance(w, MergeOrderWidget), "merge tab did not open"
    w._grid._selected = {2}; w._grid._last_selected = 2
    w._grid.move_up(); w._grid.move_up()
    assert w._grid.get_paths()[0] == FX["framed"], "reorder did not stick"
    w._confirm()
    for _ in range(500):
        _app.processEvents(); time.sleep(0.02)
        if vp.tabs.count() and not isinstance(vp.tabs.currentWidget(), MergeOrderWidget):
            break
    cur = vp.tabs.currentWidget()
    assert not isinstance(cur, MergeOrderWidget), "merge never completed"
    out = getattr(cur, "pdf_path", None)
    assert out and os.path.isfile(out), "no merged file"
    assert len(PdfReader(out).pages) == 7, f"{len(PdfReader(out).pages)} pages, expected 7"
    vp.deleteLater()


_HOST_SCRIPT = '''
import sys, os
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
app = QApplication([])
import main as MAIN
MAIN._IPC_KEY = {key!r}          # own socket, so a real running app is untouched
win = MAIN.MainWindow(open_file={src!r})
win.show()
seen = []
win._open_multi = lambda files: (seen.append(len(files)),
                                 print("MULTI", len(files), flush=True))
_open = win.open_paths
def open_paths(paths):
    _open(paths)
    if len(paths) == 1:
        print("TABS", win.viewer.tabs.count(), flush=True)
win.open_paths = open_paths
MAIN._listen_for_open_requests(win)
print("READY", int(win._ipc_server.isListening()), flush=True)
QTimer.singleShot(20000, lambda: os._exit(2))
app.exec()
'''


def _expect_line(proc, prefix, timeout=25.0):
    """Read the child's stdout until a line starts with `prefix`."""
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith(prefix):
            return line
    raise AssertionError(f"child never reported {prefix!r}")


def test_single_instance_forwards_files():
    """A second launch must hand its files to the already-running window instead
    of opening another one. Driven as two real processes, which is the case that
    actually broke: opening a PDF from the file manager started a new app."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(_TMP, "ipc_host.py")
    # A private key: the developer's own copy of the app may well be running,
    # and it would answer on the production socket.
    key = f"copyshop_test_{os.getpid()}"
    real_key, MAIN._IPC_KEY = MAIN._IPC_KEY, key
    with open(script, "w") as f:
        f.write(_HOST_SCRIPT.format(repo=repo, src=FX["normal"], key=key))

    # No instance running yet → forwarding must fail so the app opens normally.
    assert MAIN._forward_to_running_instance([FX["normal"]]) is False

    host = subprocess.Popen([sys.executable, "-u", script],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert _expect_line(host, "READY") == "READY 1", "host is not listening"
        # one file → a tab in the window that is already open
        assert MAIN._forward_to_running_instance([FX["single"]]) is True
        tabs = int(_expect_line(host, "TABS").split()[1])
        assert tabs == 2, f"forwarded file did not become a second tab ({tabs})"
    finally:
        host.kill(); host.wait(timeout=10)
        MAIN._IPC_KEY = real_key


def test_open_paths_routes_by_count():
    """One file goes straight to a tab, several go to the open/merge chooser.
    Driven against the real method with a stand-in window, so it stays
    deterministic (a second MainWindow in this process is not)."""
    class FakeWindow:
        def __init__(self):
            self.raised = 0; self.opened = []; self.multi = None
            self.viewer = self
        def _raise_to_front(self): self.raised += 1
        def _switch(self, idx): pass
        def open_file(self, path): self.opened.append(path)
        def _open_multi(self, files): self.multi = list(files)

    w = FakeWindow()
    MAIN.MainWindow.open_paths(w, [FX["normal"]])
    assert w.opened == [FX["normal"]] and w.multi is None and w.raised == 1

    w = FakeWindow()
    MAIN.MainWindow.open_paths(w, [FX["normal"], FX["single"]])
    assert w.multi == [FX["normal"], FX["single"]] and w.opened == []

    w = FakeWindow()
    MAIN.MainWindow.open_paths(w, [os.path.join(_TMP, "does_not_exist.pdf")])
    assert w.opened == [] and w.multi is None, "missing files must be ignored"


def _popup_fully_visible(cb):
    """How many of a combo's items are fully inside its open popup."""
    cb.showPopup()
    _app.processEvents(); time.sleep(0.02); _app.processEvents()
    view = cb.view(); vp = view.viewport(); model = view.model()
    n = 0
    for i in range(cb.count()):
        r = view.visualRect(model.index(i, 0))
        if r.top() >= 0 and r.bottom() <= vp.height():
            n += 1
    cb.hidePopup()
    return n


def test_dropdowns_show_all_their_options():
    """Vertical padding on QComboBox makes Qt size the drop-down one row short,
    so even a two-option dropdown had to be scrolled. Check the real stylesheet
    against a range of item counts and against every combo in the tool panels."""
    from PyQt6.QtWidgets import QComboBox, QWidget, QVBoxLayout
    old = _app.styleSheet()
    try:
        for style in (MAIN.STYLE, MAIN.LIGHT_STYLE):
            _app.setStyleSheet(style)
            for count in (2, 3, 5, 11, 14):
                host = QWidget(); lay = QVBoxLayout(host)
                cb = QComboBox(); cb.addItems([f"Option {i+1}" for i in range(count)])
                lay.addWidget(cb); host.resize(320, 80); host.show()
                _app.processEvents()
                visible = _popup_fully_visible(cb)
                host.hide()
                assert visible == count, \
                    f"{count}-item dropdown shows only {visible} items — it scrolls"
        # and the combos the tools actually use
        _app.setStyleSheet(MAIN.STYLE)
        _open(FX["normal"])
        for panel_cls in (T.NUpPanel, T.CropResizePanel, T.PageNumbersPanel, T.CompressPanel):
            p = panel_cls(); p.resize(900, 600); p.show(); _app.processEvents()
            for cb in p.findChildren(QComboBox):
                if cb.count() < 2: continue
                visible = _popup_fully_visible(cb)
                assert visible == cb.count(), \
                    (f"{panel_cls.__name__}: a {cb.count()}-option dropdown shows "
                     f"only {visible} — it scrolls")
            p.hide()
    finally:
        _app.setStyleSheet(old)


def test_light_theme_reaches_every_colour_source():
    """Startup used to set the stylesheet and the viewer palette but leave
    app_state.THEME on its dark defaults, so anything drawn with theme_color()
    — the Greyscale preview area above all — stayed dark in light mode."""
    import tools.app_state as _as
    old = _app.styleSheet()
    try:
        MAIN.apply_theme_globally("light")
        assert _as.THEME["BG"] == "#edf1f7", f"THEME not switched: {_as.THEME['BG']}"
        from tools.page_viewer import _TV
        assert _TV["viewer_bg"] == "#e8edf3"
        # the greyscale preview follows a *runtime* switch as well
        _open(FX["normal"])
        g = T.GrayscalePanel(); g.resize(900, 600); g.show()
        g._build_preview(3)
        _app.processEvents()
        light_css = g._preview_box.styleSheet()
        assert "#e8edf3" in light_css, light_css
        MAIN.apply_theme_globally("dark")
        _app.processEvents()
        dark_css = g._preview_box.styleSheet()
        assert dark_css != light_css and "#111827" in dark_css, dark_css
        g.hide()
    finally:
        MAIN.apply_theme_globally("dark")
        _app.setStyleSheet(old)


def test_greyscale_vector():
    if not (shutil.which("gs") or shutil.which("gswin64c")):
        return "SKIP (no ghostscript)"
    _open(FX["color"])
    def convert(sel):
        p = T.GrayscalePanel()
        p._scanned_path = FX["color"]   # bypass the (cached) auto-scan
        p._page_data = [(0.0, 0.0)]*3; p._manual_sel = set(sel)
        p._grey_pages = set(); p._manual_skip = set(); p._already_grey = set()
        out = os.path.join(_TMP, "grey.pdf"); p.save_pdf = lambda *a, **k: out
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path); _sync_async(p)
        p._run_action(); return cap["p"]
    out = convert({0, 1, 2})
    r = PdfReader(out)
    for i in range(3):
        assert f"WORD{i+1}" in (r.pages[i].extract_text() or ""), f"page {i} text lost (rasterised!)"
        assert not _page_has_colour(out, i) and _brightest(out, i) >= 250
    out = convert({0, 2})                       # selection honoured
    assert not _page_has_colour(out, 0) and not _page_has_colour(out, 2)
    assert _page_has_colour(out, 1)             # unselected page stays colour
    return "ok"


def test_cmyk_profiles():
    if not shutil.which("gs"):
        return "SKIP (no ghostscript)"
    _open(FX["color"])
    p = T.ColourProfilePanel()
    assert p.profile_combo.count() >= 5, "expected several CMYK profile options"
    o = os.path.join(_TMP, "cmyk.pdf")
    p.save_pdf = lambda *a, **k: o; p.open_result = lambda *a, **k: None
    p.profile_combo.setCurrentIndex(1)          # a named profile (falls back if .icc absent)
    p._run_action()
    assert len(PdfReader(o).pages) >= 1, "CMYK conversion produced no valid output"
    return "ok"


def test_booklet_pads_to_multiples_of_four():
    """A folded sheet carries four pages, so the imposition always rounds up to a
    multiple of four — and it must do that by adding blank *slots* at the end,
    never by dropping or duplicating a source page."""
    for n in (1, 2, 3, 4, 5, 6, 29, 30, 31, 32):
        sides = T._booklet_sides(n)
        flat  = [i for side in sides for i in side]
        assert sorted(i for i in flat if i is not None) == list(range(n)), \
            f"n={n}: pages lost or duplicated — {flat}"
        assert len(flat) % 4 == 0 and 0 <= len(flat) - n < 4, \
            f"n={n}: padded to {len(flat)} slots"
    # 4 pages fold as [back|front] then [inside-left|inside-right]
    assert T._booklet_sides(4) == [[3, 0], [1, 2]]


def test_booklet_page_order():
    """32 pages must come out in saddle-stitch order with nothing blank: the back
    cover (page 32) shares the first sheet with the front cover, and page 2 lands
    on the reverse of it."""
    out = _impose(FX["booklet32"], mode=0)
    got = _booklet_halves(out)
    want = [(32, 1), (2, 31), (30, 3), (4, 29), (28, 5), (6, 27), (26, 7), (8, 25),
            (24, 9), (10, 23), (22, 11), (12, 21), (20, 13), (14, 19), (18, 15), (16, 17)]
    assert got == want, f"booklet order wrong:\n got {got}\nwant {want}"


def test_booklet_keeps_rotation_and_visible_box():
    """The imposition must place pages the way the viewer shows them. Measuring
    the raw MediaBox with pypdf's merge_transformed_page dropped /Rotate and let
    a print PDF's bleed decide the sheet size."""
    import pikepdf
    src = os.path.join(_TMP, "booklet32_boxes.pdf")
    with pikepdf.open(FX["booklet32"]) as pdf:
        pdf.pages[16].obj["/Rotate"] = 180              # upside down in the viewer
        for i in (0, 31):                               # 3 mm bleed around the trim
            pdf.pages[i].obj["/MediaBox"] = pikepdf.Array([-8.5, -8.5, 603.78, 850.39])
            pdf.pages[i].obj["/CropBox"]  = pikepdf.Array([0, 0, 595.28, 841.89])
        pdf.save(src)
    out = _impose(src, mode=0)

    d = pdfium.PdfDocument(out)
    w, h = d[0].get_width(), d[0].get_height()
    assert abs(w - 2*595.28) < 1 and abs(h - 841.89) < 1, \
        f"bleed inflated the sheet to {w:.0f}x{h:.0f}, expected A3 landscape"
    # page 17 sits on the right of the last sheet; turned 180° its black bar is
    # at the top, not the bottom.
    im = d[15].render(scale=0.25).to_pil().convert("L"); d.close()
    iw, ih = im.size
    half = im.crop((iw // 2, 0, iw, ih)); hw, hh = half.size
    def _mean(box):
        c = half.crop(box)
        return sum(c.get_flattened_data()) / (c.size[0] * c.size[1]) / 255.0
    assert _mean((0, 0, hw, int(hh*.07))) < _mean((0, int(hh*.93), hw, hh)), \
        "the /Rotate of the source page was dropped"
    assert _booklet_halves(out)[0] == (32, 1), "bleed/rotation changed the page order"


def test_booklet_annotation_content_survives():
    """Content that lives in an annotation appearance (a stamp, a filled form
    field) is on the page for the user, so it must be on the printed sheet too —
    a page turned into a Form XObject leaves its annotations behind."""
    import pikepdf
    src = os.path.join(_TMP, "booklet32_stamp.pdf")
    with pikepdf.open(FX["booklet32"]) as pdf:
        ap = pikepdf.Stream(pdf, b"0 0 0 rg 0 0 300 300 re f")
        ap.Type = pikepdf.Name.XObject; ap.Subtype = pikepdf.Name.Form
        ap.BBox = pikepdf.Array([0, 0, 300, 300])
        pdf.pages[31].obj["/Annots"] = pikepdf.Array([pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Stamp,
                               Rect=pikepdf.Array([150, 250, 450, 550]), F=4,
                               AP=pikepdf.Dictionary(N=ap)))])
        pdf.save(src)
    out = _impose(src, mode=0)
    d = pdfium.PdfDocument(out)
    im = d[0].render(scale=0.25).to_pil().convert("L"); d.close()
    w, h = im.size
    left = im.crop((0, 0, w // 2, h))
    px = list(left.get_flattened_data())
    black = sum(1 for v in px if v < 60) / len(px)
    assert black > 0.15, \
        f"the stamp on page 32 is missing from the sheet (black {black:.3f})"


def test_tools_see_the_page_manager():
    """Tools must process the document "Seiten verwalten" is showing. The page
    order, rotations and inserted pages live in an in-memory PageModel until the
    user saves; reading the file instead imposed the pre-edit document — and
    since "Leere Seite einfuegen" appends the blank to the end of the file, it
    surfaced as the back of the cover."""
    from tools._base import displayed_pdf
    from tools.page_viewer import PdfTab

    # unedited document -> the original file, no copy
    _open(FX["booklet32"])
    st = AppState.get()
    assert displayed_pdf(st.current_pdf) == FX["booklet32"], \
        "an unedited document should not be rewritten"

    # 31 pages + a blank inserted after page 1 == 32 pages on screen
    src31 = os.path.join(_TMP, "booklet31.pdf")
    w = PdfWriter()
    for pg in PdfReader(FX["booklet32"], strict=False).pages[:31]:
        w.add_page(pg)
    with open(src31, "wb") as f: w.write(f)

    tab = PdfTab(src31)
    st.open_pdf(tab.pdf_path); st.page_model = tab.model
    tab._build_manage_once()
    tab.model.selected = {tab.model.order[0]}
    tab._manage_panel._insert_blank()
    assert len(tab.model.order) == 32, "the blank was not inserted"
    assert st.current_pdf == tab.pdf_path, \
        "the rebuilt file was not published — tools still read the old one"

    out = _impose(st.current_pdf, open_first=False, mode=0)
    got = _booklet_halves(out)
    assert got[0] == (31, 1), \
        f"the back of the cover should carry page 31, got {got[0]}"
    assert got[1] == ("blank", 30), \
        f"the inserted blank belongs on the inside of the cover, got {got[1]}"


def test_output_validity():
    """Every tool (incl. the multi-button Merge / Image→PDF and the transformers)
    must produce a valid, openable PDF — guards against silent corruption."""
    def out(tag): return os.path.join(_TMP, f"out_{tag}.pdf")
    def pages(path):
        return len(PdfReader(path).pages)

    # Merge 5 + 1 -> 6 pages
    m = T.MergeSplitPanel(); m.merge_list.add_files([FX["normal"], FX["single"]])
    o = out("merge"); m.save_pdf = lambda *a, **k: o; m.open_result = lambda *a, **k: None
    m._do_merge_impl()
    assert pages(o) == 6, f"merge expected 6, got {pages(o)}"

    # Image -> PDF (1 page)
    ip = T.ImgPdfPanel(); ip.img_list.add_files([FX["image"]])
    o = out("img"); ip.save_pdf = lambda *a, **k: o; ip.open_result = lambda *a, **k: None
    ip._to_pdf()
    assert pages(o) == 1, f"img2pdf expected 1, got {pages(o)}"

    # transformers on a 3-page doc -> valid output
    _open(FX["color"])
    for cls in (T.CompressPanel, T.PageNumbersPanel, T.ImposePanel,
                T.LayersPanel, T.ColourProfilePanel):
        _open(FX["color"]); p = cls(); _sync_async(p)
        o = out(cls.__name__); p.save_pdf = lambda *a, **k: o
        cap = {}; p.open_result = lambda path, t="": cap.update(p=path)
        p._run_action()
        path = cap.get("p", o)
        assert os.path.exists(path) and pages(path) >= 1, f"{cls.__name__} produced no valid output"


# ── runner ────────────────────────────────────────────────────────────────────
def main():
    global FX
    FX = _make_fixtures()
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            note = t()
            extra = f"  ({note})" if isinstance(note, str) else ""
            print(f"  PASS  {t.__name__}{extra}"); passed += 1
        except Exception as e:
            import traceback
            print(f"  FAIL  {t.__name__}: {e}\n{traceback.format_exc()}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.stdout.flush()
    os._exit(0 if failed == 0 else 1)   # skip Qt/daemon-thread teardown (harmless segfault)


if __name__ == "__main__":
    main()
