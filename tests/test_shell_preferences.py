"""Input selection and language changes must preserve a readable, live session."""
import os
from unittest.mock import patch

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QAction, QPalette
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QLineEdit, QPlainTextEdit, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget,
)

from tests.support import _TMP, _app
from tools import i18n
from tools.shell.style import apply_theme_globally
from tools.shell.window import MainWindow
from tools.theme import shell_colours


def test_selected_input_text_follows_theme_including_numeric_editors():
    host = QWidget()
    layout = QVBoxLayout(host)
    widgets = [QLineEdit('123'), QSpinBox(), QDoubleSpinBox(),
               QTextEdit('123'), QPlainTextEdit('123'), QComboBox()]
    widgets[1].setValue(42)
    widgets[2].setValue(42.5)
    widgets[-1].setEditable(True)
    widgets[-1].addItems(['123', '456'])
    for widget in widgets:
        layout.addWidget(widget)
    old_style = _app.styleSheet()
    try:
        host.show()
        for theme in ('light', 'dark', 'light'):
            apply_theme_globally(theme)
            colours = shell_colours(theme)
            for widget in widgets:
                widget.setFocus()
                if hasattr(widget, 'selectAll'):
                    widget.selectAll()
                _app.processEvents()
                editors = [widget] + widget.findChildren(QLineEdit)
                for editor in editors:
                    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
                        palette = editor.palette()
                        assert palette.color(group, QPalette.ColorRole.HighlightedText).name() == colours['TEXT'], (theme, type(editor).__name__)
                        assert palette.color(group, QPalette.ColorRole.Highlight).name() == colours['SELECTION']
    finally:
        host.close()
        host.deleteLater()
        apply_theme_globally('dark')
        _app.setStyleSheet(old_style)


def test_language_menu_saves_for_next_launch_and_keeps_session_open():
    settings = QSettings(os.path.join(_TMP, 'language-preference.ini'), QSettings.Format.IniFormat)
    window = MainWindow()
    try:
        window.show()
        with patch.object(i18n, 'QSettings', return_value=settings), \
             patch.object(i18n, '_CURRENT_LANG', 'de'), \
             patch('tools.shell.window.QMessageBox.information') as notice, \
             patch('os.execv', side_effect=AssertionError('must not restart')):
            # Use the real menu actions to cover exclusive checks as well as saving.
            actions = window._title_bar.findChildren(QAction)
            english = next(a for a in actions if a.text() == 'English')
            german = next(a for a in actions if a.text() == 'Deutsch')
            english.trigger()
            assert settings.value('locale/language') == 'en'
            assert i18n.get_language() == 'de'
            assert i18n.tr('Sprache') == 'Sprache'
            assert window.isVisible()
            assert english.isChecked() and not german.isChecked()
            notice.assert_called_once_with(
                window, 'Sprache',
                'Die gewählte Sprache wird beim nächsten Öffnen der Anwendung verwendet.',
            )
            # Startup applies the saved preference; choosing the other option can undo it.
            i18n.load_language()
            assert i18n.get_language() == 'en'
            assert i18n.tr('Sprache') == 'Language'
            german.trigger()
            assert settings.value('locale/language') == 'de'
            assert i18n.get_language() == 'en'
            assert german.isChecked() and not english.isChecked()
            assert notice.call_args.args[1] == 'Language'
            assert 'next time' in notice.call_args.args[2]
            assert window.isVisible()
    finally:
        window.close()
        window.deleteLater()
        _app.processEvents()
