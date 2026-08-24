"""
Stopping a running tool.
"""
import os
import subprocess
import time

from tools.jobs import Cancelled, Progress, null_progress
from tests.support import FX, _TMP, _app, _open, _spin


class _FakeJob:
    """A Job as far as Progress is concerned."""

    def __init__(self):
        self.cancelled = False
        self.messages = []

    def report(self, message):
        self.messages.append(message)


def test_progress_is_still_just_a_reporter():
    """Every worker body takes `report` and calls it. Turning that into an
    object that also carries cancellation must not have changed how it is
    called, or forty call sites would need touching to gain the feature."""
    job = _FakeJob()
    report = Progress(job)
    report("Seite 1 …")
    report("Seite 2 …")
    assert job.messages == ["Seite 1 …", "Seite 2 …"]
    assert report.cancelled is False
    report.check()                       # must not raise
    job.cancelled = True
    assert report.cancelled is True
    try:
        report.check()
    except Cancelled:
        pass
    else:
        raise AssertionError("check() did not raise once cancelled")
    return "callable as before, and now answers whether to stop"


def test_stopping_kills_the_child_process():
    """The reason Progress.run exists.

    The heavy tools spend their minutes inside Ghostscript, so a Stop that
    cannot reach into the child is a Stop that does nothing until the child is
    finished anyway — which is the thing being escaped.
    """
    job = _FakeJob()
    report = Progress(job)

    import threading
    started = time.time()
    threading.Timer(0.4, lambda: setattr(job, "cancelled", True)).start()
    try:
        report.run(["sleep", "30"])
    except Cancelled:
        pass
    else:
        raise AssertionError("a cancelled subprocess returned normally")
    elapsed = time.time() - started
    assert elapsed < 5, f"took {elapsed:.1f}s to stop a cancelled child"

    # And it is gone, not merely abandoned to keep a core busy.
    leftover = subprocess.run(["pgrep", "-f", "^sleep 30$"],
                              capture_output=True, text=True).stdout.strip()
    assert not leftover, f"sleep survived the cancel: {leftover}"
    return f"child stopped and reaped in {elapsed:.1f}s"


def test_progress_run_still_behaves_like_subprocess_run():
    """It replaced subprocess.run in five tools, so it has to return the same
    thing: a completed process with a return code and captured output."""
    report = null_progress()
    ok = report.run(["printf", "hello"], text=True)
    assert ok.returncode == 0 and ok.stdout == "hello", (ok.returncode, ok.stdout)
    bad = report.run(["sh", "-c", "echo oops >&2; exit 3"], text=True)
    assert bad.returncode == 3 and "oops" in bad.stderr, (bad.returncode, bad.stderr)

    try:
        report.run(["sleep", "30"], timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("the timeout was not honoured")
    return "return code, stdout, stderr and timeout all behave"


def test_every_tool_that_runs_in_the_background_can_be_stopped():
    """Structural, so a panel added later is covered without anyone
    remembering: if it hands work to run_async, it must offer a way out."""
    import ast
    import os as _os

    offenders = []
    panel_dir = _os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), "tools", "panels")
    for name in sorted(_os.listdir(panel_dir)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        source = open(_os.path.join(panel_dir, name)).read()
        if "run_async" not in source:
            continue
        tree = ast.parse(source)
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        if not any(any(getattr(b, "id", getattr(b, "attr", "")) == "BasePanel"
                       for b in c.bases) for c in classes):
            continue
        # The Stop button lives on BasePanel.build_action_row, so a panel that
        # overrides it without one has quietly opted out.
        overrides = [n for c in classes for n in c.body
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "build_action_row"]
        for fn in overrides:
            body = ast.unparse(fn)
            if "stop_btn" not in body and "add_stop_button" not in body:
                offenders.append(f"{name}:{fn.name}")
    assert not offenders, "panels with no way to stop: " + ", ".join(offenders)
    return "no panel overrides the action row without a Stop button"


def test_stopping_an_export_writes_nothing_and_leaves_no_ghostscript():
    """End to end, on the tool that gave rise to this: press Stop mid-export
    and there must be no output file, no running Ghostscript, and the panel
    back in a state that can run again."""
    from tools.panels.pdfx import PdfxPanel

    src = os.path.join(_TMP, "cancel_src.pdf")
    if not os.path.exists(src):
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from PIL import Image
        photo = os.path.join(_TMP, "cancel_photo.png")
        big = Image.new("RGB", (2400, 3400))
        pixels = big.load()
        for y in range(0, 3400, 2):
            for x in range(0, 2400, 2):
                pixels[x, y] = (x % 256, y % 256, (x + y) % 256)
        big.save(photo)
        w, h = A4
        c = canvas.Canvas(src, pagesize=A4)
        for _ in range(3):
            c.drawImage(photo, 0, 0, w, h); c.showPage()
        c.save()

    out = os.path.join(_TMP, "cancel_out.pdf")
    if os.path.exists(out):
        os.remove(out)

    _open(src)
    panel = PdfxPanel()
    panel.save_pdf = lambda *a, **k: out
    panel.open_result = lambda *a, **k: None
    logged = []
    panel.log.log = lambda m, error=False, **k: logged.append(str(m))
    try:
        assert not panel.stop_btn.isVisibleTo(panel), "Stop shows when idle"
        panel._safe_run()
        _spin(20)
        assert panel._async_running, "the export did not start"
        assert panel.stop_btn.isVisibleTo(panel), "no Stop button while running"

        assert panel.stop_running() is True
        for _ in range(600):
            _spin(5)
            if not panel._async_running:
                break
        assert not panel._async_running, "the export did not stop"
        assert not os.path.exists(out), "a stopped export wrote a file anyway"
        assert panel.run_btn.isEnabled(), "the panel cannot be used again"
        assert not panel.stop_btn.isVisibleTo(panel), "Stop still showing when idle"
        assert any("Abgebrochen" in m for m in logged), logged
        assert not any("Fehler" in m or "Error" in m for m in logged), \
            f"stopping was reported as a failure: {logged}"
    finally:
        panel.deleteLater(); _app.processEvents()
    return "no file, no error, and the panel is usable again"


def test_stopping_when_nothing_runs_is_harmless():
    """The button is hidden then, but a keyboard or a second click can still
    reach it, and neither should raise."""
    from tools.panels.compress import CompressPanel
    panel = CompressPanel()
    try:
        assert panel.stop_running() is False
        assert panel.stop_running() is False
    finally:
        panel.deleteLater(); _app.processEvents()
    return "returns False, does nothing"
