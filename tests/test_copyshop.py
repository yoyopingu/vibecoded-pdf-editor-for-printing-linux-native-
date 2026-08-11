#!/usr/bin/env python3
"""
CopyShop regression suite — self-contained, dependency-free (no pytest needed).

Run it with the system interpreter, which already has PyQt6 / pypdfium2 /
pikepdf / reportlab / img2pdf:

    python3 tests/test_copyshop.py

(It used to say "use the installed app's venv". That install is gone — the app
runs from this repo now, so there is no second copy to drift out of sync.)

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
    # dragging one card of a multi-selection moves the whole selection.
    # Hold the widget, not just its grid: nothing else keeps a merge view alive
    # (the theme registry only weakrefs it), so dropping it here let the garbage
    # collector delete the labels the grid's order_changed signal writes to.
    mw2 = MergeOrderWidget(paths); g2 = mw2._grid
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


def _settle(vp, done, tries=600):
    for _ in range(tries):
        _app.processEvents(); time.sleep(0.02)
        if done():
            return True
    return False


def _spin(n=60, delay=0.02):
    for _ in range(n):
        _app.processEvents(); time.sleep(delay)


def _open_in_manage(path):
    """Open `path` in a viewer panel and switch it into the page manager.
    Returns (viewer, tab, manage_panel)."""
    from tools.page_viewer import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    vp.open_file(path)
    _spin(60, 0.01)
    tab = vp.tabs.currentWidget()
    vp._toggle_manage()
    _spin(40, 0.01)
    # Not findChild(): manage mode reparents the panel out of the tab and into
    # the viewer's splitter. The tab keeps its own reference.
    panel = tab._manage_panel
    assert panel is not None and tab.in_manage_mode(), "page manager did not open"
    return vp, tab, panel


def test_inserted_blank_page_renders_in_the_preview():
    """"Leere Seite einfuegen" rebuilds the PDF into a temp file. The single-page
    view has to follow it there — it used to keep the old, shorter path, so the
    blank page's index was past the end of the file it was rendering from and
    the preview showed the blue "render failed" fallback at a bogus size."""
    vp, tab, panel = _open_in_manage(FX["normal"])          # 5 pages
    before = len(tab.model.order)
    tab.model.selected = {tab.model.order[1]}
    panel._insert_blank()
    _spin(40, 0.01)
    assert len(tab.model.order) == before + 1, panel.status.text()
    assert tab.pdf_path == panel.pdf_path, \
        "the tab still points at the pre-insert file"

    vp._toggle_manage()                                      # back to preview
    _spin(40, 0.01)
    sv = tab.single
    sv._current = 2                                          # the blank
    sv._render()
    _settle(vp, lambda: sv._page_w_pt > 0, tries=200)
    assert sv._page_w_pt > 0 and sv._page_h_pt > 0, \
        f"preview has no page size ({sv._page_w_pt}x{sv._page_h_pt}) — render failed"
    # A4-ish, like the page it was inserted after — not a fallback of any size
    assert 500 < sv._page_w_pt < 700 and 700 < sv._page_h_pt < 950, \
        f"blank page is {sv._page_w_pt}x{sv._page_h_pt} pt"
    img = sv._last_pm.toImage()
    mid = img.pixel(img.width()//2, img.height()//2)
    assert (mid & 0xFFFFFF) == 0xFFFFFF, \
        f"blank page is not white (got {hex(mid)}) — this is the fallback fill"
    vp.deleteLater()


def test_save_as_honours_the_page_selection():
    """Ctrl+Shift+S with pages picked in the manager saves those pages, not the
    whole document. A selection left over from an earlier visit must not
    truncate a normal Save As, and a full Save As must re-base the model onto
    the file it just wrote."""
    from PyQt6.QtWidgets import QFileDialog
    out_dir = tempfile.mkdtemp(dir=_TMP)
    def _pick(path):
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))

    vp, tab, panel = _open_in_manage(FX["normal"])           # 5 pages
    assert tab.in_manage_mode()
    picked = [tab.model.order[1], tab.model.order[3]]
    tab.model.selected = set(picked)
    sel = os.path.join(out_dir, "sel.pdf")
    _pick(sel); vp._save_as_current(); _spin(20, 0.01)
    assert len(PdfReader(sel, strict=False).pages) == 2, \
        f"{len(PdfReader(sel).pages)} pages saved, expected the 2 picked"
    assert tab.pdf_path == FX["normal"], \
        "an export of part of the document must not retarget the tab"

    # Leaving the manager makes the stale selection irrelevant again.
    vp._toggle_manage(); _spin(20, 0.01)
    assert not tab.in_manage_mode()
    tab.model.order = list(reversed(tab.model.order))   # reorder, so a stale
    full = os.path.join(out_dir, "full.pdf")            # index mapping shows up
    _pick(full); vp._save_as_current(); _spin(20, 0.01)
    assert len(PdfReader(full, strict=False).pages) == 5, \
        "a stale selection truncated an ordinary Save As"
    assert tab.pdf_path == full, "Save As did not retarget the tab"

    # The model must resolve to exactly what is in the file it now points at.
    on_disk = _pdfium_page_text(full)
    resolved = []
    for uid in tab.model.order:
        p, o = tab.model.page_source(uid, tab.pdf_path)
        resolved.append(_pdfium_page_text(p)[o])
    assert resolved == on_disk, f"model {resolved} vs file {on_disk} after Save As"
    assert resolved == list(reversed(["PAGE 1", "PAGE 2", "PAGE 3", "PAGE 4", "PAGE 5"])), \
        resolved
    vp.deleteLater()


def _pdfium_page_text(path):
    """The text layer of each page, read through pdfium (which sees an OCR
    layer that pypdf's extract_text can miss). Named apart from _page_labels
    below — two helpers with one name silently shadowed each other."""
    doc = pdfium.PdfDocument(path)
    try:
        return [doc[i].get_textpage().get_text_range().strip()
                for i in range(len(doc))]
    finally:
        doc.close()


def test_drop_marker_is_a_slim_page_slot():
    """The drag-drop indicator is a slim rounded slot the height of a page card,
    not the barbed line that used to read as a crooked arrow. Measured by
    diffing a render with and without the marker, so only the marker's own
    pixels are inspected."""
    from tools.page_viewer import PageGrid, CARD_H
    from tools.page_viewer import PageModel as _PM
    grid = PageGrid(_PM(6), FX["normal"])
    grid.resize(700, 500); grid.show()
    _spin(40, 0.02)

    def _shot():
        return grid.grab().toImage()
    plain = _shot()
    grid._drop_indicator = 3
    grid.update(); _spin(15, 0.01)
    marked = _shot()

    rows = {}
    for y in range(min(plain.height(), marked.height())):
        xs = [x for x in range(min(plain.width(), marked.width()))
              if plain.pixel(x, y) != marked.pixel(x, y)]
        if xs:
            rows[y] = (min(xs), max(xs))
    assert rows, "the drop marker painted nothing"

    ys = sorted(rows)
    height = ys[-1] - ys[0] + 1
    widths = [hi - lo + 1 for lo, hi in rows.values()]
    # Slim: a narrow slot, not a full card and not a 3px hairline
    assert 5 <= max(widths) <= 24, f"marker is {max(widths)}px across"
    # Page-shaped: as tall as the card it slots beside
    assert abs(height - CARD_H) <= 16, f"marker is {height}px tall, card is {CARD_H}"
    # Blob, not arrow: no flaring barbs — the width stays even down its length
    core = [w for y, w in zip(ys, (rows[y][1] - rows[y][0] + 1 for y in ys))
            if ys[0] + 3 <= y <= ys[-1] - 3]
    assert core and max(core) - min(core) <= 3, \
        f"width varies {min(core)}..{max(core)} along the marker — that is an arrow"
    # And it is the accent colour
    y_mid = ys[len(ys)//2]; lo, hi = rows[y_mid]
    px = marked.pixel((lo + hi)//2, y_mid)
    r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
    assert b > r and b > 100, f"marker colour {hex(px)} is not blue"


def test_several_files_go_straight_to_the_preview():
    """Picking several files must land in the sort/merge preview with no modal
    chooser in front of it, and the preview must offer both actions. The
    chooser was removed because clicking its merge button faster than the
    preview could be built queued the click and opened one preview per click."""
    from PyQt6.QtWidgets import QDialog
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget
    import tools.multi_open as MO
    assert not hasattr(MO, "MultiOpenDialog"), "the modal chooser is back"

    class FakeWindow:
        def __init__(self, vp): self.viewer = vp
        def _switch(self, idx): pass
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    paths = [FX["normal"], FX["single"], FX["framed"]]
    MAIN.MainWindow._open_multi(FakeWindow(vp), paths)
    _app.processEvents()

    assert not [w for w in _app.topLevelWidgets()
                if isinstance(w, QDialog) and w.isVisible()], "a popup appeared"
    w = vp.tabs.currentWidget()
    assert isinstance(w, MergeOrderWidget), "the preview did not open"
    assert w._grid.get_paths() == paths
    assert "Zusammenfuehren" in w._btn_go.text() and w._btn_go.isEnabled()
    assert "Einzeln" in w._btn_single.text() and w._btn_single.isEnabled()

    # a repeat request for the same files raises that tab, it does not stack
    for _ in range(4):
        MAIN.MainWindow._open_multi(FakeWindow(vp), paths)
    _app.processEvents()
    previews = [vp.tabs.widget(i) for i in range(vp.tabs.count())
                if isinstance(vp.tabs.widget(i), MergeOrderWidget)]
    assert len(previews) == 1, f"{len(previews)} preview tabs, expected 1"

    # a single file still bypasses the preview entirely
    MAIN.MainWindow._open_multi(FakeWindow(vp), [FX["color"]])
    _app.processEvents()
    assert getattr(vp.tabs.currentWidget(), "pdf_path", None) == FX["color"]
    vp.deleteLater()


def test_preview_opens_files_separately():
    """"Einzeln oeffnen" converts the same way the merge does, but gives every
    file its own tab."""
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget, PdfTab
    paths = [FX["normal"], FX["single"], FX["image"]]   # 5 + 1 + 1(converted)
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    _app.processEvents()
    w = vp.tabs.currentWidget()
    w._do_open_separately()
    assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                       for i in range(vp.tabs.count()))), \
        "open-separately never finished"
    opened = [vp.tabs.widget(i).pdf_path for i in range(vp.tabs.count())
              if isinstance(vp.tabs.widget(i), PdfTab)]
    assert len(opened) == 3, f"{len(opened)} tabs, expected 3"
    assert opened[0] == FX["normal"] and opened[1] == FX["single"]
    assert opened[2].endswith(".pdf") and os.path.isfile(opened[2]), \
        "the image was not converted"
    for p in opened:
        assert len(PdfReader(p, strict=False).pages) >= 1
    vp.deleteLater()


def _send_key(target, key, mods=None):
    """Deliver a key the way a real press arrives: ShortcutOverride first, so
    app-level filters and focused widgets get their say."""
    from PyQt6.QtCore import Qt as _Qt, QEvent as _QE
    from PyQt6.QtGui import QKeyEvent
    mods = mods if mods is not None else _Qt.KeyboardModifier.NoModifier
    for t in (_QE.Type.ShortcutOverride, _QE.Type.KeyPress, _QE.Type.KeyRelease):
        _app.sendEvent(target, QKeyEvent(t, key, mods))
    _spin(5, 0.0)


def test_open_dialogs_offer_every_supported_format():
    """The Datei menu filtered to *.pdf while the viewer happily converts images
    and Office documents on open, so the picker hid files the app can handle.
    Every open path now draws from one list."""
    from tools.multi_open import (file_dialog_filter, ALL_EXTS, IMAGE_EXTS,
                                  OFFICE_EXTS, classify)
    flt = file_dialog_filter()
    first = flt.split(";;")[0]
    for ext in ALL_EXTS:
        assert "*" + ext in first, f"{ext} missing from the dialog filter"
        assert classify("x" + ext), f"{ext} is offered but classify() rejects it"
    # formats verified against the actual converters on this machine
    for ext in (".gif", ".webp", ".png", ".jpg", ".tif", ".bmp"):
        assert ext in IMAGE_EXTS, f"{ext} should be a supported image"
    for ext in (".txt", ".csv", ".html", ".svg", ".docx", ".odt"):
        assert ext in OFFICE_EXTS, f"{ext} should be LibreOffice-convertible"
    assert "(*)" in flt, "no all-files escape hatch"

    # The Datei menu must hand that same filter to the picker. Captured from the
    # real call rather than read out of the source, so a second hard-coded
    # filter creeping back in is caught.
    from PyQt6.QtWidgets import QFileDialog
    seen = []
    orig_one, orig_many = QFileDialog.getOpenFileName, QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileName  = staticmethod(lambda *a, **k: (seen.append(a[3]), ("", ""))[1])
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (seen.append(a[3]), ([], ""))[1])
    try:
        class _W:
            _switch = lambda self, i: None
            viewer  = None
        MAIN.MainWindow._open_dialog(_W())
        MAIN.MainWindow._open_multi_dialog(_W())
    finally:
        QFileDialog.getOpenFileName, QFileDialog.getOpenFileNames = orig_one, orig_many
    assert len(seen) == 2, "the menu actions did not open a picker"
    for used in seen:
        for ext in (".png", ".docx", ".txt", ".gif"):
            assert "*" + ext in used, \
                f"the Datei menu picker hides {ext} — filter was {used[:80]!r}"


def test_opening_a_bad_file_reports_instead_of_crashing():
    """Every one of these used to end in an unhandled exception inside a Qt slot
    — which aborts the process — or, for a corrupt PDF, in a blank tab with no
    model and no explanation."""
    from PyQt6.QtWidgets import QMessageBox, QFileDialog
    from PyQt6.QtCore import QSettings
    from tools.page_viewer import PageViewerPanel
    said = []
    orig_w, orig_c = QMessageBox.warning, QMessageBox.critical
    QMessageBox.warning  = staticmethod(lambda *a, **k: said.append(a[1]))
    QMessageBox.critical = staticmethod(lambda *a, **k: said.append(a[1]))
    tmp = tempfile.mkdtemp(dir=_TMP)
    try:
        vp = PageViewerPanel(); vp.resize(800, 600); vp.show()

        cases = []
        missing = os.path.join(tmp, "gone.pdf")
        cases.append(("missing file", missing))
        odd = os.path.join(tmp, "notes.xyz"); open(odd, "w").write("hi")
        cases.append(("unknown extension", odd))
        broken = os.path.join(tmp, "broken.pdf")
        open(broken, "wb").write(b"%PDF-1.4 truncated, not a real pdf")
        cases.append(("corrupt pdf", broken))
        cases.append(("encrypted pdf", FX["encrypted"]))

        for label, path in cases:
            before = len(said)
            vp.open_file(path)
            _spin(10, 0.01)
            assert len(said) > before, f"{label}: opened with no message at all"
        assert vp.tabs.count() == 0, \
            f"{vp.tabs.count()} tab(s) opened for files that cannot be read"

        # a failed open must not become the file reopened at next startup
        s = QSettings("CopyShop", "PDFSuite")
        s.setValue("general/last_file", "SENTINEL")
        vp.open_file(broken); _spin(10, 0.01)
        assert s.value("general/last_file") == "SENTINEL", \
            "a file that failed to open was remembered for next startup"
        vp.deleteLater()
    finally:
        QMessageBox.warning, QMessageBox.critical = orig_w, orig_c


def test_ocr_produces_a_searchable_pdf():
    """Without ocrmypdf the panel dumped a .txt file — but what it offers is a
    searchable PDF. Tesseract writes exactly that; the pages just have to be
    rendered, OCR'd and stitched back together."""
    import shutil as _sh
    from tools.all_tools import _run_ocr, tesseract_langs
    if not _sh.which("tesseract"):
        return "skipped — no tesseract"
    langs = tesseract_langs()
    lang = "deu" if "deu" in langs else (langs[0] if langs else None)
    if not lang:
        return "skipped — no language packs"

    tmp = tempfile.mkdtemp(dir=_TMP)
    # a "scan": an image-only PDF with no text layer
    from PIL import ImageDraw, ImageFont
    import img2pdf
    def _font(sz):
        for p in ("/usr/share/fonts/TTF/DejaVuSans.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            if os.path.exists(p): return ImageFont.truetype(p, sz)
        return ImageFont.load_default()
    png = os.path.join(tmp, "scan.png")
    im = Image.new("RGB", (1240, 1754), "white")
    ImageDraw.Draw(im).text((100, 200), "Rechnung 4711", fill="black", font=_font(64))
    im.save(png)
    scan = os.path.join(tmp, "scan.pdf")
    with open(scan, "wb") as f: f.write(img2pdf.convert(png))
    assert not _pdfium_page_text(scan)[0], "fixture already has a text layer"

    out = os.path.join(tmp, "ocr.pdf")
    result, summary = _run_ocr(scan, out, lang, False, False, lambda m: None)
    assert result.endswith(".pdf"), f"OCR returned {result}, expected a PDF"
    assert os.path.isfile(result) and os.path.getsize(result) > 0
    assert len(PdfReader(result, strict=False).pages) == 1
    text = _pdfium_page_text(result)[0]
    assert "4711" in text, f"no searchable text layer (got {text!r})"

    # a language that is not installed says so, instead of failing obscurely
    bogus = [c for c in ("fra", "spa", "ita", "rus") if c not in langs]
    if bogus:
        try:
            _run_ocr(scan, os.path.join(tmp, "x.pdf"), bogus[0], False, False,
                     lambda m: None)
            assert False, "a missing language pack was not reported"
        except RuntimeError as e:
            assert "Sprachpaket" in str(e) or "Language pack" in str(e), str(e)
    return f"searchable PDF via tesseract ({lang})"


def test_merge_preview_hides_the_app_sidebar():
    """The preview brings its own sidebar. The app's tool nav must step aside
    while it is up — the two used to stack, and the left one offered tools that
    do not apply here. It has to come back when the preview closes."""
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    shown = []
    vp.hide_sidebar = lambda: shown.append(False)
    vp.show_sidebar = lambda: shown.append(True)

    vp.show_merge_tab([FX["normal"], FX["single"]])
    _spin(20, 0.01)
    assert isinstance(vp.tabs.currentWidget(), MergeOrderWidget)
    assert shown and shown[-1] is False, f"sidebar not hidden ({shown})"

    vp.tabs.currentWidget()._do_cancel()
    _spin(20, 0.01)
    assert shown[-1] is True, f"sidebar not restored after cancel ({shown})"

    # …and it stays away only for that tab
    vp.open_file(FX["normal"]); _spin(30, 0.01)
    vp.show_merge_tab([FX["single"], FX["framed"]]); _spin(20, 0.01)
    assert shown[-1] is False, "sidebar not hidden for a second preview"
    vp.tabs.setCurrentIndex(0); _spin(20, 0.01)
    assert shown[-1] is True, "sidebar not restored when switching to a PDF tab"
    vp.deleteLater()


def test_merge_preview_thumbnails_behave_like_the_page_manager():
    """The file grid is the page grid for files, so the same gestures apply:
    clicking empty space clears the selection, and the drop marker rises through
    the row without bouncing as the cursor passes the last thumbnail."""
    from PyQt6.QtCore import Qt as _Qt, QPoint, QPointF, QEvent as _QE
    from PyQt6.QtGui import QMouseEvent
    from tools.page_viewer import MergeOrderWidget, MARGIN, GAP
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    w = MergeOrderWidget(paths); w.resize(950, 650); w.show()
    _spin(40, 0.02)
    g = w._grid

    # clicking empty background unpicks
    g._selected = {0, 2}; g._update_selection()
    empty = QPointF(g.width() - 25, g.height() - 25)
    for t in (_QE.Type.MouseButtonPress, _QE.Type.MouseButtonRelease):
        _app.sendEvent(g, QMouseEvent(t, empty, empty, _Qt.MouseButton.LeftButton,
                                      _Qt.MouseButton.LeftButton,
                                      _Qt.KeyboardModifier.NoModifier))
    _spin(5, 0.0)
    assert not g._selected, f"clicking empty space left {sorted(g._selected)} picked"

    # drop position never goes backwards as the cursor sweeps right
    n = len(g.get_paths())
    cell_w = g._card_w + 16 + GAP
    seq = [g._pos_from_point(QPoint(x, MARGIN + 10))
           for x in range(MARGIN, MARGIN + int((n + 2) * cell_w), 7)]
    assert seq == sorted(seq), f"drop position bounces: {seq}"
    assert seq[-1] == n, f"sweeping past the end gives {seq[-1]}, expected {n}"
    assert max(seq) <= n, f"drop position {max(seq)} exceeds {n} files"


def test_merge_preview_answers_the_page_manager_shortcuts():
    """Ctrl+C / X / V / Z did nothing here while working one view over, because
    this view registered three lone QShortcuts instead of the shared filter."""
    from PyQt6.QtCore import Qt as _Qt
    from tools.page_viewer import MergeOrderWidget
    C = _Qt.KeyboardModifier.ControlModifier
    paths = [FX["normal"], FX["single"], FX["framed"]]
    w = MergeOrderWidget(paths); w.resize(950, 650); w.show()
    _spin(40, 0.02)
    g = w._grid
    g.setFocus()

    _send_key(g, _Qt.Key.Key_A, C)
    assert len(g._selected) == 3, "Ctrl+A"
    _send_key(g, _Qt.Key.Key_D, C)
    assert not g._selected, "Ctrl+D"

    g._selected = {1}; g._update_selection()
    _send_key(g, _Qt.Key.Key_C, C)
    _send_key(g, _Qt.Key.Key_V, C)
    assert len(g.get_paths()) == 4, f"Ctrl+C/Ctrl+V gave {len(g.get_paths())} files"
    assert g.get_paths().count(FX["single"]) == 2, "the copied file was not pasted"

    _send_key(g, _Qt.Key.Key_X, C)
    assert len(g.get_paths()) == 3, "Ctrl+X did not cut"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert len(g.get_paths()) == 4, "Ctrl+Z did not undo the cut"

    g._selected = {0}; g._update_selection()
    _send_key(g, _Qt.Key.Key_Delete)
    assert len(g.get_paths()) == 3, "Delete"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert len(g.get_paths()) == 4, "Ctrl+Z did not undo the delete"

    # a drag is undoable too
    order = g.get_paths()
    g._selected = {0}; g.handle_drop(0, 3)
    _spin(5, 0.0)
    assert g.get_paths() != order, "drag did not reorder"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert g.get_paths() == order, "Ctrl+Z did not undo the drag"

    # typing in the selection box keeps its own Ctrl+A
    w.sel_edit.setFocus(); w.sel_edit.setText("1,2"); _spin(5, 0.0)
    before = set(g._selected)
    _send_key(w.sel_edit, _Qt.Key.Key_A, C)
    assert g._selected == before, "Ctrl+A in the text field hijacked the file selection"


def test_merge_preview_has_no_rotate_button():
    """Rotating a whole file means nothing, and the zoom-reset button was
    wearing the page manager's rotate-left glyph, so it read as one. Same fix
    the page manager already had: label it 1:1."""
    from tools.page_viewer import MergeOrderWidget, PageGrid
    w = MergeOrderWidget([FX["normal"], FX["single"]])
    labels = [b.text() for b in w._zoom_btns]
    assert "↺" not in labels and "↻" not in labels, \
        f"the merge view still shows a rotate glyph: {labels}"
    assert "1:1" in labels, f"zoom reset is missing: {labels}"
    assert not hasattr(w._grid, "rotate_selected"), \
        "the file grid should not offer rotation at all"
    # the page manager keeps its real rotate buttons
    assert hasattr(PageGrid, "rotate_selected")


def test_preview_reports_files_it_could_not_convert():
    """A file that fails to convert is dropped from the merge. The user has to
    be told which one, or they get a document quietly missing pages — the
    removed chooser dialog was the only thing that ever showed those errors."""
    from PyQt6.QtWidgets import QMessageBox
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget
    broken = os.path.join(_TMP, "broken.png")
    with open(broken, "wb") as f:
        f.write(b"this is not an image")
    paths = [FX["normal"], broken, FX["single"]]

    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    _app.processEvents()
    warned = []
    orig = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    try:
        vp.tabs.currentWidget()._confirm()
        assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                           for i in range(vp.tabs.count()))), \
            "the merge never completed"
    finally:
        QMessageBox.warning = orig
    out = vp.tabs.currentWidget().pdf_path
    assert len(PdfReader(out, strict=False).pages) == 6, "the good files must still merge"
    assert warned, "the unconvertible file was dropped without a word"
    assert "broken.png" in " ".join(str(x) for x in warned[0]), warned[0]
    vp.deleteLater()


def test_preview_survives_fast_clicks():
    """Hammering the preview must not start a second job behind the first.

    Two conversions at once used to overwrite the panel's single worker
    attribute, dropping the last reference to a QThread that was still running
    — which Qt answers by aborting the process. Two previews converting side by
    side must both finish, each into its own output."""
    from tools.page_viewer import PageViewerPanel, MergeOrderWidget, PdfTab
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    set_a = [FX["normal"], FX["image"]]      # the image forces real work
    set_b = [FX["single"], FX["framed"], FX["image"]]
    vp.show_merge_tab(set_a); vp.show_merge_tab(set_b)
    previews = [vp.tabs.widget(i) for i in range(vp.tabs.count())
                if isinstance(vp.tabs.widget(i), MergeOrderWidget)]
    assert len(previews) == 2, "different file sets each need their own tab"
    a, b = previews
    assert a.tmp_dir != b.tmp_dir, "previews must not share a conversion dir"

    a._confirm()
    a._confirm(); a._do_open_separately(); a._do_cancel()   # all ignored: busy
    assert len(vp._workers) == 1, f"{len(vp._workers)} workers after spamming one tab"
    assert not a._btn_go.isEnabled() and not a._btn_single.isEnabled()
    b._confirm()                                            # second job, in parallel
    assert len(vp._workers) == 2, "the running worker was dropped"

    assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                       for i in range(vp.tabs.count()))), \
        "the merges never completed"
    outs = [vp.tabs.widget(i).pdf_path for i in range(vp.tabs.count())
            if isinstance(vp.tabs.widget(i), PdfTab)]
    assert len(outs) == 2, f"{len(outs)} merged tabs, expected 2"
    assert len(set(outs)) == 2, "both merges wrote to the same file"
    pages = sorted(len(PdfReader(o, strict=False).pages) for o in outs)
    assert pages == [3, 6], f"merged page counts {pages}, expected [3, 6]"
    _settle(vp, lambda: not vp._workers, tries=100)
    assert not vp._workers, "finished workers were never released"
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
raises = []
_raise = win._raise_to_front
def _raise_to_front(activation_token=""):
    raises.append(activation_token)
    print("TOKEN", activation_token or "-", "x%d" % len(raises), flush=True)
    _raise(activation_token)
win._raise_to_front = _raise_to_front
# open_paths defers the actual open to the next event-loop turn, so report the
# tab count from there rather than straight after the call.
_fwd = win._open_forwarded
def _open_forwarded(paths):
    _fwd(paths)
    if len(paths) == 1:
        print("TABS", win.viewer.tabs.count(), "raises=%d" % len(raises), flush=True)
win._open_forwarded = _open_forwarded
MAIN._listen_for_open_requests(win)
print("READY", int(win._ipc_server.isListening()), flush=True)
QTimer.singleShot(20000, lambda: os._exit(2))
app.exec()
'''


_EXIT_SCRIPT = '''
import sys, os
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.argv = ["copyshop", {src!r}]
import main as MAIN
MAIN._IPC_KEY = {key!r}          # own socket, so a real running app is untouched
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

# Quit the way a user does, once the window is up and rendering.
_show = MAIN.MainWindow.show
def show(self):
    _show(self)
    QTimer.singleShot(1500, QApplication.instance().quit)
MAIN.MainWindow.show = show
try:
    MAIN.main()
except SystemExit as e:
    print("RC", e.code, flush=True)
'''


def test_app_exits_without_crashing():
    """Quitting must not dump core.

    The widget tree used to survive until interpreter finalisation, where PyQt's
    own cleanup_on_exit atexit hook destroyed it — that walk dereferenced a
    wrapper whose C++ object had already gone and segfaulted inside
    sip_api_get_address. It hit *every* quit, and only showed up as a core dump
    after the window had already vanished. Has to run as a real process: the
    fault is in how this one shuts down."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(_TMP, "exit_host.py")
    with open(script, "w") as f:
        f.write(_EXIT_SCRIPT.format(repo=repo, src=FX["normal"],
                                    key=f"copyshop_exit_{os.getpid()}"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    runs = []
    for _ in range(3):          # the crash was racy — one clean run proves little
        p = subprocess.run([sys.executable, "-u", script], env=env,
                           capture_output=True, text=True, timeout=120)
        runs.append(p.returncode)
    bad = [rc for rc in runs if rc != 0]
    assert not bad, (f"app exited with {runs} "
                     f"(negative = killed by signal, -11 = SIGSEGV)")
    return f"{len(runs)} clean exits"


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
        # one file → a tab in the window that is already open, and the launcher's
        # XDG activation token travels with it. Without that token a Wayland
        # compositor refuses to let the running instance raise itself, which is
        # why the file used to open silently in the background.
        os.environ["XDG_ACTIVATION_TOKEN"] = "tok-abc123"
        try:
            assert MAIN._forward_to_running_instance([FX["single"]]) is True
        finally:
            os.environ.pop("XDG_ACTIVATION_TOKEN", None)
        got = _expect_line(host, "TOKEN").split()[1]
        assert got == "tok-abc123", f"activation token not forwarded (got {got!r})"
        tab_line = _expect_line(host, "TABS").split()
        tabs = int(tab_line[1])
        # tabs == 2 also proves the token line was not mistaken for a path.
        assert tabs == 2, f"forwarded file did not become a second tab ({tabs})"
        # Exactly one raise. A second, tokenless requestActivate() landing right
        # behind the good one is what left the window blinking instead of
        # coming forward on Wayland.
        raises = int(tab_line[2].split("=")[1])
        assert raises == 1, f"window was raised {raises}x for one delivery"
    finally:
        host.kill(); host.wait(timeout=10)
        MAIN._IPC_KEY = real_key


def test_open_paths_routes_by_count():
    """One file goes straight to a tab, several go to the sort/merge preview.
    Driven against the real method with a stand-in window, so it stays
    deterministic (a second MainWindow in this process is not)."""
    class FakeWindow:
        # The real _open_forwarded, so the deferred hand-off is exercised too.
        _open_forwarded = MAIN.MainWindow._open_forwarded
        def __init__(self):
            self.raised = []; self.opened = []; self.multi = None
            self.viewer = self
        def _raise_to_front(self, activation_token=""):
            self.raised.append(activation_token)
        def _switch(self, idx): pass
        def open_file(self, path): self.opened.append(path)
        def _open_multi(self, files): self.multi = list(files)

    def _deliver(w, paths, token=""):
        MAIN.MainWindow.open_paths(w, paths, token)
        _spin(10, 0.0)      # open_paths defers the open by one event-loop turn

    w = FakeWindow()
    _deliver(w, [FX["normal"]], "tok-1")
    assert w.opened == [FX["normal"]] and w.multi is None
    assert w.raised == ["tok-1"], f"raised {w.raised}, expected one raise with the token"

    w = FakeWindow()
    _deliver(w, [FX["normal"], FX["single"]])
    assert w.multi == [FX["normal"], FX["single"]] and w.opened == []
    assert len(w.raised) == 1, f"raised {len(w.raised)}x for one delivery"

    w = FakeWindow()
    _deliver(w, [os.path.join(_TMP, "does_not_exist.pdf")])
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


def _grey_fixture():
    """A page whose only colour is a half-point red mark, plus controls."""
    from reportlab.lib import colors
    W, H = A4
    p = os.path.join(_TMP, "grey_detect.pdf")
    c = canvas.Canvas(p, pagesize=A4)
    c.setFillGray(0); c.setFont("Helvetica", 40)
    c.drawString(60, 700, "BLACK ONLY"); c.showPage()                     # 1 grey
    c.setFillGray(0); c.drawString(60, 700, "TINY RED MARK")
    c.setFillColor(colors.HexColor("#ff0000"))
    c.rect(300, 400, 0.5, 0.5, fill=1, stroke=0); c.showPage()            # 2 colour
    c.setFillGray(0); c.drawString(60, 700, "FAINT TINT")
    c.setFillColor(colors.Color(0.62, 0.58, 0.58))
    c.rect(60, 100, 480, 500, fill=1, stroke=0); c.showPage()             # 3 faint
    c.setFillColor(colors.HexColor("#ff0000"))
    c.rect(60, 300, 400, 300, fill=1, stroke=0); c.showPage()             # 4 colour
    c.save()
    return p


def test_greyscale_detects_a_tiny_colour_mark():
    """A half-point red mark — a stamp, a logo dot, a coloured signature — must
    keep its page out of the conversion. The scan used to squash every page to
    128×128 first, which averaged that mark from a colour distance of 255 down to
    6, so at the default threshold of 20 it was invisible and the page was
    silently converted. That is a reprint."""
    src = _grey_fixture()
    _open(src)
    p = T.GrayscalePanel(); p.log.log = lambda *a, **k: None
    p._scan()
    assert len(p._page_data) == 4, f"scan produced {len(p._page_data)} pages"
    dist = [T._hist_stats(h, 20)[0] for h in p._page_data]
    assert dist[1] > 200, f"the tiny red mark reads as only {dist[1]}"
    assert dist[0] == 0, f"the black-only page is not neutral ({dist[0]})"

    p.mode_single.setChecked(True); p.thr.setValue(20); p._reclassify()
    assert 1 not in p._grey_pages, "page with the red mark would be converted"
    assert 3 not in p._grey_pages, "the saturated page would be converted"
    assert 0 in p._grey_pages, "the black-only page should convert"


def test_greyscale_threshold_slider_is_live():
    """Moving "Farb-Schwellwert" has to re-classify. The colour fraction used to
    be frozen with whatever the slider read when the document was scanned, so in
    the default ratio mode the control did nothing at all."""
    src = _grey_fixture()
    _open(src)
    p = T.GrayscalePanel(); p.log.log = lambda *a, **k: None
    p._scan()
    p.mode_ratio.setChecked(True); p.ratio.setValue(300)
    outcomes = set()
    for t in (1, 10, 30, 60, 80):
        p.thr.setValue(t); p._on_setting_changed()
        outcomes.add(tuple(sorted(p._grey_pages)))
    assert len(outcomes) > 1, "the threshold slider still changes nothing"


def test_greyscale_scan_recovers_from_failure():
    """A failed scan must not wedge the tool. The in-progress flag was set before
    the work and cleared after it, so an exception left it set and every later
    scan returned immediately for the rest of the session."""
    src = _grey_fixture()
    _open(src)
    p = T.GrayscalePanel(); p.log.log = lambda *a, **k: None
    boom = lambda: (_ for _ in ()).throw(RuntimeError("render exploded"))
    real = p._scan_impl
    p._scan_impl = boom
    try:
        try: p._scan()
        except RuntimeError: pass
    finally:
        p._scan_impl = real
    assert p._scanning is False, "the tool is wedged — no further scan can run"
    p._scan()
    assert len(p._page_data) == 4, "scanning did not recover"


def test_preflight_sees_a_tiny_colour_mark():
    """Preflight exists to stop a job going to press wrong, so its colour check
    must not miss a half-point mark. It squashed every page to 64×64 first, which
    averaged that mark away and reported "Keine Farbseiten erkannt" — a clean
    bill of health for a page that would cost a colour click."""
    from reportlab.lib import colors
    src = os.path.join(_TMP, "preflight_tiny.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFillGray(0); c.setFont("Helvetica", 30); c.drawString(60, 700, "MOSTLY BLACK")
    c.setFillColor(colors.HexColor("#ff0000"))
    c.rect(300, 400, 0.5, 0.5, fill=1, stroke=0)
    c.showPage(); c.save()

    _open(src)
    p = T.PreflightPanel(); p.log.log = lambda *a, **k: None
    p.chk_colour.setChecked(True)
    p._do_preflight()
    report = p.report.toPlainText()
    assert "Farbseiten:" in report, f"the colour mark was missed:\n{report}"


def _blackout_gs(inject_into_retry):
    """Patch subprocess.run so Ghostscript "succeeds" but blacks out page 2.

    That is what the real failure looks like from the outside: exit 0, empty
    stderr, right page count, ruined page."""
    import subprocess, pikepdf
    real = subprocess.run
    def faked(cmd, *a, **k):
        r = real(cmd, *a, **k)
        if "-o" in cmd:
            outp = cmd[cmd.index("-o") + 1]
            try:
                with pikepdf.open(outp, allow_overwriting_input=True) as pdf:
                    full = len(pdf.pages) >= 3
                    if full or inject_into_retry:
                        pdf.pages[1 if full else 0].contents_add(
                            pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f"))
                        pdf.save(outp)
            except Exception:
                pass
        return r
    subprocess.run = faked
    return lambda: setattr(subprocess, "run", real)


def _grey_job():
    from reportlab.lib import colors
    p = os.path.join(_TMP, "grey_job.pdf")
    c = canvas.Canvas(p, pagesize=A4)
    for i in range(3):
        c.setFillColor(colors.HexColor("#2277cc")); c.rect(40, 500, 500, 250, fill=1, stroke=0)
        c.setFillGray(0); c.setFont("Helvetica", 30)
        c.drawString(50, 430, f"CONTENT {i+1}"); c.showPage()
    c.save()
    return p


def _mean_luma(path, i):
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=0.3).to_pil().convert("L"); d.close()
    return sum(im.get_flattened_data()) / (im.size[0] * im.size[1])


def test_greyscale_never_ships_a_blacked_out_page():
    """Ghostscript can black out a transparency group while exiting 0 with an
    empty stderr and the right page count — nothing in the process result says
    anything is wrong, and it only shows on paper. Every converted page is
    therefore compared against the original before it is written, and a damaged
    one keeps its original content instead. A colour page is a nuisance; a black
    one is a reprint."""
    if not (shutil.which("gs") or shutil.which("gswin64c")):
        return "SKIP (no ghostscript)"
    gs = shutil.which("gs") or shutil.which("gswin64c")
    src = _grey_job()

    # The damaged page can be rescued by converting it on its own.
    restore = _blackout_gs(inject_into_retry=False)
    try:
        out = os.path.join(_TMP, "grey_guard1.pdf")
        res, msg = T._grey_vector(gs, src, out, {0, 1, 2}, 3, lambda m: None)
    finally:
        restore()
    assert _mean_luma(res, 1) > 100, "a blacked-out page reached the output"
    assert "nachkonvertiert" in msg, msg

    # ...and when the retry is damaged too, the original page is kept and said so.
    restore = _blackout_gs(inject_into_retry=True)
    try:
        out = os.path.join(_TMP, "grey_guard2.pdf")
        res, msg = T._grey_vector(gs, src, out, {0, 1, 2}, 3, lambda m: None)
    finally:
        restore()
    assert _mean_luma(res, 1) > 100, "a blacked-out page reached the output"
    assert "ACHTUNG" in msg and "2 (" in msg, f"damage not reported: {msg}"
    for i in (0, 2):
        assert _mean_luma(res, i) > 100, f"page {i+1} damaged too"
    return "rescued, then refused"


def test_greyscale_verification_passes_normal_pages():
    """The guard must not cost colour clicks by refusing pages that converted
    perfectly well — dense text, saturated blocks, a photo, transparency, a dark
    full-bleed cover and hairline anti-aliasing all have to come back clean."""
    if not (shutil.which("gs") or shutil.which("gswin64c")):
        return "SKIP (no ghostscript)"
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    gs = shutil.which("gs") or shutil.which("gswin64c")
    W, H = A4
    photo = os.path.join(_TMP, "grey_photo.png")
    im = Image.new("RGB", (300, 220))
    im.putdata([((x*7) % 256, (y*5) % 256, ((x+y)*3) % 256)
                for y in range(220) for x in range(300)])
    im.save(photo)

    src = os.path.join(_TMP, "grey_real.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFillGray(0); c.setFont("Helvetica", 11)
    for i in range(45):
        c.drawString(50, H-70-i*16, "Lorem ipsum dolor sit amet, consectetur. " * 2)
    c.showPage()
    for i, col in enumerate(["#ff0000", "#00ff00", "#0000ff", "#ffff00", "#000080"]):
        c.setFillColor(colors.HexColor(col)); c.rect(40, 700-i*130, 500, 110, fill=1, stroke=0)
    c.showPage()
    c.drawImage(ImageReader(photo), 60, 400, 470, 350); c.showPage()
    c.setFillColor(colors.HexColor("#2277cc")); c.rect(40, 500, 500, 250, fill=1, stroke=0)
    try: c.setFillAlpha(0.45)
    except Exception: pass
    c.setFillColor(colors.HexColor("#cc3322")); c.rect(150, 560, 300, 150, fill=1, stroke=0)
    try: c.setFillAlpha(1)
    except Exception: pass
    c.showPage()
    c.setFillColor(colors.HexColor("#101830")); c.rect(0, 0, W, H, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 40)
    c.drawString(60, 400, "DARK COVER"); c.showPage()
    c.setStrokeGray(0); c.setLineWidth(0.25)
    for i in range(60): c.line(40, 100+i*11, 550, 100+i*11)
    c.showPage(); c.save()

    out = os.path.join(_TMP, "grey_real_out.pdf")
    _, msg = T._grey_vector(gs, src, out, set(range(6)), 6, lambda m: None)
    assert "ACHTUNG" not in msg, f"legitimate pages were refused:\n{msg}"
    return "6 pages, no false positives"


def test_cmyk_never_ships_a_blacked_out_page():
    """The CMYK conversion runs the same Ghostscript colour machinery, so it
    carries the same risk — and for a prepress file nobody notices until it is on
    press. It used to write Ghostscript's output straight to the chosen path and
    only check which colour spaces were named, never whether the page still
    looked like the page."""
    if not shutil.which("gs"):
        return "SKIP (no ghostscript)"
    src = _grey_job()
    _open(src)
    restore = _blackout_gs(inject_into_retry=True)
    try:
        p = T.ColourProfilePanel(); p.log.log = lambda *a, **k: None
        out = os.path.join(_TMP, "cmyk_guard.pdf")
        p.save_pdf = lambda *a, **k: out
        p.open_result = lambda *a, **k: None
        msg = p._run_action()
    finally:
        restore()
    assert _mean_luma(out, 1) > 100, "a blacked-out page reached the CMYK output"
    assert "ACHTUNG" in msg, f"damage not reported:\n{msg}"
    return "refused"


def test_print_blackout_check_tolerates_scaling():
    """The print path scales, fits and re-centres, so its blackout check compares
    mean brightness rather than pixels — a per-pixel diff would flag healthy
    pages as damaged and quietly print everything unconverted."""
    from reportlab.lib import colors
    from tools.page_viewer import _gs_blacked_out
    import pikepdf

    def make(name, black_page=None, scaled=False):
        p = os.path.join(_TMP, name)
        c = canvas.Canvas(p, pagesize=A4)
        for i in range(4):
            if scaled:
                c.saveState(); c.translate(20, 20); c.scale(0.93, 0.93)
            c.setFillColor(colors.HexColor("#2277cc"))
            c.rect(40, 500, 500, 250, fill=1, stroke=0)
            c.setFillGray(0); c.setFont("Helvetica", 30)
            c.drawString(50, 430, f"PAGE {i+1}")
            if scaled: c.restoreState()
            c.showPage()
        c.save()
        if black_page is not None:
            with pikepdf.open(p, allow_overwriting_input=True) as pdf:
                pdf.pages[black_page].contents_add(
                    pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f"))
                pdf.save(p)
        return p

    before = make("pg_before.pdf")
    assert _gs_blacked_out(before, make("pg_same.pdf")) == []
    assert _gs_blacked_out(before, make("pg_fit.pdf", scaled=True)) == [], \
        "scaling was mistaken for damage"
    assert _gs_blacked_out(before, make("pg_bad.pdf", black_page=2)) == [2]
    assert _gs_blacked_out(before, os.path.join(_TMP, "pg_missing.pdf")) is None


def _plain_render_ink(path, page=0, scale=1):
    """Dark pixels with form rendering OFF — what actually goes on paper."""
    d = pdfium.PdfDocument(path)
    im = d[page].render(scale=scale).to_pil().convert("L"); d.close()
    return sum(1 for v in im.get_flattened_data() if v < 128)


def test_form_flatten_keeps_the_filled_values():
    """"Reduzieren (fuer Druck)" used to be `del page["/Annots"]`, which deletes
    the widget annotations — and the widget is where the value is *drawn*. The
    value stayed in the AcroForm dictionary, so the file still looked filled to
    anything inspecting fields, while the page printed blank. Ticking the box
    that says "for printing" produced an empty form."""
    from PyQt6.QtWidgets import QLineEdit
    src = os.path.join(_TMP, "form_src.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFont("Helvetica", 14); c.drawString(60, 700, "Name:")
    c.acroForm.textfield(name="name", x=140, y=694, width=300, height=22,
                         borderStyle="inset", forceBorder=True, value="")
    c.showPage(); c.save()
    empty_ink = _plain_render_ink(src)

    _open(src)
    p = T.FormsPanel(); p.log.log = lambda *a, **k: None
    p._load()
    for w in p._fields.values():
        if isinstance(w, QLineEdit): w.setText("MAX MUSTERMANN")
    p.flatten.setChecked(True)
    out = os.path.join(_TMP, "form_flat.pdf")
    p.save_pdf = lambda *a, **k: out
    p.open_result = lambda *a, **k: None
    p._run_action()

    assert _plain_render_ink(out) > empty_ink, \
        "the flattened form prints blank — the typed value was lost"
    r = PdfReader(out)
    assert "/Annots" not in r.pages[0], "widgets are still interactive"
    assert "/AcroForm" not in r.trailer["/Root"], "the form was not flattened"


def test_compress_refuses_a_damaged_result():
    """Compression is judged by the file getting smaller, which makes "lost
    content" indistinguishable from "worked well". The result is checked against
    the source before it is handed over."""
    if not shutil.which("gs"):
        return "SKIP (no ghostscript)"
    from reportlab.lib import colors
    from reportlab.lib.utils import ImageReader
    photo = os.path.join(_TMP, "cmp_photo.png")
    im = Image.new("RGB", (700, 500))
    im.putdata([((x*3) % 256, (y*7) % 256, ((x*y)//7) % 256)
                for y in range(500) for x in range(700)])
    im.save(photo)
    src = os.path.join(_TMP, "cmp_src.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(3):
        c.drawImage(ImageReader(photo), 40, 380, 500, 380)
        c.setFillGray(0); c.setFont("Helvetica", 20)
        c.drawString(50, 300, f"Page {i+1} body text")
        c.setFillColor(colors.HexColor("#cc2244")); c.rect(50, 120, 300, 120, fill=1, stroke=0)
        c.showPage()
    c.save()
    _open(src)

    def compress(patch=None, tag="ok"):
        p = T.CompressPanel(); p.log.log = lambda *a, **k: None
        p.preset.setCurrentIndex(1); p.gs_check.setChecked(True)
        o = os.path.join(_TMP, f"cmp_{tag}.pdf")
        p.save_pdf = lambda *a, **k: o
        p.open_result = lambda *a, **k: None
        if patch is None:
            return p._run_action()
        restore = _damage_gs(patch)
        try:
            return p._run_action()
        finally:
            restore()

    # Honest compression at every preset must go through untouched.
    for i in range(4):
        p = T.CompressPanel(); p.log.log = lambda *a, **k: None
        p.preset.setCurrentIndex(i); p.gs_check.setChecked(True)
        o = os.path.join(_TMP, f"cmp_p{i}.pdf")
        p.save_pdf = lambda *a, **k: o
        p.open_result = lambda *a, **k: None
        p._run_action()          # must not raise
        assert len(PdfReader(o).pages) == 3

    import pikepdf
    for patch, expect in (
            (lambda pdf: pdf.pages.__delitem__(1), "Seitenzahl"),
            (lambda pdf: pdf.pages[1].contents_add(
                pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f")), "beschaedigt")):
        try:
            compress(patch, tag="bad")
            assert False, "a damaged compression was accepted"
        except AssertionError:
            raise
        except Exception as e:
            assert expect in str(e), f"unexpected error: {e}"
    return "4 presets clean, 2 failures caught"


def _damage_gs(patch):
    """Let Ghostscript run, then corrupt its output — exit code stays 0."""
    import subprocess, pikepdf
    real = subprocess.run
    def faked(cmd, *a, **k):
        r = real(cmd, *a, **k)
        if "-o" in cmd:
            outp = cmd[cmd.index("-o") + 1]
            try:
                with pikepdf.open(outp, allow_overwriting_input=True) as pdf:
                    patch(pdf); pdf.save(outp)
            except Exception:
                pass
        return r
    subprocess.run = faked
    return lambda: setattr(subprocess, "run", real)


def _print_dialog(n_pages=10, name="print_src.pdf"):
    from tools.page_viewer import PdfTab, PrintDialog
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 90); c.drawCentredString(300, 400, f"P{i+1}"); c.showPage()
    c.save()
    tab = PdfTab(src)
    return tab, PrintDialog(tab.pdf_path, tab.model, tab)


def test_print_spools_exactly_what_was_asked_for():
    """The job handed to the spooler has to be the pages the operator picked, in
    the order and rotation the viewer is showing — including page-manager edits
    that have not been saved to the file yet."""
    tab, dlg = _print_dialog()
    def spool(tag):
        out = os.path.join(_TMP, f"spool_{tag}.pdf")
        dlg._write_subset_pdf(dlg._get_pages(), out)
        return _page_labels(out), out

    dlg.radio_range.setChecked(True); dlg.range_edit.setText("3-5, 8")
    assert spool("range")[0] == ["P3", "P4", "P5", "P8"]
    assert [p + 1 for p in dlg._preview_pages()] == [3, 4, 5, 8], \
        "the preview disagrees with the job"

    tab.single._current = 6
    dlg.radio_current.setChecked(True)
    assert spool("current")[0] == ["P7"], "'current page' printed the wrong sheet"

    dlg.radio_all.setChecked(True)
    assert len(spool("all")[0]) == 10

    # Unsaved reorder + rotation must reach the printer.
    tab.model.move(0, 10)
    tab.model.selected = {tab.model.order[0]}
    tab.model.rotate_selected(90)
    dlg.radio_range.setChecked(True); dlg.range_edit.setText("1-3")
    labels, out = spool("edited")
    assert labels == ["P2", "P3", "P4"], f"page-manager order ignored: {labels}"
    rot = [int(p.get("/Rotate", 0) or 0) for p in PdfReader(out).pages]
    assert rot == [90, 0, 0], f"page-manager rotation ignored: {rot}"


def test_print_preview_and_job_agree_on_a_bad_range():
    """The preview used to clamp an out-of-range entry while the job rejected it,
    so "5-99" on a ten-page file previewed six printable pages and then refused
    to print. Showing a job that cannot run is its own kind of lie."""
    tab, dlg = _print_dialog()
    dlg.radio_range.setChecked(True)
    dlg.range_edit.setText("5-99")
    assert dlg._get_pages() is None, "the job accepted an out-of-range request"
    assert not dlg._preview_pages(), "the preview promised pages that will not print"
    dlg.range_edit.setText("4-6")
    assert dlg._get_pages() == [3, 4, 5]
    assert [p + 1 for p in dlg._preview_pages()] == [4, 5, 6]


def test_print_reports_the_sheets_it_actually_sent():
    """Unreadable pages are dropped from the job, so counting the requested
    pages told the operator more sheets were coming than the printer got — while
    listing the skipped ones in the same sentence."""
    tab, dlg = _print_dialog()
    dlg._progress = None
    dlg._after_print_close = lambda: None
    dlg._finish(list(range(10)), 2, [3, 7])
    text = dlg.status_lbl.text()
    assert "8" in text and "16" in text, \
        f"expected 8 pages x 2 copies = 16 sheets, got: {text}"
    assert "3, 7" in text or "[3, 7]" in text, f"skipped pages not named: {text}"


def test_print_never_destroys_colour_in_the_spooled_file():
    """Choosing greyscale must ask the *printer* for monochrome, not bake it
    into the job.

    Qt reports defaultColorMode() == GrayScale for driverless/IPP queues that
    are plainly colour (an EPSON ET-8500 and two Xerox presses on this machine),
    so the dialog opened in Graustufen — and greyscale was applied by converting
    the PDF with Ghostscript before spooling. The colour was gone for good: a job
    re-routed to a colour printer, or settings chosen on another machine, could
    not bring it back. Every mode now leaves the file's colour intact and
    expresses the choice as a CUPS option."""
    import subprocess
    from reportlab.lib import colors
    src = os.path.join(_TMP, "print_colour.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFillColor(colors.HexColor("#d02030")); c.rect(60, 500, 460, 220, fill=1, stroke=0)
    c.setFillColor(colors.HexColor("#1f77d0")); c.rect(60, 250, 460, 220, fill=1, stroke=0)
    c.showPage(); c.save()

    def saturation(path):
        d = pdfium.PdfDocument(path)
        pil = d[0].render(scale=0.4).to_pil().convert("RGB"); d.close()
        return T._hist_stats(T._colour_histogram(pil), 20)[0]

    src_sat = saturation(src)
    assert src_sat > 100, "fixture is not colourful enough to test with"

    tab, dlg = _print_dialog(1, "print_colour_tab.pdf")
    dlg = None
    from tools.page_viewer import PdfTab, PrintDialog
    tab = PdfTab(src)
    dlg = PrintDialog(tab.pdf_path, tab.model, tab)

    assert dlg.color_combo.isEnabled(), \
        "the colour control is locked — the user cannot override a wrong guess"
    assert dlg.color_combo.currentData() == "auto", \
        "the dialog does not open on 'printer decides'"

    captured = {}
    real = subprocess.run
    def spy(cmd, *a, **k):
        if cmd and cmd[0] == "lp":
            captured["opts"] = [x for i, x in enumerate(cmd) if cmd[i-1] == "-o"]
            keep = os.path.join(_TMP, f"spooled_{captured['tag']}.pdf")
            shutil.copyfile(cmd[-1], keep)
            captured["file"] = keep
            class R: returncode = 0; stdout = "request id is test-1"; stderr = ""
            return R()
        return real(cmd, *a, **k)

    expected = {"auto": [], "color": ["print-color-mode=color"],
                "mono":  ["print-color-mode=monochrome", "ColorModel=Gray"]}
    for mode, want in expected.items():
        captured.clear(); captured["tag"] = mode
        subprocess.run = spy
        try:
            dlg._print_via_gs([0], 1, mode, False, False, "long", 0,
                              "test-printer", 0, "A4", 0, 3.0, lambda m: None)
        finally:
            subprocess.run = real
        got = [o for o in captured["opts"]
               if "color" in o.lower() or o.startswith("ColorModel")]
        assert got == want, f"{mode}: sent {got}, expected {want}"
        assert saturation(captured["file"]) == src_sat, \
            f"{mode}: the spooled file lost its colour — it cannot be recovered"
    return "auto / color / mono, colour intact"


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


def _fine_detail_pdf(name="fine_detail.pdf"):
    """A page of small text — interpolation is measurable on it."""
    p = os.path.join(_TMP, name)
    if not os.path.exists(p):
        c = canvas.Canvas(p, pagesize=A4)
        c.setFont("Helvetica", 9)
        for y in range(40, 800, 11):
            c.drawString(30, y, "The quick brown fox jumps over the lazy dog 0123456789 " * 2)
        c.showPage(); c.save()
    return p


def _open_single_view(path, w=1000, h=760):
    """A viewer showing `path`, settled on its first real render."""
    from tools.page_viewer import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(w, h); vp.show()
    vp.open_file(path)
    _settle(vp, lambda: vp.tabs.count() and vp.tabs.currentWidget().single._last_pm,
            tries=300)
    sv = vp.tabs.currentWidget().single
    _settle(vp, lambda: sv._render_task is None
            and not getattr(sv, "_showing_provisional", False), tries=300)
    return vp, sv


def _target_scale_of(sv, zoom):
    pad = 16
    fit = min((sv._view.width() - pad) / sv._page_w_pt,
              (sv._view.height() - pad) / sv._page_h_pt)
    import tools.page_viewer as _pv
    cap = getattr(_pv, "MAX_RENDER_PX", 4000)   # default: the old inline literal
    return min(fit * zoom, cap / sv._page_w_pt, cap / sv._page_h_pt)


def _matches_pdfium(pm, path, page_index, scale):
    """Is this pixmap the actual pdfium render at `scale`, pixel for pixel?"""
    from PyQt6.QtGui import QImage as _QI
    doc = pdfium.PdfDocument(path)
    try:
        pg = doc[page_index]
        truth = pg.render(scale=scale).to_pil().convert("RGB")
        pg.close()
    finally:
        doc.close()
    img = pm.toImage().convertToFormat(_QI.Format.Format_RGB32)
    if (img.width(), img.height()) != truth.size:
        return False, f"{img.width()}x{img.height()} vs pdfium {truth.size[0]}x{truth.size[1]}"
    tb = truth.tobytes()
    total = img.width() * img.height()
    bits = img.bits(); bits.setsize(total * 4); buf = bytes(bits)
    acc = n = 0
    for i in range(0, total * 4, 4 * 997):        # sample, ~1 in 1000 pixels
        j = (i // 4) * 3
        acc += abs(buf[i+2] - tb[j]) + abs(buf[i+1] - tb[j+1]) + abs(buf[i] - tb[j+2])
        n += 1
    mean = acc / max(1, n)
    return mean < 0.5, f"mean channel difference {mean:.2f}"


def test_zoom_settles_on_a_real_render_not_an_interpolation():
    """The resting image must be an actual render at the zoom being shown.

    It used to be whatever was cached, Qt-scaled: any zoom within 1.45x of the
    cached scale was declared good enough and no real render was ever queued, so
    the user was left looking at interpolated pixels. 1.3x and 1.4x sit squarely
    in that old band."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        for zoom in (1.3, 1.4):
            sv._zoom = zoom
            sv._render()
            assert _settle(vp, lambda: sv._render_task is None
                           and not getattr(sv, "_showing_provisional", False),
                           tries=400), f"zoom {zoom} never settled"
            ok, detail = _matches_pdfium(sv._last_pm, src, 0, _target_scale_of(sv, zoom))
            assert ok, f"zoom {zoom} settled on an interpolated image — {detail}"
    finally:
        vp.deleteLater()


def test_zoom_shows_a_provisional_image_before_the_real_one():
    """Something sharp-ish has to be on screen immediately, flagged as a
    stand-in, with the exact render queued behind it."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        before = sv._last_pm.width()
        sv._zoom = 1.3
        sv._render()                      # synchronous part only
        assert sv._last_pm.width() > before, "nothing was shown for the new zoom"
        assert getattr(sv, "_showing_provisional", False), \
            "the stand-in was not flagged provisional"
        assert sv._render_task is not None, "no exact render was queued"
        assert _settle(vp, lambda: not getattr(sv, "_showing_provisional", False),
                       tries=400)
        assert sv._render_task is None, "the task outlived the final render"
    finally:
        vp.deleteLater()


def test_a_newer_zoom_cancels_the_render_still_in_flight():
    """Requirement on the generation counter: a render overtaken by a newer
    request must be cancelled, and must not be able to paint over the newer one
    if it finishes anyway."""
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        sv._zoom = 2.0
        sv._render()
        stale_task = sv._render_task
        stale_gen  = sv._render_gen
        assert stale_task is not None

        sv._zoom = 3.0
        sv._render()
        assert not stale_task._active, "the overtaken render was not cancelled"
        assert sv._render_gen > stale_gen, "the generation counter did not move"
        assert sv._render_task is not stale_task

        assert _settle(vp, lambda: sv._render_task is None
                       and not getattr(sv, "_showing_provisional", False),
                       tries=500), "never settled"
        settled = sv._last_pm.width()

        # A late emission carrying the stale generation must be ignored.
        sv._on_page_ready(stale_gen, sv._last_pm.toImage().scaled(40, 40),
                          0, 0, 100.0, 100.0, 1.0, [], False)
        assert sv._last_pm.width() == settled, \
            "a stale render was allowed to overwrite the current one"

        ok, detail = _matches_pdfium(sv._last_pm, src, 0, _target_scale_of(sv, 3.0))
        assert ok, f"did not settle on the newest zoom — {detail}"
    finally:
        vp.deleteLater()


def test_render_cap_is_named_and_enforced():
    """The 4000px ceiling is a placeholder until tiling exists; it should at
    least be one named constant rather than a number sprinkled about."""
    import tools.page_viewer as pv
    assert isinstance(pv.MAX_RENDER_PX, int) and pv.MAX_RENDER_PX > 0
    src = _fine_detail_pdf()
    vp, sv = _open_single_view(src)
    try:
        sv._zoom = 40.0                    # far past the cap
        sv._render()
        _settle(vp, lambda: sv._render_task is None
                and not getattr(sv, "_showing_provisional", False), tries=600)
        pm = sv._last_pm
        assert max(pm.width(), pm.height()) <= pv.MAX_RENDER_PX + 1, \
            f"render {pm.width()}x{pm.height()} exceeds the cap"
        # and it still settles on a real render rather than an upscale
        ok, detail = _matches_pdfium(pm, src, 0, _target_scale_of(sv, 40.0))
        assert ok, f"capped zoom settled on an interpolation — {detail}"
    finally:
        vp.deleteLater()


def _cache_fixture(tmp, name, n_pages, label):
    p = os.path.join(tmp, name)
    c = canvas.Canvas(p, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 40); c.drawCentredString(300, 400, f"{label}{i}"); c.showPage()
    c.save()
    return p


def _count_parses():
    """Context-manager-ish spy returning (restore, parsed_paths)."""
    import pypdfium2 as _pdfium
    parsed = []
    real = _pdfium.PdfDocument
    class _Spy(real):
        def __init__(self, inp, *a, **k):
            if isinstance(inp, str): parsed.append(inp)
            super().__init__(inp, *a, **k)
    _pdfium.PdfDocument = _Spy
    def restore(): _pdfium.PdfDocument = real
    return restore, parsed


def test_document_cache_reuses_and_reloads():
    """The point of the cache: parse a file once, not once per page. And a file
    that changed on disk must not come back from it — the page manager rewrites
    its temp PDFs constantly, sometimes within the same millisecond."""
    from tools.render import document_cache as dc
    tmp = tempfile.mkdtemp(dir=_TMP)
    dc.close_all()
    a = _cache_fixture(tmp, "a.pdf", 2, "A")

    restore, parsed = _count_parses()
    try:
        for _ in range(6):
            with dc.page_document(a) as doc:
                assert len(doc) == 2
        assert parsed.count(a) == 1, f"parsed {parsed.count(a)}x for 6 opens"

        time.sleep(0.01)
        _cache_fixture(tmp, "a.pdf", 3, "B")      # same path, new revision
        with dc.page_document(a) as doc:
            assert len(doc) == 3, "an edited file came back from the cache"
        assert parsed.count(a) == 2, "the new revision was not parsed"
        assert dc.stats()["open"] == 1, "the superseded revision was left open"

        for i in range(dc.MAX_DOCUMENTS + 4):     # push past the cap
            with dc.page_document(_cache_fixture(tmp, f"x{i}.pdf", 1, "X")):
                pass
        assert dc.stats()["open"] <= dc.MAX_DOCUMENTS, dc.stats()
    finally:
        restore()
        dc.close_all()
    assert dc.stats()["open"] == 0, "close_all left documents open"


def test_document_cache_never_closes_a_document_in_use():
    """Eviction drops a handle from the registry at once but must leave the
    close to its last user. Closing a document mid-render is a use-after-free in
    pdfium, i.e. a crash with no Python traceback to find it by."""
    from tools.render import document_cache as dc
    tmp = tempfile.mkdtemp(dir=_TMP)
    dc.close_all()
    keep = _cache_fixture(tmp, "keep.pdf", 1, "K")

    handle = dc._checkout(keep)          # stand in for being inside page_document
    try:
        for i in range(dc.MAX_DOCUMENTS + 3):
            with dc.page_document(_cache_fixture(tmp, f"e{i}.pdf", 1, "E")):
                pass
        assert handle.retired, "the handle should have been evicted by now"
        assert handle.users == 1, f"users={handle.users}"
        with handle.lock:                # still usable while checked out
            page = handle.doc[0]
            try:
                assert page.get_width() > 0
            finally:
                page.close()
    finally:
        dc._checkin(handle)

    try:
        handle.doc[0]
        still_open = True
    except Exception:
        still_open = False
    assert not still_open, "an evicted document was left open after its last user"
    dc.close_all()


def test_render_paths_go_through_the_document_cache():
    """_render_image used to open and close the whole PDF for every single page.
    Five pages of one file should now cost one parse."""
    from tools.page_viewer import _render_image
    from tools.render import document_cache as dc
    dc.close_all()
    restore, parsed = _count_parses()
    try:
        for i in range(5):
            img = _render_image(FX["normal"], i, 90)
            assert not img.isNull() and img.width() == 90
            # not the "could not render" fill
            assert (img.pixel(img.width() // 2, img.height() // 2) & 0xFFFFFF) != 0x2A3A5A
        n = parsed.count(FX["normal"])
        assert n == 1, f"the file was parsed {n}x for 5 page renders"
    finally:
        restore()
        dc.close_all()


def _pdfium_dims(path, index=0):
    doc = pdfium.PdfDocument(path)
    try:
        return doc[index].get_width(), doc[index].get_height()
    finally:
        doc.close()


def test_rotation_reaches_the_tools():
    """A page turned in "Seiten verwalten" has to be turned for the tools too.
    Three of them read AppState.current_pdf — the file on disk — instead of the
    flattened view, so they measured and previewed the page in its original
    orientation and produced a crop for the wrong side."""
    from tools._base import displayed_pdf
    from tools.all_tools import CropResizePanel, NUpPanel
    tab, panel = _manage(3, "rot_tools.pdf")
    st = AppState.get()
    w0, h0 = _pdfium_dims(st.current_pdf)
    assert h0 > w0, "fixture should start portrait"

    panel.grid.rotate_selected(0)          # no-op: identity view, no rewrite
    assert displayed_pdf(st.current_pdf) == st.current_pdf

    tab.model.selected = {tab.model.order[0]}
    panel.grid.rotate_selected(90)
    flat = displayed_pdf(st.current_pdf)
    assert flat != st.current_pdf, "rotation did not produce a flattened view"
    assert PdfReader(flat, strict=False).pages[0].get("/Rotate") == 90
    w1, h1 = _pdfium_dims(flat)
    assert (round(w1), round(h1)) == (round(h0), round(w0)), \
        f"rotated page measures {w1:.0f}x{h1:.0f}, expected {h0:.0f}x{w0:.0f}"

    # Watch which file the panels actually open. current_pdf() was always
    # correct — the bug was these three code paths reaching past it to
    # AppState.current_pdf, so only the real call shows it.
    opened = []
    real = pdfium.PdfDocument
    class _Spy(real):
        def __init__(self, inp, *a, **k):
            if isinstance(inp, str): opened.append(inp)
            super().__init__(inp, *a, **k)
    pdfium.PdfDocument = _Spy
    try:
        crop = CropResizePanel()
        for label, call in (
                ("crop margins",  lambda: crop._set_margins_for_size(595.0, 842.0)),
                ("crop preview",  lambda: crop._render_preview(400, 500, 1.0)),
                ("n-up preview",  lambda: NUpPanel()._render_preview(400, 500, 1.0))):
            opened.clear()
            call()
            assert opened, f"{label} opened no document at all"
            assert all(p == flat for p in opened), \
                f"{label} read {[os.path.basename(p) for p in opened]}, not the rotated view"
    finally:
        pdfium.PdfDocument = real


def test_rotation_reaches_the_preview():
    """The single-page view turns the bitmap but used to keep the page's original
    width and height, so a rotated page was fitted into a portrait box, measured
    as portrait in the "Masse" readout, and drawn smaller than it should be."""
    from tools.page_viewer import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    vp.open_file(FX["normal"])
    _spin(60, 0.01)
    tab = vp.tabs.currentWidget()
    sv  = tab.single
    _settle(vp, lambda: sv._page_w_pt > 0, tries=200)
    portrait = (sv._page_w_pt, sv._page_h_pt)
    assert portrait[1] > portrait[0], "fixture should start portrait"
    area_before = sv._last_pm.width() * sv._last_pm.height()

    tab._build_manage_once()
    tab.model.selected = {tab.model.order[0]}
    tab._manage_panel.grid.rotate_selected(90)
    sv._current = 0
    sv._render()
    assert _settle(vp, lambda: sv._page_w_pt > portrait[0], tries=250), \
        "the preview never picked up the rotation"
    assert (round(sv._page_w_pt), round(sv._page_h_pt)) == \
           (round(portrait[1]), round(portrait[0])), \
        f"preview reports {sv._page_w_pt:.0f}x{sv._page_h_pt:.0f}, expected the swap"
    pm = sv._last_pm
    assert pm.width() > pm.height(), "the rendered page is not landscape"
    # fitted to the same window, so it should not have shrunk
    assert pm.width() * pm.height() > area_before * 0.9, \
        "the rotated page was fitted into the old portrait box"
    vp.deleteLater()


def test_char_boxes_follow_a_rotated_page():
    """Text rectangles are measured before the bitmap is turned; if they do not
    turn with it, selecting text on a rotated page highlights the wrong place."""
    from tools.page_viewer import _rotate_char_boxes
    W, H = 400.0, 800.0
    box = [("a", 10.0, 20.0, 30.0, 50.0)]      # near the top-left
    assert _rotate_char_boxes(box, 0, W, H) == box
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 90, W, H)[0]
    assert (x0, y0, x1, y1) == (H - 50.0, 10.0, H - 20.0, 30.0)
    assert 0 <= x0 < x1 <= H and 0 <= y0 < y1 <= W, "90° box left the image"
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 180, W, H)[0]
    assert (x0, y0, x1, y1) == (W - 30.0, H - 50.0, W - 10.0, H - 20.0)
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 270, W, H)[0]
    assert (x0, y0, x1, y1) == (20.0, W - 30.0, 50.0, W - 10.0)
    assert 0 <= x0 < x1 <= H and 0 <= y0 < y1 <= W, "270° box left the image"


def _manage(n_pages=6, name="mgr.pdf"):
    """A PdfTab with its page manager built, wired into AppState."""
    from tools.page_viewer import PdfTab
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 80); c.drawCentredString(300, 400, f"P{i+1}"); c.showPage()
    c.save()
    tab = PdfTab(src)
    st = AppState.get(); st.open_pdf(tab.pdf_path); st.page_model = tab.model
    tab._build_manage_once()
    return tab, tab._manage_panel


def _page_labels(path):
    return [p.extract_text().strip().replace("\n", "") for p in PdfReader(path).pages]


def _answer_dialog(word):
    """Answer the copy/move QMessageBox by button text, without showing it."""
    from PyQt6.QtWidgets import QMessageBox
    def picked(self):
        for b in self.buttons():
            if word in b.text():
                return b
        return None
    QMessageBox.exec = lambda self: self.setResult(0)
    QMessageBox.clickedButton = picked


def test_manage_open_as_tab_copies_or_moves():
    """"Als neuen Tab oeffnen" asks whether the pages should stay. Moving them is
    what the removed "Nach Bereichen..." split was for, only driven by picking
    pages instead of typing ranges into a prompt."""
    from PyQt6.QtWidgets import QMessageBox
    from tools._base import displayed_pdf
    tab, mp = _manage(6, "mgr_tab.pdf")
    opened = []
    AppState.get().open_result = lambda p, t="": opened.append(p)
    real = (QMessageBox.exec, QMessageBox.clickedButton)
    try:
      _answer_dialog("Kopieren")
      mp.model.selected = {mp.model.order[1], mp.model.order[2]}
      mp._open_as_tab()
      assert _page_labels(opened[-1]) == ["P2", "P3"], _page_labels(opened[-1])
      assert len(mp.model.order) == 6, "copy must leave the document alone"

      _answer_dialog("Verschieben")
      mp.model.selected = {mp.model.order[0], mp.model.order[1]}
      mp._open_as_tab()
      assert _page_labels(opened[-1]) == ["P1", "P2"], _page_labels(opened[-1])
      assert len(mp.model.order) == 4, "move must remove them from the document"
      assert _page_labels(displayed_pdf(AppState.get().current_pdf)) == \
        ["P3", "P4", "P5", "P6"]

      _answer_dialog("Abbrechen")          # matches nothing -> treated as cancel
      mp.model.selected = {mp.model.order[0]}
      n_before, n_opened = len(mp.model.order), len(opened)
      mp._open_as_tab()
      assert len(mp.model.order) == n_before and len(opened) == n_opened, \
        "cancelling must neither open a tab nor touch the pages"
    finally:
        QMessageBox.exec, QMessageBox.clickedButton = real


def test_manage_inserts_several_files_at_once():
    """"Aus Datei(en) einfuegen..." replaced the separate merge button, so it has
    to accept more than one file and insert them after the selection."""
    from PyQt6.QtWidgets import QFileDialog
    from tools._base import displayed_pdf
    extras = []
    for tag, n in (("X", 2), ("Y", 1)):
        p = os.path.join(_TMP, f"ins_{tag}.pdf")
        c = canvas.Canvas(p, pagesize=A4)
        for i in range(n):
            c.setFont("Helvetica", 80); c.drawCentredString(300, 400, f"{tag}{i+1}")
            c.showPage()
        c.save(); extras.append(p)

    tab, mp = _manage(3, "mgr_ins.pdf")
    real = QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (extras, ""))
    try:
        mp.model.selected = {mp.model.order[0]}      # after page 1
        mp._insert_from_file()
    finally:
        QFileDialog.getOpenFileNames = real
    assert len(mp.model.order) == 6, f"expected 6 pages, got {len(mp.model.order)}"
    assert _page_labels(displayed_pdf(AppState.get().current_pdf)) == \
        ["P1", "X1", "X2", "Y1", "P2", "P3"]


def test_manage_panel_has_no_duplicate_actions():
    """Saving moved to Datei ▸ and merge folded into insert-from-file. Those
    buttons must be gone from the sidebar, not merely hidden, and Strg+S must be
    owned by exactly one thing or Qt delivers it to neither."""
    from PyQt6.QtGui import QShortcut
    tab, mp = _manage(3, "mgr_dup.pdf")
    for gone in ("_merge", "_split_ranges", "_save", "_save_as", "_do_save"):
        assert not hasattr(mp, gone), f"{gone} is still on the panel"

    win = MAIN.MainWindow()
    try:
        names = []
        for m in win._title_bar.menu_bar.actions():
            if m.menu():
                names += [a.text() for a in m.menu().actions() if a.text()]
        assert "Speichern" in names and "Speichern unter…" in names, names
        keys = [s.key().toString() for s in win.viewer.findChildren(QShortcut)]
        assert "Ctrl+S" not in keys, "Ctrl+S is registered twice — it will not fire"
    finally:
        win.deleteLater(); _app.processEvents()


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
