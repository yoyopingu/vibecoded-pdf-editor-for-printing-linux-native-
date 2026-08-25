"""
The Protokoll: the app's memory of everything it has said.

The status bar's centre line is only the present — one message, replaced the
moment the next one comes. The Protokoll is every message that ever passed
through, kept for the whole session and opened on demand (click the centre
line, or Hilfe ▸ "Logs anzeigen"). This is decision 2: one log store, one
window, two entry points.

`notify()` is the single funnel. It appends a `(timestamp, level, text)` row to
the store *and* publishes the message on the bus, so the StatusBar — a pure
subscriber that has no idea where the message came from — shows it. Nothing in
the tool panels ever reaches the bar directly; they call `notify()` (or the
`LogAdapter` that wraps it) and the bus does the rest.
"""
import time

from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QPlainTextEdit, QPushButton,
                             QVBoxLayout)

from tools.app_state import AppState, theme_color
from tools.i18n import tr

# The store: one list of (timestamp, level, text) shared by everything. It is
# the session's memory — the status line is only the present, the Protokoll is
# the whole of what has been said. Levels are INFO (progress/general), OK
# (a result) and ERR (a failure).
log_store = []


def notify(text, level="OK", hold=False):
    """Record `text` and show it on the bar's centre line, via the bus.

    `hold` decides how long the centre line keeps it: transient messages
    (progress) return to the view default after a few seconds; held ones (tool
    results, errors) stay until the next message or an explicit clear. Every
    message — held or not — is still written to the store, so nothing a tool
    said is ever lost.
    """
    text = text or ""
    log_store.append((_now(), level, text))
    AppState.get().app_message_requested.emit(text, bool(hold))


def clear_log():
    """Empty the store — what the Protokoll's "Leeren" button does."""
    log_store.clear()


def _now():
    return time.strftime("%H:%M:%S")


class LogAdapter:
    """A drop-in for the old LogBox widget.

    The panels still write ``self.log.log(msg)`` / ``self.log.clear_log()`` and
    should never notice the change. Here those calls become `notify()`: a plain
    message is a transient INFO line, an ``error=True`` is a held ERR line, and
    BasePanel's explicit result path asks for a held OK line with ``hold=True``.
    """

    def log(self, msg, error=False, *, hold=False):
        if error:
            notify(msg, level="ERR", hold=True)
        elif hold:
            notify(msg, level="OK", hold=True)
        else:
            notify(msg, level="INFO", hold=False)

    def clear_log(self):
        # The store is session memory — one run must not wipe the whole
        # history that came before it, so this is deliberately a no-op.
        pass


class ProtokollWindow(QDialog):
    """The scrollable log of every app message — decision 2's Protokoll."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Protokoll"))
        self.setObjectName("protokoll")
        self.setMinimumSize(560, 420)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)

        self._body = QPlainTextEdit()
        self._body.setReadOnly(True)
        lay.addWidget(self._body, 1)

        foot = QHBoxLayout()
        foot.setSpacing(8)
        self._clear_btn = QPushButton(tr("Leeren"))
        self._clear_btn.clicked.connect(self._on_clear)
        self._close_btn = QPushButton(tr("Schließen"))
        self._close_btn.clicked.connect(self.accept)
        foot.addWidget(self._clear_btn)
        foot.addStretch()
        foot.addWidget(self._close_btn)
        lay.addLayout(foot)

        self.refresh()

    def refresh(self):
        if log_store:
            self._body.setStyleSheet("")
            self._body.setPlainText(
                "\n".join(f"{ts} · {lv} · {tx}" for ts, lv, tx in log_store))
        else:
            # A dim placeholder so an empty log reads as "nothing here" rather
            # than a blank box.
            self._body.setStyleSheet(
                f"QPlainTextEdit{{color:{theme_color('DIM')};font-style:italic;}}")
            self._body.setPlainText(tr("Protokoll ist leer."))

    def showEvent(self, event):
        # Re-read the store on every show, so the window never shows a stale
        # snapshot left from an earlier open.
        self.refresh()
        super().showEvent(event)

    def _on_clear(self):
        clear_log()
        self.refresh()