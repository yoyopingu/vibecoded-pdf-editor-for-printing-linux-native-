"""
The find bar: the field, the count, and the walking of hits.

State belongs to the panel, not to a tab — the field is one field above all of
them, and switching tabs abandons the search. A search run before a tab switch
has its highlights taken off every document, not just the current one.
"""
from PyQt6.QtCore import QObject, QSize
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLineEdit, QLabel

from tools.app_state import theme_color
from tools.i18n import tr
from tools.shell.icons import icon, rotated
from tools.shell.style import search_icon
from tools.theme import _TV
from tools.viewer.tab import PdfTab


class FindBar(QObject):
    """Owns the widgets and the search state. The host panel supplies the
    current tab and a way to walk every open one; `icon_button` is the button
    that opens the field, for the host's toolbar row."""

    def __init__(self, host, act):
        """`act` is the host's button factory (see _build_doc_actions)."""
        super().__init__(host)
        self._host = host
        self._job    = None
        self._hits   = []
        self._index  = -1
        self._needle = ""

        # An icon until it is used, then a field. 26 px at rest against 214 px
        # in use: the tab strip keeps the difference for the nine tenths of the
        # time nobody is searching.
        self.box = QWidget()
        self.box.setObjectName("findBox")
        fl = QHBoxLayout(self.box)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(2)
        self.edit = QLineEdit()
        self.edit.setObjectName("findEdit")
        self.edit.setPlaceholderText(tr("Im Dokument suchen…"))
        self.edit.setFixedWidth(150)
        self.edit.returnPressed.connect(self._entered)
        fl.addWidget(self.edit)
        self.count = QLabel("")
        self.count.setObjectName("dimLabel")
        self.count.setMinimumWidth(58)
        fl.addWidget(self.count)
        self._prev = act("", tr("Vorheriger Treffer"),
                         lambda: self.step(-1), "Umschalt+F3", icon=True)
        self._prev.setIcon(rotated(icon("chev", colour=theme_color("DIM")), 180))
        self._prev.setIconSize(QSize(16, 16))
        self._next = act("", tr("Nächster Treffer"),
                         lambda: self.step(+1), "F3", icon=True)
        self._next.setIcon(icon("chev", colour=theme_color("DIM")))
        self._next.setIconSize(QSize(16, 16))
        self._close = act("", tr("Suche schliessen"),
                          lambda: self.set_visible(False), icon=True)
        self._close.setIcon(icon("close", colour=theme_color("DIM")))
        self._close.setIconSize(QSize(16, 16))
        for b in (self._prev, self._next, self._close):
            b.setParent(self.box)
            fl.addWidget(b)
        self.box.setVisible(False)

        self.open_btn = act("", tr("Im Dokument suchen"),
                            self.toggle, "Strg+F", icon=True)
        self.open_btn.setIcon(search_icon(_TV['text']))
        self.open_btn.setIconSize(QSize(15, 15))

    def retheme(self):
        self.open_btn.setIcon(search_icon(_TV['text']))
        # The step/close chevrons are drawn in the shell theme's DIM and go
        # stale on a switch otherwise.
        dim = theme_color("DIM")
        self._prev.setIcon(rotated(icon("chev", colour=dim), 180))
        self._next.setIcon(icon("chev", colour=dim))
        self._close.setIcon(icon("close", colour=dim))

    def toggle(self):
        """Strg+F — open the find bar, or close it if it is already open."""
        if self._host._current() is None:
            return
        self.set_visible(not self.box.isVisible())

    def set_visible(self, visible):
        visible = bool(visible) and self._host._current() is not None
        self.box.setVisible(visible)
        self.open_btn.setVisible(not visible)
        if visible:
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
            self.count.setText(tr("Keine Treffer"))
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
        self.count.setText(f"{index + 1} / {len(self._hits)}")
