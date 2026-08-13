"""
Merge Preview.
"""
import os, time, tempfile
from PyQt6.QtWidgets import QLabel
from pypdf import PdfReader
from tools.viewer.model import PageModel
import main as MAIN
from tests.support import FX, _TMP, _app, _pdfium_page_text, _settle, _spin


def _click(grid, pos, mod):
    from PyQt6.QtWidgets import QApplication as _QA
    orig = _QA.keyboardModifiers
    _QA.keyboardModifiers = staticmethod(lambda: mod)
    try:
        grid._on_click(pos)
    finally:
        _QA.keyboardModifiers = orig


def test_merge_view_matches_manage_view():
    """The multi-file merge view is "Seiten verwalten" for files. Its cards and
    its selection behaviour must be the same — they used to be a separate
    implementation with single-select only, no zoom and boxed cards."""
    from PyQt6.QtCore import Qt as _Qt
    from tools.viewer.page_grid import PageGrid, CARD_W
    from tools.viewer.merge import FileGrid, MergeOrderWidget
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    model = PageModel(4)
    pgrid = PageGrid(model, FX["normal"])
    fgrid = FileGrid(paths)

    NONE  = _Qt.KeyboardModifier.NoModifier
    CTRL  = _Qt.KeyboardModifier.ControlModifier
    SHIFT = _Qt.KeyboardModifier.ShiftModifier
    for mod, pos in [(NONE, 0), (CTRL, 2), (CTRL, 2), (SHIFT, 3), (NONE, 1), (SHIFT, 3), (CTRL, 0)]:
        _click(pgrid, pos, mod); _click(fgrid, pos, mod)
        pages = {i for i, u in enumerate(model.order) if u in model.selected}
        assert pages == fgrid._selected, \
            f"after {mod}+click {pos}: pages {sorted(pages)} vs files {sorted(fgrid._selected)}"
    pgrid.select_all(); fgrid.select_all()
    assert len(fgrid._selected) == 4, "select all"
    pgrid.deselect_all(); fgrid.deselect_all()
    assert not fgrid._selected, "deselect all"

    pc, fc = pgrid._cards[0], fgrid._cards[0]
    assert pc.size() == fc.size(), f"card size {pc.size()} vs {fc.size()}"
    assert pc.styleSheet() == fc.styleSheet(), "unselected card styling differs"
    pc.set_selected(True); fc.set_selected(True)
    assert pc.styleSheet() == fc.styleSheet(), "selected card styling differs"

    # zoom, like the page grid
    w0 = fgrid._card_w; fgrid.zoom_in()
    assert fgrid._card_w > w0 and fgrid._cards[0].width() == fgrid._card_w + 16
    fgrid.zoom_reset(); assert fgrid._card_w == CARD_W

    # the sidebar offers the same sections
    mw = MergeOrderWidget(paths)
    labels = {w.text() for w in mw.findChildren(QLabel) if w.objectName() == "sectionLabel"}
    for section in ("ANSICHT", "AUSWAHL", "REIHENFOLGE", "OPERATIONEN"):
        assert any(section in l for l in labels), f"{section} missing from the merge sidebar"
    assert hasattr(mw, "sel_edit") and hasattr(mw, "status")


def test_merge_view_reorders_and_removes():
    from tools.viewer.merge import MergeOrderWidget
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    mw = MergeOrderWidget(paths); g = mw._grid
    g._selected = {0, 1}; g._last_selected = 0
    g.move_down()
    assert g.get_paths()[:3] == [paths[2], paths[0], paths[1]], "move down"
    assert g._selected == {1, 2}, "selection must follow the move"
    g.move_up()
    assert g.get_paths()[0] == paths[0], "move up"
    # dragging one card of a multi-selection moves the whole selection.
    # Hold the widget, not just its grid: nothing else keeps a merge view alive
    # (the theme registry only weakrefs it), so dropping it here let the garbage
    # collector delete the labels the grid's order_changed signal writes to.
    mw2 = MergeOrderWidget(paths); g2 = mw2._grid
    g2._selected = {0, 1}; g2._last_selected = 0
    g2.handle_drop(0, 3, multi=True)
    assert g2.get_paths() == [paths[2], paths[0], paths[1], paths[3]], g2.get_paths()
    # selection field parses ranges like the page manager
    mw.sel_edit.setText("1, 3-4"); mw._apply_sel_edit()
    assert g._selected == {0, 2, 3} and mw.sel_edit.text() == "1, 3-4"
    g._selected = {3}; g.remove_selected()
    assert len(g.get_paths()) == 3 and paths[3] not in g.get_paths()


def test_merge_tab_end_to_end():
    """Open the merge tab, reorder, confirm — the result must be one PDF in the
    order shown, replacing the merge tab."""
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    paths = [FX["normal"], FX["single"], FX["framed"]]      # 5 + 1 + 1 pages
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    for _ in range(30): _app.processEvents(); time.sleep(0.01)
    w = vp.tabs.currentWidget()
    assert isinstance(w, MergeOrderWidget), "merge tab did not open"
    w._grid._selected = {2}; w._grid._last_selected = 2
    w._grid.move_up(); w._grid.move_up()
    assert w._grid.get_paths()[0] == FX["framed"], "reorder did not stick"
    w._confirm()
    for _ in range(500):
        _app.processEvents(); time.sleep(0.02)
        if vp.tabs.count() and not isinstance(vp.tabs.currentWidget(), MergeOrderWidget):
            break
    cur = vp.tabs.currentWidget()
    assert not isinstance(cur, MergeOrderWidget), "merge never completed"
    out = getattr(cur, "pdf_path", None)
    assert out and os.path.isfile(out), "no merged file"
    assert len(PdfReader(out).pages) == 7, f"{len(PdfReader(out).pages)} pages, expected 7"
    vp.deleteLater()


def _open_in_manage(path):
    """Open `path` in a viewer panel and switch it into the page manager.
    Returns (viewer, tab, manage_panel)."""
    from tools.viewer.panel import PageViewerPanel
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    vp.open_file(path)
    _spin(60, 0.01)
    tab = vp.tabs.currentWidget()
    vp._toggle_manage()
    _spin(40, 0.01)
    # Not findChild(): manage mode reparents the panel out of the tab and into
    # the viewer's splitter. The tab keeps its own reference.
    panel = tab._manage_panel
    assert panel is not None and tab.in_manage_mode(), "page manager did not open"
    return vp, tab, panel


def test_inserted_blank_page_renders_in_the_preview():
    """"Leere Seite einfuegen" rebuilds the PDF into a temp file. The single-page
    view has to follow it there — it used to keep the old, shorter path, so the
    blank page's index was past the end of the file it was rendering from and
    the preview showed the blue "render failed" fallback at a bogus size."""
    vp, tab, panel = _open_in_manage(FX["normal"])          # 5 pages
    before = len(tab.model.order)
    tab.model.selected = {tab.model.order[1]}
    panel._insert_blank()
    _spin(40, 0.01)
    assert len(tab.model.order) == before + 1, panel.status.text()
    assert tab.pdf_path == panel.pdf_path, \
        "the tab still points at the pre-insert file"

    vp._toggle_manage()                                      # back to preview
    _spin(40, 0.01)
    sv = tab.single
    sv._current = 2                                          # the blank
    sv._render()
    _settle(vp, lambda: sv._page_w_pt > 0, tries=200)
    assert sv._page_w_pt > 0 and sv._page_h_pt > 0, \
        f"preview has no page size ({sv._page_w_pt}x{sv._page_h_pt}) — render failed"
    # A4-ish, like the page it was inserted after — not a fallback of any size
    assert 500 < sv._page_w_pt < 700 and 700 < sv._page_h_pt < 950, \
        f"blank page is {sv._page_w_pt}x{sv._page_h_pt} pt"
    img = sv._last_pm.toImage()
    mid = img.pixel(img.width()//2, img.height()//2)
    assert (mid & 0xFFFFFF) == 0xFFFFFF, \
        f"blank page is not white (got {hex(mid)}) — this is the fallback fill"
    vp.deleteLater()


def test_save_as_honours_the_page_selection():
    """Ctrl+Shift+S with pages picked in the manager saves those pages, not the
    whole document. A selection left over from an earlier visit must not
    truncate a normal Save As, and a full Save As must re-base the model onto
    the file it just wrote."""
    from PyQt6.QtWidgets import QFileDialog
    out_dir = tempfile.mkdtemp(dir=_TMP)
    def _pick(path):
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))

    vp, tab, panel = _open_in_manage(FX["normal"])           # 5 pages
    assert tab.in_manage_mode()
    picked = [tab.model.order[1], tab.model.order[3]]
    tab.model.selected = set(picked)
    sel = os.path.join(out_dir, "sel.pdf")
    _pick(sel); vp._save_as_current(); _spin(20, 0.01)
    assert len(PdfReader(sel, strict=False).pages) == 2, \
        f"{len(PdfReader(sel).pages)} pages saved, expected the 2 picked"
    assert tab.pdf_path == FX["normal"], \
        "an export of part of the document must not retarget the tab"

    # Leaving the manager makes the stale selection irrelevant again.
    vp._toggle_manage(); _spin(20, 0.01)
    assert not tab.in_manage_mode()
    tab.model.order = list(reversed(tab.model.order))   # reorder, so a stale
    full = os.path.join(out_dir, "full.pdf")            # index mapping shows up
    _pick(full); vp._save_as_current(); _spin(20, 0.01)
    assert len(PdfReader(full, strict=False).pages) == 5, \
        "a stale selection truncated an ordinary Save As"
    assert tab.pdf_path == full, "Save As did not retarget the tab"

    # The model must resolve to exactly what is in the file it now points at.
    on_disk = _pdfium_page_text(full)
    resolved = []
    for uid in tab.model.order:
        p, o = tab.model.page_source(uid, tab.pdf_path)
        resolved.append(_pdfium_page_text(p)[o])
    assert resolved == on_disk, f"model {resolved} vs file {on_disk} after Save As"
    assert resolved == list(reversed(["PAGE 1", "PAGE 2", "PAGE 3", "PAGE 4", "PAGE 5"])), \
        resolved
    vp.deleteLater()


def test_drop_marker_is_a_slim_page_slot():
    """The drag-drop indicator is a slim rounded slot the height of a page card,
    not the barbed line that used to read as a crooked arrow. Measured by
    diffing a render with and without the marker, so only the marker's own
    pixels are inspected."""
    from tools.viewer.page_grid import PageGrid, CARD_H
    from tools.viewer.model import PageModel as _PM
    grid = PageGrid(_PM(6), FX["normal"])
    grid.resize(700, 500); grid.show()
    _spin(40, 0.02)

    def _shot():
        return grid.grab().toImage()
    plain = _shot()
    grid._drop_indicator = 3
    grid.update(); _spin(15, 0.01)
    marked = _shot()

    rows = {}
    for y in range(min(plain.height(), marked.height())):
        xs = [x for x in range(min(plain.width(), marked.width()))
              if plain.pixel(x, y) != marked.pixel(x, y)]
        if xs:
            rows[y] = (min(xs), max(xs))
    assert rows, "the drop marker painted nothing"

    ys = sorted(rows)
    height = ys[-1] - ys[0] + 1
    widths = [hi - lo + 1 for lo, hi in rows.values()]
    # Slim: a narrow slot, not a full card and not a 3px hairline
    assert 5 <= max(widths) <= 24, f"marker is {max(widths)}px across"
    # Page-shaped: as tall as the card it slots beside
    assert abs(height - CARD_H) <= 16, f"marker is {height}px tall, card is {CARD_H}"
    # Blob, not arrow: no flaring barbs — the width stays even down its length
    core = [w for y, w in zip(ys, (rows[y][1] - rows[y][0] + 1 for y in ys))
            if ys[0] + 3 <= y <= ys[-1] - 3]
    assert core and max(core) - min(core) <= 3, \
        f"width varies {min(core)}..{max(core)} along the marker — that is an arrow"
    # And it is the accent colour
    y_mid = ys[len(ys)//2]; lo, hi = rows[y_mid]
    px = marked.pixel((lo + hi)//2, y_mid)
    r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
    assert b > r and b > 100, f"marker colour {hex(px)} is not blue"


def test_several_files_go_straight_to_the_preview():
    """Picking several files must land in the sort/merge preview with no modal
    chooser in front of it, and the preview must offer both actions. The
    chooser was removed because clicking its merge button faster than the
    preview could be built queued the click and opened one preview per click."""
    from PyQt6.QtWidgets import QDialog
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    import tools.multi_open as MO
    assert not hasattr(MO, "MultiOpenDialog"), "the modal chooser is back"

    class FakeWindow:
        def __init__(self, vp): self.viewer = vp
        def _switch(self, idx): pass
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    paths = [FX["normal"], FX["single"], FX["framed"]]
    MAIN.MainWindow._open_multi(FakeWindow(vp), paths)
    _app.processEvents()

    assert not [w for w in _app.topLevelWidgets()
                if isinstance(w, QDialog) and w.isVisible()], "a popup appeared"
    w = vp.tabs.currentWidget()
    assert isinstance(w, MergeOrderWidget), "the preview did not open"
    assert w._grid.get_paths() == paths
    assert "Zusammenfuehren" in w._btn_go.text() and w._btn_go.isEnabled()
    assert "Einzeln" in w._btn_single.text() and w._btn_single.isEnabled()

    # a repeat request for the same files raises that tab, it does not stack
    for _ in range(4):
        MAIN.MainWindow._open_multi(FakeWindow(vp), paths)
    _app.processEvents()
    previews = [vp.tabs.widget(i) for i in range(vp.tabs.count())
                if isinstance(vp.tabs.widget(i), MergeOrderWidget)]
    assert len(previews) == 1, f"{len(previews)} preview tabs, expected 1"

    # a single file still bypasses the preview entirely
    MAIN.MainWindow._open_multi(FakeWindow(vp), [FX["color"]])
    _app.processEvents()
    assert getattr(vp.tabs.currentWidget(), "pdf_path", None) == FX["color"]
    vp.deleteLater()


def test_preview_opens_files_separately():
    """"Einzeln oeffnen" converts the same way the merge does, but gives every
    file its own tab."""
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    from tools.viewer.tab import PdfTab
    paths = [FX["normal"], FX["single"], FX["image"]]   # 5 + 1 + 1(converted)
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    _app.processEvents()
    w = vp.tabs.currentWidget()
    w._do_open_separately()
    assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                       for i in range(vp.tabs.count()))), \
        "open-separately never finished"
    opened = [vp.tabs.widget(i).pdf_path for i in range(vp.tabs.count())
              if isinstance(vp.tabs.widget(i), PdfTab)]
    assert len(opened) == 3, f"{len(opened)} tabs, expected 3"
    assert opened[0] == FX["normal"] and opened[1] == FX["single"]
    assert opened[2].endswith(".pdf") and os.path.isfile(opened[2]), \
        "the image was not converted"
    for p in opened:
        assert len(PdfReader(p, strict=False).pages) >= 1
    vp.deleteLater()


def _send_key(target, key, mods=None):
    """Deliver a key the way a real press arrives: ShortcutOverride first, so
    app-level filters and focused widgets get their say."""
    from PyQt6.QtCore import Qt as _Qt, QEvent as _QE
    from PyQt6.QtGui import QKeyEvent
    mods = mods if mods is not None else _Qt.KeyboardModifier.NoModifier
    for t in (_QE.Type.ShortcutOverride, _QE.Type.KeyPress, _QE.Type.KeyRelease):
        _app.sendEvent(target, QKeyEvent(t, key, mods))
    _spin(5, 0.0)


def test_open_dialogs_offer_every_supported_format():
    """The Datei menu filtered to *.pdf while the viewer happily converts images
    and Office documents on open, so the picker hid files the app can handle.
    Every open path now draws from one list."""
    from tools.multi_open import (file_dialog_filter, ALL_EXTS, IMAGE_EXTS,
                                  OFFICE_EXTS, classify)
    flt = file_dialog_filter()
    first = flt.split(";;")[0]
    for ext in ALL_EXTS:
        assert "*" + ext in first, f"{ext} missing from the dialog filter"
        assert classify("x" + ext), f"{ext} is offered but classify() rejects it"
    # formats verified against the actual converters on this machine
    for ext in (".gif", ".webp", ".png", ".jpg", ".tif", ".bmp"):
        assert ext in IMAGE_EXTS, f"{ext} should be a supported image"
    for ext in (".txt", ".csv", ".html", ".svg", ".docx", ".odt"):
        assert ext in OFFICE_EXTS, f"{ext} should be LibreOffice-convertible"
    assert "(*)" in flt, "no all-files escape hatch"

    # The Datei menu must hand that same filter to the picker. Captured from the
    # real call rather than read out of the source, so a second hard-coded
    # filter creeping back in is caught.
    from PyQt6.QtWidgets import QFileDialog
    seen = []
    orig_one, orig_many = QFileDialog.getOpenFileName, QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileName  = staticmethod(lambda *a, **k: (seen.append(a[3]), ("", ""))[1])
    QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: (seen.append(a[3]), ([], ""))[1])
    try:
        class _W:
            _switch = lambda self, i: None
            viewer  = None
        MAIN.MainWindow._open_dialog(_W())
        MAIN.MainWindow._open_multi_dialog(_W())
    finally:
        QFileDialog.getOpenFileName, QFileDialog.getOpenFileNames = orig_one, orig_many
    assert len(seen) == 2, "the menu actions did not open a picker"
    for used in seen:
        for ext in (".png", ".docx", ".txt", ".gif"):
            assert "*" + ext in used, \
                f"the Datei menu picker hides {ext} — filter was {used[:80]!r}"


def test_opening_a_bad_file_reports_instead_of_crashing():
    """Every one of these used to end in an unhandled exception inside a Qt slot
    — which aborts the process — or, for a corrupt PDF, in a blank tab with no
    model and no explanation."""
    from PyQt6.QtWidgets import QMessageBox, QFileDialog
    from PyQt6.QtCore import QSettings
    from tools.viewer.panel import PageViewerPanel
    said = []
    orig_w, orig_c = QMessageBox.warning, QMessageBox.critical
    QMessageBox.warning  = staticmethod(lambda *a, **k: said.append(a[1]))
    QMessageBox.critical = staticmethod(lambda *a, **k: said.append(a[1]))
    tmp = tempfile.mkdtemp(dir=_TMP)
    try:
        vp = PageViewerPanel(); vp.resize(800, 600); vp.show()

        cases = []
        missing = os.path.join(tmp, "gone.pdf")
        cases.append(("missing file", missing))
        odd = os.path.join(tmp, "notes.xyz"); open(odd, "w").write("hi")
        cases.append(("unknown extension", odd))
        broken = os.path.join(tmp, "broken.pdf")
        open(broken, "wb").write(b"%PDF-1.4 truncated, not a real pdf")
        cases.append(("corrupt pdf", broken))
        cases.append(("encrypted pdf", FX["encrypted"]))

        for label, path in cases:
            before = len(said)
            vp.open_file(path)
            _spin(10, 0.01)
            assert len(said) > before, f"{label}: opened with no message at all"
        assert vp.tabs.count() == 0, \
            f"{vp.tabs.count()} tab(s) opened for files that cannot be read"

        # a failed open must not become the file reopened at next startup
        s = QSettings("CopyShop", "PDFSuite")
        s.setValue("general/last_file", "SENTINEL")
        vp.open_file(broken); _spin(10, 0.01)
        assert s.value("general/last_file") == "SENTINEL", \
            "a file that failed to open was remembered for next startup"
        vp.deleteLater()
    finally:
        QMessageBox.warning, QMessageBox.critical = orig_w, orig_c


def test_merge_preview_hides_the_app_sidebar():
    """The preview brings its own sidebar. The app's tool nav must step aside
    while it is up — the two used to stack, and the left one offered tools that
    do not apply here. It has to come back when the preview closes."""
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    vp = PageViewerPanel(); vp.resize(1000, 700); vp.show()
    shown = []
    vp.hide_sidebar = lambda: shown.append(False)
    vp.show_sidebar = lambda: shown.append(True)

    vp.show_merge_tab([FX["normal"], FX["single"]])
    _spin(20, 0.01)
    assert isinstance(vp.tabs.currentWidget(), MergeOrderWidget)
    assert shown and shown[-1] is False, f"sidebar not hidden ({shown})"

    vp.tabs.currentWidget()._do_cancel()
    _spin(20, 0.01)
    assert shown[-1] is True, f"sidebar not restored after cancel ({shown})"

    # …and it stays away only for that tab
    vp.open_file(FX["normal"]); _spin(30, 0.01)
    vp.show_merge_tab([FX["single"], FX["framed"]]); _spin(20, 0.01)
    assert shown[-1] is False, "sidebar not hidden for a second preview"
    vp.tabs.setCurrentIndex(0); _spin(20, 0.01)
    assert shown[-1] is True, "sidebar not restored when switching to a PDF tab"
    vp.deleteLater()


def test_merge_preview_thumbnails_behave_like_the_page_manager():
    """The file grid is the page grid for files, so the same gestures apply:
    clicking empty space clears the selection, and the drop marker rises through
    the row without bouncing as the cursor passes the last thumbnail."""
    from PyQt6.QtCore import Qt as _Qt, QPoint, QPointF, QEvent as _QE
    from PyQt6.QtGui import QMouseEvent
    from tools.viewer.merge import MergeOrderWidget
    from tools.viewer.page_grid import MARGIN, GAP
    paths = [FX["normal"], FX["single"], FX["framed"], FX["mixed"]]
    w = MergeOrderWidget(paths); w.resize(950, 650); w.show()
    _spin(40, 0.02)
    g = w._grid

    # clicking empty background unpicks
    g._selected = {0, 2}; g._update_selection()
    empty = QPointF(g.width() - 25, g.height() - 25)
    for t in (_QE.Type.MouseButtonPress, _QE.Type.MouseButtonRelease):
        _app.sendEvent(g, QMouseEvent(t, empty, empty, _Qt.MouseButton.LeftButton,
                                      _Qt.MouseButton.LeftButton,
                                      _Qt.KeyboardModifier.NoModifier))
    _spin(5, 0.0)
    assert not g._selected, f"clicking empty space left {sorted(g._selected)} picked"

    # drop position never goes backwards as the cursor sweeps right
    n = len(g.get_paths())
    cell_w = g._card_w + 16 + GAP
    seq = [g._pos_from_point(QPoint(x, MARGIN + 10))
           for x in range(MARGIN, MARGIN + int((n + 2) * cell_w), 7)]
    assert seq == sorted(seq), f"drop position bounces: {seq}"
    assert seq[-1] == n, f"sweeping past the end gives {seq[-1]}, expected {n}"
    assert max(seq) <= n, f"drop position {max(seq)} exceeds {n} files"


def test_merge_preview_answers_the_page_manager_shortcuts():
    """Ctrl+C / X / V / Z did nothing here while working one view over, because
    this view registered three lone QShortcuts instead of the shared filter."""
    from PyQt6.QtCore import Qt as _Qt
    from tools.viewer.merge import MergeOrderWidget
    C = _Qt.KeyboardModifier.ControlModifier
    paths = [FX["normal"], FX["single"], FX["framed"]]
    w = MergeOrderWidget(paths); w.resize(950, 650); w.show()
    _spin(40, 0.02)
    g = w._grid
    g.setFocus()

    _send_key(g, _Qt.Key.Key_A, C)
    assert len(g._selected) == 3, "Ctrl+A"
    _send_key(g, _Qt.Key.Key_D, C)
    assert not g._selected, "Ctrl+D"

    g._selected = {1}; g._update_selection()
    _send_key(g, _Qt.Key.Key_C, C)
    _send_key(g, _Qt.Key.Key_V, C)
    assert len(g.get_paths()) == 4, f"Ctrl+C/Ctrl+V gave {len(g.get_paths())} files"
    assert g.get_paths().count(FX["single"]) == 2, "the copied file was not pasted"

    _send_key(g, _Qt.Key.Key_X, C)
    assert len(g.get_paths()) == 3, "Ctrl+X did not cut"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert len(g.get_paths()) == 4, "Ctrl+Z did not undo the cut"

    g._selected = {0}; g._update_selection()
    _send_key(g, _Qt.Key.Key_Delete)
    assert len(g.get_paths()) == 3, "Delete"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert len(g.get_paths()) == 4, "Ctrl+Z did not undo the delete"

    # a drag is undoable too
    order = g.get_paths()
    g._selected = {0}; g.handle_drop(0, 3)
    _spin(5, 0.0)
    assert g.get_paths() != order, "drag did not reorder"
    _send_key(g, _Qt.Key.Key_Z, C)
    assert g.get_paths() == order, "Ctrl+Z did not undo the drag"

    # typing in the selection box keeps its own Ctrl+A
    w.sel_edit.setFocus(); w.sel_edit.setText("1,2"); _spin(5, 0.0)
    before = set(g._selected)
    _send_key(w.sel_edit, _Qt.Key.Key_A, C)
    assert g._selected == before, "Ctrl+A in the text field hijacked the file selection"


def test_merge_preview_has_no_rotate_button():
    """Rotating a whole file means nothing, and the zoom-reset button was
    wearing the page manager's rotate-left glyph, so it read as one. Same fix
    the page manager already had: label it 1:1."""
    from tools.viewer.merge import MergeOrderWidget
    from tools.viewer.page_grid import PageGrid
    w = MergeOrderWidget([FX["normal"], FX["single"]])
    labels = [b.text() for b in w._zoom_btns]
    assert "↺" not in labels and "↻" not in labels, \
        f"the merge view still shows a rotate glyph: {labels}"
    assert "1:1" in labels, f"zoom reset is missing: {labels}"
    assert not hasattr(w._grid, "rotate_selected"), \
        "the file grid should not offer rotation at all"
    # the page manager keeps its real rotate buttons
    assert hasattr(PageGrid, "rotate_selected")


def test_preview_reports_files_it_could_not_convert():
    """A file that fails to convert is dropped from the merge. The user has to
    be told which one, or they get a document quietly missing pages — the
    removed chooser dialog was the only thing that ever showed those errors."""
    from PyQt6.QtWidgets import QMessageBox
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    broken = os.path.join(_TMP, "broken.png")
    with open(broken, "wb") as f:
        f.write(b"this is not an image")
    paths = [FX["normal"], broken, FX["single"]]

    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    vp.show_merge_tab(paths)
    _app.processEvents()
    warned = []
    orig = QMessageBox.warning
    QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))
    try:
        vp.tabs.currentWidget()._confirm()
        assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                           for i in range(vp.tabs.count()))), \
            "the merge never completed"
    finally:
        QMessageBox.warning = orig
    out = vp.tabs.currentWidget().pdf_path
    assert len(PdfReader(out, strict=False).pages) == 6, "the good files must still merge"
    assert warned, "the unconvertible file was dropped without a word"
    assert "broken.png" in " ".join(str(x) for x in warned[0]), warned[0]
    vp.deleteLater()


def test_preview_survives_fast_clicks():
    """Hammering the preview must not start a second job behind the first.

    Two conversions at once used to overwrite the panel's single worker
    attribute, dropping the last reference to a QThread that was still running
    — which Qt answers by aborting the process. Two previews converting side by
    side must both finish, each into its own output."""
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget
    from tools.viewer.tab import PdfTab
    vp = PageViewerPanel(); vp.resize(900, 600); vp.show()
    set_a = [FX["normal"], FX["image"]]      # the image forces real work
    set_b = [FX["single"], FX["framed"], FX["image"]]
    vp.show_merge_tab(set_a); vp.show_merge_tab(set_b)
    previews = [vp.tabs.widget(i) for i in range(vp.tabs.count())
                if isinstance(vp.tabs.widget(i), MergeOrderWidget)]
    assert len(previews) == 2, "different file sets each need their own tab"
    a, b = previews
    assert a.tmp_dir != b.tmp_dir, "previews must not share a conversion dir"

    a._confirm()
    a._confirm(); a._do_open_separately(); a._do_cancel()   # all ignored: busy
    from tools.jobs import active_jobs
    running = lambda: [j for j in active_jobs() if j.name == "convert-files"]
    assert len(running()) == 1, f"{len(running())} jobs after spamming one tab"
    assert not a._btn_go.isEnabled() and not a._btn_single.isEnabled()
    b._confirm()                                            # second job, in parallel
    assert len(running()) == 2, "the running job was dropped"

    assert _settle(vp, lambda: not any(isinstance(vp.tabs.widget(i), MergeOrderWidget)
                                       for i in range(vp.tabs.count()))), \
        "the merges never completed"
    outs = [vp.tabs.widget(i).pdf_path for i in range(vp.tabs.count())
            if isinstance(vp.tabs.widget(i), PdfTab)]
    assert len(outs) == 2, f"{len(outs)} merged tabs, expected 2"
    assert len(set(outs)) == 2, "both merges wrote to the same file"
    pages = sorted(len(PdfReader(o, strict=False).pages) for o in outs)
    assert pages == [3, 6], f"merged page counts {pages}, expected [3, 6]"
    _settle(vp, lambda: not running(), tries=100)
    assert not running(), "finished jobs were never released"
    vp.deleteLater()


def test_closing_a_merge_tab_deletes_its_conversion_directory():
    """Opening several files converts them into a temp directory per tab, and
    closing the tab is what removes it.

    It never did. shutil was imported inside one method of the viewer module and
    used at module level in two others, so the rmtree raised NameError straight
    into `except Exception: pass` — every multi-file open left its converted
    PDFs on disk until reboot, silently."""
    import tempfile
    from tools.viewer.panel import PageViewerPanel
    from tools.viewer.merge import MergeOrderWidget

    vp = PageViewerPanel(); vp.resize(900, 700); vp.show()
    try:
        w = MergeOrderWidget([FX["normal"], FX["single"]])
        w.tmp_dir = tempfile.mkdtemp(prefix="copyshop_test_")
        with open(os.path.join(w.tmp_dir, "converted.pdf"), "w") as fh:
            fh.write("x")
        idx = vp.tabs.addTab(w, "merge")
        assert os.path.isdir(w.tmp_dir)
        vp._close_tab(idx)
        _spin(20, 0.0)
        assert not os.path.isdir(w.tmp_dir), \
            f"the conversion directory survived the tab: {w.tmp_dir}"
    finally:
        vp.deleteLater(); _app.processEvents()
