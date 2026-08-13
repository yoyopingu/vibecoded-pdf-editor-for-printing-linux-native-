"""
Click a number field and its value is selected, ready to be typed over.

Every other application behaves this way and this one did not: clicking a spin
box put a caret between two digits, so changing 20 to 5 meant selecting the text
by hand, or arrowing, or three backspaces. There are around sixty of these
fields across the tools, the print dialog and the settings, so this is one
event filter on the application rather than sixty subclasses.

The second click is the point of the flag. Selecting on every click would make
the field impossible to edit — click to place a caret and the selection comes
straight back. So the value is selected when the widget *takes* focus, and once
it has focus, clicks behave normally and put the caret where you clicked.

Keyboard focus (Tab) selects too, which is what Tab through a form should do.
"""

from PyQt6.QtCore import QObject, QEvent, QTimer
from PyQt6.QtWidgets import QAbstractSpinBox, QLineEdit


class _SelectOnFocus(QObject):
    """Selects a number field's contents when it gains focus."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn and _is_number_field(obj):
            # Queued: a mouse press delivers FocusIn first and *then* sets the
            # caret from the click, which would drop a selection made here.
            QTimer.singleShot(0, lambda: _select_all(obj))
        return super().eventFilter(obj, event)


def _is_number_field(obj):
    """A spin box, or the line edit inside one.

    Deliberately not every QLineEdit: selecting a whole file path or a page
    range on click would be a nuisance, since those are usually edited rather
    than replaced.
    """
    if isinstance(obj, QAbstractSpinBox):
        return True
    return isinstance(obj, QLineEdit) and isinstance(obj.parent(), QAbstractSpinBox)


def _select_all(obj):
    try:
        if not obj.hasFocus():
            return          # focus moved on again before this ran
        obj.selectAll()
    except RuntimeError:
        pass                # widget deleted between the click and this call


_filter = None


def install(app):
    """Apply it to every number field in the application, present and future."""
    global _filter
    if _filter is None:
        _filter = _SelectOnFocus()
        app.installEventFilter(_filter)
    return _filter
