"""
The entry point. The application's name lives in tools/branding.py.

Starten:  copyshop-pdf              (installed)
          python3 main.py           (from the repository)
          python3 -m tools.app
          … any of them, with a file:  … datei.pdf

Dependencies are listed in requirements.txt and README.md, and nowhere else.
They used to be spelled out here as pacman and apt command lines, which drifted:
the list named python-pytesseract, which nothing has imported since the OCR
panel started driving the tesseract binary itself — and requirements.txt said so
in as many words, two files apart.

Inside the package rather than a top-level main.py: an installed top-level
module called "main" sits in site-packages under a name anything might want, and
the console script pointed at it. main.py at the repository root is a launcher
for the documented dev command, and is not installed.
"""

import sys, os

# Before the imports below, not after them: importing the thirteen tool panels
# is itself a place things go wrong, and a traceback raised there is exactly
# the kind that would otherwise leave no trace at all — the window never
# appears, so there is nothing to read the error from.
from tools.logging_setup  import install as _install_logging
from tools.logging_setup  import install_qt_message_handler
_install_logging()

from PyQt6.QtWidgets import QApplication

from tools.i18n           import load_language
from tools.render.queue   import shutdown_render_queue, apply_performance_settings
from tools.shell.inputs   import install as install_number_field_behaviour
from tools.shell.instance import (_forward_to_running_instance,
                                  _listen_for_open_requests)
from tools.shell.settings import (AppSettings, _full_page_cache_bytes,
                                  _thumb_cache_bytes)
from tools.shell.style    import AppStyle, apply_theme_globally, app_icon
from tools.shell.window   import MainWindow

# Re-exported because the suite drives the app through this module.
from tools.shell.style    import STYLE, LIGHT_STYLE              # noqa: F401
from tools.branding import app_title


def main():
    app = QApplication(sys.argv)

    # Needs the QApplication to exist before Qt will route messages to it.
    install_qt_message_handler()

    app.setApplicationName(app_title())
    app.setStyle(AppStyle.create())

    # Click a number field and its value is selected, ready to be typed over.
    install_number_field_behaviour(app)

    # Stop the render worker before anything gets torn down, so a task can't
    # finish and emit into receivers we are about to delete below.
    app.aboutToQuit.connect(shutdown_render_queue)

    # The flattened copies the tools read from are whole documents in the temp
    # directory. Take ours with us on the way out, and clear what a run that
    # crashed — or any version from before this existed — left behind. Both
    # are safe: a snapshot is a cache, and ensure_view_snapshot writes another
    # if the one it wanted has gone.
    from tools.snapshots import discard_all_snapshots, sweep_orphan_snapshots
    sweep_orphan_snapshots()
    app.aboutToQuit.connect(discard_all_snapshots)

    # Decrypted copies are the same housekeeping with more at stake: each is a
    # document with the protection its owner put on it taken off, and they
    # were being left in a directory every account on the machine can read.
    from tools.pdf_access import (discard_all_unlocked_copies,
                                  sweep_orphan_unlocked_copies)
    sweep_orphan_unlocked_copies()
    app.aboutToQuit.connect(discard_all_unlocked_copies)

    # Hand the files to an already-running instance and quit, before building
    # any UI. With no files this just raises the existing window — the app is
    # tab-based, so a second launch should never mean a second window.
    _cli_files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    try:
        if _forward_to_running_instance(_cli_files):
            return
    except Exception:
        pass   # no running instance reachable — carry on and open normally

    load_language()   # must be after QApplication — QSettings needs it

    # Apply persisted settings before building the window
    s = AppSettings.get()
    apply_theme_globally(s.theme())

    apply_performance_settings(
        prerender        = s.prerender(),
        thumb_bytes      = _thumb_cache_bytes(s.ram_percent()),
        full_page_bytes  = _full_page_cache_bytes(s.ram_percent()),
    )

    app.setWindowIcon(app_icon())

    args = _cli_files

    # Reopen last file if the setting is enabled and no file was passed on CLI
    if not args and s.reopen_last() and s.last_file() and os.path.isfile(s.last_file()):
        win = MainWindow(open_file=s.last_file())
    elif len(args) > 1:
        win = MainWindow(open_files=args)
    elif len(args) == 1:
        win = MainWindow(open_file=args[0])
    else:
        win = MainWindow()

    win.show()

    # After the window exists, so a dialog has something to sit on — and so a
    # crash the last run died of is reported against a built UI rather than an
    # empty screen.
    from tools.shell.crash_report import install as _install_crash_reporter
    _install_crash_reporter(win)

    try:
        _listen_for_open_requests(win)
    except Exception:
        pass   # QtNetwork unavailable — the app still works, just not shared
    rc = app.exec()

    # Tear the widget tree down here, while the event loop and the interpreter
    # are both still healthy. Left alive, it was PyQt's own cleanup_on_exit
    # atexit hook that destroyed it during interpreter finalisation, and that
    # walk hit a wrapper whose C++ object was already gone — every single quit
    # ended in a segfault inside sip_api_get_address. deleteLater + one event
    # loop pass disposes of it in the normal Qt order instead.
    win.deleteLater()
    app.processEvents()
    del win
    sys.exit(rc)


if __name__ == "__main__":
    main()
