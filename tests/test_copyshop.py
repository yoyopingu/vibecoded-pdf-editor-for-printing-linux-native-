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

from PyQt6.QtWidgets import QApplication, QStackedWidget
from PyQt6.QtCore import QTimer
_app = QApplication(sys.argv)

from pypdf import PdfReader
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image

from tools.app_state import AppState
from tools.page_viewer import PageModel, _ThumbnailCache
import tools.all_tools as T
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
    # encrypted (from normal)
    enc = os.path.join(_TMP, "encrypted.pdf")
    import pikepdf
    with pikepdf.open(p) as pdf:
        pdf.save(enc, encryption=pikepdf.Encryption(owner="o", user="u"))
    # an image for Image→PDF
    img = os.path.join(_TMP, "img.png")
    Image.new("RGB", (400, 600), (40, 90, 160)).save(img)
    return {"normal": p, "single": s, "color": col, "encrypted": enc, "image": img}


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
