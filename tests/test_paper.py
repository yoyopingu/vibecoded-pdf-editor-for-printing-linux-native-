"""
Paper sizes.

One list, edited by the operator, used by every dropdown that offers a size.
There used to be three that disagreed.
"""
import os

import tools.paper as paper
from tests.support import FX, _TMP, _app


def _clean():
    for name in list(paper.custom_sizes()):
        paper.remove_custom(name)
    for name in list(paper.hidden_names()):
        paper.set_hidden(name, False)


def test_a_size_added_once_is_offered_everywhere():
    """The point of the exercise. A shop that runs a sheet nobody thought of
    adds it in Einstellungen; before this there were three separate lists and
    adding one meant finding all three — which is why they had drifted apart,
    the tools reaching A0 while never having heard of SRA3."""
    from tools.panels._shared import _paper_sizes_pt
    from tools.printing.spool import paper_size_pt
    _clean()
    try:
        paper.add_custom("Hausformat", 330, 480)

        tools_labels = list(_paper_sizes_pt())
        assert any("Hausformat" in l for l in tools_labels), \
            "the tools' dropdown never saw it"
        size = paper_size_pt("Hausformat")
        assert size is not None, "the spooler cannot resolve it"
        assert abs(size[0] / paper.MM_TO_PT - 330) < 0.5
        assert abs(size[1] / paper.MM_TO_PT - 480) < 0.5
    finally:
        _clean()
    assert paper_size_pt("Hausformat") is None, "removing it left it behind"
    return "added once, offered in the tools and understood by the spooler"


def test_hiding_a_size_takes_it_off_the_lists_without_breaking_it():
    """Unticking is about what gets offered. A job saved with that size, or a
    queue reporting it, has to keep working — otherwise tidying the dropdown
    quietly breaks documents that already exist."""
    from tools.panels._shared import _paper_sizes_pt
    from tools.printing.spool import paper_size_pt
    _clean()
    try:
        assert any("Legal" in l for l in _paper_sizes_pt())
        paper.set_hidden("Legal", True)

        assert not any("Legal" in l for l in _paper_sizes_pt()), \
            "a hidden size is still being offered"
        assert "Legal" not in paper.sizes()
        assert paper_size_pt("Legal") is not None, \
            "hiding a size stopped it resolving — that breaks existing jobs"
        assert "Legal" in paper.all_sizes()
    finally:
        _clean()
    assert any("Legal" in l for l in _paper_sizes_pt()), "unhiding did nothing"
    return "off the dropdowns, still understood"


def test_the_shop_cannot_break_the_list_by_accident():
    """The two mistakes worth catching at the point of entry: a size with no
    name, and one that shadows a size that ships."""
    _clean()
    try:
        for bad_name in ("", "   "):
            try:
                paper.add_custom(bad_name, 100, 100)
                raise AssertionError(f"{bad_name!r} was accepted as a name")
            except ValueError:
                pass
        try:
            paper.add_custom("A4", 100, 100)
            raise AssertionError("a built-in name was allowed to be shadowed")
        except ValueError as e:
            assert "A4" in str(e)
        for w, h in ((0, 100), (100, 0), (9000, 100)):
            try:
                paper.add_custom("Zu gross", w, h)
                raise AssertionError(f"{w}x{h} mm was accepted")
            except ValueError:
                pass
        # Adding the same name twice replaces rather than duplicates.
        paper.add_custom("Wiederholt", 100, 200)
        paper.add_custom("Wiederholt", 150, 250)
        assert len([n for n in paper.custom_sizes() if n == "Wiederholt"]) == 1
        assert abs(paper.size_pt("Wiederholt")[0] / paper.MM_TO_PT - 150) < 0.5
    finally:
        _clean()
    return "no nameless, no shadowing, no impossible sheets, no duplicates"


def test_the_built_in_list_covers_what_a_copyshop_runs():
    """It reached A3 on the printing side and A0 in the tools, with no SRA or
    RA anywhere — the oversized stock a job is imposed on and trimmed back."""
    names = paper.builtin_names()
    for expected in ("A3", "A4", "SRA3", "SRA4", "RA3", "Letter", "Tabloid"):
        assert expected in names, f"{expected} is not offered"
    w, h = paper.size_pt("SRA3")
    assert abs(w / paper.MM_TO_PT - 320) < 1 and abs(h / paper.MM_TO_PT - 450) < 1
    return f"{len(names)} sizes, SRA and RA included"


def test_the_settings_page_edits_the_same_list_everything_reads():
    """The dialog is the only way in for an operator, so it is the path worth
    testing rather than the module underneath it."""
    from PyQt6.QtCore import Qt
    from tools.shell.settings import PrepressDialog
    _clean()
    dlg = PrepressDialog()
    try:
        before = dlg._paper_list.count()
        dlg._paper_name.setText("Sonderformat")
        dlg._paper_w.setValue(210); dlg._paper_h.setValue(600)
        dlg._add_paper()
        assert dlg._paper_list.count() == before + 1, "the row never appeared"
        assert "Sonderformat" in paper.custom_sizes()

        # A built-in refuses to be deleted, and says why rather than doing
        # nothing.
        for i in range(dlg._paper_list.count()):
            if dlg._paper_list.item(i).data(Qt.ItemDataRole.UserRole) == "A4":
                dlg._paper_list.setCurrentRow(i)
                break
        dlg._remove_paper()
        assert "A4" in paper.builtin_names() and paper.size_pt("A4")
        assert dlg._paper_msg.text(), "it refused silently"

        # The shop's own one does go.
        for i in range(dlg._paper_list.count()):
            if dlg._paper_list.item(i).data(Qt.ItemDataRole.UserRole) == "Sonderformat":
                dlg._paper_list.setCurrentRow(i)
                break
        dlg._remove_paper()
        assert "Sonderformat" not in paper.custom_sizes(), "it would not delete"
    finally:
        dlg.close(); dlg.deleteLater(); _app.processEvents()
        _clean()
    return "add, refuse to delete a built-in, delete your own"
