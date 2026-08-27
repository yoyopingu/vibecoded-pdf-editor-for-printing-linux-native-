"""
What the application calls itself.

One module, because the name reaches the window title, the wordmark in the
title bar, the about box, the crash dialogs, the log header and the Qt
application name — and a rename that lands on four of those and misses two
leaves the app arguing with itself about what it is. Change APP_NAME here and
every visible occurrence follows.

What is deliberately *not* in here, and must not be renamed with it:

  * ``QSettings("CopyShop", "PDFSuite")`` — the settings store. Renaming the
    organisation or application key does not migrate anything; it points the
    app at an empty store, so every preference, remembered printer, paper
    size and recent file silently reverts to its default on the next launch.
  * ``COPYSHOP_LOG_DIR`` and the ``copyshop_*`` temp prefixes — an installed
    launcher, a desktop entry and a running session all pass these between
    them, and the sweep that removes stale flattened copies at startup
    recognises its own files by that prefix.

Those are storage keys. This is a label. They are allowed to disagree.
"""

# The name shown to the user, everywhere.
APP_NAME = "Folio"

# What it is, in one line — the about box and the desktop entry's comment.
APP_TAGLINE = "PDF-Werkzeug für Copyshop und Druckvorstufe"

# Keep in sync with pyproject.toml's version field.
APP_VERSION = "3.0.0"


def app_title() -> str:
    """For the window title and the title-bar wordmark."""
    return APP_NAME


def versioned() -> str:
    """Name and version, for the about box and the log header."""
    return f"{APP_NAME} v{APP_VERSION}"
