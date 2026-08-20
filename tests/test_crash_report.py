"""
Crash report.

A failure that only reaches a file is a failure the user never learns about:
the window closes, or a button does nothing, and the report waits in a
directory there was no reason to open. These cover it reaching the screen.

Every test runs in a child process. The exception hooks, faulthandler and the
reporter registration are all process-wide state, and installing them in the
test process would outlive the test that asked for them.
"""
import json
import os
import subprocess
import sys
import tempfile

from tests.support import _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Captures what the dialog was built with instead of blocking on exec(), and
# prints it as JSON on the last line for the parent to read.
_HARNESS = '''
import json, sys
from PyQt6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
import tools.app
import tools.shell.crash_report as CR
shown = []
QMessageBox.exec = lambda self: (shown.append({{
    "title": self.windowTitle(), "text": self.text(),
    "cause": self.informativeText(), "detail": self.detailedText(),
    "buttons": [b.text() for b in self.buttons()]}}), 0)[1]
CR.install()
{body}
app.processEvents()
print("@@" + json.dumps(shown))
'''


def _dialogs(body, log_dir, expect_crash=False):
    """Run `body` under the harness; return the list of dialogs it raised."""
    env = dict(os.environ, COPYSHOP_LOG_DIR=log_dir, PYTHONPATH=REPO,
               QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, "-c", _HARNESS.format(body=body)],
                       env=env, capture_output=True, text=True,
                       errors="replace", timeout=180)
    if expect_crash:
        assert p.returncode != 0, "the child was supposed to die, and did not"
        return []
    assert p.returncode == 0, f"child failed: {p.stderr[-2000:]}"
    line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
    assert line, f"harness produced no result: {p.stdout[-800:]} {p.stderr[-800:]}"
    return json.loads(line[-1][2:])


def _crash(log_dir):
    """Segfault a child, leaving a native crash for the next start to find."""
    env = dict(os.environ, COPYSHOP_LOG_DIR=log_dir, PYTHONPATH=REPO,
               QT_QPA_PLATFORM="offscreen")
    p = subprocess.run(
        [sys.executable, "-c", "import tools.app, ctypes; ctypes.string_at(0)"],
        env=env, capture_output=True, text=True, errors="replace", timeout=180)
    assert p.returncode != 0, "the child did not crash"


def test_an_unhandled_exception_reaches_the_user():
    """It used to reach the log and stop there — the app is started from a
    desktop entry, so there is no terminal for it to appear in either."""
    d = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "try: raise MemoryError('cannot allocate bitmap')\n"
        "except MemoryError: sys.excepthook(*sys.exc_info())\n", d)
    assert len(shown) == 1, f"expected one dialog, got {len(shown)}"
    dlg = shown[0]
    assert "MemoryError" in dlg["detail"] and "Traceback" in dlg["detail"], \
        "the report does not carry the traceback it is for"
    # The whole point of the dialog over the log: it says what it means.
    assert "Arbeitsspeicher" in dlg["cause"], dlg["cause"]
    assert any("kopieren" in b for b in dlg["buttons"]), \
        "no way to copy the report out of the dialog"


def test_a_native_crash_is_reported_at_the_next_start():
    """Nothing can be shown at the moment of a segfault — there is no
    interpreter left to build a dialog with, which is exactly why faulthandler
    writes through a fd opened in advance. The next start is the first chance
    anyone has to say the last one died, and saying nothing is what made the
    app look like it had simply vanished."""
    d = tempfile.mkdtemp(dir=_TMP)
    _crash(d)
    shown = _dialogs("pass", d)
    assert len(shown) == 1, "the crash the last run died of was not reported"
    assert "Fatal Python error" in shown[0]["detail"], \
        "the report does not contain the native traceback"
    assert "unerwartet" in shown[0]["text"], shown[0]["text"]


def test_a_crash_is_reported_once_and_not_at_every_start_afterwards():
    """The report is a mailbox, not a log: left in place it would greet the
    user on every start for ever, and the second showing is the one that
    teaches them to dismiss it without reading."""
    d = tempfile.mkdtemp(dir=_TMP)
    _crash(d)
    assert len(_dialogs("pass", d)) == 1, "first start did not report it"
    assert _dialogs("pass", d) == [], "the same crash was reported twice"


def test_a_clean_run_is_not_reported_as_a_crash():
    """The crash file gets a session header at every start, crash or not. If
    that alone counted, every ordinary start would accuse the one before it."""
    d = tempfile.mkdtemp(dir=_TMP)
    assert _dialogs("pass", d) == [], "a first run reported a crash"
    assert _dialogs("pass", d) == [], "a clean run was reported as a crash"


def test_the_same_fault_repeating_shows_one_dialog():
    """An exception in a paint or timer handler comes back on every repaint or
    tick. Left alone that is a wall of identical boxes the user cannot get out
    from behind — while the log still wants every one of them."""
    d = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "def again():\n"
        "    try: raise ValueError('same every time')\n"
        "    except ValueError: sys.excepthook(*sys.exc_info())\n"
        "for _ in range(6):\n"
        "    again(); app.processEvents()\n", d)
    assert len(shown) == 1, f"{len(shown)} dialogs for one repeating fault"
    log = open(os.path.join(d, "copyshop.log"), encoding="utf-8").read()
    assert log.count("same every time") >= 6, \
        "suppressing the dialog also suppressed the log entries"


def test_a_worker_thread_failure_is_shown_from_the_gui_thread():
    """threading.excepthook runs on the thread that died, and widgets may only
    be touched from the thread that owns them — so the report has to cross
    threads before it can be shown at all."""
    d = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "import threading\n"
        "def boom(): raise PermissionError(13, 'Permission denied')\n"
        "t = threading.Thread(target=boom, name='render'); t.start(); t.join()\n",
        d)
    assert len(shown) == 1, "a thread died without the user being told"
    assert "Hintergrund" in shown[0]["text"], shown[0]["text"]
    assert "verweigert" in shown[0]["cause"], shown[0]["cause"]


def test_the_cause_is_named_for_the_failures_this_app_actually_has():
    """The guesses are worth making only because these cases are common and
    the raw exception text is not plain at all. Each is a real failure mode of
    this application: a locked file, a full disk, a missing external binary,
    an encrypted document."""
    d = tempfile.mkdtemp(dir=_TMP)
    cases = [
        ("raise OSError(28, 'No space left on device')", "Datentraeger"),
        ("raise FileNotFoundError(2, \"No such file\", 'tesseract')", "Tesseract"),
        ("raise ValueError('PdfError: damaged file')",   "beschaedigt"),
        ("raise RuntimeError('wrapped C/C++ object of type QLabel has been deleted')",
         "Qt"),
    ]
    for raise_stmt, expect in cases:
        shown = _dialogs(
            f"try: {raise_stmt}\n"
            f"except Exception: sys.excepthook(*sys.exc_info())\n", d)
        assert shown, f"no dialog for {raise_stmt}"
        assert expect in shown[0]["cause"], \
            f"{raise_stmt} -> {shown[0]['cause']!r}, wanted {expect!r}"


def test_a_guess_is_not_made_from_words_that_merely_look_like_a_program_name():
    """The first version matched the bare substrings. "gs" is inside
    "settings", and "ghostscript" is inside this repo's own
    tools/ghostscript.py — which appears in the traceback of anything that
    fails in there, so a Ghostscript timeout would have been reported to the
    user as Ghostscript being missing. A confident wrong answer is worse than
    no answer, because it sends them off to reinstall something that is
    already installed."""
    d = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "try: raise ValueError('could not apply settings for this page')\n"
        "except ValueError: sys.excepthook(*sys.exc_info())\n", d)
    assert shown, "no dialog at all"
    assert "Ghostscript" not in shown[0]["cause"], \
        f'"settings" was read as Ghostscript: {shown[0]["cause"]!r}'

    # And a real Ghostscript failure is still recognised.
    d2 = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "try: raise RuntimeError('Ghostscript nicht gefunden.')\n"
        "except RuntimeError: sys.excepthook(*sys.exc_info())\n", d2)
    assert "Ghostscript" in shown[0]["cause"], shown[0]["cause"]


def test_an_unrecognised_failure_says_so_rather_than_inventing_a_cause():
    """A guess that is always confident is a guess nobody can trust. Anything
    outside the known patterns has to admit it, or the ones that are offered
    stop being worth reading."""
    d = tempfile.mkdtemp(dir=_TMP)
    shown = _dialogs(
        "try: raise ValueError('something entirely unfamiliar 4f3a9')\n"
        "except ValueError: sys.excepthook(*sys.exc_info())\n", d)
    assert shown, "no dialog at all"
    assert "nicht automatisch bestimmen" in shown[0]["cause"], shown[0]["cause"]
    # It still hands over the traceback, which is the part that is always true.
    assert "something entirely unfamiliar 4f3a9" in shown[0]["detail"]


def test_a_failing_reporter_cannot_swallow_the_log_entry():
    """The dialog is the addition; the file is the thing that has to keep
    working. A reporter that raises — no display, Qt torn down mid-quit —
    must not take the record down with it."""
    d = tempfile.mkdtemp(dir=_TMP)
    env = dict(os.environ, COPYSHOP_LOG_DIR=d, PYTHONPATH=REPO,
               QT_QPA_PLATFORM="offscreen")
    p = subprocess.run([sys.executable, "-c",
        "import sys\n"
        "import tools.app\n"
        "from tools.logging_setup import set_crash_reporter\n"
        "set_crash_reporter(lambda *a: 1/0)\n"
        "try: raise ValueError('MUST-STILL-BE-LOGGED')\n"
        "except ValueError: sys.excepthook(*sys.exc_info())\n"],
        env=env, capture_output=True, text=True, errors="replace", timeout=180)
    assert p.returncode == 0, f"a broken reporter took the app down: {p.stderr[-800:]}"
    log = open(os.path.join(d, "copyshop.log"), encoding="utf-8").read()
    assert "MUST-STILL-BE-LOGGED" in log, "the log entry was lost"
