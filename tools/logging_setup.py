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

_LOG_NAME   = "copyshop.log"
_CRASH_NAME = "copyshop-crash.log"

# One day of ordinary use came to roughly a megabyte, so this keeps about a
# week without the file ever being one an editor struggles to open.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS   = 3

# The crash file only ever grows by a traceback at a time; the cap is here so
# a machine that segfaults in a loop cannot fill the disk.
_CRASH_CAP = 512 * 1024

# These talk at DEBUG about every image they touch. Raised to WARNING, they
# still report what actually went wrong and nothing else.
_NOISY = ("PIL", "img2pdf", "pikepdf", "fontTools", "matplotlib", "urllib3")

# faulthandler writes from a signal handler and cannot open a file at crash
# time, so it keeps this one open for the life of the process.
_crash_file = None
_installed  = False


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


def _install_crash_handler():
    """Point faulthandler at its own always-open file, under a header saying
    which run is about to write there."""
    global _crash_file
    path = crash_log_path()
    try:
        # Truncate rather than rotate: a stale native traceback is worth less
        # than the certainty that the file cannot grow without bound.
        if os.path.exists(path) and os.path.getsize(path) > _CRASH_CAP:
            os.truncate(path, 0)
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
        try:
            logging.critical("unhandled exception\n%s", "".join(
                traceback.format_exception(exc_type, exc_value, exc_tb)))
        finally:
            # Still print it: run from a terminal, that is where it is wanted.
            sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        # A thread dying of SystemExit is a thread being asked to stop.
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "?")
        logging.critical("unhandled exception in thread %s\n%s", name, "".join(
            traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback)))

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

    logging.info("--- CopyShop PDF Suite starting (pid %s, python %s) ---",
                 os.getpid(), sys.version.split()[0])
    # A session with a start line and no exit line is one that died without
    # getting the chance to write anything — which is the thing worth knowing
    # when someone reports that the app "just closed".
    atexit.register(lambda: logging.info(
        "--- CopyShop PDF Suite exiting cleanly (pid %s) ---", os.getpid()))


def install_qt_message_handler():
    """Send Qt's own diagnostics to the log as well.

    Qt writes them to stderr, which the .desktop launcher discards — so the
    warning naming the widget that failed to paint, or the fatal that is about
    to abort the process, has been going nowhere. Kept apart from install() so
    the logging setup itself stays importable without Qt.
    """
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        logging.debug("Qt message handler unavailable", exc_info=True)
        return

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
