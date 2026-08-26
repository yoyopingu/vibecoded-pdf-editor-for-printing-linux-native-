"""
The find bar: the field, the count, and the walking of hits.

State belongs to the panel, not to a tab — the field is one field above all of
them, and switching tabs abandons the search. A search run before a tab switch
has its highlights taken off every document, not just the current one.

The bar floats over the right end of the document row rather than riding in its
layout: opening it must not shove Speichern/Drucken across the row (Acrobat's
search bar overlays, too). It is reparented onto the doc row and placed by hand
on every resize, so no space is ever reserved or taken from the row.
"""
from PyQt6.QtCore import QEvent, QObject, QSize, Qt
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel, QFrame

from tools.app_state import theme_color
from tools.i18n import tr
from tools.shell.icons import icon, rotated
from tools.shell.style import search_icon
from tools.theme import _TV, shell_colours
from tools.viewer.tab import PdfTab


class FindBar(QObject):
    """Owns the widgets and the search state. The host panel supplies the
    current tab and a way to walk every open one; `icon_button` is the button
    that opens the field, for the host's toolbar row."""

    # The field's resting width and the box's right margin, both tuned to the
    # Acrobat benchmark: a field a full search term has room in, a box that
    # never clips against the window's right edge even at the narrowest width.
    FIELD_W   = 260
    RIGHT_M   = 10

    def __init__(self, host, act):
        """`act` is the host's button factory (see _build_doc_actions)."""
        super().__init__(host)
        self._host    = host
        self._job     = None
        self._hits    = []
        self._index   = -1
        self._needle  = ""
        self._doc_row = None

        self.box = QWidget()
        self.box.setObjectName("findBox")
        fl = QHBoxLayout(self.box)
        fl.setContentsMargins(8, 2, 8, 2)
        fl.setSpacing(4)

        self.edit = QLineEdit()
        self.edit.setObjectName("findEdit")
        self.edit.setPlaceholderText(tr("Im Dokument suchen…"))
        self.edit.setFixedWidth(self.FIELD_W)
        self.edit.returnPressed.connect(self._entered)
        fl.addWidget(self.edit)

        self.count = QLabel("")
        self.count.setObjectName("findCount")
        self.count.setMinimumWidth(64)
        fl.addWidget(self.count)

        self._prev = act("", tr("Vorheriger Treffer"),
                         lambda: self.step(-1), "Umschalt+F3", icon=True)
        self._prev.setObjectName("findNavBtn")
        self._prev.setIcon(rotated(icon("chev", colour=theme_color("DIM")), 180))
        self._prev.setIconSize(QSize(14, 14))
        self._prev.setFixedSize(24, 24)
        self._prev.setCursor(Qt.CursorShape.PointingHandCursor)
        fl.addWidget(self._prev)

        self._next = act("", tr("Nächster Treffer"),
                         lambda: self.step(+1), "F3", icon=True)
        self._next.setObjectName("findNavBtn")
        self._next.setIcon(icon("chev", colour=theme_color("DIM")))
        self._next.setIconSize(QSize(14, 14))
        self._next.setFixedSize(24, 24)
        self._next.setCursor(Qt.CursorShape.PointingHandCursor)
        fl.addWidget(self._next)

        # The close cross is kept apart from the step buttons: a 12 px gap and
        # a hairline divider separate the two affordances so the × never reads
        # as part of the prev/next pair.
        fl.addSpacing(12)
        self._div = QFrame()
        self._div.setObjectName("findDiv")
        self._div.setFrameShape(QFrame.Shape.VLine)
        self._div.setFixedWidth(1)
        self._div.setFixedHeight(20)
        fl.addWidget(self._div)
        fl.addSpacing(6)

        self._close = act("", tr("Suche schliessen"),
                          lambda: self.set_visible(False), icon=True)
        self._close.setObjectName("findCloseBtn")
        self._close.setIcon(icon("close", colour=theme_color("DIM")))
        self._close.setIconSize(QSize(14, 14))
        self._close.setFixedSize(24, 24)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        fl.addWidget(self._close)

        self._style_box()

        self.open_btn = act("", tr("Im Dokument suchen"),
                            self.toggle, "Strg+F", icon=True)
        self.open_btn.setIcon(search_icon(_TV['text']))
        self.open_btn.setIconSize(QSize(15, 15))

        self.box.setVisible(False)

    def _shell(self):
        """The current theme's shell palette, for the locally-scoped QSS."""
        dark = theme_color("BG") == shell_colours("dark")["BG"]
        return shell_colours("dark" if dark else "light")

    def _style_box(self):
        """The find bar's whole look lives here, objectName-scoped on the box,
        so it survives a stylesheet rewrite without touching style.py."""
        c = self._shell()
        s2, s3   = c["SURFACE_2"], c["SURFACE_3"]
        strong   = c["LINE_STRONG"]
        line     = c["LINE"]
        acc      = c["ACC"]
        text     = c["TEXT"]
        dim      = c["DIM"]
        hover    = c["HOVER"]
        ibord    = c["INPUT_BORDER"]
        self.box.setStyleSheet(f"""
QWidget#findBox {{
    background: {s2}; border: 1px solid {strong}; border-radius: 8px;
}}
QLineEdit#findEdit {{
    min-height: 28px; font-size: 12px; padding: 0 9px;
    border: 1px solid {ibord}; border-radius: 7px;
    background: {s3}; color: {text};
}}
QPushButton#findNavBtn {{
    background: transparent; color: {dim};
    border: 1px solid {line}; border-radius: 6px;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
    padding: 0;
}}
QPushButton#findNavBtn:hover {{
    background: {hover}; border-color: {acc}; color: {text};
}}
QPushButton#findNavBtn:pressed {{ background: {hover}; }}
QPushButton#findCloseBtn {{
    background: transparent; color: {dim};
    border: none; border-radius: 6px;
    min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
    padding: 0;
}}
QPushButton#findCloseBtn:hover {{ background: {hover}; color: {text}; }}
QPushButton#findCloseBtn:pressed {{ background: {hover}; }}
QLabel#findCount {{
    color: {dim}; font-size: 11.5px; background: transparent;
    font-variant-numeric: tabular-nums;
}}
QFrame#findDiv {{ background: {line}; }}
""")

    def attach_doc_row(self, doc_row):
        """Float the box over the right end of the document row. It is not in
        the row's layout, so it takes no space and can never move the buttons
        beside it."""
        self._doc_row = doc_row
        self.box.setParent(doc_row)
        doc_row.installEventFilter(self)
        self.box.raise_()
        self._reposition()

    def eventFilter(self, obj, event):
        if obj is self._doc_row and event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self):
        """Right-align the floating box in the doc row, vertically centred."""
        if self._doc_row is None:
            return
        w = self.box.sizeHint().width()
        h = self.box.sizeHint().height()
        x = self._doc_row.width() - w - self.RIGHT_M
        y = (self._doc_row.height() - h) // 2
        self.box.setGeometry(x, y, w, h)

    def retheme(self):
        self.open_btn.setIcon(search_icon(_TV['text']))
        # The step/close chevrons are drawn in the shell theme's DIM and go
        # stale on a switch otherwise.
        dim = theme_color("DIM")
        self._prev.setIcon(rotated(icon("chev", colour=dim), 180))
        self._next.setIcon(icon("chev", colour=dim))
        self._close.setIcon(icon("close", colour=dim))
        self._style_box()

    def toggle(self):
        """Strg+F — open the find bar, or close it if it is already open."""
        if self._host._current() is None:
            return
        self.set_visible(not self.box.isVisible())

    def set_visible(self, visible):
        visible = bool(visible) and self._host._current() is not None
        self.box.setVisible(visible)
        if visible:
            self.box.raise_()
            self._reposition()
            self.edit.setFocus()
            self.edit.selectAll()
        else:
            self.count.setText("")
            self.clear()

    def _entered(self):
        """Enter in the field. The same term again means "find the next one",
        which is what every other search field in the world does."""
        text = self.edit.text().strip()
        if not text:
            return
        if text == self._needle and self._hits:
            self.step(+1)
        else:
            self._start(text)

    def _cancel(self):
        if self._job is not None:
            self._job.cancel()
            self._job = None

    def clear(self):
        """Nothing is being searched for any more — take the marks off the
        page. Every tab, not just the current one: a search run before a tab
        switch has left its highlights on the other document."""
        self._cancel()
        self._hits   = []
        self._index  = -1
        self._needle = ""
        for i in range(self._host.tabs.count()):
            w = self._host.tabs.widget(i)
            if isinstance(w, PdfTab):
                w.single.clear_find_hits()

    def _set_count(self, n):
        self.count.setText(tr("{p0} Treffer").format(p0=n))

    def _start(self, text):
        tab = self._host._current()
        if tab is None or not tab.model:
            return
        self._cancel()
        self._needle = text
        self._hits   = []
        self._index  = -1
        tab.single.clear_find_hits()
        self.count.setText(tr("Suche läuft…"))

        from tools.jobs import submit
        from tools.viewer.search import find_all
        model, path = tab.model, tab.pdf_path

        def work(job):
            def report(pos, total, _found):
                job.signals.progress.emit(f"{pos + 1} / {total}")
            return find_all(path, model, text,
                            should_stop=lambda: job.cancelled, progress=report)

        # Owned by the tab, so closing the document stops the search with it.
        self._job = submit(
            work, owner=tab, name="find",
            on_progress=self.count.setText,
            on_done=lambda hits, t=tab, q=text: self._done(t, q, hits))

    def _done(self, tab, needle, hits):
        self._job = None
        if tab is not self._host._current() or needle != self._needle:
            return                # moved on, to another tab or another word
        self._hits = list(hits)
        if not self._hits:
            self._set_count(0)
            tab.single.clear_find_hits()
            return
        from tools.viewer.search import first_at_or_after
        self._show(first_at_or_after(self._hits, tab.single.current_page))

    def step(self, direction):
        if not self._hits:
            return
        self._show((self._index + direction) % len(self._hits))

    def _show(self, index):
        """Go to one occurrence: mark it, turn to its page, and scroll it into
        view if the page is larger than the window."""
        tab = self._host._current()
        if tab is None or not self._hits:
            return
        index = index % len(self._hits)
        self._index = index
        hit = self._hits[index]
        if tab.single.current_page != hit.page:
            tab.single.go_to(hit.page + 1)
        tab.single.set_find_hits(self._hits, index)
        if hit.boxes:
            x0, y0, x1, y1 = hit.boxes[0]
            tab.single.reveal_page_point((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        self._set_count(len(self._hits))
