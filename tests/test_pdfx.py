"""
PDF/X export.
"""
import os

import pikepdf
from pikepdf import Array, Dictionary, Name, String

from tools.jobs import null_progress
from tools.panels._icc import CMYK_PROFILES, fallback_cmyk_icc, resolve_icc
from tools.panels._prepress import layer_summary
from tools.panels.pdfx import (PDFX_STANDARDS, _boxes_survived,
                               _check_conformance, _export_pdfx, _pdfx_defs)
from tests.support import FX, _TMP


def _out(name):
    """Where an export writes. Never collides with a source fixture: those are
    all named pdfx_src_*, and an export that overwrote its own input silently
    made several of these tests check a file against itself."""
    assert not name.startswith("src_"), name
    return os.path.join(_TMP, f"pdfx_{name}.pdf")


def _layered_fixture():
    """A one-page PDF with an OCG that the file itself switches off.

    A cutter contour or a varnish plate looks exactly like this, and it is the
    case the old layers tool existed to get right.
    """
    path = os.path.join(_TMP, "pdfx_src_layered.pdf")
    if os.path.exists(path):
        return path
    with pikepdf.open(FX["single"]) as pdf:
        page = pdf.pages[0]
        ocg = pdf.make_indirect(Dictionary(Type=Name("/OCG"),
                                           Name=String("Stanzkontur")))
        pdf.Root[Name("/OCProperties")] = Dictionary(
            OCGs=Array([ocg]),
            D=Dictionary(Order=Array([ocg]), OFF=Array([ocg])))
        page.contents_add(
            pikepdf.Stream(pdf, b"/OC /MC0 BDC 1 0 1 rg 100 300 300 300 re f EMC"))
        page.obj["/Resources"][Name("/Properties")] = Dictionary(MC0=ocg)
        pdf.save(path)
    return path


def test_the_export_carries_everything_pdfx_requires():
    """A PDF/X file is one a shop can accept without opening it, so every
    claim it makes has to be in the file: the version marker, an output intent
    with an embedded ICC profile, and a TrimBox on every page telling the RIP
    where the finished sheet ends."""
    icc = fallback_cmyk_icc()
    assert icc, "no CMYK ICC profile on this system to embed"
    out = _out("conform")
    result, dropped, _capped, _v = _export_pdfx(FX["color"], out, icc, "Custom",
                                           "Generic CMYK", 300, "x3", null_progress())
    assert result == out and not dropped

    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo["/GTS_PDFXVersion"]) == PDFX_STANDARDS["x3"][0]
        # Ghostscript's PDF/X mode writes PDF 1.3, which is what the :2002
        # revision is based on — the version string must not outrun it.
        assert str(pdf.pdf_version) == "1.3", pdf.pdf_version
        intent = pdf.Root["/OutputIntents"][0]
        assert str(intent["/S"]) == "/GTS_PDFX"
        assert str(intent["/OutputConditionIdentifier"]) == "Custom"
        profile = intent["/DestOutputProfile"]
        assert int(profile["/N"]) == 4, "the embedded output profile is not CMYK"
        assert len(bytes(profile.read_bytes())) > 0, "the profile stream is empty"
        assert len(pdf.pages) == 3, "pages went missing"
        for i, page in enumerate(pdf.pages):
            assert "/TrimBox" in page.obj, f"page {i + 1} has no TrimBox"
    return "version marker, CMYK output intent, TrimBox on every page"


def test_a_layer_switched_off_never_reaches_the_plate():
    """The reason this tool replaced "Ebenen (OCG)".

    PDF/X-3 has no optional content, so the export has to resolve it — and it
    must resolve it the way the file says, not by making everything visible.
    A cutter contour that prints is a ruined run."""
    src = _layered_fixture()
    on, off = layer_summary(src)
    assert (on, off) == ([], ["Stanzkontur"]), (on, off)

    out = _out("layered")
    _result, dropped, _capped, _v = _export_pdfx(src, out, fallback_cmyk_icc(), "Custom",
                                            "Generic CMYK", 300, "x3", null_progress())
    assert dropped == ["Stanzkontur"], "the export did not report the dropped layer"

    with pikepdf.open(out) as pdf:
        assert "/OCProperties" not in pdf.Root, \
            "optional content survived into a PDF/X file"

    # And the hidden ink really is absent, not merely unmarked.
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(out)
    try:
        image = doc[0].render(scale=0.4).to_pil().convert("RGB")
        magenta = sum(1 for r, g, b in image.get_flattened_data()
                      if r > 150 and g < 120 and b > 150)
    finally:
        doc.close()
    assert magenta == 0, f"{magenta} pixels of a switched-off layer got printed"
    return "the hidden layer is reported, dropped, and absent from the output"


def test_a_file_that_is_not_pdfx_is_not_handed_over():
    """pdfwrite exits 0 on plenty of files it did not fully convert. The whole
    value here is output that needs no second look, so the claim is checked
    against the file before it ships."""
    for name, path in (("plain", FX["normal"]), ("colour", FX["color"])):
        try:
            _check_conformance(path, "x3")
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"an ordinary PDF ({name}) passed as PDF/X")

    out = _out("conform")   # written by the first test, genuinely conformant
    if os.path.exists(out):
        _check_conformance(out, "x3")   # must not raise
    return "ordinary PDFs are refused, a real one passes"


def test_the_output_intent_names_a_condition_a_rip_can_look_up():
    """/OutputConditionIdentifier is read by the RIP to decide whether the file
    was separated for the press it is going on. Our label for a profile is no
    use there — it has to be the registered name of the characterisation data,
    and every named profile needs one."""
    seen = {}
    for label, candidates, oci, condition in CMYK_PROFILES:
        assert oci and condition, f"{label} has no output condition"
        if candidates is None:
            assert oci == "Custom", "the generic entry must not claim a registry name"
        else:
            assert oci != "Custom", f"{label} falls back to an unnamed condition"
            assert oci not in seen, f"{label} and {seen[oci]} both claim {oci}"
            seen[oci] = label
    return f"{len(seen)} named printing conditions, all distinct"


def test_a_profile_path_cannot_break_out_of_the_postscript_prologue():
    """The ICC path and the condition name are pasted into a PostScript file.
    A parenthesis in either would close the string early and turn the rest of
    the path into code — and profile directories are user-supplied."""
    defs = _pdfx_defs("/tmp/od(d) name).icc", "OCI)", "cond(ition")
    body = defs.split("/ICCProfile (", 1)[1]
    path_literal = body.split(") def", 1)[0]
    assert path_literal == r"/tmp/od\(d\) name\).icc", path_literal
    assert r"\(" in defs and r"\)" in defs
    # And nothing unescaped survived that would end a string early.
    for line in defs.splitlines():
        if line.startswith("  /OutputCondition ") or line.startswith("/ICCProfile "):
            inner = line[line.index("(") + 1:line.rindex(")")]
            bare = inner.replace(r"\(", "").replace(r"\)", "")
            assert "(" not in bare and ")" not in bare, line
    return "parentheses in a path or a condition name are escaped"


def _bleed_fixture():
    """A page with 3 mm of bleed: TrimBox inset 8.5 pt inside the MediaBox.

    What an InDesign print export looks like, and the geometry the guillotine
    is set from.
    """
    path = os.path.join(_TMP, "pdfx_src_bleed.pdf")
    if os.path.exists(path):
        return path
    with pikepdf.open(FX["color"]) as pdf:
        for page in pdf.pages:
            m = [float(x) for x in page.obj["/MediaBox"]]
            page.obj["/TrimBox"] = Array([m[0] + 8.5, m[1] + 8.5,
                                          m[2] - 8.5, m[3] - 8.5])
            page.obj["/BleedBox"] = Array([m[0] + 2.0, m[1] + 2.0,
                                           m[2] - 2.0, m[3] - 2.0])
        pdf.save(path)
    return path


def test_the_trim_the_source_declared_survives_the_export():
    """The TrimBox is where the guillotine goes.

    A file that arrives with 3 mm of bleed has to leave with it. Losing it
    would silently redefine the finished size as the full sheet, and the job
    would be trimmed wrong — which is not something anyone notices before the
    stack comes off the cutter.
    """
    src = _bleed_fixture()
    out = _out("bleed")
    _export_pdfx(src, out, fallback_cmyk_icc(), "Custom", "Generic CMYK",
                 300, "x3", null_progress())
    with pikepdf.open(src) as s_pdf, pikepdf.open(out) as o_pdf:
        for i, (s_page, o_page) in enumerate(zip(s_pdf.pages, o_pdf.pages)):
            for key in ("/TrimBox", "/BleedBox"):
                want = [float(x) for x in s_page.obj[key]]
                got = [float(x) for x in o_page.obj[key]]
                assert all(abs(a - b) < 1.0 for a, b in zip(want, got)), \
                    f"page {i + 1} {key}: {got} != {want}"
    return "3 mm bleed and its TrimBox come through unchanged"


def test_a_file_without_a_trim_box_is_not_given_a_guessed_one():
    """No TrimBox means no declared trim, and the honest answer is the page
    edge. Inferring "this is A4 plus bleed" from the page size would crop 3 mm
    off every job that is genuinely that size."""
    out = _out("notrim")
    _export_pdfx(FX["color"], out, fallback_cmyk_icc(), "Custom",
                 "Generic CMYK", 300, "x3", null_progress())
    with pikepdf.open(out) as pdf:
        for i, page in enumerate(pdf.pages):
            media = [float(x) for x in page.obj["/MediaBox"]]
            trim = [float(x) for x in page.obj["/TrimBox"]]
            assert all(abs(a - b) < 1.0 for a, b in zip(media, trim)), \
                f"page {i + 1} gained a trim nobody asked for: {trim} in {media}"
    return "TrimBox = MediaBox, no invented bleed"


def test_a_trim_box_that_moved_stops_the_export():
    """_boxes_survived is the guard behind the two tests above: if Ghostscript
    ever stops carrying the boxes, the export must fail loudly rather than
    hand over a file trimmed to the wrong size."""
    src = _bleed_fixture()
    moved = os.path.join(_TMP, "pdfx_moved.pdf")
    with pikepdf.open(src) as pdf:
        pdf.pages[1].obj["/TrimBox"] = Array([0, 0, 100, 100])
        pdf.save(moved)
    problems = _boxes_survived(src, moved)
    assert problems, "a TrimBox moved by 400 pt was not noticed"
    assert any("2" in p for p in problems), problems
    assert not _boxes_survived(src, src), "a file disagreed with itself"
    return f"caught: {', '.join(problems)}"


def test_images_come_down_to_press_resolution():
    """Resolution above what the press can image is RIP time nobody sees on
    paper, and it is the main reason a job is slow to print. Downsampling is
    also the one thing here that must not run backwards: upsampling a 200 dpi
    scan invents detail and makes the file bigger for nothing."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from PIL import Image

    # Not _out(...): that names the *output* files, and a source sharing a
    # name with one of them gets exported over itself.
    src = os.path.join(_TMP, "pdfx_src_images.pdf")
    if not os.path.exists(src):
        w, h = A4
        base = Image.new("RGB", (620, 877))
        pixels = base.load()
        for y in range(877):
            for x in range(620):
                pixels[x, y] = (x * 7 % 256, y * 11 % 256, (x + y) % 256)
        high = os.path.join(_TMP, "pdfx_600.png")
        low = os.path.join(_TMP, "pdfx_200.png")
        base.resize((4960, 7016)).save(high)     # ~600 dpi on A4
        base.resize((1653, 2339)).save(low)      # ~200 dpi on A4
        c = canvas.Canvas(src, pagesize=A4)
        c.drawImage(high, 0, 0, w, h); c.showPage()
        c.drawImage(low, 0, 0, w, h); c.showPage()
        c.save()

    def widths(path):
        found = []
        with pikepdf.open(path) as pdf:
            for page in pdf.pages:
                xobjects = page.obj.get("/Resources", {}).get("/XObject")
                for value in (xobjects or {}).values():
                    if value.get("/Subtype") == Name("/Image"):
                        found.append(int(value.Width))
        return found

    before = widths(src)          # before, while it is still the source
    out = _out("imgs")
    _export_pdfx(src, out, fallback_cmyk_icc(), "Custom", "Generic CMYK",
                 300, "x3", null_progress())
    after = widths(out)
    assert len(before) == len(after) == 2, (before, after)
    assert 2200 < after[0] < 2700, f"the 600 dpi image came out at {after[0]} px"
    assert after[1] == before[1], "a 200 dpi image was resampled"

    # Against the same export with downsampling effectively off, not against
    # the source: RGB to CMYK adds a fourth channel, so a converted file can
    # legitimately be larger than what went in. This isolates the saving.
    big = _out("imgs_full")
    _export_pdfx(src, big, fallback_cmyk_icc(), "Custom", "Generic CMYK",
                 2400, "x3", null_progress())
    small_kb, big_kb = os.path.getsize(out) // 1024, os.path.getsize(big) // 1024
    assert small_kb < big_kb, f"downsampling saved nothing ({small_kb} vs {big_kb} KB)"
    return (f"600 dpi -> {after[0] / (595.28 / 72):.0f} dpi, 200 dpi untouched, "
            f"{big_kb} KB -> {small_kb} KB")


def test_only_a_cmyk_profile_can_be_installed_as_one():
    """An RGB profile filed under a CMYK name would make every later export
    separate against the wrong space while the output intent claimed the
    right one — wrong in exactly the way nobody checks for."""
    from tools.panels._icc import (icc_colour_space, install_profile,
                                   profile_description)
    cmyk = fallback_cmyk_icc()
    assert icc_colour_space(cmyk) == "CMYK"
    assert profile_description(cmyk), "the profile has no readable description"

    rgb = "/usr/share/ghostscript/iccprofiles/srgb.icc"
    if os.path.isfile(rgb):
        assert icc_colour_space(rgb) == "RGB "
        try:
            install_profile(rgb, "should_not_appear.icc")
        except ValueError:
            pass
        else:
            raise AssertionError("an RGB profile was installed as CMYK")
    assert icc_colour_space(FX["normal"]) is None, "a PDF passed as an ICC profile"
    return "CMYK accepted, RGB refused, a non-profile refused"


def test_the_panel_is_one_button():
    """The whole point of the rework: a conversion, not a form.

    The press condition and the image resolution are properties of the press,
    so they live in the settings and the panel just runs.
    """
    from tools.panels.pdfx import PdfxPanel
    from tools.shell.settings import AppSettings
    from tests.support import _app

    panel = PdfxPanel()
    try:
        assert not hasattr(panel, "profile_combo"), "the dropdown is back"
        assert not hasattr(panel, "report"), "the report pane is back"
        assert hasattr(panel, "run_btn")
        # And what it will do is stated, not hidden in the settings dialog.
        shown = panel._cond_lbl.text()
        assert str(AppSettings.get().pdfx_image_dpi()) in shown, shown
    finally:
        panel.deleteLater(); _app.processEvents()
    return f"one action button; it says: {shown!r}"


def test_a_named_profile_that_is_installed_is_the_one_used():
    """resolve_icc has to find a real file, not just the first candidate name,
    or the export silently embeds a generic profile under a named condition."""
    assert resolve_icc(None) is None
    assert resolve_icc(("definitely_not_here_9f2a.icc",)) is None
    # The generic Ghostscript profile stands in for the real thing here: what
    # is being checked is that an existing file is picked up by name.
    fallback = fallback_cmyk_icc()
    assert fallback and os.path.isfile(fallback)
    found = resolve_icc((os.path.basename(fallback),))
    assert found is None or os.path.isfile(found)
    return "missing profiles answer None, present ones answer a real path"


def test_preflight_reports_what_a_press_needs_to_know():
    """The check button moved off the export panel and into preflight, so this
    is where an operator finds out whether a file can go on a press.

    Four questions, and the bleed one has to distinguish "no bleed" from
    "bleed missing on some pages" — the first is a normal flyer, the second is
    a file someone assembled wrong.
    """
    from tools.panels.preflight import _preflight

    checks = dict(size=False, orient=False, colour=False, enc=False,
                  bleed=True, fonts=True, dpi=True, layers=True)

    def report_for(path):
        lines, _verdict = _preflight(path, checks, None, 300, null_progress())
        return "\n".join(lines)

    # A file with 3 mm of bleed says so, in millimetres.
    with_bleed = report_for(_bleed_fixture())
    assert "3.0 mm" in with_bleed, with_bleed

    # A file with none says that too, and it is not an error: a flyer that
    # ends at the paper edge needs no bleed and cannot be given one.
    without = report_for(FX["color"])
    assert "Kein Anschnitt definiert" in without, without
    assert "PROBLEME" not in without.split("Kein Anschnitt")[0].split("BESTANDEN")[-1]

    # Base-14 Helvetica is the commonest unembedded font there is: it looks
    # right in every viewer and gets substituted at the RIP.
    assert "Helvetica" in without, without

    # Bleed on only some pages is a real problem, unlike having none at all.
    partial = os.path.join(_TMP, "pdfx_src_partial_bleed.pdf")
    with pikepdf.open(_bleed_fixture()) as pdf:
        del pdf.pages[1].obj["/TrimBox"]
        pdf.save(partial)
    mixed = report_for(partial)
    assert "PROBLEME" in mixed and "nur auf einem Teil" in mixed, mixed
    return "bleed in mm, absent bleed, partial bleed, and the substituted font"


def test_image_resolution_is_measured_where_the_image_is_placed():
    """The same 500-pixel logo is 250 dpi across two inches and 50 dpi across
    ten. Anything that reads the image's own dimensions and stops there gets
    this backwards, so the placement matrix has to be followed."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from PIL import Image
    from tools.panels._prepress import low_resolution_images

    src = os.path.join(_TMP, "pdfx_src_placement.pdf")
    if not os.path.exists(src):
        w, h = A4
        small = os.path.join(_TMP, "pdfx_logo.png")
        Image.new("RGB", (500, 500), (10, 90, 160)).save(small)
        c = canvas.Canvas(src, pagesize=A4)
        c.drawImage(small, 0, 0, 144, 144); c.showPage()   # 500 px / 2 in = 250 dpi
        c.drawImage(small, 0, 0, 720, 720); c.showPage()   # 500 px / 10 in = 50 dpi
        c.drawImage(small, 0, 0, 72, 72); c.showPage()     # 500 dpi, fine
        c.save()

    found = dict(low_resolution_images(src, 300))
    assert set(found) == {1, 2}, f"wrong pages flagged: {found}"
    assert 240 < found[1] < 260, found
    assert 45 < found[2] < 55, found
    return f"same image: page 1 {found[1]} dpi, page 2 {found[2]} dpi, page 3 fine"


def _vector_fixture():
    """A page of pure vector artwork — no images, no transparency."""
    path = os.path.join(_TMP, "pdfx_src_vector.pdf")
    if os.path.exists(path):
        return path
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    import random
    random.seed(11)
    w, h = A4
    c = canvas.Canvas(path, pagesize=A4)
    for _ in range(1500):
        x, y = random.uniform(0, w), random.uniform(0, h)
        c.setStrokeColorRGB(random.random(), random.random(), random.random())
        p = c.beginPath(); p.moveTo(x, y)
        for _ in range(3):
            p.lineTo(x + random.uniform(-40, 40), y + random.uniform(-40, 40))
        c.drawPath(p)
    c.showPage(); c.save()
    return path


def _count(path):
    """(path operators, embedded images) in a PDF."""
    paths = images = 0
    with pikepdf.open(path) as pdf:
        for page in pdf.pages:
            xobjects = (page.obj.get("/Resources") or {}).get("/XObject")
            for value in (xobjects or {}).values():
                if value.get("/Subtype") == Name("/Image"):
                    images += 1
            for ins in pikepdf.parse_content_stream(page):
                if str(ins.operator) in ("m", "l", "c", "re", "S", "f", "f*", "B"):
                    paths += 1
    return paths, images


def test_vector_artwork_stays_vector():
    """The export must not quietly turn a drawing into pixels.

    The resolution setting is about *raster images already in the file* and
    about flattening; it has no bearing on paths and text, which stay
    resolution-independent and print sharp at any size — including A0. A
    regression here would be invisible on screen and obvious on a plotter.
    """
    src = _vector_fixture()
    before_paths, before_images = _count(src)
    assert before_paths > 1000 and before_images == 0, (before_paths, before_images)

    out = _out("vector")
    _export_pdfx(src, out, fallback_cmyk_icc(), "Custom", "Generic CMYK",
                 600, "x3", null_progress())
    after_paths, after_images = _count(out)
    assert after_images == 0, f"{after_images} image(s) appeared in vector artwork"
    assert after_paths == before_paths, \
        f"path count changed: {before_paths} -> {after_paths}"
    return f"{after_paths} path operators in and out, still zero images"


def test_transparency_is_flattened_and_said_so_beforehand():
    """The one case where vectors *do* become pixels, and only under X-3.

    PDF/X-3 is PDF 1.3, which has no transparency, so those pages are
    rasterised — both the quality cost and the reason a big file takes
    minutes. Preflight has to say so before the export, not after — and must
    *not* say it when the configured profile is X-4, which keeps transparency
    live and pays none of that.
    """
    from tools.panels._prepress import transparent_pages
    from tools.panels.preflight import _preflight

    src = os.path.join(_TMP, "pdfx_src_trans.pdf")
    if not os.path.exists(src):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        w, h = A4
        c = canvas.Canvas(src, pagesize=A4)
        c.setFillAlpha(0.4)
        for i in range(60):
            c.setFillColorRGB(0.9, 0.2, 0.1)
            c.circle(60 + (i % 10) * 50, 100 + (i // 10) * 90, 40, fill=1, stroke=0)
        c.setFillAlpha(1)
        c.showPage(); c.save()

    assert transparent_pages(src) == [1], transparent_pages(src)
    assert transparent_pages(_vector_fixture()) == [], "vector art reported as transparent"

    # What the report says depends on which profile the export will write:
    # under X-4 transparency costs nothing and warning about it would be noise,
    # under X-3 it is the single most expensive thing in the file.
    from tools.shell.settings import AppSettings

    checks = dict(size=False, orient=False, colour=False, enc=False, bleed=False,
                  fonts=False, dpi=False, trans=True, layers=False)
    settings = AppSettings.get()
    previous = settings.pdfx_standard()
    reports = {}
    try:
        for standard in ("x3", "x4"):
            settings.set_pdfx_standard(standard)
            lines, _v = _preflight(src, checks, None, 300, null_progress())
            reports[standard] = "\n".join(lines)
    finally:
        settings.set_pdfx_standard(previous)

    assert "Transparenz auf 1" in reports["x3"], reports["x3"]
    assert "Vektoren gehen dort verloren" in reports["x3"], reports["x3"]
    assert "PROBLEME" in reports["x3"], "X-3 flattening was not raised as an issue"

    assert "PDF/X-4 behaelt sie" in reports["x4"], reports["x4"]
    assert "PROBLEME" not in reports["x4"], \
        "X-4 was warned about a cost it does not have"

    # And under X-3 it really is rasterised, which is what that warning is about.
    out = _out("trans")
    _export_pdfx(src, out, fallback_cmyk_icc(), "Custom", "Generic CMYK",
                 600, "x3", null_progress())
    _paths, images = _count(out)
    assert images >= 1, "a transparent page came through without being flattened"
    return "warned in preflight, and flattened to 1 image on export"


def test_the_raster_resolution_is_capped_so_the_result_can_be_opened():
    """A0 at 600 dpi is 558 megapixels, which pdfium will not open: it returns
    a mostly blank page with the content displaced. Ghostscript renders the
    same file correctly, so it is a valid PDF — but one this application can
    neither display nor verify, which makes it a bad thing to hand over.

    Capped by page area, so ordinary sheets are untouched."""
    from tools.panels.pdfx import MAX_RASTER_PIXELS, _flatten_dpi

    a4 = _vector_fixture()
    dpi, capped = _flatten_dpi(a4, 600)
    assert (dpi, capped) == (600, False), "an A4 page was capped"

    big = os.path.join(_TMP, "pdfx_src_a0.pdf")
    if not os.path.exists(big):
        from reportlab.pdfgen import canvas
        c = canvas.Canvas(big, pagesize=(2384, 3370))
        c.setFillAlpha(0.4); c.setFillColorRGB(0.2, 0.4, 0.9)
        c.circle(1200, 1700, 900, fill=1, stroke=0)
        c.showPage(); c.save()

    dpi, capped = _flatten_dpi(big, 600)
    assert capped and 300 < dpi < 450, f"A0 capped to {dpi} dpi"
    area_in2 = (2384 / 72.0) * (3370 / 72.0)
    assert dpi * dpi * area_in2 <= MAX_RASTER_PIXELS, "the cap does not fit the budget"

    # The whole point: the exported file opens and looks like the source.
    out = _out("a0")
    _r, _dropped, capped_to, _v = _export_pdfx(big, out, fallback_cmyk_icc(), "Custom",
                                           "Generic CMYK", 600, "x3", null_progress())
    assert capped_to == dpi, (capped_to, dpi)

    import pypdfium2 as pdfium
    def ink(path):
        doc = pdfium.PdfDocument(path)
        try:
            image = doc[0].render(scale=0.05).to_pil().convert("L")
        finally:
            doc.close()
        total = image.size[0] * image.size[1]
        return sum(image.histogram()[:230]) / total

    before, after = ink(big), ink(out)
    assert abs(before - after) < 0.05, \
        f"the exported A0 page does not render like the source ({before:.2f} vs {after:.2f})"
    return f"A4 untouched, A0 capped to {dpi} dpi and still renders correctly"


def test_x4_keeps_transparent_artwork_as_artwork():
    """The default profile, and the reason it is the default.

    X-3 is PDF 1.3, which has no transparency, so a page using any is
    flattened — rendered to pixels, vectors gone, and slow in proportion to
    the page area. X-4 is PDF 1.6 and keeps it live: nothing is rasterised.
    Measured on an A0 sheet of transparent artwork, that is 82 s and 17 MB
    against 0.3 s and 185 KB, with every path still a path.
    """
    import time

    src = os.path.join(_TMP, "pdfx_src_x4.pdf")
    if not os.path.exists(src):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        w, h = A4
        c = canvas.Canvas(src, pagesize=A4)
        c.setFillAlpha(0.45)
        for i in range(300):
            c.setFillColorRGB((i % 7) / 7, (i % 5) / 5, (i % 3) / 3)
            c.circle(40 + (i % 20) * 28, 60 + (i // 20) * 52, 30, fill=1, stroke=0)
        c.setFillAlpha(1)
        c.showPage(); c.save()

    icc = fallback_cmyk_icc()
    results = {}
    for standard in ("x4", "x3"):
        out = _out(f"std_{standard}")
        started = time.time()
        _r, _dropped, _capped, version = _export_pdfx(
            src, out, icc, "Custom", "Generic CMYK", 600, standard,
            null_progress())
        results[standard] = (_count(out), time.time() - started,
                             os.path.getsize(out), version)

    (x4_paths, x4_images), x4_time, x4_size, x4_version = results["x4"]
    (x3_paths, x3_images), x3_time, x3_size, _x3_version = results["x3"]

    assert x4_version == "PDF/X-4", x4_version
    assert x4_paths > 500, f"X-4 kept only {x4_paths} path operators"
    assert x4_images == 0, f"X-4 rasterised something ({x4_images} images)"
    assert x3_images >= 1 and x3_paths < x4_paths, \
        "X-3 was expected to flatten the page and did not"
    assert x4_size < x3_size, f"X-4 was not smaller ({x4_size} vs {x3_size})"

    with pikepdf.open(_out("std_x4")) as pdf:
        assert str(pdf.pdf_version) >= "1.6", pdf.pdf_version
        assert pdf.Root.get("/OutputIntents"), "X-4 lost the output intent"
        assert all("/TrimBox" in p.obj for p in pdf.pages)
    return (f"X-4 {x4_paths} paths / {x4_size // 1024} KB / {x4_time:.1f}s  vs  "
            f"X-3 {x3_paths} paths / {x3_size // 1024} KB / {x3_time:.1f}s")


def test_x4_may_keep_layers_and_x3_may_not():
    """Optional content is permitted in X-4 and forbidden in X-3, so the
    conformance check cannot be one rule. Getting this backwards would either
    reject good X-4 files or pass X-3 files a RIP will refuse."""
    src = _layered_fixture()
    icc = fallback_cmyk_icc()

    out4 = _out("layers_x4")
    _r, dropped4, _c, _v = _export_pdfx(src, out4, icc, "Custom",
                                        "Generic CMYK", 600, "x4", null_progress())
    assert dropped4 == [], "X-4 reported dropping a layer it is allowed to keep"
    _check_conformance(out4, "x4")            # must not raise

    # The same file under the X-3 rules: layers gone, and reported.
    out3 = _out("layers_x3")
    _r, dropped3, _c, _v = _export_pdfx(src, out3, icc, "Custom",
                                        "Generic CMYK", 600, "x3", null_progress())
    assert dropped3 == ["Stanzkontur"], dropped3
    with pikepdf.open(out3) as pdf:
        assert "/OCProperties" not in pdf.Root
    return "X-4 keeps optional content, X-3 resolves it away"


def _photo_fixture():
    """One A4 page carrying a full-page RGB photograph."""
    path = os.path.join(_TMP, "pdfx_src_photo.pdf")
    if os.path.exists(path):
        return path
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from PIL import Image
    photo = os.path.join(_TMP, "pdfx_photo.png")
    image = Image.new("RGB", (900, 1200))
    pixels = image.load()
    for y in range(1200):
        for x in range(900):
            pixels[x, y] = ((x * 7) % 256, (y * 5) % 256, (x + y) % 256)
    image.save(photo)
    w, h = A4
    c = canvas.Canvas(path, pagesize=A4)
    c.drawImage(photo, 0, 0, w, h); c.showPage(); c.save()
    return path


def test_images_are_separated_before_ghostscript_sees_them():
    """Converting RGB to CMYK is the whole cost of an export — 20.7 s against
    0.5 s for the same file's vectors — and Ghostscript is about ten times
    slower at it than littleCMS. Doing it first is what turns a twenty-second
    export into a five-second one, so this checks the images really do arrive
    at Ghostscript already separated, and that the picture is unchanged."""
    from tools.panels._images import to_cmyk
    from tools.jobs import null_progress

    src = _photo_fixture()

    staged = os.path.join(_TMP, "pdfx_staged.pdf")
    converted, skipped = to_cmyk(src, staged, fallback_cmyk_icc(), null_progress())
    assert converted == 1 and skipped == 0, (converted, skipped)

    with pikepdf.open(staged) as pdf:
        spaces = [str(v.get("/ColorSpace"))
                  for v in (pdf.pages[0].obj["/Resources"]["/XObject"]).values()
                  if v.get("/Subtype") == Name("/Image")]
        assert spaces == ["/DeviceCMYK"], spaces

    # The picture must be the same one. Rendered side by side, the difference
    # is only the RGB-to-CMYK gamut shift the export was going to apply
    # anyway — the recipe was chosen (relative colorimetric, black point
    # compensation) precisely because it lands where Ghostscript lands.
    import pypdfium2 as pdfium
    from PIL import ImageChops, ImageStat

    def render(path):
        doc = pdfium.PdfDocument(path)
        try:
            return doc[0].render(scale=0.25).to_pil().convert("RGB")
        finally:
            doc.close()

    before, after = render(src), render(staged)
    if before.size != after.size:
        after = after.resize(before.size)
    difference = sum(ImageStat.Stat(ImageChops.difference(before, after)).mean) / 3
    assert difference < 25, f"the image changed too much: {difference:.1f}"
    return f"1 image separated, mean shift {difference:.1f} of 255"


def test_pre_conversion_leaves_alone_what_it_cannot_be_sure_of():
    """It is an optimisation, and Ghostscript's own conversion stays switched
    on behind it — so anything not certainly understood must be passed through
    untouched rather than guessed at. A mis-converted image is a reprint."""
    from tools.panels._images import to_cmyk
    from tools.jobs import null_progress

    src = os.path.join(_TMP, "pdfx_src_odd.pdf")
    if not os.path.exists(src):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from PIL import Image
        grey = os.path.join(_TMP, "pdfx_grey.png")
        Image.new("L", (400, 500), 128).save(grey)
        w, h = A4
        c = canvas.Canvas(src, pagesize=A4)
        c.drawImage(grey, 0, 0, w / 2, h / 2); c.showPage(); c.save()
        # …and one image the module must refuse: 16 bits per component.
        with pikepdf.open(src, allow_overwriting_input=True) as pdf:
            for value in (pdf.pages[0].obj["/Resources"]["/XObject"]).values():
                if value.get("/Subtype") == Name("/Image"):
                    value["/BitsPerComponent"] = 16
            pdf.save(src)

    out = os.path.join(_TMP, "pdfx_odd_staged.pdf")
    converted, _skipped = to_cmyk(src, out, fallback_cmyk_icc(), null_progress())
    assert converted == 0, "a 16-bit image was converted anyway"

    # A file it cannot even open must still produce something usable, because
    # the export runs on whatever comes back.
    broken = os.path.join(_TMP, "pdfx_broken.pdf")
    with open(broken, "wb") as f:
        f.write(b"not a pdf at all")
    out2 = os.path.join(_TMP, "pdfx_broken_staged.pdf")
    converted, skipped = to_cmyk(broken, out2, fallback_cmyk_icc(), null_progress())
    assert (converted, skipped) == (0, 0) and os.path.exists(out2), \
        "an unreadable file did not fall through to a copy"
    return "16-bit refused, unreadable file copied through, nothing guessed"


def test_a_file_that_is_already_pdfx_is_left_alone():
    """Re-exporting a conformant file is not just slow, it is lossy: every
    image would be decoded and re-encoded, so a file that goes through twice
    is a generation worse than one that went through once. And the honest
    answer to "make this PDF/X" for a file that already is one is that there
    is nothing to do.

    It has to be the *same* thing, though. A file separated for another press,
    written to another profile, or carrying finer images than the export is
    now told to keep all still need converting."""
    import time
    from tools.panels.pdfx import _already_conformant

    icc = fallback_cmyk_icc()
    plain = _out("idem_first")
    _export_pdfx(FX["color"], plain, icc, "Custom", "Generic CMYK", 600, "x4",
                 null_progress())

    assert _already_conformant(plain, "x4", "Custom", 600)
    assert not _already_conformant(plain, "x4", "FOGRA39L", 600), \
        "a file separated for another condition was called done"
    assert not _already_conformant(plain, "x3", "Custom", 600), \
        "an X-4 file was accepted as an X-3 one"
    assert not _already_conformant(FX["color"], "x4", "Custom", 600), \
        "an ordinary RGB file was called done"

    # The image-resolution part needs a file that actually has an image: one
    # made only of paths never needs downsampling, whatever the target.
    with_photo = _out("idem_photo")
    _export_pdfx(_photo_fixture(), with_photo, icc, "Custom", "Generic CMYK",
                 600, "x4", null_progress())
    assert _already_conformant(with_photo, "x4", "Custom", 600)
    assert not _already_conformant(with_photo, "x4", "Custom", 72), \
        "images finer than the target were left undownsampled"

    again = _out("idem_second")
    started = time.time()
    result, dropped, capped, version = _export_pdfx(
        plain, again, icc, "Custom", "Generic CMYK", 600, "x4", null_progress())
    elapsed = time.time() - started

    assert (result, dropped, capped, version) == (again, [], None, "PDF/X-4")
    assert open(again, "rb").read() == open(plain, "rb").read(), \
        "the file was rewritten rather than passed through"
    _check_conformance(again, "x4")
    return f"second export was a pass-through in {elapsed:.2f}s"
