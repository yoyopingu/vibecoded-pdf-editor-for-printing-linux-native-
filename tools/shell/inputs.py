"""
Two things every application does that this one did not, both as one filter on
the QApplication rather than as sixty widget subclasses.

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

And the wheel does not change a setting the pointer merely happens to be over.
Scrolling down a dialog past a dropdown quietly changed the paper size, the
duplex edge or the colour mode — the print dialog is a column of them, and the
one place you cannot afford a setting to change without being asked is the one
that sends a job to a printer. A control takes the wheel only once it has
keyboard focus, which means it was clicked or tabbed into on purpose; otherwise
the wheel goes where it was going, to whatever scrolls behind it.
"""

from PyQt6.QtCore import QObject, QEvent, QTimer
from PyQt6.QtWidgets import (QAbstractSlider, QAbstractSpinBox, QComboBox,
                             QLineEdit)


class _InputBehaviour(QObject):
    """Number fields select on focus; the wheel does not touch what it hovers."""

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.FocusIn and _is_number_field(obj):
            # Queued: a mouse press delivers FocusIn first and *then* sets the
            # caret from the click, which would drop a selection made here.
            QTimer.singleShot(0, lambda: _select_all(obj))
        elif kind == QEvent.Type.Wheel and _ignores_hover_wheel(obj):
            # Eaten, not passed on: accepting it here and returning True stops
            # the widget acting on it, and Qt then offers the wheel to the
            # scroll area behind — which is where the user was aiming.
            event.ignore()
            return True
        return super().eventFilter(obj, event)


def _ignores_hover_wheel(obj):
    """Is this a control the wheel should leave alone right now?

    Dropdowns, spin boxes and sliders, unless they hold keyboard focus. Focus
    means the control was clicked or tabbed into, so the wheel was aimed at it
    rather than passing over it on the way down a dialog.

    An open dropdown is exempt: its popup is a list, and scrolling a list is
    what the wheel is for.
    """
    if not isinstance(obj, (QComboBox, QAbstractSpinBox, QAbstractSlider)):
        return False
    try:
        if isinstance(obj, QComboBox) and obj.view() is not None \
           and obj.view().isVisible():
            return False
        return not obj.hasFocus()
    except RuntimeError:
        return False        # widget on its way out


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
        _filter = _InputBehaviour()
        app.installEventFilter(_filter)
    return _filter
