"""Enter runs the visible tool even when its shared page preview has focus."""
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QCheckBox, QDialog, QLineEdit, QPushButton, QSpinBox, QVBoxLayout
from tests.support import FX, _app, _wait_for
from tools.shell.window import MainWindow


def test_enter_runs_only_the_active_tool_from_options_or_preview():
    win = MainWindow()
    calls = []
    panels = [(i, panel) for i, panel in enumerate(win._by_nav)
              if hasattr(panel, 'run_btn')]
    for i, panel in panels:
        panel._run_action = lambda i=i: calls.append(i) or 'done'
    try:
        win.show()
        win.viewer.open_file(FX['normal'])
        assert _wait_for(lambda: win.viewer.tabs.count() == 1)
        view = win.viewer.tabs.currentWidget().single
        for i, panel in panels:
            win._switch(i)
            _app.processEvents()
            target = (panel._tool_splitter if getattr(panel, '_tool_splitter', None)
                      is not None else view)
            controls = [panel.findChild(cls) for cls in (QLineEdit, QSpinBox, QCheckBox)]
            for focus in (target, panel.run_btn, panel.log, *controls):
                if focus is None or not focus.isVisible() or not focus.isEnabled():
                    continue
                focus.setFocus()
                for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    before = len(calls)
                    page = view._current
                    QTest.keyClick(focus, key)
                    assert calls[before:] == [i], (type(panel).__name__, focus, calls[before:])
                    assert view._current == page, 'Enter turned the source page'
            panel.run_btn.setEnabled(False)
            QTest.keyClick(target, Qt.Key.Key_Return)
            assert len(calls) == before + 1, 'Enter reran a busy tool'
            panel.run_btn.setEnabled(True)
        win._switch(0)
        view.setFocus()
        before = len(calls)
        page = view._current
        QTest.keyClick(view, Qt.Key.Key_Return)
        assert len(calls) == before, 'a hidden tool ran in the ordinary viewer'
        assert view._current == page + 1, 'ordinary viewer Enter navigation broke'
    finally:
        win.close()
        win.deleteLater()
        _app.processEvents()


def test_tool_enter_does_not_take_enter_from_a_modal_dialog():
    win = MainWindow()
    calls = []
    panel = next(panel for panel in win._by_nav if hasattr(panel, 'run_btn'))
    panel._run_action = lambda: calls.append('tool') or 'done'
    dlg = QDialog(win)
    button = QPushButton('Accept', dlg)
    button.setDefault(True)
    button.clicked.connect(dlg.accept)
    QVBoxLayout(dlg).addWidget(button)
    try:
        win.show()
        win._switch(win._by_nav.index(panel))
        dlg.setModal(True)
        dlg.show()
        button.setFocus()
        _app.processEvents()
        QTest.keyClick(button, Qt.Key.Key_Return)
        assert dlg.result() == QDialog.DialogCode.Accepted
        assert not calls
    finally:
        dlg.close()
        dlg.deleteLater()
        win.close()
        win.deleteLater()
        _app.processEvents()
