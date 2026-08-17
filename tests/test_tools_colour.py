"""
Tools Colour.
"""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels.colour_profile import ColourProfilePanel
from tools.panels.grayscale import GrayscalePanel, _grey_vector
from tools.panels.preflight import PreflightPanel
from tools.panels._colour import _hist_stats
from tests.support import FX, _TMP, _app, _brightest, _open, _spin, _sync_async


def _page_has_colour(path, i):
    d = pdfium.PdfDocument(path)
    im = d[i].render(scale=1, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
    px = list(im.get_flattened_data()); d.close()
    return any(abs(r-g) > 12 or abs(g-b) > 12 for r, g, b in px)


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
    p = GrayscalePanel(); _sync_async(p); p.log.log = lambda *a, **k: None
    p._scan()
    assert len(p._page_data) == 4, f"scan produced {len(p._page_data)} pages"
    dist = [_hist_stats(h, 20)[0] for h in p._page_data]
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
    p = GrayscalePanel(); _sync_async(p); p.log.log = lambda *a, **k: None
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
    p = GrayscalePanel(); _sync_async(p); p.log.log = lambda *a, **k: None
    import tools.panels.grayscale as G
    real = G._scan_pages
    G._scan_pages = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render exploded"))
    try:
        p._scan()
    finally:
        G._scan_pages = real
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
    p = PreflightPanel(); _sync_async(p); p.log.log = lambda *a, **k: None
    p.chk_colour.setChecked(True)
    p._run_action()
    report = p.report.toPlainText()
    assert "Farbseiten:" in report, f"the colour mark was missed:\n{report}"


def _blackout_gs(inject_into_retry):
    """Make Ghostscript "succeed" while blacking out page 2.

    That is what the real failure looks like from the outside: exit 0, empty
    stderr, right page count, ruined page.

    Hooked on Progress.run rather than subprocess.run: the tools shell out
    through the progress object now, so that a Stop reaches the child. This
    stub has to sit where the call actually goes."""
    import pikepdf
    from tools.jobs import Progress
    real = Progress.run
    def faked(self, cmd, *a, **k):
        r = real(self, cmd, *a, **k)
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
    Progress.run = faked
    return lambda: setattr(Progress, "run", real)


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
        res, msg = _grey_vector(gs, src, out, {0, 1, 2}, 3, null_progress())
    finally:
        restore()
    assert _mean_luma(res, 1) > 100, "a blacked-out page reached the output"
    assert "nachkonvertiert" in msg, msg

    # ...and when the retry is damaged too, the original page is kept and said so.
    restore = _blackout_gs(inject_into_retry=True)
    try:
        out = os.path.join(_TMP, "grey_guard2.pdf")
        res, msg = _grey_vector(gs, src, out, {0, 1, 2}, 3, null_progress())
    finally:
        restore()
    assert _mean_luma(res, 1) > 100, "a blacked-out page reached the output"
    assert "ACHTUNG" in msg and "2 (" in msg, f"damage not reported: {msg}"
    for i in (0, 2):
        assert _mean_luma(res, i) > 100, f"page {i+1} damaged too"
    return "rescued, then refused"


def test_greyscale_subset_verify_compares_the_right_pages():
    """The subset optimization extracts only selected pages for Ghostscript.
    The verify step must compare each converted page against the ORIGINAL of
    that same page, not against original page [subset_position]. With a sparse
    selection (e.g. page 1 of a 4-page doc), the buggy code compared the
    blacked-out conversion of page 1 against original page 0 — and when page 0
    was dark, the damage was invisible and the broken page shipped silently."""
    if not (shutil.which("gs") or shutil.which("gswin64c")):
        return "SKIP (no ghostscript)"
    gs = shutil.which("gs") or shutil.which("gswin64c")

    # Page 0: full black (so the buggy compare-against-src[0] would see
    # dark→dark and miss the blackout).  Page 1: light content (the page
    # whose conversion we black out).  Pages 2-3: light, unselected.
    from reportlab.lib import colors
    src = os.path.join(_TMP, "grey_sparse.pdf")
    c = canvas.Canvas(src, pagesize=A4)
    c.setFillColor(colors.black); c.rect(0, 0, A4[0], A4[1], fill=1, stroke=0); c.showPage()
    c.setFillColor(colors.HexColor("#2277cc")); c.rect(40, 500, 500, 250, fill=1, stroke=0)
    c.setFillGray(0); c.setFont("Helvetica", 30); c.drawString(50, 430, "TARGET"); c.showPage()
    for _ in range(2):
        c.setFillGray(0); c.setFont("Helvetica", 20); c.drawString(50, 700, "filler"); c.showPage()
    c.save()

    # Black out page 0 of whatever GS produces (the subset has 1 page =
    # original page 1; the batch retry also has 1 page).  This stub is
    # subset-aware: it always blacks out position 0 of the output.
    from tools.jobs import Progress
    real = Progress.run
    def faked(self, cmd, *a, **k):
        r = real(self, cmd, *a, **k)
        import pikepdf
        if "-o" in cmd:
            outp = cmd[cmd.index("-o") + 1]
            try:
                with pikepdf.open(outp, allow_overwriting_input=True) as pdf:
                    if len(pdf.pages) >= 1:
                        pdf.pages[0].contents_add(
                            pikepdf.Stream(pdf, b"0 g 0 0 3000 3000 re f"))
                        pdf.save(outp)
            except Exception:
                pass
        return r
    Progress.run = faked
    try:
        out = os.path.join(_TMP, "grey_sparse_out.pdf")
        res, msg = _grey_vector(gs, src, out, {1}, 4, null_progress())
    finally:
        Progress.run = real

    # The conversion of page 1 was blacked out.  The verify MUST have caught
    # it: page 1 keeps its original (light, blue) content, not a black page.
    assert _mean_luma(res, 1) > 100, \
        "a blacked-out page shipped — verify compared against the wrong original"
    assert "ACHTUNG" in msg or "1 (" in msg, \
        f"damage not reported for the sparse selection: {msg}"
    # Page 0 (unselected, dark) must be untouched.
    assert _mean_luma(res, 0) < 50, "unselected dark page was altered"
    return "sparse selection verified correctly"


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
    _, msg = _grey_vector(gs, src, out, set(range(6)), 6, null_progress())
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
        # The summary is logged now rather than returned — the work happens on
        # a worker and the panel reports when it comes back.
        logged = []
        p = ColourProfilePanel(); _sync_async(p)
        p.log.log = lambda m, *a, **k: logged.append(m)
        out = os.path.join(_TMP, "cmyk_guard.pdf")
        p.save_pdf = lambda *a, **k: out
        p.open_result = lambda *a, **k: None
        p._run_action()
        msg = "\n".join(logged)
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
    from tools.printing.spool import _gs_blacked_out
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


def test_greyscale_vector():
    if not (shutil.which("gs") or shutil.which("gswin64c")):
        return "SKIP (no ghostscript)"
    _open(FX["color"])
    def convert(sel):
        p = GrayscalePanel()
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
    p = ColourProfilePanel(); _sync_async(p)
    assert p.profile_combo.count() >= 5, "expected several CMYK profile options"
    o = os.path.join(_TMP, "cmyk.pdf")
    p.save_pdf = lambda *a, **k: o; p.open_result = lambda *a, **k: None
    p.profile_combo.setCurrentIndex(1)          # a named profile (falls back if .icc absent)
    p._run_action()
    assert len(PdfReader(o).pages) >= 1, "CMYK conversion produced no valid output"
    return "ok"


def _nested_colour_pdf(name="cs_nested.pdf"):
    """A page whose only colour lives inside a Form XObject — what N-Up,
    imposition and merge all produce."""
    import pikepdf
    inner = os.path.join(_TMP, "cs_inner.pdf")
    c = canvas.Canvas(inner, pagesize=A4)
    c.setFillColorRGB(1, 0, 0); c.rect(100, 400, 300, 200, fill=1, stroke=0)
    c.showPage(); c.save()
    out = os.path.join(_TMP, name)
    with pikepdf.open(inner) as src, pikepdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(595, 842))
        form = pdf.copy_foreign(pikepdf.Page(src.pages[0]).as_form_xobject())
        page.add_overlay(form, pikepdf.Rectangle(0, 0, 595, 842))
        pdf.save(out)
    return out


def _mixed_colour_pdf(name="cs_mixed.pdf"):
    """An RGB image — which puts /DeviceRGB in /Resources — and a CMYK vector
    fill, which lives only in the content stream."""
    from PIL import Image as _I
    img = os.path.join(_TMP, "cs_rgb.png")
    _I.new("RGB", (40, 40), (200, 30, 30)).save(img)
    out = os.path.join(_TMP, name)
    c = canvas.Canvas(out, pagesize=A4)
    c.drawImage(img, 60, 600, 120, 120)
    c.setFillColorCMYK(0, 1, 1, 0); c.rect(60, 300, 200, 150, fill=1, stroke=0)
    c.showPage(); c.save()
    return out


def test_colour_scan_sees_inside_a_form_xobject():
    """Imposition, N-Up and merge turn a page into a Form XObject, after which
    the page's own content stream is just "/Fm0 Do". A scan that does not
    recurse finds nothing at all there — which is what the greyscale tool did,
    so an imposed colour page was never recognised as colour."""
    from tools.colorspace import page_colorspaces, is_grey_only, describe
    names = page_colorspaces(_nested_colour_pdf(), 0)
    assert "/DeviceRGB" in names, f"colour inside the form was not seen: {sorted(names)}"
    assert not is_grey_only(names), "an imposed colour page counted as grey"
    assert describe(names) == "RGB"


def test_colour_scan_reads_resources_and_content_stream():
    """Both, always. The Farbprofil tool stopped at the first thing /Resources
    gave it, so a page with an RGB image and a CMYK vector fill was reported as
    RGB — the wrong answer from the tool whose whole job is that answer."""
    from tools.colorspace import page_colorspaces, has_cmyk, has_rgb, describe
    names = page_colorspaces(_mixed_colour_pdf(), 0)
    assert has_rgb(names), f"the RGB image was missed: {sorted(names)}"
    assert has_cmyk(names), f"the CMYK fill was missed: {sorted(names)}"
    assert describe(names) == "RGB + CMYK"


def test_an_unreadable_page_is_not_called_grey():
    """Nothing found means the page could not be read. Treating that as grey
    would make the greyscale tool skip converting it."""
    from tools.colorspace import page_colorspaces, is_grey_only
    assert page_colorspaces(os.path.join(_TMP, "cs_does_not_exist.pdf"), 0) == frozenset()
    assert not is_grey_only(frozenset())


def test_farbprofil_report_names_every_colour_space_present():
    """What the operator reads has to be right — it is what decides whether a
    file gets converted before it goes to a professional press.

    The report used to stop scanning as soon as /Resources yielded anything, so
    a file with an RGB image and a CMYK vector fill was reported as plain RGB
    and recommended for conversion it had partly had already."""
    _open(_mixed_colour_pdf("cs_report.pdf"))
    p = ColourProfilePanel(); p.log.log = lambda *a, **k: None
    p._inspect()
    report = p.report.toPlainText()
    assert "CMYK" in report and "RGB" in report, f"report missed a colour space:\n{report}"
    assert "Gemischt" in report, f"mixed file not flagged as mixed:\n{report}"


def test_colour_scan_does_not_degrade_on_a_large_content_stream():
    """The scan has to stay linear in the size of the stream.

    It was not. Matching operands-then-operator — `[\\d.]+\\s+` four times over
    followed by [kK] — backtracks catastrophically through a page that is mostly
    coordinates and contains no CMYK, which is the ordinary case for a large
    vector drawing. Measured at 15.3 seconds on one 53 MB page, against 260 ms
    to read that stream off disk, and it ran on the GUI thread.

    This builds the shape that provoked it: a long run of numeric operands with
    no CMYK operator anywhere in it."""
    import time
    from tools.colorspace import colour_operators

    # ~8 MB of plausible path drawing: numbers, moves, lines, strokes, no CMYK.
    chunk = b"0.482 0.913 m 12.5 33.75 l 100.25 4.5 l S\n"
    data = b"1 0 0 RG 0.5 w\n" + chunk * (8_000_000 // len(chunk))

    start = time.perf_counter()
    names = colour_operators(data)
    elapsed = time.perf_counter() - start

    assert "/DeviceRGB" in names, "the stroke colour was missed"
    assert "/DeviceCMYK" not in names, "invented a CMYK operator"
    # Generous: the point is orders of magnitude, not a stopwatch. The old
    # spelling took tens of seconds on this input.
    assert elapsed < 3.0, f"{len(data)/1e6:.0f} MB took {elapsed:.1f} s"
    return f"{len(data)/1e6:.0f} MB in {elapsed*1000:.0f} ms"


def _many_page_pdf(n_pages, name="cs_many.pdf"):
    out = os.path.join(_TMP, name)
    if not os.path.exists(out):
        c = canvas.Canvas(out, pagesize=A4)
        for i in range(n_pages):
            c.setFillColorRGB(0.2, 0.4, 0.9)
            c.rect(60, 600, 200, 100, fill=1, stroke=0)
            c.setFont("Helvetica", 30); c.drawString(70, 400, f"page {i+1}")
            c.showPage()
        c.save()
    return out


def test_scanning_a_document_opens_it_once():
    """The whole-document scan asked page_colorspaces in a loop, and every one
    of those opened the file again — 300 opens of the same PDF for a 300-page
    job, where the answer for every page is behind the handle already in hand.
    Measured at 12.5 s against 63 ms."""
    import pikepdf
    from tools.colorspace import document_colorspaces, _cache

    src = _many_page_pdf(120)
    _cache.clear()
    opens = []
    real_open = pikepdf.open
    pikepdf.open = lambda *a, **k: (opens.append(a[0] if a else None),
                                    real_open(*a, **k))[1]
    try:
        names = document_colorspaces(src)
    finally:
        pikepdf.open = real_open

    assert "/DeviceRGB" in names, sorted(names)
    assert len(opens) == 1, f"opened the file {len(opens)}x for 120 pages"


def test_a_document_scan_leaves_every_page_cached():
    """Each page is put in the cache on the way past, so the viewer's label and
    the greyscale scan get theirs for nothing afterwards."""
    import pikepdf
    from tools.colorspace import document_colorspaces, cached_page_colorspaces, _cache

    src = _many_page_pdf(120)
    _cache.clear()
    document_colorspaces(src)
    for page in (0, 60, 119):
        assert cached_page_colorspaces(src, page) is not None, \
            f"page {page} was scanned but not remembered"


def test_the_cache_drops_the_oldest_not_everything():
    """A document with more pages than the cache used to wipe it each time it
    filled, so scanning a long file left only its last few pages and the viewer
    went back to reading page 1 off disk."""
    from tools.colorspace import _remember, _cache, _CACHE_MAX

    _cache.clear()
    for i in range(_CACHE_MAX + 50):
        _remember(("doc", i), frozenset({"/DeviceGray"}))
    assert len(_cache) == _CACHE_MAX, len(_cache)
    assert ("doc", _CACHE_MAX + 49) in _cache, "the newest entry was dropped"
    assert ("doc", 49) not in _cache, "the oldest entry was kept"
    assert ("doc", 60) in _cache, "everything was cleared instead of the oldest"


def test_colour_operators_read_the_stream_the_way_a_pdf_writes_it():
    """The operand separator before an operator is optional, and PDF whitespace
    is six characters rather than three.

    `1 0 0rg` is legal and optimisers emit it; the scan skipped it because a
    digit sitting against the operator looked like the middle of a token. A
    letter there does mean that — the RG in /DeviceRGB is not the operator —
    but a digit is just the last operand written tight."""
    from tools.colorspace import colour_operators

    finds = [
        (b"1 0 0 rg\n",     {"/DeviceRGB"},  "the ordinary spelling"),
        (b"1 0 0rg\n",      {"/DeviceRGB"},  "no space before the operator"),
        (b"0 0 0 1k\n",     {"/DeviceCMYK"}, "the same, for CMYK"),
        (b"1 0 0\frg\n",    {"/DeviceRGB"},  "form feed counts as whitespace"),
        (b"q 1 0 0 RG Q",   {"/DeviceRGB"},  "the stroke-side operator"),
        (b"1 0 0 rg",       {"/DeviceRGB"},  "operator at the very end"),
    ]
    rejects = [
        (b"/DeviceRGB cs",  "RG inside a name"),
        (b"1 0 0 rgb",      "rgb is a different token"),
        (b"123 rg",         "one operand is not a triple"),
        (b"1 0 0 1 0 0 cm", "cm is not a colour operator"),
    ]
    for data, expect, why in finds:
        assert colour_operators(data) == expect, f"{why}: {data!r}"
    for data, why in rejects:
        assert colour_operators(data) == set(), f"{why}: {data!r}"


def test_the_two_searches_agree_on_every_answer():
    """_uses looks for candidates with bytes.find and, once there are clearly
    too many to walk from Python, hands the rest of the buffer to the regex
    engine. The two halves have to agree — a switch that changed the answer
    would be a wrong colour profile on exactly the large pages it exists for.

    Each case here puts its real operator past _LOOP_BUDGET candidates, so the
    answer is decided by the regex half, and compares against the same buffer
    scanned with the budget raised out of the way."""
    import tools.colorspace as CS
    from tools.colorspace import colour_operators

    noise = b"gx" * (CS._LOOP_BUDGET * 3)      # 'g' candidates, none an operator
    cases = {
        "gray after the switch":  noise + b"\n0.5 g\n",
        "rgb after the switch":   noise + b"\n1 0 0 rg\n",
        "cmyk after the switch":  noise + b"\n0 0 0 1 k\n",
        "nothing at all":         noise,
        "operator with too few operands": noise + b"\n1 0 k\n",
        "letter before the token":        noise + b"\n0.5 Xg\n",
        "compact, no space before op":    noise + b"\n1 0 0rg\n",
        "stroke-side capitals":           noise + b"\n0 0 0 1 K\n",
    }
    real_budget = CS._LOOP_BUDGET
    for label, data in cases.items():
        via_regex = colour_operators(data)
        CS._LOOP_BUDGET = len(data) + 1        # never switch: find walks it all
        try:
            via_find = colour_operators(data)
        finally:
            CS._LOOP_BUDGET = real_budget
        assert via_regex == via_find, \
            f"{label}: regex half said {sorted(via_regex)}, find half {sorted(via_find)}"
    return f"{len(cases)} buffers, both halves agree"


def test_a_page_of_mostly_gray_candidates_is_still_quick():
    """The page this was found on holds 664,930 letters `g`, exactly one of
    which is the operator, and the first 227,458 candidates are not. Walking
    those from Python cost 181 ms of the 464 ms the whole scan took."""
    import time
    from tools.colorspace import colour_operators

    # The shape: a great many 'g' that are not operators, then one that is.
    # Sized so the threshold discriminates rather than merely passing —
    # measured at 94 ms walking these from the regex engine and 686 ms walking
    # them from Python, so 300 ms fails the old spelling with room to spare on
    # a loaded machine.
    data = b"".join((b"/Fg%d Do\n" % i) for i in range(800_000)) + b"\n0.5 g\n"
    start = time.perf_counter()
    names = colour_operators(data)
    elapsed = time.perf_counter() - start
    assert "/DeviceGray" in names, "the fill was missed behind the noise"
    assert elapsed < 0.30, f"{len(data)/1e6:.0f} MB took {elapsed*1000:.0f} ms"
    return f"{len(data)/1e6:.0f} MB, {elapsed*1000:.0f} ms"


def test_content_streams_are_joined_not_accumulated():
    """A page's /Contents is often an array, and the page manager adds parts to
    it freely. Building the buffer with += copies everything read so far on
    every part, which is quadratic in the number of them."""
    import time, pikepdf
    from tools.colorspace import _content_bytes

    part = b"0.5 g 10 10 m 20 20 l S\n" + b"%" + b"x" * 40_000 + b"\n"
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(595, 842))
        page = pdf.pages[0]
        page.obj["/Contents"] = pikepdf.Array(
            [pdf.make_stream(part) for _ in range(400)])
        start = time.perf_counter()
        data = _content_bytes(page.obj, pikepdf)
        elapsed = time.perf_counter() - start
    assert data.count(b"0.5 g") == 400, "parts were lost"
    assert elapsed < 2.0, f"400 parts took {elapsed:.1f} s"
    return f"400 parts, {len(data)/1e6:.1f} MB in {elapsed*1000:.0f} ms"


def test_a_stream_shared_between_pages_is_read_once():
    """Pages that share a content stream are one scan, not one per page.

    Imposition, N-Up's repeat mode and a duplicated page in the page manager
    all produce documents like this. The per-page cache cannot see it — two
    pages are two keys — so the identity of the bytes is cached as well."""
    import pikepdf
    import tools.colorspace as CS
    from tools.colorspace import document_colorspaces

    src = os.path.join(_TMP, "shared_stream.pdf")
    with pikepdf.new() as pdf:
        pdf.add_blank_page(page_size=(200, 200))
        stream = pdf.make_stream(b"1 0 0 rg 10 10 100 100 re f\n")
        pdf.pages[0].obj["/Contents"] = stream
        for _ in range(7):                      # eight pages, one stream
            pdf.pages.append(pdf.pages[0])
        pdf.save(src)

    reads = []
    real = CS._content_bytes
    CS._content_bytes = lambda obj, pp: (reads.append(1), real(obj, pp))[1]
    try:
        CS._cache.clear(); CS._stream_cache.clear()
        names = document_colorspaces(src)
    finally:
        CS._content_bytes = real
    assert "/DeviceRGB" in names, names
    assert len(reads) == 1, f"one stream, eight pages, {len(reads)} inflations"
    return f"8 pages sharing 1 stream -> {len(reads)} read"


def test_running_greyscale_without_scanning_first_still_works():
    """Ausführen on a document nothing has analysed yet has to scan and then
    convert, as it always did — but the scan is on a worker now, so the two are
    chained through the event loop instead of running one after the other in
    the same call.

    Driven with the real run_async rather than _sync_async: the chaining is the
    thing being tested, and it turns on a queued timer landing after the scan
    job's `finished` signal."""
    if not shutil.which("gs"):
        return "SKIP (no ghostscript)"
    _open(FX["normal"])
    p = GrayscalePanel(); p.resize(900, 600)
    logged = []
    p.log.log = lambda m, *a, **k: logged.append(str(m))
    out = os.path.join(_TMP, "grey_unscanned.pdf")
    p.save_pdf = lambda *a, **k: out
    opened = []
    p.open_result = lambda path, t="": opened.append(path)

    assert not p._page_data, "the fixture is meant to start unscanned"
    p._run_action()

    import time
    end = time.time() + 90
    while time.time() < end and not opened:
        _app.processEvents(); time.sleep(0.005)

    assert p._page_data, f"the scan never ran:\n{logged}"
    assert opened, f"the conversion never followed the scan:\n{logged}"
    assert os.path.exists(out), "no output file"
    return f"scanned {len(p._page_data)} pages, then converted"


def test_a_document_with_nothing_to_convert_says_so():
    """The same chain, when the scan decides no page qualifies. The message has
    to reach the log — it is raised from a timer callback, where _safe_run is
    not there to turn an exception into a log line."""
    _open(FX["color"])
    p = GrayscalePanel(); p.resize(900, 600)
    logged = []
    p.log.log = lambda m, *a, **k: logged.append(str(m))
    p.save_pdf = lambda *a, **k: os.path.join(_TMP, "grey_none.pdf")
    p.open_result = lambda *a, **k: None
    p._run_action()

    import time
    end = time.time() + 60
    while time.time() < end and not any("Keine Seiten" in m for m in logged):
        _app.processEvents(); time.sleep(0.005)
    assert any("Keine Seiten" in m for m in logged), \
        f"the refusal never reached the log:\n{logged}"
    return "reported, not swallowed"
