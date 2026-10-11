"""Booklet ranges follow the selected signature and remain usable."""
from tests.support import _app, _wait_for
from tests.printing_support import _print_dialog


def test_booklet_range_follows_selection_and_keeps_endpoints_ordered():
    tab, dlg = _print_dialog(8, 'booklet_range.pdf')
    try:
        dlg._handling_bar.setCurrentIndex(3)
        assert _wait_for(lambda: len(dlg._preview._sizes) == 8)
        assert dlg._preview._nav_count() == 4
        dlg.radio_range.setChecked(True)
        dlg.range_edit.setText('1-4')
        assert dlg.booklet_from.maximum() == dlg.booklet_to.maximum() == 1
        assert dlg.booklet_to.value() == 1
        assert dlg._preview._nav_count() == 2
        dlg.radio_all.setChecked(True)
        assert dlg.booklet_to.value() == dlg.booklet_to.maximum() == 2
        assert dlg._preview._nav_count() == 4
        dlg.booklet_from.setValue(2)
        assert dlg._preview._nav_count() == 2
        dlg.booklet_to.setValue(1)
        assert dlg.booklet_from.value() == 1
        assert dlg._preview._nav_count() == 2
        dlg.booklet_from.setValue(2)
        assert dlg.booklet_to.value() == 2
        dlg.booklet_side.setCurrentIndex(dlg.booklet_side.findData('back'))
        assert dlg._preview._nav_count() == 1
    finally:
        dlg.close()
        tab.deleteLater()
        _app.processEvents()
