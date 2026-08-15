"""
Rulers and guides.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from tests.support import FX, _app, _open_single_view, _settle, _spin

MM_PER_PT = 25.4 / 72.0


def _mm(points):
    return [round(p * MM_PER_PT, 1) for p in points]


def _at_mm(sv, axis, millimetres):
    """Where a guide `millimetres` into the sheet lands on screen."""
    ox, oy, px_per_pt = sv._sheet_on_screen()
    origin = oy if axis == "h" else ox
    return origin + millimetres / MM_PER_PT * px_per_pt


def test_ctrl_r_shows_the_rulers():
    """Acrobat's shortcut, and Acrobat's default: off until asked for.

    The info-bar button is the same switch, and has to show the state even
    when the shortcut was what flipped it — otherwise a pressed-looking button
    turns the rulers *off*."""
    vp, sv = _open_single_view(FX["normal"], 900, 700)
    try:
        assert not sv._ruler_top.isVisible(), "the rulers started out on"
        assert not sv._ruler_btn.isChecked()
        press = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_R,
                          Qt.KeyboardModifier.ControlModifier, "r")
        sv.keyPressEvent(press)
        _spin(10)
        assert sv._ruler_top.isVisible() and sv._ruler_left.isVisible(), \
            "Ctrl+R did not show both rulers"
        assert sv._ruler_btn.isChecked(), "the button did not follow the shortcut"

        sv.keyPressEvent(press)
        _spin(10)
        assert not sv._ruler_top.isVisible(), "Ctrl+R did not hide them again"
        assert not sv._ruler_btn.isChecked()

        sv._ruler_btn.click()
        _spin(10)
        assert sv._ruler_top.isVisible(), "the button did not show the rulers"
        sv._ruler_btn.click()
        _spin(10)
        assert not sv._ruler_top.isVisible(), "the button did not hide them"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "off, on, off — by shortcut and by button, staying in step"


def test_a_guide_stays_where_it_was_put_on_the_page():
    """A guide 40 mm into the sheet is 40 mm into the sheet at any zoom.

    That is the whole reason they are kept in page coordinates rather than in
    pixels: a guide marking a margin is useless if zooming moves it off the
    margin."""
    vp, sv = _open_single_view(FX["normal"], 900, 700)
    try:
        sv.toggle_rulers(); _spin(10)
        sv._drop_guide("h", _at_mm(sv, "h", 40.0))
        sv._drop_guide("v", _at_mm(sv, "v", 25.0))
        page = sv._guides[sv._current_page_key()]
        assert _mm(page["h"]) == [40.0], _mm(page["h"])
        assert _mm(page["v"]) == [25.0], _mm(page["v"])

        # Where it is drawn must follow the page, not stay put on the glass.
        before_px = sv._view._guides_h[0]
        sv._zoom = 2.5
        sv._render()
        _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                tries=300)
        _spin(10)
        page = sv._guides[sv._current_page_key()]
        assert _mm(page["h"]) == [40.0], "zooming moved the guide on the page"
        after_px = sv._view._guides_h[0]
        assert abs(after_px - before_px) > 5, \
            "the guide stayed at the same place on screen while the page grew"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "40 mm at 1.0x and at 2.5x, drawn in two different places"


def test_guides_belong_to_the_page_they_were_drawn_on():
    """As in Acrobat, which offers "clear guides on page" separately from
    "clear all guides" precisely because they are per page."""
    vp, sv = _open_single_view(FX["normal"], 900, 700)
    try:
        sv.toggle_rulers(); _spin(10)
        sv._drop_guide("h", _at_mm(sv, "h", 30.0))
        first = sv._current_page_key()

        sv.next_page()
        _settle(vp, lambda: sv._render_task is None and sv._region_task is None,
                tries=300)
        _spin(10)
        assert sv._current_page_key() != first
        assert not sv._guides.get(sv._current_page_key(), {"h": []})["h"], \
            "a guide drawn on page 1 turned up on page 2"
        assert sv._view._guides_h == [], "page 2 is drawing page 1's guide"

        sv._drop_guide("h", _at_mm(sv, "h", 60.0))
        sv._clear_guides(everywhere=False)
        assert not sv._guides.get(sv._current_page_key(), {"h": []})["h"]
        assert _mm(sv._guides[first]["h"]) == [30.0], \
            "clearing this page's guides took another page's with it"

        sv._clear_guides(everywhere=True)
        assert not sv._guides, "clear all left something behind"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "per page, and cleared per page"


def test_a_guide_dropped_off_the_sheet_is_not_kept():
    """Letting go outside the page discards it — which is also how an unwanted
    guide is thrown away: drag it back over its ruler."""
    vp, sv = _open_single_view(FX["normal"], 900, 700)
    try:
        sv.toggle_rulers(); _spin(10)
        sv._drop_guide("h", _at_mm(sv, "h", -30.0))     # above the sheet
        sv._drop_guide("v", _at_mm(sv, "v", 9999.0))    # off to the right
        assert not sv._guides.get(sv._current_page_key(), {"h": [], "v": []})["h"]
        assert not sv._guides.get(sv._current_page_key(), {"h": [], "v": []})["v"]

        # One that is kept, then dragged off the sheet again.
        sv._drop_guide("h", _at_mm(sv, "h", 50.0))
        assert len(sv._guides[sv._current_page_key()]["h"]) == 1
        sv._guide_moved("h", 0, _at_mm(sv, "h", -20.0))
        assert not sv._guides[sv._current_page_key()]["h"], \
            "dragging a guide off the sheet did not remove it"
    finally:
        vp.deleteLater(); _app.processEvents()
    return "dropped outside: discarded; dragged outside: removed"
