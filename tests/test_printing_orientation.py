"""Automatic orientation must follow displayed pages, including mixed jobs."""
import os
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
import pypdfium2 as pdfium
from tools.printing import spool
from tools.render.document_cache import PDFIUM_LOCK
from tools.viewer.model import PageModel
from tests.support import _TMP


def _mixed_pages():
    src = os.path.join(_TMP, "mixed_orientation.pdf")
    c = canvas.Canvas(src, pagesize=(595, 842))
    for w, h in ((595, 842), (842, 595)):
        c.setPageSize((w, h))
        # Both outer marks must survive fixed-size placement on matching media.
        c.rect(10, 10, 20, 20, fill=1)
        c.rect(w - 30, h - 30, 20, 20, fill=1)
        c.showPage()
    c.save()
    return src


def _shapes(path):
    return [(float(p.mediabox.width), float(p.mediabox.height))
            for p in PdfReader(path).pages]


def test_auto_pdf_orientation_follows_each_selected_page():
    src = _mixed_pages()
    model = PageModel(2)
    out = os.path.join(_TMP, "mixed_orientation_out.pdf")
    for idx in (spool.FIT, spool.FIXED, spool.SHRINK):
        for pages in ([0, 1], [1, 0]):
            spool.build_print_pdf(src, model, pages, out, paper_key="A4",
                                  orient_idx=0, scale_idx=idx)
            assert [w > h for w, h in _shapes(out)] == [p == 1 for p in pages]
    for orient in (1, 2):
        spool.build_print_pdf(src, model, [0, 1], out, paper_key="A4",
                              orient_idx=orient)
        assert all((w > h) == (orient == 2) for w, h in _shapes(out))


def test_auto_orientation_honours_crop_and_page_manager_rotation():
    src = _mixed_pages()
    cropped = os.path.join(_TMP, "cropped_orientation.pdf")
    writer = PdfWriter(clone_from=src)
    writer.pages[0].cropbox.upper_right = (300, 200)
    with open(cropped, "wb") as f:
        writer.write(f)
    model = PageModel(2)
    model.rotations[model.order[1]] = 90
    out = os.path.join(_TMP, "cropped_orientation_out.pdf")
    spool.build_print_pdf(cropped, model, [0, 1], out, paper_key="A4",
                          orient_idx=0, scale_idx=spool.FIXED)
    assert [w > h for w, h in _shapes(out)] == [True, False]


def test_normalization_does_not_choose_orientation_from_text():
    src = os.path.join(_TMP, "sideways_text.pdf")
    c = canvas.Canvas(src, pagesize=(595, 842))
    c.translate(100, 100)
    c.rotate(90)
    c.drawString(0, 0, "SIDEWAYS TEXT DOES NOT MAKE THIS A LANDSCAPE PAGE")
    c.showPage()
    c.save()
    out = os.path.join(_TMP, "sideways_text_out.pdf")
    spool.build_print_pdf(src, PageModel(1), [0], out, paper_key="",
                          orient_idx=0)
    reader = PdfReader(out)
    page = reader.pages[0]
    w, h = _shapes(out)[0]
    if page.rotation % 180:
        w, h = h, w
    assert abs(w - 595) < 1 and abs(h - 842) < 1, (w, h)


def test_auto_fixed_spool_keeps_mixed_page_orientation():
    src = _mixed_pages()
    real_run = spool._run_capturing
    real_gs = spool.ghostscript_binary
    seen = []

    def capture(cmd, timeout):
        if cmd[0] == "lp":
            seen.append(_shapes(cmd[-1]))
            # Inspect the actual spooled pixels: changing MediaBox alone
            # cannot restore marks Ghostscript already clipped away.
            with PDFIUM_LOCK:
                with pdfium.PdfDocument(cmd[-1]) as doc:
                    for i, (w, h) in enumerate(((595, 842), (842, 595))):
                        page = doc[i]
                        try:
                            image = page.render(scale=1).to_pil().convert("L")
                            bounds = image.point(lambda v: 255 if v < 100 else 0).getbbox()
                            assert bounds is not None
                            assert abs(bounds[2] - bounds[0] - (w - 20) * pct / 100) < 3
                            assert abs(bounds[3] - bounds[1] - (h - 20) * pct / 100) < 3
                        finally:
                            page.close()
            return type("Result", (), {"returncode": 0})()
        return real_run(cmd, timeout)

    spool._run_capturing = capture
    try:
        for gs in (real_gs, lambda: None):
            spool.ghostscript_binary = gs
            for pct in (100, 70):
                spool.print_via_gs(src, PageModel(2), [0, 1], 1, "auto", True,
                                   False, "long", "test-printer", spool.FIXED,
                                   "A4", 0, lambda *_: None, scale_pct=pct)
                assert [w > h for w, h in seen[-1]] == [False, True], seen[-1]
    finally:
        spool._run_capturing = real_run
        spool.ghostscript_binary = real_gs
