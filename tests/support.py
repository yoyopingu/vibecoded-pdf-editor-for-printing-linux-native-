"""
What every test module needs: the application, the fixture PDFs, and the
helpers more than one subject uses.

Importing this is what makes a test module runnable — it puts the repo on the
path, creates the QApplication, and builds the fixture PDFs. FX is built here,
at import, rather than by a runner: it used to be filled in by main(), so under
pytest every test that touched it failed on an empty dict before it began.

Import order below is not arbitrary; see the note on main.
"""
import os, sys, time, tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Before tools.app is imported below, because importing it is what configures
# logging. Left unset, every test run appended its fixture chatter to the log
# the user's real installation writes to — a day of runs buried the actual
# tracebacks under a megabyte of /tmp paths.
os.environ.setdefault("COPYSHOP_LOG_DIR",
                      os.path.join(tempfile.gettempdir(), "copyshop_test_logs"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv)

from pypdf import PdfReader
import pypdfium2 as pdfium
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from PIL import Image

from tools.jobs import null_progress
from tools.app_state import AppState
from tools.viewer.model import PageModel
from tools.panels.nup import NUpPanel

# Not used here, and not removable: the entry-point module pulls in
# PyQt6.QtNetwork, and loading that extension module later — once the render
# threads are running — segfaults inside the import machinery. Importing it now
# is the ordering that works.
import tools.app as MAIN                                            # noqa: F401


MM = 2.8346456693


_TMP = tempfile.mkdtemp(prefix="copyshop_tests_")


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
    # locked: a real user password, nothing can read a page without it
    enc = os.path.join(_TMP, "encrypted.pdf")
    with pikepdf.open(p) as pdf:
        pdf.save(enc, encryption=pikepdf.Encryption(owner="o", user="u"))
    # restricted: an owner password and no user password. Encrypted, and read
    # by every viewer there is — this is what most "password-protected" files
    # in circulation actually are.
    restricted = os.path.join(_TMP, "restricted.pdf")
    with pikepdf.open(p) as pdf:
        pdf.save(restricted, encryption=pikepdf.Encryption(
            owner="o", user="", allow=pikepdf.Permissions(extract=False)))
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
    return {"normal": p, "single": s, "color": col, "encrypted": enc,
            "restricted": restricted, "image": img,
            "framed": fr, "framed_cropbox": box, "framed_rot90": rot, "mixed": mix,
            "booklet32": bk}


def _BK_GREY(i):
    """Grey level identifying source page `i` of the booklet fixture."""
    return 0.90 - 0.02 * i


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
    """Make run_async run synchronously for deterministic tests.

    Follows the real contract rather than only its happy path: progress goes to
    on_progress and a failure goes to on_error when one was given, which is how
    a panel learns its work fell over. Without an on_error the exception
    propagates, exactly as run_async's default does by logging it.
    """
    def run(work, on_done, *, on_error=None, on_progress=None, busy_label=None):
        try:
            result = work(on_progress or (null_progress()))
        except Exception as exc:
            if on_error is None:
                raise
            on_error(exc)
            return
        on_done(result)
    panel.run_async = run


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


def _nup(src, *, cols=1, rows=1, fmt="A4  (210x297mm)", margins=20.0,
         gaps=0.0, name="nup_c.pdf"):
    """Run the real N-Up panel over `src` and return the output path."""
    _open(src)
    p = NUpPanel()
    p.cols.setValue(cols); p.rows.setValue(rows)
    # None means the sheet is sized from the source page and the grid.
    p.out_fmt.set_format(p.AUTO_FORMAT if fmt is None else fmt)
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


def _settle(vp, done, tries=600):
    """Pump the event loop until `done()`. `vp` is unused — kept because most
    callers pass the viewer they are waiting on, which reads better."""
    for _ in range(tries):
        _app.processEvents(); time.sleep(0.02)
        if done():
            return True
    return False


def _spin(n=60, delay=0.02):
    for _ in range(n):
        _app.processEvents(); time.sleep(delay)


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


def _open_single_view(path, w=1000, h=760):
    """A viewer showing `path`, settled on its first real render."""
    from tools.viewer.panel import PageViewerPanel
    from tools.render.caches import _FullPageCache
    # Start from a cold cache: these tests care about what happens when the
    # cached render is coarser or finer than the zoom asked for, and a render
    # left behind by an earlier test decides that for them.
    _FullPageCache.invalidate()
    vp = PageViewerPanel(); vp.resize(w, h); vp.show()
    vp.open_file(path)
    _settle(vp, lambda: vp.tabs.count() and vp.tabs.currentWidget().single._last_pm,
            tries=300)
    sv = vp.tabs.currentWidget().single
    _settle(vp, lambda: sv._render_task is None
            and not getattr(sv, "_showing_provisional", False), tries=300)
    return vp, sv


def _page_labels(path):
    return [p.extract_text().strip().replace("\n", "") for p in PdfReader(path).pages]


# Built once, at import: see the module docstring.
FX = _make_fixtures()
