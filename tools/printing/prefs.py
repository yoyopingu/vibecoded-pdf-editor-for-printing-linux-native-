"""
What the print dialog remembers between runs.

The dialog opened on the queue's defaults every time, so a shop that prints the
same job the same way all day re-picked the tray, the paper and the sides on
every document. Acrobat and the GTK dialog both reopen on what you used last.

Remembered per printer, not globally: the tray that means "letterhead" on one
machine is not the same drawer on another, and a saved paper size from a
different queue may not even exist on this one. The printer itself is
remembered globally — that is the one the dialog should open on.

Precedence when the dialog opens, weakest first:

    what Qt guessed  <  what the queue says  <  what you used last

so a printer never used before opens on its own settings — which is what the
desktop's printer settings are for — and one used before opens on yours.

Nothing here is load-bearing: a missing, corrupt or stale store just means the
dialog opens on the queue's defaults, which is where it started.
"""

import json
import logging

from PyQt6.QtCore import QSettings

# The same store the rest of the app uses.
_ORG, _APP = "CopyShop", "PDFSuite"
_LAST_PRINTER = "printing/last_printer"
_BY_PRINTER = "printing/by_printer"

# Only settings that describe *how* to print. Not the copy count, which is a
# property of the job in front of you rather than of how you like to print, and
# not the page range, which belongs to the document.
REMEMBERED = ("paper", "orientation", "color", "colorconv", "scale",
              "collate", "duplex", "duplex_edge", "paper_source")


def _settings():
    return QSettings(_ORG, _APP)


def last_printer():
    """The printer the dialog should open on, or None."""
    value = _settings().value(_LAST_PRINTER, "")
    return value or None


def _all():
    try:
        raw = _settings().value(_BY_PRINTER, "")
        return json.loads(raw) if raw else {}
    except Exception:
        logging.debug("printing prefs: unreadable store", exc_info=True)
        return {}


def for_printer(printer_name):
    """What was last used on this printer, as a dict — empty if nothing."""
    if not printer_name:
        return {}
    saved = _all().get(printer_name)
    return dict(saved) if isinstance(saved, dict) else {}


def remember(printer_name, values):
    """Store the settings a job was just sent with."""
    if not printer_name:
        return
    kept = {k: v for k, v in values.items() if k in REMEMBERED}
    try:
        store = _all()
        store[printer_name] = kept
        qs = _settings()
        qs.setValue(_BY_PRINTER, json.dumps(store))
        qs.setValue(_LAST_PRINTER, printer_name)
        qs.sync()
    except Exception:
        logging.debug("printing prefs: could not save", exc_info=True)


def forget():
    """Drop everything remembered. For tests, and for a settings reset."""
    qs = _settings()
    qs.remove(_BY_PRINTER)
    qs.remove(_LAST_PRINTER)
    qs.sync()
