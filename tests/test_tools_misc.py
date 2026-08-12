"""
Tools Misc.
"""
import os, time, tempfile, shutil
from PyQt6.QtWidgets import QStackedWidget
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image
from tools.app_state import AppState
from tools._base import BasePanel
import pypdfium2 as pdfium
import tools.all_tools as T
from tests.support import FX, _TMP, _app, _open, _pdfium_page_text, _sync_async


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
