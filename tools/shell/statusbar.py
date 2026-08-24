"""
The window-level status bar: one bar under every view.

The per-tab info bar it replaces was three things at once — the document's
readings, the app's mouth, and a zoom control that only reached the preview.
Here the readings report on the left (format · colour profile · colour/grey
counter · preflight light), the centre carries the one message the app most
recently had to say (click it for the Protokoll), and the right end holds the
ruler switch and a zoomer that is a remote control for whichever view is
active — the view owns its zoom, the bar only talks to it.

Nothing here knows where its information comes from: the window feeds it
through the setters, and the zoom/ruler/message clicks leave as signals.
"""
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QTimer
from PyQt6.QtGui import QFontMetrics
from PyQt6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QWidget)

from tools.app_state import AppState, theme_color
from tools.i18n import tr
from tools.shell.icons import icon
from tools.theme import _TV, _register_themed

_ZOOMER_H = 28          # the concept's zoomer pill height
_ZOOM_BTN_W = 28
_ZOOM_VAL_W = 46
_MSG_HOLD_MS = 6000     # how long a transient message stays before the
                        # view default returns


class _MessageLabel(QLabel):
    """The centre message: elided, centred, and clickable.

    A plain QLabel clips long text at both ends once it is centred, so the
    text is re-elided on every resize. The click opens the Protokoll —
    which is also why the label carries a pointing-hand cursor and the
    hover surface of a button rather than the flat look of a reading."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setObjectName("sbMsg")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr("Protokoll öffnen"))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._full = ""

    def set_full_text(self, text):
        self._full = text or ""
        self._relabel()

    def _relabel(self):
        fm = QFontMetrics(self.font())
        self.setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight,
                                   max(10, self.width() - 28)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relabel()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton \
                and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class StatusBar(QWidget):
    """Readings left, transient message centre, ruler + zoomer right.
    See docs/gui-concept.html's .statusbar."""

    zoom_out_requested = pyqtSignal()
    zoom_in_requested = pyqtSignal()
    zoom_fit_requested = pyqtSignal()
    ruler_toggled = pyqtSignal(bool)
    message_clicked = pyqtSignal()
    preflight_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statusBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(38)

        self._default_msg = ""
        self._held = False           # a held message survives a view switch
        self._pf_state = "unknown"
        self._pf_issues = []
        self._counts = None          # (colour, grey) or None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 10, 0)
        lay.setSpacing(10)

        # ── Readings ──────────────────────────────────────────────────────────
        self._fmt_lbl = self._reading()
        self._cs_lbl = self._reading()
        self._counts_lbl = self._reading()
        self._counts_lbl.setToolTip(tr("Farbige Seiten / Graustufen-Seiten im Dokument"))
        self._seps = [self._sep(), self._sep()]
        lay.addWidget(self._fmt_lbl)
        lay.addWidget(self._seps[0])
        lay.addWidget(self._cs_lbl)
        lay.addWidget(self._seps[1])
        lay.addWidget(self._counts_lbl)

        # The preflight light: green when the document could go on a press,
        # amber for what is worth knowing first, hidden until there is an
        # answer. The findings travel in the tooltip; the click asks for the
        # full check. Matches the concept's .pf pill: 22px high, 11px radius,
        # dot with glow, hover on the accent-soft ground.
        self._pf_btn = QWidget()
        self._pf_btn.setObjectName("pfLight")
        self._pf_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pf_btn.setFixedHeight(22)
        pf_lay = QHBoxLayout(self._pf_btn)
        pf_lay.setContentsMargins(9, 0, 9, 0)
        pf_lay.setSpacing(6)
        self._pf_dot = QLabel()
        self._pf_dot.setObjectName("pfDot")
        self._pf_dot.setFixedSize(7, 7)
        self._pf_text = QLabel("")
        self._pf_text.setObjectName("pfText")
        pf_lay.addWidget(self._pf_dot)
        pf_lay.addWidget(self._pf_text)
        self._pf_btn.installEventFilter(self)
        self._pf_state = "unknown"
        self._pf_issues = []
        lay.addWidget(self._pf_btn)

        # ── Message ───────────────────────────────────────────────────────────
        self._msg = _MessageLabel()
        self._msg.clicked.connect(self.message_clicked.emit)
        lay.addWidget(self._msg, 1)

        self._msg_timer = QTimer()
        self._msg_timer.setSingleShot(True)
        self._msg_timer.timeout.connect(self.clear_message)

        # ── Ruler + zoomer ────────────────────────────────────────────────────
        self._ruler_btn = QPushButton()
        self._ruler_btn.setObjectName("iconBtn")
        self._ruler_btn.setCheckable(True)
        self._ruler_btn.setFixedSize(_ZOOMER_H, _ZOOMER_H)
        self._ruler_btn.setToolTip(tr("Lineale und Hilfslinien") + "  (Strg+R)")
        self._ruler_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ruler_btn.clicked.connect(
            lambda on: self.ruler_toggled.emit(bool(on)))
        lay.addWidget(self._ruler_btn)

        zoomer = QWidget()
        zoomer.setObjectName("sbZoomer")
        zoomer.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        zoomer.setFixedHeight(_ZOOMER_H)
        zl = QHBoxLayout(zoomer)
        zl.setContentsMargins(2, 2, 2, 2)
        zl.setSpacing(0)

        self._zoom_out_btn = self._zoom_btn(tr("Verkleinern"),
                                            self.zoom_out_requested.emit)
        self._zoom_lbl = QLabel("100 %")
        self._zoom_lbl.setObjectName("sbZoomVal")
        self._zoom_lbl.setFixedWidth(_ZOOM_VAL_W)
        self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_in_btn = self._zoom_btn(tr("Vergrössern"),
                                           self.zoom_in_requested.emit)
        div = QWidget()
        div.setObjectName("sbZoomDiv")
        div.setFixedSize(1, 14)
        div.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._zoom_fit_btn = self._zoom_btn(tr("Ganze Seite") + "  (Strg+0)",
                                            self.zoom_fit_requested.emit)
        for w in (self._zoom_out_btn, self._zoom_lbl, self._zoom_in_btn,
                  div, self._zoom_fit_btn):
            zl.addWidget(w)
        lay.addWidget(zoomer)

        self.set_preflight("unknown")
        self._render_counts()
        self._subscribe()
        _register_themed(self)
        self._apply_theme()

    def _subscribe(self):
        """The readings arrive on the status bus. Whoever owns the current
        value — the active tab's view, the greyscale tool's scan — publishes
        by emitting; the bar has no idea who that is and never needs to."""
        bus = AppState.get()
        bus.zoom_changed.connect(self.set_zoom_percent)
        bus.page_metrics_changed.connect(self.set_metrics)
        bus.colorspace_changed.connect(self.set_colorspace)
        bus.preflight_changed.connect(self.set_preflight)
        bus.colour_counts_changed.connect(self._on_counts)
        bus.ruler_changed.connect(self.set_rulers_checked)
        bus.app_message_requested.connect(self.show_message)

    def _on_counts(self, counts):
        if counts is None:
            self.set_colour_counts(None, None)
        else:
            self.set_colour_counts(*counts)

    # ── small builders ────────────────────────────────────────────────────────

    def _reading(self):
        lbl = QLabel("")
        lbl.setObjectName("sbReading")
        return lbl

    def _sep(self):
        lbl = QLabel("·")
        lbl.setObjectName("sbSep")
        return lbl

    def _zoom_btn(self, tip, fn):
        btn = QPushButton()
        btn.setObjectName("iconBtn")
        btn.setFixedSize(_ZOOM_BTN_W, _ZOOMER_H - 4)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(fn)
        return btn

    # ── readings ──────────────────────────────────────────────────────────────

    def set_metrics(self, text):
        """Format and size as one reading — "A4 · 210×297 mm". Empty hides."""
        self._fmt_lbl.setText(text or "")
        self._sync_readings()

    def set_colorspace(self, text):
        """The colour profile reading — "sRGB". Empty hides."""
        self._cs_lbl.setText(text or "")
        self._sync_readings()

    def set_colour_counts(self, colour, grey):
        """The colour/greyscale page counter (decision 5). None clears."""
        self._counts = None if colour is None else (int(colour), int(grey))
        self._render_counts()

    def _render_counts(self):
        if self._counts is None:
            self._counts_lbl.setText("")
        else:
            c, g = self._counts
            strong = f"color:{_TV['text']};font-weight:600"
            self._counts_lbl.setText(
                f'<span style="{strong}">{c}</span> {tr("farbig")}'
                f' · <span style="{strong}">{g}</span> {tr("Graustufen")}')
        self._sync_readings()

    def _sync_readings(self):
        """An absent reading takes its dot with it — an empty bar that still
        shows "· ·" reads as a rendering fault rather than "no document"."""
        filled = [bool(self._fmt_lbl.text()), bool(self._cs_lbl.text()),
                  bool(self._counts_lbl.text())]
        for lbl, has in zip((self._fmt_lbl, self._cs_lbl, self._counts_lbl),
                            filled):
            lbl.setVisible(has)
        for sep, left, right in zip(self._seps, filled, filled[1:]):
            sep.setVisible(left and right)

    # ── the preflight light ───────────────────────────────────────────────────

    def set_preflight(self, state, issues=()):
        """state: "unknown" | "running" | "ok" | "warn"."""
        self._pf_state = state
        self._pf_issues = list(issues)
        t = _TV
        label, colour, hover = {
            "running": (tr("Druckvorstufe …"), t['dim'], t['hover']),
            "ok":      (tr("Druckvorstufe OK"), t['ok'], t['hover']),
        }.get(state, ("", t['dim'], t['hover']))
        if state == "warn":
            # tr() has no plural machinery, so the branch is here rather than a
            # "1 Hinweis(e)" that is wrong in both directions.
            n = len(self._pf_issues)
            label = tr("1 Hinweis") if n == 1 else tr('{p0} Hinweise').format(p0=n)
            colour = t['warn']
        self._pf_text.setText(label)
        self._pf_btn.setVisible(bool(label))
        self._pf_dot.setStyleSheet(
            f"QLabel#pfDot{{background:{colour};border-radius:3px;}}")
        self._pf_text.setStyleSheet(
            f"QLabel#pfText{{color:{colour};font-size:12px;background:transparent;}}")
        self._pf_btn.setStyleSheet(
            f"QWidget#pfLight{{background:transparent;border-radius:11px;"
            f"border:none;}}"
            f"QWidget#pfLight:hover{{background:{hover};}}")
        self._pf_text.setToolTip(
            "\n".join(self._pf_issues) if self._pf_issues
            else tr("Druckvorstufenprüfung"))
        self._pf_dot.setToolTip(
            "\n".join(self._pf_issues) if self._pf_issues
            else tr("Druckvorstufenprüfung"))

    def eventFilter(self, obj, event):
        if obj is self._pf_btn and event.type() == event.Type.MouseButtonRelease:
            if self._pf_btn.rect().contains(event.position().toPoint()):
                self.preflight_requested.emit()
                return True
        return super().eventFilter(obj, event)

    def set_default_message(self, text):
        """What the bar says when nothing more recent has happened — the
        current view's own line, set by the window on every view switch."""
        self._default_msg = text or ""
        if not self._msg_timer.isActive() and not self._held:
            self._msg.set_full_text(self._default_msg)

    def show_message(self, text, hold=False):
        """One message, centred. Transient messages return to the view
        default after six seconds; held ones (tool results, errors) stay
        until the next message or an explicit reset — decision 2."""
        self._msg_timer.stop()
        self._held = bool(hold)
        self._msg.set_full_text(text or "")
        if text and not hold:
            self._msg_timer.start(_MSG_HOLD_MS)

    def clear_message(self):
        self._msg_timer.stop()
        self._held = False
        self._msg.set_full_text(self._default_msg)

    # ── the right end ─────────────────────────────────────────────────────────

    def set_zoom_percent(self, pct):
        self._zoom_lbl.setText(f"{int(round(pct))} %")

    def set_rulers_checked(self, on):
        self._ruler_btn.blockSignals(True)
        self._ruler_btn.setChecked(bool(on))
        self._ruler_btn.blockSignals(False)

    # ── theme ─────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        dim = theme_color("DIM")
        self._ruler_btn.setIcon(icon("ruler", colour=dim, size=16))
        self._zoom_out_btn.setIcon(icon("minus", colour=dim, size=16))
        self._zoom_in_btn.setIcon(icon("plus", colour=dim, size=16))
        self._zoom_fit_btn.setIcon(icon("fit", colour=dim, size=16))
        for b in (self._ruler_btn, self._zoom_out_btn, self._zoom_in_btn,
                  self._zoom_fit_btn):
            b.setIconSize(QSize(16, 16))
        self._render_counts()
        self.set_preflight(self._pf_state, self._pf_issues)
