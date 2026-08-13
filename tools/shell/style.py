"""
How the application looks: the palette, the two stylesheets built from it,
the theme switch, and the window icon.
"""
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush,
                         QIcon, QPolygon)
from tools.theme import SHELL_KEYS, shell_colours


def _build_style(dark: bool) -> str:
    """The stylesheet for one theme, from the palette in tools/theme.py.

    The colours were spelled out here as literals, a second copy of the same
    values that _THEME_COLOURS held and a third dark blue away from the module
    constants above them. The short local names are kept — they appear a few
    hundred times in the sheet below, and `{_BG}` reads better than a lookup.
    """
    c = shell_colours("dark" if dark else "light")
    _BG    = c["BG"];           _PANEL   = c["PANEL"];      _TEXT  = c["TEXT"]
    _DIM   = c["DIM"];          _HOVER   = c["HOVER"];      _LINE  = c["LINE"]
    _IB    = c["INPUT_BG"];     _IBD     = c["INPUT_BORDER"]
    _LB    = c["LIST_BORDER"]
    _SEL   = c["SELECTION"];    _LHOV    = c["LIST_HOVER"]
    _TAB_B = c["TAB_BAR"];      _TAB_P   = c["TAB_PANE"]
    _SCB   = c["SCROLL_BAR"];   _SCT     = c["SCROLL_TRACK"]
    _SB    = c["SIDE"];         _SB_LOGO = c["SIDEBAR_LOGO"]
    _NB_TEXT = c["NAV_TEXT"];   _NB_HOV  = c["NAV_HOVER"]
    _NB_ACT_TEXT = c["NAV_ACTIVE_TEXT"]
    _VB_BG = c["VIEW_BTN"];     _VB_HOV  = c["VIEW_BTN_HOVER"]
    _BTN_DIS = c["BTN_DISABLED"]; _BTN_DIS_T = c["BTN_DISABLED_TEXT"]
    _GB    = c["GROUP_BOX"]
    _ON_ACC = c["ON_ACCENT"];   _CLOSE = c["CLOSE_BTN"]

    _ACC, _ACC_HOV, _ACC_PRS = c["ACC"], c["ACC_HOVER"], c["ACC_PRESSED"]
    # Spelled out as rgba(): Qt reads a 9-character "#rrggbbaa" as "#aarrggbb"
    # and would silently take the alpha byte for the red channel.
    _r, _g, _b = (int(_ACC[1:][i:i+2], 16) for i in (0, 2, 4))
    _ACC_DIM = f"rgba({_r},{_g},{_b},0.35)"     # nav hover marker

    return f"""
* {{
    font-family: 'Noto Sans', 'DejaVu Sans', sans-serif;
    font-size: 13px;
}}
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT};
}}
QLabel {{ color: {_TEXT}; background: transparent; }}
QLabel#dimLabel {{ color: {_DIM}; font-size: 12px; }}
QLabel#formLabel {{
    color: {_DIM};
    font-size: 12px;
    min-width: 180px;
    max-width: 220px;
    qproperty-wordWrap: true;
}}
QLabel#sectionLabel {{
    color: {_ACC}; font-size: 9px; font-weight: bold; letter-spacing: 3px;
    text-transform: uppercase;
}}
QLabel#currentFileLabel {{ color: {_TEXT}; font-size: 12px; }}
QWidget#currentFileBar {{
    background: {_PANEL}; border-bottom: 1px solid {_LINE};
    min-height: 44px;
}}

/* ── Title bar ──────────────────────────────────────────── */
QWidget#titleBar {{
    background: {_SB_LOGO};
    border-bottom: 2px solid {_LINE};
}}
QLabel#titleBarLabel {{
    color: {_TEXT}; font-size: 13px; font-weight: bold;
    letter-spacing: 0.5px; background: transparent;
}}
QMenuBar#titleMenuBar {{
    background: transparent; color: {_TEXT};
    font-size: 13px; border: none;
}}
QMenuBar#titleMenuBar::item {{
    background: transparent; padding: 5px 12px; border-radius: 5px;
}}
QMenuBar#titleMenuBar::item:selected {{
    background: {_HOVER};
}}
QMenu {{
    background: {_PANEL}; color: {_TEXT};
    border: 1px solid {_LINE};
    border-radius: 6px;
    padding: 4px 0;
}}
QMenu::item {{ padding: 6px 28px 6px 18px; border-radius: 3px; }}
QMenu::item:selected {{ background: {_HOVER}; }}
QMenu::separator {{ height: 1px; background: {_LINE}; margin: 4px 10px; }}
QPushButton#titleBarBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 0;
    font-size: 14px; min-width: 42px;
}}
QPushButton#titleBarBtn:hover {{ background: {_HOVER}; color: {_TEXT}; }}
QPushButton#titleBarBtn:last-child:hover {{ background: {_CLOSE}; color: {_ON_ACC}; }}

/* ── Sidebar ────────────────────────────────────────────── */
QWidget#sidebar {{
    background: {_SB};
    border-right: 1px solid {_LINE};
}}
QWidget#sidebarLogo {{ background: {_SB_LOGO}; }}

/* ── Nav buttons ────────────────────────────────────────── */
QPushButton#navBtn {{
    background: transparent; color: {_NB_TEXT};
    border: none; border-left: 4px solid transparent;
    padding: 9px 16px; text-align: left;
    font-size: 12px; border-radius: 0; min-height: 36px;
}}
QPushButton#navBtn:hover {{
    background: {_NB_HOV}; color: {_NB_ACT_TEXT};
    border-left: 4px solid {_ACC_DIM};
}}
QPushButton#navBtn[active="true"] {{
    background: {_NB_HOV}; color: {_NB_ACT_TEXT};
    border-left: 4px solid {_ACC}; font-weight: bold;
}}
QPushButton#viewerBtn {{
    background: {_VB_BG}; color: {_NB_ACT_TEXT};
    border: none; border-left: 4px solid {_ACC};
    padding: 11px 16px; text-align: left;
    font-size: 13px; font-weight: bold;
    border-radius: 0; min-height: 40px;
}}
QPushButton#viewerBtn:hover {{ background: {_VB_HOV}; }}
QPushButton#viewerBtn[active="true"] {{ background: {_VB_HOV}; }}

/* ── Buttons ────────────────────────────────────────────── */
QPushButton#actionBtn {{
    background: {_ACC}; color: {_ON_ACC}; border: none;
    border-radius: 7px; padding: 8px 20px;
    font-weight: bold; font-size: 13px; min-height: 32px;
    letter-spacing: 0.3px;
}}
QPushButton#actionBtn:hover {{ background: {_ACC_HOV}; }}
QPushButton#actionBtn:pressed {{ background: {_ACC_PRS}; }}
QPushButton#actionBtn:disabled {{ background: {_BTN_DIS}; color: {_BTN_DIS_T}; }}
QPushButton#secondaryBtn {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_IBD}; border-radius: 6px;
    padding: 6px 14px; min-height: 28px;
}}
/* Small square icon buttons (the zoom −/+/⟳ above a preview). They are fixed to
   22×22, where secondaryBtn's padding pushes the glyph clean out of the box —
   they rendered as three empty rectangles. */
QPushButton#iconBtn {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_IBD}; border-radius: 4px;
    padding: 0px; min-height: 0px; min-width: 0px;
    font-size: 13px;
}}
QPushButton#iconBtn:hover {{ background: {_HOVER}; border-color: {_ACC}; }}
QPushButton#iconBtn:pressed {{ background: {_SEL}; }}
QPushButton#secondaryBtn:hover {{ background: {_HOVER}; border-color: {_ACC}; }}
QPushButton#secondaryBtn:pressed {{ background: {_SEL}; }}

/* ── Input fields ───────────────────────────────────────── */
QLineEdit {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_IBD}; border-radius: 5px;
    padding: 5px 10px; min-height: 28px;
    selection-background-color: {_SEL};
}}
QLineEdit:focus {{
    border: 2px solid {_ACC};
    padding: 4px 9px;
}}
/* Combo boxes get their height from min-height, NEVER from vertical padding:
   Qt sizes the drop-down list from the combo's content height, so padding here
   made every popup exactly one row too short — a two-option dropdown had to be
   scrolled to reach the second option. */
QComboBox {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_IBD}; border-radius: 5px;
    padding: 0px 10px; min-height: 38px;
    selection-background-color: {_SEL};
}}
QComboBox:focus {{
    border: 2px solid {_ACC};
    padding: 0px 9px;
}}
/* No ::drop-down override here: giving it `border:none` and no arrow image left
   every combo box looking like a plain text field, with nothing to show it can
   be opened. Without the rule the style draws its own arrow. */
QComboBox QAbstractItemView {{
    background: {_IB}; color: {_TEXT};
    selection-background-color: {_SEL};
    border: 1px solid {_IBD};
    border-radius: 5px;
    padding: 4px;
    outline: none;
}}
/* The popup rows need an explicit height, otherwise they collapse to the bare
   text height and the list becomes a cramped sliver. */
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 2px 8px;
    border-radius: 3px;
}}
QComboBox QAbstractItemView::item:selected {{ background: {_SEL}; color: {_TEXT}; }}

/* Spin boxes are styled separately from the fields above: as soon as their
   padding/min-height came from that shared rule, Qt took over drawing the
   up/down sub-controls and rendered them as two blank slivers with no arrows.
   Colours + an explicit button width keep the theme AND the real arrows. */
QSpinBox, QDoubleSpinBox {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_IBD}; border-radius: 5px;
    padding-left: 10px; min-height: 36px;
    selection-background-color: {_SEL};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {_ACC}; }}
/* Width only — nothing else. Any paint property here (a background, even a
   transparent one) makes the stylesheet engine draw the buttons itself, and it
   has no arrow to draw: they came out as two blank slivers you could barely
   hit. With just a width the style keeps drawing its own arrows. */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{ width: 20px; }}

/* ── Lists & views ──────────────────────────────────────── */
QListWidget, QListView, QTreeView, QTableView {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_LB}; border-radius: 6px;
    padding: 3px; outline: none;
    alternate-background-color: {_LHOV};
}}
QListWidget::item, QListView::item, QTreeView::item, QTableView::item {{
    padding: 4px 4px; border-radius: 3px;
    background: transparent; color: {_TEXT};
}}
QListWidget::item:alternate, QListView::item:alternate,
QTreeView::item:alternate, QTableView::item:alternate {{
    background: {_LHOV}; color: {_TEXT};
}}
QListWidget::item:selected, QListView::item:selected,
QTreeView::item:selected, QTableView::item:selected {{
    background: {_SEL}; color: {_TEXT};
    border-radius: 3px;
}}
QListWidget::item:hover, QListView::item:hover,
QTreeView::item:hover, QTableView::item:hover {{
    background: {_LHOV}; color: {_TEXT};
}}
QHeaderView::section {{
    background: {_IB}; color: {_DIM};
    border: none; border-bottom: 1px solid {_LB};
    padding: 5px 8px; font-size: 11px; font-weight: bold;
    letter-spacing: 1px;
}}

/* ── Text areas ─────────────────────────────────────────── */
QTextEdit, QPlainTextEdit {{
    background: {_IB}; color: {_TEXT};
    border: 1px solid {_LB}; border-radius: 6px;
    font-family: 'JetBrains Mono','Cascadia Code','DejaVu Sans Mono',monospace;
    font-size: 12px; padding: 6px;
    selection-background-color: {_SEL};
}}

/* ── Checkboxes & radios ────────────────────────────────── */
/* background:transparent is not cosmetic — without it these inherit the window
   background from the QWidget rule above and paint it as an opaque bar right
   across the group box they sit in, which looked like a rendering fault. */
QCheckBox {{ color: {_TEXT}; spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 4px;
    border: 1px solid {_IBD}; background: {_IB};
}}
QCheckBox::indicator:hover {{ border-color: {_ACC}; }}
QCheckBox::indicator:checked {{
    background: {_ACC}; border: 1px solid {_ACC};
}}
QRadioButton {{ color: {_TEXT}; spacing: 8px; background: transparent; }}
QRadioButton::indicator {{
    width: 15px; height: 15px; border-radius: 8px;
    border: 1px solid {_IBD}; background: {_IB};
}}
QRadioButton::indicator:hover {{ border-color: {_ACC}; }}
QRadioButton::indicator:checked {{ background: {_ACC}; border: 2px solid {_ACC}; }}

/* ── Group boxes ────────────────────────────────────────── */
QGroupBox {{
    background: {_GB};
    border: 1px solid {_IBD}; border-radius: 8px;
    margin-top: 22px; padding: 10px 8px 8px 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px; top: 5px;
    padding: 0 6px;
    color: {_ACC}; font-size: 9px; font-weight: bold;
    letter-spacing: 2px;
}}

/* ── Scrollbars ─────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {_SCT}; width: 8px; border-radius: 4px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {_SCB}; border-radius: 4px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {_ACC}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {_SCT}; height: 8px; border-radius: 4px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {_SCB}; border-radius: 4px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {_ACC}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Slider ─────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    background: {_IBD}; height: 4px; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {_ACC}; width: 14px; height: 14px;
    border-radius: 7px; margin: -5px 0;
    border: 2px solid {_IB};
}}
QSlider::sub-page:horizontal {{ background: {_ACC}; border-radius: 2px; }}

/* ── Separator ──────────────────────────────────────────── */
QFrame#separator {{ background: {_LINE}; max-height: 1px; min-height: 1px; }}

/* ── Tabs ───────────────────────────────────────────────── */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {_LINE};
    background: {_TAB_P};
}}
QTabBar {{
    background: {_TAB_B};
    border-bottom: 1px solid {_LINE};
}}
QTabBar::tab {{
    background: {_TAB_B}; color: {_DIM};
    padding: 8px 16px; border: none;
    border-right: 1px solid {_LINE};
    min-width: 90px; font-size: 12px;
}}
QTabBar::tab:selected {{
    background: {_TAB_P}; color: {_TEXT};
    font-weight: bold;
    border-bottom: 3px solid {_ACC};
}}
QTabBar::tab:hover:!selected {{ background: {_HOVER}; color: {_TEXT}; }}
QTabBar::close-button {{ subcontrol-position: right; }}
"""


STYLE       = _build_style(dark=True)


LIGHT_STYLE = _build_style(dark=False)


class AppStyle:
    """Factory for the application style, with our own tab close button.

    Fusion draws SP_TabCloseButton as a red-boxed cross — it read as a Windows
    error icon sitting in the corner of every open tab. Overriding it here
    covers every tab the app opens without touching the several places that
    create them. The glyph is a neutral grey on purpose so it needs no repaint
    when the theme is switched.
    """
    @staticmethod
    def create():
        from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyleFactory
        from PyQt6.QtGui import QIcon, QPixmap, QPainter, QPen, QColor

        def _cross(size=16, colour=_ICON_CROSS, alpha=255):
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            c = QColor(colour); c.setAlpha(alpha)
            p.setPen(QPen(c, 1.6))
            m = size * 0.3
            p.drawLine(int(m), int(m), int(size - m), int(size - m))
            p.drawLine(int(size - m), int(m), int(m), int(size - m))
            p.end()
            return pm

        class _Style(QProxyStyle):
            def standardIcon(self, standardIcon, option=None, widget=None):
                if standardIcon == QStyle.StandardPixmap.SP_TabCloseButton:
                    icon = QIcon()
                    icon.addPixmap(_cross(alpha=190), QIcon.Mode.Normal)
                    icon.addPixmap(_cross(colour=_ICON_CROSS_ACTIVE),
                                   QIcon.Mode.Active)
                    return icon
                return super().standardIcon(standardIcon, option, widget)

        return _Style(QStyleFactory.create("Fusion"))


def apply_theme_globally(theme: str):
    """Push `theme` into every place that holds colours: the app stylesheet, the
    viewer palette (_TV, which re-styles the registered panels) and
    app_state.THEME.

    Startup used to set only the first two, so on a light-mode launch every
    widget styled through theme_color() kept the dark defaults — the Greyscale
    preview area most visibly."""
    QApplication.instance().setStyleSheet(LIGHT_STYLE if theme == "light" else STYLE)
    import tools.app_state as _as
    colours = shell_colours("light" if theme == "light" else "dark")
    _as.THEME.update({k: colours[k] for k in SHELL_KEYS})
    from tools.theme import set_viewer_theme
    set_viewer_theme(theme)          # last: it re-runs every panel's _apply_theme


# The icon's own colours. Fixed, not from the palette: this is a brand mark that
# ends up in a task bar and a launcher, where the application's light or dark
# setting means nothing. The blue is the one it has always been drawn in, which
# is a slightly deeper accent than the interface uses.
_ICON_INK   = "#1a1a2e"
_ICON_PAPER = "#eaeaea"
_ICON_FOLD  = "#cccccc"
_ICON_ACC   = "#3d82f0"

# The tab's close cross, and the red it turns under the pointer. Fixed for a
# duller reason: AppStyle is installed once at startup and Qt caches what
# standardIcon() hands back, so a themed value here would only ever be the theme
# the application happened to start in. A mid grey reads on either background.
_ICON_CROSS        = "#8892a4"
_ICON_CROSS_ACTIVE = "#e05260"


def app_icon():
    """The window icon, drawn rather than shipped as a file.

    A sheet of paper with a folded corner, three lines of text and the accent
    dot. It was twenty-five lines in the middle of main(), between forwarding
    the command line and opening the window.
    """
    icon_pm = QPixmap(64, 64)
    icon_pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(icon_pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(_ICON_INK)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 64, 64, 10, 10)
    p.setBrush(QBrush(QColor(_ICON_PAPER)))
    p.drawRoundedRect(12, 8, 32, 42, 3, 3)
    p.setBrush(QBrush(QColor(_ICON_INK)))
    p.drawPolygon(QPolygon([QPoint(36,8), QPoint(44,8), QPoint(44,16)]))
    p.setBrush(QBrush(QColor(_ICON_FOLD)))
    p.drawPolygon(QPolygon([QPoint(36,8), QPoint(44,16), QPoint(36,16)]))
    p.setPen(QPen(QColor(_ICON_ACC), 2))
    p.drawLine(18, 24, 38, 24)
    p.drawLine(18, 30, 38, 30)
    p.drawLine(18, 36, 30, 36)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(_ICON_ACC)))
    p.drawEllipse(38, 42, 14, 14)
    p.end()
    return QIcon(icon_pm)
