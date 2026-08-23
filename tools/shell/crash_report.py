"""
Telling the user something went wrong, and what it probably was.

Before this, a failure was written to a file and nothing else happened: the
window closed, or a button quietly did nothing, and the report sat in a
directory nobody had reason to open.

Two kinds of failure arrive here by two different routes, because they have to:

  * An unhandled Python exception. The process is still alive and the event
    loop is still turning, so it is shown at the moment it happens.
  * A native crash. There is nothing to show at the time — a segfault leaves
    no interpreter to build a dialog with, which is the whole reason
    faulthandler writes through a fd it opened in advance. What is possible is
    telling the user at the next start, which is what every browser does, and
    what report_previous_crash() is for.

On explaining the cause: the guesses below are pattern matches against the
failures this application actually has — an encrypted file, a missing
Ghostscript, a full disk, a widget outliving its C++ half. They are worth
making because those cases are common and the plain exception text is not
plain at all. Everything else says it does not know, which is the honest
answer and keeps the guesses that are offered worth reading.
"""

import logging
import re

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox

from tools.branding import APP_NAME
from tools.i18n import tr
from tools.logging_setup import (crash_log_path, log_dir, log_path,
                                 previous_crash, set_crash_reporter)

# The same fault repeated is the normal shape of this: an exception in a paint
# or timer handler fires again on every repaint or tick. The record still goes
# to the log every time; the dialog is what gets held back, or the app becomes
# a wall of identical boxes nobody can close.
_MAX_DIALOGS = 3

# The external programs this app drives, and how each names itself when it is
# the thing that failed. Ghostscript's several platform names are spelled out
# here for recognising a message, not for running one — tools/ghostscript.py
# is the only place allowed to decide which of them to execute.
_EXTERNAL_TOOLS = (
    (r"ghostscript|gswin\d*c|\bgs\b",  "Ghostscript"),
    (r"\btesseract\b",                 "Tesseract"),
    (r"libreoffice|\bsoffice\b",       "LibreOffice"),
    (r"\bqpdf\b",                      "qpdf"),
)

# Naming one of those programs is not the same as it being absent: qpdf
# returning something unexpected mentions qpdf just as much as qpdf being
# missing does, and telling someone to install what they already have sends
# them off to fix the wrong thing. The name has to arrive together with a
# reason to think it is not there.
_MISSING = re.compile(
    r"not found|nicht gefunden|no such file|cannot find|can't find"
    r"|command not found|not installed|nicht installiert", re.I)

_seen = set()
_shown = 0
_open = False


def open_log_folder(parent=None):
    """Show the folder holding the log and the crash file.

    Falls back to naming the paths: with no file manager registered the
    request still has a perfectly good answer, and failing silently would
    leave the one menu item about diagnosing problems doing nothing.
    """
    if QDesktopServices.openUrl(QUrl.fromLocalFile(log_dir())):
        return
    QMessageBox.information(parent, tr("Fehlerprotokoll anzeigen"),
                            f"{log_path()}\n{crash_log_path()}")


def _guess(exc_type, exc_value, text):
    """A plain-language cause, or None when there is nothing honest to say."""
    name = getattr(exc_type, "__name__", "") or ""
    msg  = f"{exc_value}"
    low  = (msg + "\n" + text).lower()

    # Out of memory. Large PDFs at high zoom are how this app reaches it.
    if name == "MemoryError":
        return tr("Der Arbeitsspeicher hat nicht gereicht. Sehr grosse "
                  "Seiten bei hohem Zoom sind die haeufigste Ursache — mit "
                  "weniger Zoom oder weniger geoeffneten Dateien erneut "
                  "versuchen.")

    # Disk full — an errno, because the message text for it varies by locale.
    if getattr(exc_value, "errno", None) == 28:
        return tr("Auf dem Datentraeger ist kein Platz mehr frei.")

    if name == "PermissionError":
        return tr("Der Zugriff wurde vom Betriebssystem verweigert. Meist ist "
                  "das Ziel schreibgeschuetzt, gehoert einem anderen Benutzer, "
                  "oder die Datei ist in einem anderen Programm geoeffnet.")

    # An external binary this app drives, rather than a document. Matched
    # against the exception's own message and not the traceback: the traceback
    # carries this repo's file paths, and tools/ghostscript.py raising a
    # timeout would otherwise be reported as Ghostscript being missing.
    # Word-bounded for the same reason in miniature — "gs" as a loose
    # substring is inside "settings".
    if name == "FileNotFoundError" or _MISSING.search(msg):
        for pattern, label in _EXTERNAL_TOOLS:
            if re.search(pattern, msg, re.I):
                return tr("{p0} wurde nicht gefunden. Dieses Programm wird "
                          "separat installiert — siehe README, Abschnitt "
                          "Requirements.").format(p0=label)

    if name in ("ModuleNotFoundError", "ImportError"):
        return tr("Ein benoetigtes Python-Paket fehlt oder laesst sich nicht "
                  "laden. requirements.txt nennt die Pakete, die installiert "
                  "sein muessen.")

    if name == "FileNotFoundError":
        return tr("Die Datei war nicht mehr da. Sie wurde vermutlich "
                  "verschoben, umbenannt oder geloescht, waehrend sie hier "
                  "geoeffnet war.")

    # pikepdf/pypdf, which is how a bad document announces itself.
    if "password" in low or "encrypted" in low:
        return tr("Die PDF-Datei ist passwortgeschuetzt und kann ohne das "
                  "Passwort nicht bearbeitet werden.")
    if any(k in low for k in ("pdferror", "damaged", "not a pdf",
                              "eof marker", "startxref", "invalid pdf")):
        return tr("Die PDF-Datei ist beschaedigt oder entspricht nicht dem "
                  "Standard. Andere Programme koennen sie trotzdem anzeigen — "
                  "sie sind toleranter als die Bibliothek, die hier bearbeitet.")

    # The characteristic PyQt lifetime failure: Python still holding a handle
    # to a widget whose C++ half Qt has already destroyed.
    if "has been deleted" in low or "wrapped c/c++ object" in low:
        return tr("Ein Fenster-Element wurde benutzt, nachdem Qt es bereits "
                  "geloescht hatte. Das ist ein Fehler in dieser Anwendung, "
                  "nicht an Ihrer Datei — der Bericht unten gehoert in eine "
                  "Fehlermeldung an die Entwickler.")

    if name == "RecursionError":
        return tr("Eine Verschachtelung war zu tief — bei PDFs mit sehr tief "
                  "verschachtelten Strukturen kann das vorkommen.")

    return None


def _guess_native(text):
    """The same, for a faulthandler dump. There is no exception to read, only
    the names of the libraries on the stack."""
    low = text.lower()
    if "sip" in low or "pyqt" in low:
        return tr("Der Absturz geschah in der Qt-Anbindung, meist wenn ein "
                  "Fenster-Element benutzt wird, nachdem Qt es geloescht hat. "
                  "Das ist ein Fehler in dieser Anwendung, nicht an Ihrer "
                  "Datei.")
    if "pdfium" in low:
        return tr("Der Absturz geschah beim Darstellen einer Seite. Meist "
                  "liegt das an einer beschaedigten PDF-Datei, die die "
                  "Darstellungs-Bibliothek nicht verarbeiten kann.")
    if "ghostscript" in low or "libgs" in low:
        return tr("Der Absturz geschah in Ghostscript, waehrend eine Datei "
                  "konvertiert wurde.")
    return None


def _show(title, headline, cause, detail, parent=None):
    """The dialog itself. Never raises: it is called from an exception hook,
    and a reporter that throws would replace the failure with its own."""
    global _open, _shown
    if _open:
        return          # an exception raised while this box is up must not stack
    _open = True
    try:
        # Built once, in full. Setting the cause and then overwriting it with
        # the "no more of these" note dropped the "cause unknown" line on the
        # floor, and left an empty paragraph where it had been.
        body = cause or tr("Die Ursache liess sich nicht automatisch "
                           "bestimmen. Die technischen Details stehen unten.")
        if _shown + 1 >= _MAX_DIALOGS:
            body += "\n\n" + tr("Weitere Fehler dieser Sitzung werden nur "
                                "noch ins Protokoll geschrieben.")
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(headline)
        box.setInformativeText(body)
        box.setDetailedText(detail)
        copy_btn = box.addButton(tr("Bericht kopieren"),
                                 QMessageBox.ButtonRole.ActionRole)
        log_btn  = box.addButton(tr("Fehlerprotokoll anzeigen"),
                                 QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(QMessageBox.StandardButton.Close)
        # Otherwise the buttons above dismiss the box, and the point of them is
        # that the user can still read it afterwards.
        copy_btn.clicked.disconnect()
        log_btn.clicked.disconnect()
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(detail))
        log_btn.clicked.connect(lambda: open_log_folder(box))
        _shown += 1
        box.exec()
    except Exception:
        logging.debug("the crash dialog could not be shown", exc_info=True)
    finally:
        _open = False


class _Marshal(QObject):
    """Carries a report from whichever thread raised onto the GUI thread.

    Widgets may only be touched from the thread that owns them, and
    threading.excepthook runs on the thread that died — so the report cannot
    be shown where it arrives. A queued signal is the crossing.
    """
    arrived = pyqtSignal(str, object, object, str)


_marshal = None


def install(parent=None):
    """Route unhandled exceptions to a dialog, and report a previous native
    crash if the last run left one behind."""
    global _marshal
    if _marshal is not None:
        return
    _marshal = _Marshal()
    _marshal.arrived.connect(lambda *a: _on_report(*a, parent=parent),
                             Qt.ConnectionType.QueuedConnection)
    set_crash_reporter(
        lambda kind, et, ev, text: _marshal.arrived.emit(kind, et, ev, text))
    report_previous_crash(parent)


def _on_report(kind, exc_type, exc_value, text, parent=None):
    if _shown >= _MAX_DIALOGS:
        return          # already said so; the log still has every one of them
    # One dialog per distinct fault. The signature is the exception and where
    # it was raised, so the same fault repeating on every repaint is one box.
    sig = (getattr(exc_type, "__name__", "?"), text.strip().rsplit("\n", 1)[-1])
    if sig in _seen:
        return
    _seen.add(sig)
    if QApplication.instance() is None:
        return          # too early or too late for a dialog; the log has it

    where = tr("in einem Hintergrund-Prozess") if kind.startswith("thread:") \
        else tr("in der Anwendung")
    _show(tr("Fehler in {p0}").format(p0=APP_NAME),
          tr("Es ist ein unerwarteter Fehler {p0} aufgetreten.").format(p0=where),
          _guess(exc_type, exc_value, text), text, parent)


def report_previous_crash(parent=None):
    """Show the native crash the last run died of, if it left one.

    Deliberately at startup rather than at the crash: see the module docstring
    — at the moment of a segfault there is no interpreter left to ask.
    """
    text = previous_crash()
    if not text or QApplication.instance() is None:
        return
    _show(tr("{p0} wurde unerwartet beendet").format(p0=APP_NAME),
          tr("Beim letzten Mal wurde das Programm unerwartet beendet."),
          _guess_native(text), text, parent)
