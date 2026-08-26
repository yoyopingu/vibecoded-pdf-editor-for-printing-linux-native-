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
import subprocess
from contextlib import contextmanager

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Before tools.app is imported below, because importing it is what configures
# logging. Left unset, every test run appended its fixture chatter to the log
# the user's real installation writes to — a day of runs buried the actual
# tracebacks under a megabyte of /tmp paths.
os.environ.setdefault("COPYSHOP_LOG_DIR",
                      os.path.join(tempfile.gettempdir(), "copyshop_test_logs"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Before any QSettings exists, and before tools.app is imported below.
#
# Every settings object in this application is QSettings("CopyShop",
# "PDFSuite"), which resolves to ~/.config/CopyShop/PDFSuite.conf — the file
# the operator's own installation uses. Left alone, a test run takes its
# defaults from whatever that person last changed in the Einstellungen
# dialogs, and writes its own scratch values back over them.
#
# Both halves of that have bitten: turning on "Seiten fortlaufend scrollen" in
# the app made seventeen render tests fail, because they assume the paged
# layout and were suddenly measuring a continuous strip — a real preference
# breaking an unrelated suite, with nothing in the failure to say so. And in
# the other direction the suite has been quietly editing preferences that do
# not belong to it.
#
# setPath redirects the whole user scope into the throwaway test directory, so
# every QSettings built from here on — tools/paper.py and
# tools/printing/prefs.py included — starts empty and stays local.
from PyQt6.QtCore import QSettings as _QSettings
_SETTINGS_DIR = tempfile.mkdtemp(prefix="copyshop_test_settings_")
for _fmt in (_QSettings.Format.NativeFormat, _QSettings.Format.IniFormat):
    _QSettings.setPath(_fmt, _QSettings.Scope.UserScope, _SETTINGS_DIR)

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


# _TMP must stay per-process unique (mkdtemp's random suffix is what does that):
# the future parallel runner runs each test module in its own process with no
# shared state, so a fixed name here would make modules stomp each other's
# fixture files.
_TMP = tempfile.mkdtemp(prefix="copyshop_tests_")


def shutdown_app_background():
    """Stop every thread that would otherwise still be touching Qt objects
    while the interpreter tears the QApplication down at exit.

    The app wires this to QApplication.aboutToQuit, which never fires in the
    tests because no test runs an event loop — so the render thread and the
    global job pool stay live, and Qt destroying the QApplication out from
    under them segfaults the process after the results are printed (green run,
    exit 139, roughly three times in eight). tests/conftest.py calls the same
    thing at pytest_sessionfinish; this is the runner's equivalent so
    tests/run.py can stop the threads instead of os._exit()ing past them.

    What it stops: the render worker thread, every pending/cancellable job on
    Qt's global QThreadPool (and it waits for the pool to drain), and the open
    pdfium document handles. Each stage is wrapped in its own try/except and
    the whole thing is idempotent — safe to call more than once, and safe when
    Qt has already begun its own teardown.
    """
    from tools.render.queue import shutdown_render_queue
    shutdown_render_queue()


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
    # booklet32 is built on first access, not here. It is 32 pages of reportlab
    # work consumed only by test_tools_nup and test_render; building it eagerly
    # cost the other eighteen modules a second or two for nothing. The dict
    # below has __missing__ wired to _build_booklet32 so the existing
    # FX["booklet32"] lookup continues to work, and the build happens exactly
    # once per process.
    return {"normal": p, "single": s, "color": col, "encrypted": enc,
            "restricted": restricted, "image": img,
            "framed": fr, "framed_cropbox": box, "framed_rot90": rot, "mixed": mix}


def _build_booklet32():
    W, H = A4
    bk = os.path.join(_TMP, "booklet32.pdf")
    if os.path.exists(bk):
        return bk
    c = canvas.Canvas(bk, pagesize=A4)
    for i in range(32):
        c.setFillGray(_BK_GREY(i)); c.rect(0, 0, W, H, fill=1, stroke=0)
        c.setFillGray(0.0);         c.rect(0, 0, W, H * 0.05, fill=1, stroke=0)
        c.showPage()
    c.save()
    return bk


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
    """A FIXED-duration pump: always runs all n iterations, no early exit.
    _settle is the bounded variant that returns as soon as `done()` is true."""
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
class _LazyFX(dict):
    """FX with one fixture built on demand rather than at import.

    ``booklet32`` is 32 pages of reportlab work consumed by exactly two modules
    (test_tools_nup, test_render). Building it eagerly cost the other eighteen
    modules a second or two for nothing. The dict subclass keeps the existing
    ``FX["booklet32"]`` lookup working; the build happens on first access and
    is cached for the rest of the process.
    """
    def __missing__(self, key):
        if key != "booklet32":
            raise KeyError(key)
        path = _build_booklet32()
        self[key] = path
        return path


FX = _LazyFX(_make_fixtures())


def _as_bytes(v):
    """Coerce `v` to bytes: pass bytes through, encode str as utf-8."""
    return v if isinstance(v, bytes) else v.encode("utf-8")


class FakeCompletedProcess:
    """A completed subprocess, hand-rolled so tests can fake code paths that
    shell out via subprocess.run/Popen and inspect the result. No mocks.

    Mirrors what call sites actually read from a CompletedProcess: returncode,
    stdout, stderr. stdout/stderr are stored as bytes (str input is encoded
    utf-8) so downstream parsing (page counts, "Error" lines) sees byte
    content, matching the real subprocess default.
    """
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = _as_bytes(stdout)
        self.stderr = _as_bytes(stderr)

    def check_returncode(self, cmd=None):
        """Raise if returncode != 0, mirroring subprocess.CompletedProcess."""
        if self.returncode:
            raise subprocess.CalledProcessError(self.returncode, cmd)

    def ok(self):
        """True when the fake exited 0."""
        return self.returncode == 0


class _FakeHelper:
    """The object `fake_ghostscript` yields: a place for the test's Pattern A
    spy to record its (args, kwargs), and a canned way to turn a captured gs
    argv into a successful FakeCompletedProcess by copying the input PDF to the
    output target verbatim."""

    def __init__(self, canned_stdout, copy_input_to_output=True):
        self.calls = []
        self._canned_stdout = canned_stdout
        self._copy = copy_input_to_output

    def copy_input_to_output(self, args):
        """Given a gs argv, write the input PDF to the output target and
        return a FakeCompletedProcess(returncode=0, stdout=canned_stdout).

        The input is the positional file arg that follows -f, or the last
        positional otherwise. The output target is parsed from the first of
        -sOutputFile=/path, -sOutputFile /path, -o=/path or -o /path. With
        copy_input_to_output=False this skips touching the filesystem and only
        returns the fake — for failure-path tests that inspect the command
        without producing output.
        """
        output = None
        for i, a in enumerate(args):
            if a in ("-sOutputFile", "-o") and i + 1 < len(args):
                output = args[i + 1]
            elif a.startswith(("-sOutputFile=", "-o=")):
                output = a.split("=", 1)[1]
            if output is not None:
                break
        if output is None:
            raise ValueError(
                "no output flag (-sOutputFile=/ -o) in gs argv: %r" % (args,))
        pos = [a for a in args if not a.startswith("-") and a != output]
        if "-f" in args:
            i = args.index("-f")
            src = args[i + 1] if i + 1 < len(args) else pos[-1] if pos else None
        else:
            src = pos[-1] if pos else None
        if src is None:
            raise ValueError("no input file in gs argv: %r" % (args,))
        if self._copy:
            parent = os.path.dirname(output)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(src, "rb") as f:
                data = f.read()
            with open(output, "wb") as f:
                f.write(data)
        return FakeGhostscript(returncode=0, stdout=self._canned_stdout)


FakeGhostscript = FakeCompletedProcess


@contextmanager
def fake_ghostscript(module, *, canned_stdout=b"", copy_input_to_output=True):
    """Swap `module.ghostscript_binary` for a fake returning a fake gs path.

    Mirrors the hand-rolled save/replace/try/finally pattern at
    test_printing.py:342-358 — the consumer module binds the FUNCTION
    `ghostscript_binary` into its own namespace at import, so the fake must
    replace the consumer module's attribute, not the one in tools/ghostscript.

    The fake returns the truthy sentinel path "__fake_gs__" (the real function
    returns a path or None, and callers do `if ghostscript_binary(): ...`
    before building a subprocess command with it). A test that lets the gs
    command actually execute must also stub subprocess.run (Pattern A) to
    intercept it; `canned_stdout` defaults to b"" and callers override it with
    whatever stdout their code path parses.

    Yields a helper whose `.calls` records every (args, kwargs) tuple the
    test's Pattern A spy appended, and whose `copy_input_to_output(args)` turns
    a captured gs argv into a FakeCompletedProcess success while copying the
    input PDF to the output file verbatim (pass copy_input_to_output=False to
    skip that last behaviour, e.g. for failure-path tests that just inspect the
    command without producing output).
    """
    real = getattr(module, "ghostscript_binary")
    helper = _FakeHelper(canned_stdout, copy_input_to_output)

    def fake(*args, **kwargs):
        return "__fake_gs__"

    try:
        module.ghostscript_binary = fake
        yield helper
    finally:
        module.ghostscript_binary = real
