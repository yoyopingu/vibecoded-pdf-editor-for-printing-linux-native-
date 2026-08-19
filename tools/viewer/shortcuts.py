"""
The clipboard/undo keys a thumbnail grid answers to.

Both "Seiten verwalten" (ManagePanel) and the merge preview (MergeOrderWidget)
show a grid of cards and take the same keyboard: Ctrl+A/C/V/X/Z/Y/D, Delete and
Backspace. Each used to carry its own QApplication-wide event filter for this —
same key set, same ShortcutOverride-then-KeyPress dance, same modal-dialog and
text-field guards, differing only in which methods the keys called. One class
now, parameterized by the six actions and the grid it selects on.
"""
from PyQt6.QtCore import QObject, Qt, QEvent
from PyQt6.QtWidgets import QApplication, QLineEdit

_CLIPBOARD_KEYS = (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_V,
                   Qt.Key.Key_X, Qt.Key.Key_Z, Qt.Key.Key_Y, Qt.Key.Key_D)


class ThumbGridShortcutFilter(QObject):
    """App-level Ctrl+A/C/V/X/Z/Y/D and Delete/Backspace for one grid view.

    `is_live` is called with no arguments and decides whether this filter's
    owner is the thing that should answer right now — visible, and for the
    merge view, not mid-operation. `grid` needs select_all()/deselect_all();
    `delete`/`copy`/`cut`/`paste`/`undo`/`redo` are the plain callables an
    accepted key runs.
    """

    def __init__(self, is_live, grid, delete, copy, cut, paste, undo, redo,
                 parent=None):
        super().__init__(parent)
        self._is_live = is_live
        self._grid    = grid
        self._delete, self._copy, self._cut = delete, copy, cut
        self._paste, self._undo, self._redo = paste, undo, redo

    def eventFilter(self, obj, event):
        # Never intercept while a modal dialog is open — its widgets own the keys.
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        # Claim ShortcutOverride so widgets don't eat our Ctrl combos.
        # MUST use accept()+return False (not return True) so Qt still dispatches
        # the subsequent KeyPress — return True eats the event entirely.
        if t == QEvent.Type.ShortcutOverride:
            if not self._is_live():
                return False
            if isinstance(QApplication.focusWidget(), QLineEdit):
                return False
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl and event.key() in _CLIPBOARD_KEYS:
                event.accept()
            # Delete/Backspace need no override — Qt only routes a key through
            # ShortcutOverride when something has registered it as a shortcut,
            # and neither is one anywhere in this app. Ctrl+P is deliberately
            # not in _CLIPBOARD_KEYS either: it is a global QShortcut at
            # PdfViewerWidget level, and claiming it here would stop that
            # shortcut from firing while a grid is visible.
            return False

        if t != QEvent.Type.KeyPress or not self._is_live():
            return False
        if isinstance(QApplication.focusWidget(), QLineEdit):
            return False

        k     = event.key()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not ctrl:
            self._delete(); return True
        if ctrl:
            if k == Qt.Key.Key_A: self._grid.select_all();   return True
            if k == Qt.Key.Key_D: self._grid.deselect_all(); return True
            if k == Qt.Key.Key_C: self._copy();  return True
            if k == Qt.Key.Key_X: self._cut();   return True
            if k == Qt.Key.Key_V: self._paste(); return True
            if k == Qt.Key.Key_Z and not shift: self._undo(); return True
            if (k == Qt.Key.Key_Z and shift) or k == Qt.Key.Key_Y:
                self._redo(); return True
        return False
