"""
Manage.
"""
import os
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from tools.app_state import AppState
import pypdfium2 as pdfium
import tools.app as MAIN
from tests.support import FX, _TMP, _app, _open, _page_labels, _settle, _spin


def _pdfium_dims(path, index=0):
    doc = pdfium.PdfDocument(path)
    try:
        return doc[index].get_width(), doc[index].get_height()
    finally:
        doc.close()


def test_rotation_reaches_the_tools():
    """A page turned in "Seiten verwalten" has to be turned for the tools too.
    Three of them read AppState.current_pdf — the file on disk — instead of the
    flattened view, so they measured and previewed the page in its original
    orientation and produced a crop for the wrong side."""
    from tools._base import ensure_view_snapshot
    from tools.panels.crop_resize import CropResizePanel
    from tools.panels.nup import NUpPanel
    tab, panel = _manage(3, "rot_tools.pdf")
    st = AppState.get()
    w0, h0 = _pdfium_dims(st.current_pdf)
    assert h0 > w0, "fixture should start portrait"

    panel.grid.rotate_selected(0)          # no-op: identity view, no rewrite
    assert ensure_view_snapshot(st.current_pdf) == st.current_pdf

    tab.model.selected = {tab.model.order[0]}
    panel.grid.rotate_selected(90)
    flat = ensure_view_snapshot(st.current_pdf)
    assert flat != st.current_pdf, "rotation did not produce a flattened view"
    assert PdfReader(flat, strict=False).pages[0].get("/Rotate") == 90
    w1, h1 = _pdfium_dims(flat)
    assert (round(w1), round(h1)) == (round(h0), round(w0)), \
        f"rotated page measures {w1:.0f}x{h1:.0f}, expected {h0:.0f}x{w0:.0f}"

    # Watch which file the panels actually open. current_pdf() was always
    # correct — the bug was these three code paths reaching past it to
    # AppState.current_pdf, so only the real call shows it.
    opened = []
    real = pdfium.PdfDocument
    class _Spy(real):
        def __init__(self, inp, *a, **k):
            if isinstance(inp, str): opened.append(inp)
            super().__init__(inp, *a, **k)
    pdfium.PdfDocument = _Spy
    try:
        crop = CropResizePanel()
        for label, call in (
                ("crop margins",  lambda: crop._set_margins_for_size(595.0, 842.0)),
                ("crop preview",  lambda: crop._render_preview(400, 500, 1.0)),
                ("n-up preview",  lambda: NUpPanel()._render_preview(400, 500, 1.0))):
            opened.clear()
            call()
            assert opened, f"{label} opened no document at all"
            assert all(p == flat for p in opened), \
                f"{label} read {[os.path.basename(p) for p in opened]}, not the rotated view"
    finally:
        pdfium.PdfDocument = real


def test_rotation_reaches_the_preview():
    """The single-page view turns the bitmap but used to keep the page's original
    width and height, so a rotated page was fitted into a portrait box, measured
    as portrait in the "Masse" readout, and drawn smaller than it should be."""
    from tools.viewer.panel import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    vp.open_file(FX["normal"])
    _spin(60, 0.01)
    tab = vp.tabs.currentWidget()
    sv  = tab.single
    _settle(vp, lambda: sv._page_w_pt > 0, tries=200)
    portrait = (sv._page_w_pt, sv._page_h_pt)
    assert portrait[1] > portrait[0], "fixture should start portrait"
    area_before = sv._last_pm.width() * sv._last_pm.height()

    tab._build_manage_once()
    tab.model.selected = {tab.model.order[0]}
    tab._manage_panel.grid.rotate_selected(90)
    sv._current = 0
    sv._render()
    # Wait for the render, not for the dimensions: those are now corrected as
    # soon as the rotation is seen, before anything has been re-rendered.
    assert _settle(vp, lambda: sv._render_task is None
                   and not getattr(sv, "_showing_provisional", False)
                   and sv._page_w_pt > portrait[0], tries=250), \
        "the preview never picked up the rotation"
    assert (round(sv._page_w_pt), round(sv._page_h_pt)) == \
           (round(portrait[1]), round(portrait[0])), \
        f"preview reports {sv._page_w_pt:.0f}x{sv._page_h_pt:.0f}, expected the swap"
    pm = sv._last_pm
    assert pm.width() > pm.height(), "the rendered page is not landscape"
    # fitted to the same window, so it should not have shrunk
    assert pm.width() * pm.height() > area_before * 0.9, \
        "the rotated page was fitted into the old portrait box"
    vp.deleteLater()


def test_char_boxes_follow_a_rotated_page():
    """Text rectangles are measured before the bitmap is turned; if they do not
    turn with it, selecting text on a rotated page highlights the wrong place."""
    from tools.render.region import _rotate_char_boxes
    W, H = 400.0, 800.0
    box = [("a", 10.0, 20.0, 30.0, 50.0)]      # near the top-left
    assert _rotate_char_boxes(box, 0, W, H) == box
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 90, W, H)[0]
    assert (x0, y0, x1, y1) == (H - 50.0, 10.0, H - 20.0, 30.0)
    assert 0 <= x0 < x1 <= H and 0 <= y0 < y1 <= W, "90° box left the image"
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 180, W, H)[0]
    assert (x0, y0, x1, y1) == (W - 30.0, H - 50.0, W - 10.0, H - 20.0)
    (_, x0, y0, x1, y1) = _rotate_char_boxes(box, 270, W, H)[0]
    assert (x0, y0, x1, y1) == (20.0, W - 30.0, 50.0, W - 10.0)
    assert 0 <= x0 < x1 <= H and 0 <= y0 < y1 <= W, "270° box left the image"


def _manage(n_pages=6, name="mgr.pdf"):
    """A PdfTab with its page manager built, wired into AppState."""
    from tools.viewer.tab import PdfTab
    src = os.path.join(_TMP, name)
    c = canvas.Canvas(src, pagesize=A4)
    for i in range(n_pages):
        c.setFont("Helvetica", 80); c.drawCentredString(300, 400, f"P{i+1}"); c.showPage()
    c.save()
    tab = PdfTab(src)
    st = AppState.get(); st.open_pdf(tab.pdf_path); st.page_model = tab.model
    tab._build_manage_once()
    return tab, tab._manage_panel


def _answer_dialog(word):
    """Answer the copy/move QMessageBox by button text, without showing it."""
    from PyQt6.QtWidgets import QMessageBox
    def picked(self):
        for b in self.buttons():
            if word in b.text():
                return b
        return None
    QMessageBox.exec = lambda self: self.setResult(0)
    QMessageBox.clickedButton = picked


def test_manage_open_as_tab_copies_or_moves():
    """"Als neuen Tab oeffnen" asks whether the pages should stay. Moving them is
    what the removed "Nach Bereichen..." split was for, only driven by picking
    pages instead of typing ranges into a prompt."""
    from PyQt6.QtWidgets import QMessageBox
    from tools._base import ensure_view_snapshot
    tab, mp = _manage(6, "mgr_tab.pdf")
    opened = []
    AppState.get().open_result = lambda p, t="": opened.append(p)
    real = (QMessageBox.exec, QMessageBox.clickedButton)
    try:
      _answer_dialog("Kopieren")
      mp.model.selected = {mp.model.order[1], mp.model.order[2]}
      mp._open_as_tab()
      assert _page_labels(opened[-1]) == ["P2", "P3"], _page_labels(opened[-1])
      assert len(mp.model.order) == 6, "copy must leave the document alone"

      _answer_dialog("Verschieben")
      mp.model.selected = {mp.model.order[0], mp.model.order[1]}
      mp._open_as_tab()
      assert _page_labels(opened[-1]) == ["P1", "P2"], _page_labels(opened[-1])
      assert len(mp.model.order) == 4, "move must remove them from the document"
      assert _page_labels(ensure_view_snapshot(AppState.get().current_pdf)) == \
        ["P3", "P4", "P5", "P6"]

      _answer_dialog("Abbrechen")          # matches nothing -> treated as cancel
      mp.model.selected = {mp.model.order[0]}
      n_before, n_opened = len(mp.model.order), len(opened)
      mp._open_as_tab()
      assert len(mp.model.order) == n_before and len(opened) == n_opened, \
        "cancelling must neither open a tab nor touch the pages"
    finally:
        QMessageBox.exec, QMessageBox.clickedButton = real


def test_manage_inserts_several_files_at_once():
    """"Aus Datei(en) einfuegen..." replaced the separate merge button, so it has
    to accept more than one file and insert them after the selection."""
    from PyQt6.QtWidgets import QFileDialog
    from tools._base import ensure_view_snapshot
    extras = []
    for tag, n in (("X", 2), ("Y", 1)):
        p = os.path.join(_TMP, f"ins_{tag}.pdf")
        c = canvas.Canvas(p, pagesize=A4)
        for i in range(n):
            c.setFont("Helvetica", 80); c.drawCentredString(300, 400, f"{tag}{i+1}")
            c.showPage()
        c.save(); extras.append(p)

    tab, mp = _manage(3, "mgr_ins.pdf")
    real = QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (extras, ""))
    try:
        mp.model.selected = {mp.model.order[0]}      # after page 1
        mp._insert_from_file()
    finally:
        QFileDialog.getOpenFileNames = real
    assert len(mp.model.order) == 6, f"expected 6 pages, got {len(mp.model.order)}"
    assert _page_labels(ensure_view_snapshot(AppState.get().current_pdf)) == \
        ["P1", "X1", "X2", "Y1", "P2", "P3"]


def test_manage_panel_has_no_duplicate_actions():
    """Saving moved to Datei ▸ and merge folded into insert-from-file. Those
    buttons must be gone from the sidebar, not merely hidden, and Strg+S must be
    owned by exactly one thing or Qt delivers it to neither."""
    from PyQt6.QtGui import QShortcut
    tab, mp = _manage(3, "mgr_dup.pdf")
    for gone in ("_merge", "_split_ranges", "_save", "_save_as", "_do_save"):
        assert not hasattr(mp, gone), f"{gone} is still on the panel"

    win = MAIN.MainWindow()
    try:
        names = []
        for m in win._title_bar.menu_bar.actions():
            if m.menu():
                names += [a.text() for a in m.menu().actions() if a.text()]
        assert "Speichern" in names and "Speichern unter…" in names, names
        keys = [s.key().toString() for s in win.viewer.findChildren(QShortcut)]
        assert "Ctrl+S" not in keys, "Ctrl+S is registered twice — it will not fire"
    finally:
        win.deleteLater(); _app.processEvents()


def test_a_snapshot_is_never_flattened_twice():
    """ensure_view_snapshot returns its own output unchanged when handed back.

    The check used to be `base_path in _VIEW_SNAPSHOTS.values()`, a scan over
    every value on every call. It is a set now, which only stays correct if the
    two structures are updated together — so this also asserts they agree after
    a snapshot is superseded."""
    import tools._base as B
    from tools._base import ensure_view_snapshot
    from tools.viewer.model import PageModel

    st = _open(FX["normal"])
    st.page_model = PageModel(5)
    st.page_model.order.reverse()             # no longer a 1:1 view of the file
    flat = ensure_view_snapshot(st.current_pdf)
    assert flat != st.current_pdf, "reordered model should have been flattened"
    assert flat in B._SNAPSHOT_PATHS
    assert set(B._VIEW_SNAPSHOTS.values()) == B._SNAPSHOT_PATHS, "structures drifted"

    # Handing the snapshot back must not apply the model to it a second time.
    assert ensure_view_snapshot(flat) == flat

    # Superseding it drops the old path from both.
    st.page_model.rotations[st.page_model.order[0]] = 90
    newer = ensure_view_snapshot(st.current_pdf)
    assert newer != flat
    assert flat not in B._SNAPSHOT_PATHS, "stale path left in the set"
    assert set(B._VIEW_SNAPSHOTS.values()) == B._SNAPSHOT_PATHS, "structures drifted"


def test_a_page_card_is_one_widget_and_carries_no_stylesheet():
    """What made switching into the page manager slow.

    A card used to be three widgets — a frame, a label for the thumbnail and a
    label for the number — each carrying its own stylesheet. The page manager
    builds one per page, so a thousand-page document meant 3000 widgets and
    3000 stylesheet parses on the GUI thread before the grid could be shown,
    and the window was frozen for all of it. Measured on that first switch:

        200 pages     360 ms -> 120 ms
        500 pages     680 ms -> 147 ms
       1000 pages    1237 ms -> 215 ms

    A card paints itself now. That is the property worth pinning: a timing
    would be a stopwatch reading on one machine, but "no children and no
    stylesheet" is the thing that cannot come back by accident — and it is what
    Okular's thumbnail list and Qt's own icon views do for the same reason.
    """
    from PyQt6.QtWidgets import QWidget
    from tools.viewer.page_grid import PageGrid
    from tools.viewer.model import PageModel

    grid = PageGrid(PageModel(6), FX["normal"])
    _app.processEvents()
    try:
        card = grid._cards[0]
        kids = card.findChildren(QWidget)
        assert not kids, f"a card holds {len(kids)} child widget(s): {kids}"
        assert card.styleSheet() == "", \
            f"a card carries a stylesheet again: {card.styleSheet()!r}"
        # And nothing above it puts one back on the cards by selector, which
        # was the first thing tried and did not help: Qt still resolves an
        # inherited sheet for every widget it can match.
        assert grid.styleSheet() == "", \
            f"the grid carries a stylesheet the cards must be matched against"
        # It still draws: a card with no children must paint its own content.
        from PyQt6.QtGui import QPixmap
        pm = QPixmap(card.size()); pm.fill()
        card.render(pm)
        assert not pm.toImage().isNull(), "the card painted nothing"
    finally:
        grid.deleteLater(); _app.processEvents()
    return "one widget, no stylesheet, paints itself"
