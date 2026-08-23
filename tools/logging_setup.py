"""
Where the log comes from, and what is allowed into it.

The app is started from a .desktop entry, so stderr goes nowhere the user can
read. The file this module sets up is therefore the only record of what went
wrong, and it has to survive the two failures that matter most:

  * A Python exception nobody caught — on the GUI thread, in a worker thread,
    or inside a Qt slot. sys.excepthook alone covers only the first of those.
  * A native crash. PyQt hands C++ objects to Python and a lifetime mismatch
    segfaults the interpreter outright — app.py's own teardown comment is
    about exactly such a crash. Python logging cannot report those at all,
    because there is no exception and no interpreter left to run a handler;
    faulthandler, which writes from a signal handler using a fd it opened in
    advance, can. That is what copyshop-crash.log is for.

Third-party libraries are held at WARNING. At DEBUG, PIL narrates every PNG
chunk it decodes and img2pdf every image it converts — together most of the
file, which is how a real traceback becomes impossible to find.

Set COPYSHOP_LOG_DIR to redirect all of it; the test suite does, so that
running the tests cannot bury the user's real log under fixture noise.
"""

import atexit
import faulthandler
import logging
import logging.handlers
import os
import sys
import tempfile
import threading
import time
import traceback
from tools.branding import versioned

_LOG_NAME   = "copyshop.log"
_CRASH_NAME = "copyshop-crash.log"

# One day of ordinary use came to roughly a megabyte, so this keeps about a
# week without the file ever being one an editor struggles to open.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS   = 3

# These talk at DEBUG about every image they touch. Raised to WARNING, they
# still report what actually went wrong and nothing else.
_NOISY = ("PIL", "img2pdf", "pikepdf", "fontTools", "matplotlib", "urllib3")

# faulthandler writes from a signal handler and cannot open a file at crash
# time, so it keeps this one open for the life of the process.
_crash_file = None
_installed  = False
_crash_file_closed = False
_qt_handler_installed = False

# Set by the Qt side (tools/shell/crash_report.py) so a crash can reach the
# user rather than only the file. A plain callback, so this module keeps
# working — and keeps being importable — with no GUI in the process at all.
_reporter = None

# A native crash from the *previous* run, found at startup. Nothing can be
# shown at the moment of a segfault: the interpreter is gone. The next start
# is the first opportunity to tell anyone it happened.
_previous_crash = None


def set_crash_reporter(fn):
    """Register `fn(kind, exc_type, exc_value, text)` to be shown to the user.

    Called for every unhandled exception, from whichever thread raised it —
    so `fn` is responsible for getting itself onto the GUI thread.
    """
    global _reporter
    _reporter = fn


def previous_crash():
    """The native crash left behind by the last run, or None. Read once at
    startup; see _install_crash_handler for why it cannot be reported live."""
    return _previous_crash


def _notify(kind, exc_type, exc_value, text):
    """Hand a failure to the reporter, if one is registered.

    Guarded, and deliberately after the logging call at every call site: a
    reporter that raises (no display, Qt torn down mid-quit) must not be able
    to lose the record that was already written to the file.
    """
    if _reporter is None:
        return
    try:
        _reporter(kind, exc_type, exc_value, text)
    except Exception:
        logging.debug("crash reporter failed", exc_info=True)


def log_dir():
    """The directory the log files live in — COPYSHOP_LOG_DIR when set, else
    the XDG data directory, else the temp directory if neither can be made."""
    override = os.environ.get("COPYSHOP_LOG_DIR")
    if override:
        try:
            os.makedirs(override, exist_ok=True)
            return override
        except Exception:
            pass    # fall through to the default rather than lose the log
    base = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "copyshop_pdf_suite")
    try:
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        return tempfile.gettempdir()


def log_path():
    """The rotating log every Python-level message goes to."""
    return os.path.join(log_dir(), _LOG_NAME)


def crash_log_path():
    """The file faulthandler writes a native traceback into. Separate from the
    main log on purpose: faulthandler holds an open fd, and a rotation would
    rename the file out from under it and send the one report that matters to
    an inode nothing can find again."""
    return os.path.join(log_dir(), _CRASH_NAME)


def _close_crash_file():
    """Close the faulthandler crash file on clean exit."""
    global _crash_file, _crash_file_closed
    if _crash_file is not None and not _crash_file_closed:
        try:
            _crash_file.flush()
            _crash_file.close()
        except Exception:
            logging.debug("could not close crash file", exc_info=True)
        _crash_file = None
        _crash_file_closed = True


def _install_crash_handler():
    """Point faulthandler at its own always-open file, under a header saying
    which run is about to write there.

    Anything already in that file was put there by a run that died before it
    could say so — a segfault leaves no interpreter to raise, catch or display
    anything. So the file is read here, at the one moment there is a process
    healthy enough to report it, and then emptied: what it holds afterwards is
    only ever a crash nobody has been told about yet.
    """
    global _crash_file, _previous_crash
    path = crash_log_path()
    try:
        if os.path.exists(path):
            try:
                prev = open(path, encoding="utf-8", errors="replace").read()
            except Exception:
                prev = ""
            # faulthandler's own banner. Without it the file holds nothing but
            # the header of a run that started and exited perfectly normally.
            if "Fatal Python error" in prev:
                _previous_crash = prev.strip()
                # The rotating log is where history belongs; this file is a
                # mailbox, and is about to be emptied.
                logging.critical(
                    "the previous session died of a native crash:\n%s",
                    _previous_crash)
            try:
                os.truncate(path, 0)
            except Exception:
                logging.debug("could not clear the crash file", exc_info=True)
        _crash_file = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        _crash_file.write(
            f"\n--- session {os.getpid()} started "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        _crash_file.flush()
        # all_threads: a segfault on a render worker is as likely as one on
        # the GUI thread, and the GUI thread's stack alone would not show it.
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        logging.debug("faulthandler could not be installed", exc_info=True)


def _install_excepthooks():
    """Route the exceptions that would otherwise only reach a stderr nobody
    sees. sys.excepthook covers the main thread (and, because PyQt defers to
    an installed hook, Qt slots); threading.excepthook covers the rest, which
    sys.excepthook never sees."""
    def _hook(exc_type, exc_value, exc_tb):
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            logging.critical("unhandled exception\n%s", text)
            _notify("exception", exc_type, exc_value, text)
        finally:
            # Still print it: run from a terminal, that is where it is wanted.
            sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        # A thread dying of SystemExit is a thread being asked to stop.
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "?")
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        logging.critical("unhandled exception in thread %s\n%s", name, text)
        _notify("thread:" + str(name), args.exc_type, args.exc_value, text)

    sys.excepthook       = _hook
    threading.excepthook = _thread_hook


def install():
    """Configure file logging, the crash handler and the exception hooks.

    Safe to call more than once — the second call does nothing, so importing
    the app twice cannot attach a second handler and double every line.
    """
    global _installed
    if _installed:
        return
    _installed = True

    handler = None
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
            encoding="utf-8", errors="replace")
    except Exception:
        # A read-only or missing home is not a reason to fail to start; the
        # app runs, it just reports to stderr like any other program.
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s %(levelname)s %(message)s")

    if handler is not None:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    _install_excepthooks()
    _install_crash_handler()
    atexit.register(_close_crash_file)

    logging.info(f"--- {versioned()} starting (pid %s, python %s) ---",
                 os.getpid(), sys.version.split()[0])
    # A session with a start line and no exit line is one that died without
    # getting the chance to write anything — which is the thing worth knowing
    # when someone reports that the app "just closed".
    atexit.register(lambda: logging.info(
        f"--- {versioned()} exiting cleanly (pid %s) ---", os.getpid()))


def install_qt_message_handler():
    """Send Qt's own diagnostics to the log as well.

    Qt writes them to stderr, which the .desktop launcher discards — so the
    warning naming the widget that failed to paint, or the fatal that is about
    to abort the process, has been going nowhere. Kept apart from install() so
    the logging setup itself stays importable without Qt.
    """
    global _qt_handler_installed
    if _qt_handler_installed:
        return
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        logging.debug("Qt message handler unavailable", exc_info=True)
        return
    _qt_handler_installed = True

    _levels = {
        QtMsgType.QtDebugMsg:    logging.DEBUG,
        QtMsgType.QtInfoMsg:     logging.INFO,
        QtMsgType.QtWarningMsg:  logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg:    logging.CRITICAL,
    }
    qt_log = logging.getLogger("Qt")

    def _handler(mode, context, message):
        try:
            where = ""
            if getattr(context, "file", None):
                where = f" ({context.file}:{context.line})"
            qt_log.log(_levels.get(mode, logging.WARNING), "%s%s", message, where)
        except Exception:
            pass    # a logging failure must never take the message handler down

    qInstallMessageHandler(_handler)
