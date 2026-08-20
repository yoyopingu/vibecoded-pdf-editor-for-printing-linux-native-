"""
Logging.

The app is launched from a .desktop entry, so stderr is discarded and the log
file is the only account of a failure anyone can go back to. These cover the
kinds of failure that were leaving no account at all.
"""
import os
import subprocess
import sys
import tempfile

from tests.support import _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(body, log_dir, expect_crash=False):
    """Run `body` in a child with its own log directory, and return the log
    text. A child, because faulthandler and the exception hooks are process
    state — installing them in the test process would outlive the test."""
    env = dict(os.environ,
               COPYSHOP_LOG_DIR=log_dir,
               PYTHONPATH=REPO,
               QT_QPA_PLATFORM="offscreen")
    src = "import os\nimport tools.app\n" + body
    p = subprocess.run([sys.executable, "-c", src], env=env,
                       capture_output=True, text=True, errors="replace",
                       timeout=180)
    if expect_crash:
        assert p.returncode != 0, "the child was supposed to die, and did not"
    else:
        assert p.returncode == 0, f"child failed: {p.stderr[-2000:]}"
    path = os.path.join(log_dir, "copyshop.log")
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def test_a_native_crash_leaves_a_traceback_behind():
    """A segfault is the failure this app is most likely to die of — PyQt hands
    C++ objects to Python, and a lifetime mismatch takes the interpreter out
    with no exception raised. Python logging cannot report that: there is no
    exception to hook and no interpreter left to run a handler.

    It used to leave nothing whatever. The process vanished, the log's last
    line was whatever happened to be written before it, and there was no way
    to tell a crash from a clean quit. faulthandler writes from the signal
    handler using a fd opened in advance, which is why it still gets a report
    out when nothing else can.
    """
    d = tempfile.mkdtemp(dir=_TMP)
    _run("import ctypes; ctypes.string_at(0)", d, expect_crash=True)
    crash = os.path.join(d, "copyshop-crash.log")
    assert os.path.exists(crash), "a segfault left no crash file at all"
    txt = open(crash, encoding="utf-8").read()
    assert "Fatal Python error" in txt, f"no native traceback recorded: {txt!r}"
    # The stack has to name the code that crashed, or it cannot be acted on.
    assert "ctypes" in txt or "string_at" in txt, txt[:400]
    # Threads matter: a crash on a render worker would not show in the main
    # thread's stack, and that is where this app does its heavy work.
    assert "Current thread" in txt


def test_a_crash_is_distinguishable_from_a_clean_quit():
    """"It just closed" is the whole of most reports. A session that logged a
    start and never logged an exit is one that died, and that difference is
    what makes the rest of the log worth reading."""
    d = tempfile.mkdtemp(dir=_TMP)
    crashed = _run("import ctypes; ctypes.string_at(0)", d, expect_crash=True)
    assert "starting (pid" in crashed
    assert "exiting cleanly" not in crashed, \
        "a crashed run claimed it exited cleanly"

    d2 = tempfile.mkdtemp(dir=_TMP)
    clean = _run("pass", d2)
    assert "starting (pid" in clean and "exiting cleanly" in clean, \
        "a clean run did not record its own exit"


def test_an_exception_in_a_worker_thread_is_recorded():
    """sys.excepthook never sees these. The app runs its rendering, its jobs
    and its verification off the GUI thread, so an exception dying quietly in
    one of them was a whole class of failure the log could not show."""
    d = tempfile.mkdtemp(dir=_TMP)
    txt = _run(
        "import threading\n"
        "def boom(): raise RuntimeError('WORKER-BOOM')\n"
        "t = threading.Thread(target=boom, name='probe'); t.start(); t.join()\n",
        d)
    assert "WORKER-BOOM" in txt, "a thread died without saying so"
    assert "probe" in txt, "the report does not name the thread that died"
    assert "RuntimeError" in txt


def test_qt_s_own_diagnostics_reach_the_log():
    """Qt writes its warnings to stderr, and the desktop launcher throws
    stderr away — so the message naming the widget that failed to paint, or
    the fatal about to abort the process, was going nowhere."""
    d = tempfile.mkdtemp(dir=_TMP)
    txt = _run(
        "from PyQt6.QtWidgets import QApplication\n"
        "from PyQt6.QtCore import qWarning\n"
        "from tools.logging_setup import install_qt_message_handler\n"
        "app = QApplication([])\n"
        "install_qt_message_handler()\n"
        "qWarning(b'QT-DIAGNOSTIC-MARKER')\n",
        d)
    assert "QT-DIAGNOSTIC-MARKER" in txt, "a Qt warning was lost"


def test_third_party_debug_chatter_stays_out_of_the_log():
    """At DEBUG, PIL narrates every PNG chunk and img2pdf every image. That was
    the bulk of the file — the reason a real traceback was hard to find in it
    — while the app's own debug lines are what make one readable."""
    d = tempfile.mkdtemp(dir=_TMP)
    txt = _run(
        "import logging\n"
        "logging.getLogger('PIL.PngImagePlugin').debug('PIL-NOISE')\n"
        "logging.getLogger('img2pdf').debug('IMG2PDF-NOISE')\n"
        "logging.getLogger('PIL').warning('PIL-REAL-PROBLEM')\n"
        "logging.getLogger('tools.probe').debug('APP-DEBUG')\n",
        d)
    assert "PIL-NOISE" not in txt and "IMG2PDF-NOISE" not in txt, \
        "third-party debug chatter is still being written"
    assert "PIL-REAL-PROBLEM" in txt, \
        "silencing the chatter also silenced a real third-party warning"
    assert "APP-DEBUG" in txt, "the app's own debug logging was lost too"


def test_the_log_cannot_grow_without_bound():
    """A day of ordinary use came to about a megabyte, and nothing ever
    trimmed it."""
    d = tempfile.mkdtemp(dir=_TMP)
    _run("import logging\n"
         "log = logging.getLogger('tools.spam')\n"
         "for i in range(60000): log.info('x' * 100)\n", d)
    sizes = {f: os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)}
    total = sum(sizes.values())
    assert total < 12 * 1024 * 1024, f"log grew to {total} bytes: {sizes}"
    assert any(f.endswith(".1") for f in sizes), \
        f"nothing rotated, so nothing is capping growth: {sizes}"


def test_the_test_suite_does_not_write_to_the_real_log():
    """Importing the app is what configures logging, and every test module
    imports it. Pointed at the installed location, a day of test runs buried
    the user's actual tracebacks under a megabyte of /tmp fixture paths."""
    from tools.logging_setup import log_dir
    assert os.environ.get("COPYSHOP_LOG_DIR"), \
        "tests/support.py no longer redirects the log"
    real = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "copyshop_pdf_suite")
    assert os.path.abspath(log_dir()) != os.path.abspath(real), \
        "the suite is logging into the installed app's own log directory"


def test_installing_twice_does_not_double_every_line():
    """The app imports as a module and runs as a script, and the tests import
    it repeatedly; a second install() attaching a second handler would write
    everything twice and make the file that much harder to read."""
    d = tempfile.mkdtemp(dir=_TMP)
    txt = _run(
        "import logging\n"
        "from tools.logging_setup import install\n"
        "install(); install(); install()\n"
        "logging.getLogger('tools.probe').error('ONCE-ONLY')\n",
        d)
    assert txt.count("ONCE-ONLY") == 1, \
        f"line written {txt.count('ONCE-ONLY')} times, not once"
