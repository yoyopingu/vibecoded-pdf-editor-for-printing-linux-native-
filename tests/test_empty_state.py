"""
The empty window: "Zuletzt geöffnet", and the canvas swap that shows it.
"""
import os

from tests.support import FX, _TMP, _app, _settle, _spin


def _saved_recent():
    from tools.shell.settings import AppSettings
    return AppSettings.get()._qs.value("general/recent_files", "")


def _restore_recent(raw):
    from tools.shell.settings import AppSettings
    qs = AppSettings.get()._qs
    if raw:
        qs.setValue("general/recent_files", raw)
    else:
        qs.remove("general/recent_files")


def test_recent_files_orders_dedupes_and_drops_missing():
    """Newest first, re-opening a file moves it back to the front rather than
    duplicating it, a file that no longer exists is filtered out rather than
    shown as a dead card, and the list never grows past four."""
    from tools.shell.settings import AppSettings
    s = AppSettings.get()
    saved = _saved_recent()
    try:
        s._qs.remove("general/recent_files")
        a = os.path.join(_TMP, "recent_a.pdf")
        b = os.path.join(_TMP, "recent_b.pdf")
        gone = os.path.join(_TMP, "recent_gone.pdf")
        for p in (a, b, gone):
            open(p, "wb").write(b"%PDF-1.4\n%%EOF")

        s.add_recent_file(a)
        s.add_recent_file(b)
        s.add_recent_file(gone)
        os.remove(gone)
        assert s.recent_files() == [b, a], \
            "a missing file should be filtered, not shown as a dead card"

        s.add_recent_file(a)             # re-opening: moves to the front
        assert s.recent_files() == [a, b], "re-opening should not duplicate"

        for i in range(6):
            p = os.path.join(_TMP, f"recent_extra{i}.pdf")
            open(p, "wb").write(b"%PDF-1.4\n%%EOF")
            s.add_recent_file(p)
        assert len(s.recent_files()) == 4, "keeps at most four"
    finally:
        _restore_recent(saved)


def test_opening_the_first_file_hides_the_empty_state_and_renders():
    """The canvas swap has to flip before the new tab's content is built —
    a page view constructed while its ancestor is still hidden skips its
    first render, and nothing later asks it to try again."""
    from tools.viewer.panel import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    assert vp._empty_state.isVisible() and not vp.tabs.isVisible(), \
        "should open on the empty state"

    vp.open_file(FX["normal"])
    assert _settle(vp, lambda: vp.tabs.count()
                   and vp.tabs.currentWidget().single._last_pm, tries=300), \
        "the first page never rendered"
    assert vp.tabs.isVisible() and not vp._empty_state.isVisible()

    vp._close_tab(0)
    _spin(20, 0.01)
    assert vp._empty_state.isVisible() and not vp.tabs.isVisible(), \
        "closing the last tab should bring the empty state back"
    vp.deleteLater(); _app.processEvents()


def test_the_empty_state_offers_recent_files_and_reopens_one():
    from tools.shell.settings import AppSettings
    from tools.viewer.panel import PageViewerPanel
    s = AppSettings.get()
    saved = _saved_recent()
    try:
        s._qs.remove("general/recent_files")
        s.add_recent_file(FX["single"])

        vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
        assert [c for c in vp._empty_state._cards], "no recent card was built"
        card = vp._empty_state._cards[0]

        card.clicked.emit()
        assert _settle(vp, lambda: vp.tabs.count()
                       and vp.tabs.currentWidget().single._last_pm, tries=300), \
            "clicking a recent card never opened it"
        assert vp.tabs.currentWidget().pdf_path == FX["single"]
        vp.deleteLater(); _app.processEvents()
    finally:
        _restore_recent(saved)


def test_dropping_files_opens_one_or_previews_several():
    """One dropped file opens directly; several go to the same merge-or-
    separate preview a multi-select Datei-öffnen does — files_dropped is
    wired to the same dispatch, not a second implementation of it."""
    from tools.viewer.panel import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()

    vp._empty_state.files_dropped.emit([FX["single"]])
    assert _settle(vp, lambda: vp.tabs.count() == 1, tries=300)
    assert vp.tabs.currentWidget().pdf_path == FX["single"]

    vp.deleteLater(); _app.processEvents()

    vp2 = PageViewerPanel(); vp2.resize(1000, 700); vp2.show()
    vp2._empty_state.files_dropped.emit([FX["single"], FX["normal"]])
    _spin(30, 0.01)
    from tools.viewer.merge import MergeOrderWidget
    assert vp2.tabs.count() == 1
    assert isinstance(vp2.tabs.widget(0), MergeOrderWidget), \
        "several dropped files should land in the merge preview, not open separately"
    vp2.deleteLater(); _app.processEvents()


def test_drop_only_accepts_files_this_app_can_open():
    """dragEnterEvent's filter is the same classify() every open path uses —
    a .zip dragged in must not be offered as something the drop will accept."""
    from PyQt6.QtCore import QMimeData, QUrl

    from tools.viewer.empty_state import EmptyStateWidget
    w = EmptyStateWidget()

    good = QMimeData()
    good.setUrls([QUrl.fromLocalFile(FX["normal"])])
    assert w._local_files(good) == [FX["normal"]]

    bad_path = os.path.join(_TMP, "not_supported.zip")
    open(bad_path, "wb").write(b"PK\x03\x04")
    bad = QMimeData()
    bad.setUrls([QUrl.fromLocalFile(bad_path)])
    assert w._local_files(bad) == []

    w.deleteLater(); _app.processEvents()
