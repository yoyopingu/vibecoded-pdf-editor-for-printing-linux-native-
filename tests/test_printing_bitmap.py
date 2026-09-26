"""Printing: bitmap behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _lp_options, _stub_queue


def test_print_as_bitmap_offers_a_resolution_and_only_then():
    """The rasterised route needs a resolution, and the number means nothing
    while the box is unticked — so it follows the tick, the way the duplex
    binding-edge selector follows its own checkbox."""
    tab, dlg = _print_dialog()
    try:
        assert not dlg.bitmap_check.isChecked(), "bitmap must not be the default"
        assert not dlg.bitmap_dpi.isEnabled(), \
            "the resolution is selectable while it does nothing"
        assert [dlg.bitmap_dpi.itemData(i) for i in range(dlg.bitmap_dpi.count())] \
            == [150, 300, 600, 1200]
        assert dlg.bitmap_dpi.currentData() == 300, "300 dpi is the sane default"

        dlg.bitmap_check.setChecked(True); _app.processEvents()
        assert dlg.bitmap_dpi.isEnabled()
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "150/300/600/1200 dpi, default 300, enabled only when ticked"


def test_the_chosen_dpi_is_what_gets_rasterised():
    """Unticked, the printer's own resolution is the right one to render at.
    Ticked, the operator has named one and it is theirs that must be used —
    asking for 150 dpi and getting the printer's 600 would be the setting
    quietly doing nothing."""
    tab, dlg = _print_dialog()
    try:
        assert dlg._raster_dpi(600) == 600, \
            "unticked, the printer's resolution should be left alone"
        dlg.bitmap_check.setChecked(True)
        for i, want in ((0, 150), (2, 600), (3, 1200)):
            dlg.bitmap_dpi.setCurrentIndex(i)
            assert dlg._raster_dpi(600) == want, \
                f"asked for {want} dpi, would rasterise at {dlg._raster_dpi(600)}"
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "the chosen dpi wins over the printer's, and only when chosen"


def test_printing_as_a_bitmap_does_not_go_through_ghostscript():
    """The whole point of the option: Ghostscript re-interprets the PDF and
    resolves its fonts a second time, which is the substitution the operator
    is trying to get away from. Rasterising the Ghostscript output instead of
    the original would print that substitution at higher resolution — the bug,
    faithfully reproduced."""
    import inspect
    from tools.printing.dialog import PrintDialog
    src = inspect.getsource(PrintDialog._do_print)
    assert "not as_bitmap" in src, \
        "the Ghostscript branch is no longer guarded by the bitmap choice"
    tab, dlg = _print_dialog()
    try:
        assert dlg.prints_as_bitmap() is False
        dlg.bitmap_check.setChecked(True)
        assert dlg.prints_as_bitmap() is True
    finally:
        dlg.close(); dlg.deleteLater(); tab.deleteLater(); _app.processEvents()
    return "the Ghostscript path is skipped when a bitmap was asked for"

def test_paper_names_survive_the_round_trip_through_qt():
    """The name->id table and the id->name one are the same table read both
    ways; written out twice they drifted, and one of the two copies was the
    PyQt5 spelling that could not even be evaluated."""
    from tools.printing.dialog import _qt_page_sizes
    from PyQt6.QtGui import QPageSize
    table = _qt_page_sizes()
    assert "A4" in table and "Letter" in table
    back = {v: k for k, v in table.items()}
    for name, ident in table.items():
        assert back[ident] == name, f"{name} does not survive the round trip"
        # And each id must really build a page size Qt accepts.
        assert QPageSize(ident).isValid(), f"{name} is not a page size Qt knows"
    return f"{len(table)} paper names, all valid and reversible"


def test_no_qt_print_attribute_is_spelled_the_pyqt5_way():
    """`QPrinter.PageSize.A4` is PyQt5; PyQt6 moved those ids onto QPageSize.
    It sat in the Qt print path raising AttributeError for every job that
    reached it, and nothing reached it, because that path only ran when
    Ghostscript had already failed.

    Rarely-run code is exactly where this survives, so check it by reading
    rather than by running: every attribute the source takes off one of these
    classes has to exist on it. Parsed, not grepped, so prose about the bug
    does not count as a use of it.
    """
    import ast, pathlib
    from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
    from PyQt6.QtGui import QPageLayout, QPageSize

    classes = {"QPrinter": QPrinter, "QPrinterInfo": QPrinterInfo,
               "QPageLayout": QPageLayout, "QPageSize": QPageSize}
    missing = []
    for path in sorted(pathlib.Path("tools").rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id in classes:
                if not hasattr(classes[base.id], node.attr):
                    missing.append(f"{path}:{node.lineno} {base.id}.{node.attr}")
    assert not missing, ("attributes PyQt6 does not have:\n  "
                         + "\n  ".join(missing))
    return "every QPrinter/QPageSize/QPageLayout attribute used exists"
