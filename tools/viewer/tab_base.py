"""
What a tab is, to the widgets living inside one.

Three modules used to import PdfTab itself — single_page, manage and the print
dialog — and every one of them wanted the same thing: the tab this widget sits
in, found by walking up the parent chain and testing each step with isinstance.
None of them called a method on the class or constructed one. That single name
was enough to make tab.py cyclic with all three, which is why tab.py imported
them back from inside functions.

The isinstance test needs a class, not the class. PdfTabBase is that class:
empty, imported by tab.py and by anything that needs to recognise a tab, and
importing nothing itself. The walk lives here too, since all three were writing
the same loop.
"""
from PyQt6.QtWidgets import QWidget


class PdfTabBase(QWidget):
    """The part of a tab its children are allowed to know about — which is only
    that it exists. PdfTab adds everything else."""


def owning_tab(widget):
    """The tab `widget` belongs to, or None if it is not in one.

    `widget` itself counts, so the callers that already hold a candidate can
    pass it straight in. A widget that has been reparented out of its tab — the
    manage panel is, into the viewer's splitter — walks to None rather than
    guessing, and the caller decides what that means."""
    while widget is not None:
        if isinstance(widget, PdfTabBase):
            return widget
        widget = widget.parent()
    return None
