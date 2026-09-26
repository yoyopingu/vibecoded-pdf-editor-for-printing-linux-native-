"""Printing: prefs behavior."""
import os, shutil
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pypdfium2 as pdfium
from tools.jobs import null_progress
from tools.panels._colour import _colour_histogram, _hist_stats
from tests.support import FX, _TMP, _app, _open, _page_labels, _spin
from tests.printing_support import _print_dialog, _stub_queue, _fake_qt_printer


def test_a_remembered_tray_survives_being_stored_and_read_back():
    """The tray combo carried a (keyword, choice) tuple as its item data, and
    QComboBox.findData compares Python objects by identity — so a tuple rebuilt
    from parsed text never matched the one already in the combo, and the
    remembered tray silently did not come back. Literals hide it: they are
    interned, so a toy example works."""
    from PyQt6.QtWidgets import QComboBox

    parsed = "InputSlot Tray2".split()          # not interned, as from lpoptions
    tuples = QComboBox()
    tuples.addItem("Tray2", (parsed[0], parsed[1]))
    assert tuples.findData(("InputSlot", "Tray2")) == -1, \
        "tuple item data now matches by value — the workaround can be dropped"

    strings = QComboBox()
    strings.addItem("Tray2", parsed[1])
    assert strings.findData("Tray2") == 0, "string item data must match by value"

def test_a_printer_appearing_later_shows_up_without_a_restart():
    """The enumerated list was cached for the session and never asked again, so
    a printer connected while the app was running stayed invisible until a
    restart. Every open re-enumerates now; an unchanged answer must not disturb
    the combo, and a changed one must keep the current selection if it survives."""
    from tools.printing import dialog as D

    class _Combo:
        def __init__(self): self.items = []; self.idx = -1; self.enabled = True
        def clear(self): self.items = []; self.idx = -1
        def addItem(self, text, data=None): self.items.append((text, data))
        def count(self): return len(self.items)
        def setEnabled(self, v): self.enabled = v
        def blockSignals(self, v): pass
        def findData(self, d):
            return next((i for i, (_, x) in enumerate(self.items) if x == d), -1)
        def setCurrentIndex(self, i): self.idx = i
        def currentData(self):
            return self.items[self.idx][1] if 0 <= self.idx < len(self.items) else None
        class _Sig:
            def connect(self, *a): pass
            def disconnect(self, *a): pass
        currentIndexChanged = _Sig()

    class _Dlg:
        _apply_printer_list = D.PrintDialog._apply_printer_list
        _on_printers_enumerated = D.PrintDialog._on_printers_enumerated
        _settings_touched = False
        def __init__(self): self.printer_combo = _Combo(); self.changed = 0
        def _on_printer_changed(self): self.changed += 1

    old_cache = D._PRINTER_LIST_CACHE
    old_last = D.prefs.last_printer
    try:
        D.prefs.last_printer = lambda: ""
        D._PRINTER_LIST_CACHE = None

        dlg = _Dlg()
        dlg._apply_printer_list(["Officejet"], "Officejet")
        assert dlg.printer_combo.currentData() == "Officejet"
        assert dlg.changed == 1, "first population applies the printer's defaults"

        # Same answer arriving again: nothing is rebuilt, nothing re-applied.
        dlg._on_printers_enumerated(["Officejet"], "Officejet")
        assert dlg.changed == 1, "an unchanged list must not re-apply defaults"

        # A printer appears. It must show up, and the selection must survive.
        dlg._on_printers_enumerated(["Officejet", "Laser"], "Officejet")
        assert [d for _, d in dlg.printer_combo.items] == [
            D.PDF_PRINTER, "Officejet", "Laser"]
        assert dlg.printer_combo.currentData() == "Officejet", "selection was lost"
        assert dlg.changed == 1, "same printer still selected — defaults not re-applied"

        # The selected printer goes away: fall back rather than point at nothing.
        dlg._on_printers_enumerated(["Laser"], "Laser")
        assert dlg.printer_combo.currentData() == "Laser"
        assert dlg.changed == 2, "the printer really changed, so defaults apply"
    finally:
        D.prefs.last_printer = old_last
        D._PRINTER_LIST_CACHE = old_cache

def test_a_refresh_never_undoes_a_deliberate_choice():
    """A background re-enumeration can land after the operator has set the paper.
    If the selected printer has gone, the combo has to change — but the settings
    they chose must survive it, the same rule _apply_queue_info already follows."""
    from tools.printing import dialog as D

    class _Combo:
        def __init__(self): self.items = []; self.idx = -1
        def clear(self): self.items = []; self.idx = -1
        def addItem(self, text, data=None): self.items.append((text, data))
        def count(self): return len(self.items)
        def setEnabled(self, v): pass
        def blockSignals(self, v): pass
        def findData(self, d):
            return next((i for i, (_, x) in enumerate(self.items) if x == d), -1)
        def setCurrentIndex(self, i): self.idx = i
        def currentData(self):
            return self.items[self.idx][1] if 0 <= self.idx < len(self.items) else None
        class _Sig:
            def connect(self, *a): pass
            def disconnect(self, *a): pass
        currentIndexChanged = _Sig()

    class _Dlg:
        _apply_printer_list = D.PrintDialog._apply_printer_list
        _on_printers_enumerated = D.PrintDialog._on_printers_enumerated
        def __init__(self):
            self.printer_combo = _Combo(); self.changed = 0
            self._settings_touched = False
        def _on_printer_changed(self): self.changed += 1

    old_cache, old_last = D._PRINTER_LIST_CACHE, D.prefs.last_printer
    try:
        D.prefs.last_printer = lambda: ""
        D._PRINTER_LIST_CACHE = None
        dlg = _Dlg()
        dlg._apply_printer_list(["office"], "office")
        assert dlg.changed == 1

        dlg._settings_touched = True          # the operator picks A3, say
        dlg._on_printers_enumerated(["other"], "other")
        assert [d for _, d in dlg.printer_combo.items] == [D.PDF_PRINTER, "other"], \
            "the new list still has to be shown"
        assert dlg.changed == 1, "defaults were re-applied over a deliberate choice"
    finally:
        D.prefs.last_printer = old_last
        D._PRINTER_LIST_CACHE = old_cache

