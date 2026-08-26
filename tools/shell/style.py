"""
How the application looks: the palette, the two stylesheets built from it,
the theme switch, and the window icon.
"""
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush,
                         QIcon, QPolygon)
from tools.theme import SHELL_KEYS, set_viewer_theme, shell_colours


def _build_style(dark: bool) -> str:
    """The stylesheet for one theme, from the palette in tools/theme.py.

    The colours were spelled out here as literals, a second copy of the same
    values that _THEME_COLOURS held and a third dark blue away from the module
    constants above them. The short local names are kept — they appear a few
    hundred times in the sheet below, and `{_BG}` reads better than a lookup.

    The rules follow docs/gui-concept.html: neutral chrome with 1px lines,
    blue only for selection/active/primary, fields on SURFACE_3, buttons and
    inset wells on SURFACE_2, hovers one surface-step up, tabs rounded on the
    right with the active one carrying a top accent bar.
    """
    c = shell_colours("dark" if dark else "light")
    _BG    = c["BG"];           _PANEL   = c["PANEL"];      _TEXT  = c["TEXT"]
    _DIM   = c["DIM"];          _HOVER   = c["HOVER"];      _LINE  = c["LINE"]
    _FAINT = c["FAINT"]
    _S2    = c["SURFACE_2"];    _S3      = c["SURFACE_3"]
    _LSTRONG = c["LINE_STRONG"]
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
    # The concept's --accent-soft: the ground a selected nav item or an active
    # stage sits on. Dark needs more of it than light to read against its
    # surface.
    _ACC_SOFT = f"rgba({_r},{_g},{_b},{0.16 if dark else 0.12})"
    # A disabled input's border fades toward its own ground — the enabled line
    # is already near the field background in dark, so a brighter grey (which
    # was used once) only made a disabled field MORE outlined than a live one.
    _DIS_LINE = "#28303f" if dark else "#e6eaf2"
    # The tab strip's own four colours, theme-specific so the ACTIVE tab is
    # unmistakable (the audit found active vs inactive ~4% luminance apart in
    # dark and inactive text ~3.2:1): a clearly lighter raised fill for the
    # active tab, a brighter inactive text that clears WCAG AA, and neutral
    # hover fills that never read as the active one.
    _TAB_INACTIVE_TEXT = "#aab3c2" if dark else "#4b5466"
    _TAB_HOV           = "#242b38" if dark else "#e4e8ef"
    _TAB_ACTIVE        = "#38445a" if dark else "#ffffff"
    _CLOSE_HOV         = "#2c3442" if dark else "#d7dce5"
    # The "+" new-tab button's hover fill: clearly lighter than its SURFACE_3
    # ground in dark and clearly darker in light, so it responds under the
    # pointer in either theme. A dedicated value — no existing surface sits
    # far enough off SURFACE_3 in both directions.
    _NEWTAB_HOV        = "#343e52" if dark else "#dde2ea"
    # The scrollbar thumb under the pointer — a neutral grey one step up from
    # the resting thumb, never the blue accent (the audit: a blue hover on an
    # 8 px grey bar read as a selection, not a scroll control).
    _SCROLL_HOV        = "#4b5568" if dark else "#9aa4b8"
    # The BETA chip text — readable (≥4.5:1) in both themes while staying a
    # quiet mark rather than an announcement. Dark FAINT was 3.55:1 (border
    # line) and light FAINT was 2.84:1 (too dim), so it gets its own values.
    # Light is darkened a step past FAINT to clear WCAG AA on the near-white
    # sidebar (FAINT-light reads 4.15:1).
    _BETA              = "#8792a7" if dark else "#5b687b"

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
    color: {_FAINT}; font-size: 10px; font-weight: bold; letter-spacing: 2px;
}}
QLabel#currentFileLabel {{ color: {_TEXT}; font-size: 12px; }}
QWidget#currentFileBar {{
    background: {_PANEL}; border-bottom: 1px solid {_LINE};
    min-height: 44px;
}}

/* ── Title bar ──────────────────────────────────────────── */
QWidget#titleBar {{
    background: {_SB_LOGO};
    border-bottom: 1px solid {_LSTRONG};
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
    background: transparent; padding: 5px 12px; border-radius: 6px;
}}
QMenuBar#titleMenuBar::item:selected {{
    background: {_HOVER};
}}
QMenu {{
    background: {_S2}; color: {_TEXT};
    border: 1px solid {_LSTRONG};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 26px 6px 14px; border-radius: 5px; }}
QMenu::item:selected {{ background: {_S3}; }}
QMenu::separator {{ height: 1px; background: {_LINE}; margin: 4px 10px; }}
QPushButton#titleBarBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 7px;
    font-size: 14px; min-width: 42px;
}}
QPushButton#titleBarBtn:hover {{ background: {_HOVER}; color: {_TEXT}; }}
QPushButton#titleBarBtn:last-child:hover {{ background: {_CLOSE}; color: {_ON_ACC}; }}
QPushButton#themeBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 7px;
    min-width: 42px; margin-right: 2px;
}}
QPushButton#themeBtn:hover {{ background: {_HOVER}; color: {_TEXT}; }}

/* ── Sidebar ────────────────────────────────────────────── */
QWidget#sidebar {{
    background: {_SB};
    border-right: 1px solid {_LSTRONG};
    padding-bottom: 4px;
}}
/* The containers inside it stay out of the way. They are plain QWidgets, so the
   blanket `QWidget {{ background: {_BG} }}` above painted the window colour over
   the sidebar's own — the column came out two-tone, sidebar-coloured for the
   height of the view switch and window-coloured for the tool list below it. */
QWidget#sidebarSlot, QWidget#toolList {{ background: transparent; }}
QWidget#sidebarLogo {{ background: {_SB_LOGO}; }}

/* ── Nav buttons ────────────────────────────────────────── */
/* Tightened from 36 px rows: nine tools at 36 px spread the column down the
   whole window, which is what left the group headings floating in space with
   no visible relationship to the entries under them. */
/* Indented past the heading's own 16 px, so the entries read as sitting under
   it rather than beside it — the indent is what says "contained by". */
QPushButton#navBtn {{
    background: transparent; color: {_NB_TEXT};
    border: none; border-left: 3px solid transparent;
    padding: 5px 14px 5px 21px; text-align: left;
    font-size: 12px; border-radius: 7px; min-height: 26px;
}}
/* The tool-list group headings — FARBE, INHALT, AUSGABE. Their own name, not
   the shared #sectionLabel: that one is 9 px with 3 px of letter-spacing, which
   is legible inside a dialog beside the field it names and turns into a row of
   disconnected capitals when it has to head a list from across the column.
   The rule above closes the group off from the one before it; the heading then
   belongs to what follows it, which is the whole point of a heading. */
QLabel#navGroup {{
    color: {_FAINT}; font-size: 10px; font-weight: bold; letter-spacing: 1px;
    border-top: 1px solid {_LINE};
}}
QPushButton#navBtn:hover {{
    background: {_NB_HOV}; color: {_NB_ACT_TEXT};
}}
QPushButton#navBtn[active="true"] {{
    background: {_ACC_SOFT}; color: {_NB_ACT_TEXT};
    border-left: 3px solid {_ACC}; font-weight: bold;
}}
/* The three views, as one segmented control at the top of the sidebar.
   One rectangle holding three segments — the border and the ground belong to
   the container, never to the segments. Giving each segment its own border
   turned the control into three separate blobs floating in the column. */
QWidget#viewSwitch {{
    background: {_VB_BG};
    border: 1px solid {_LINE};
    border-radius: 9px;
}}
QPushButton#viewSeg {{
    background: transparent; color: {_NB_TEXT};
    border: none; border-radius: 6px;
    padding: 6px 2px; font-size: 11px; min-height: 26px;
}}
QPushButton#viewSeg:hover {{ color: {_NB_ACT_TEXT}; }}
/* No accent fill on the checked segment — the raised surface says it, and the
   extra width of a bold label pushed "Seiten verwalten" past the 224 px column
   and clipped it. */
QPushButton#viewSeg:checked {{
    background: {_S3}; color: {_TEXT}; font-weight: bold;
}}
/* The BETA chip under the sidebar slot — deliberately quiet: a small, dim,
   monospace mark, not the accent-coloured announcement it used to be. Readable
   (≥3.5:1 in both themes) with bottom padding so it does not sit flush. */
QLabel#betaChip {{
    color: {_BETA}; font-family: 'JetBrains Mono','DejaVu Sans Mono',monospace;
    font-size: 11px; letter-spacing: 1px;
}}
QCheckBox#stageHead {{
    color: {_TEXT}; font-weight: bold; font-size: 12px; padding: 6px 0 2px;
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
    background: {_S2}; color: {_TEXT};
    border: 1px solid {_LINE}; border-radius: 7px;
    padding: 6px 14px; min-height: 28px;
}}
/* Small square icon buttons (the zoom −/+/⟳ above a preview). They are fixed to
   22×22, where secondaryBtn's padding pushes the glyph clean out of the box —
   they rendered as three empty rectangles. */
QPushButton#iconBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 7px;
    padding: 0px; min-height: 0px; min-width: 0px;
    font-size: 13px;
}}
QPushButton#iconBtn:hover {{ background: {_HOVER}; color: {_TEXT}; }}
QPushButton#iconBtn:pressed {{ background: {_SEL}; }}
QPushButton#iconBtn:disabled {{ color: {_BTN_DIS_T}; background: transparent; }}
/* The status bar's own small icon buttons (the zoomer −/+/⛶, the page-nav
   ◂/▸, the ruler). They differ from #iconBtn in one essential: #iconBtn's
   `min-width:0px` lets the layout shrink a fixed-size button below its hit
   target, so these get an explicit 24px floor instead of trusting setFixedSize
   against the sheet. */
QPushButton#sbIconBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 5px;
    min-width: 24px; min-height: 24px; padding: 0px;
}}
QPushButton#sbIconBtn:hover {{ background: {_HOVER}; color: {_TEXT}; }}
QPushButton#sbIconBtn:pressed {{ background: {_SEL}; }}
QPushButton#sbIconBtn:disabled {{ color: {_BTN_DIS_T}; background: transparent; }}
QPushButton#sbIconBtn:checked {{ background: {_ACC_SOFT}; }}
QPushButton#secondaryBtn:hover {{ background: {_S3}; border-color: {_ACC}; }}
QPushButton#secondaryBtn:pressed {{ background: {_SEL}; }}

/* ── Input fields ───────────────────────────────────────── */
QLineEdit {{
    background: {_S3}; color: {_TEXT};
    border: 1px solid {_LINE}; border-radius: 7px;
    padding: 5px 10px; min-height: 28px;
    selection-background-color: {_SEL};
}}
QLineEdit:focus {{
    border: 2px solid {_ACC};
    padding: 4px 9px;
}}
/* A disabled field must LOOK disabled: its text and placeholder dim to a
   mid-grey and its border fades toward the field ground. Without this the
   QSS colour/border above override the palette's Disabled group, so a
   disabled QLineEdit rendered identically to an enabled one — which is
   exactly what the print dialog's range field looked like. */
QLineEdit:disabled {{
    color: {_BTN_DIS_T};
    background: {_BTN_DIS};
    border: 1px solid {_DIS_LINE};
}}
/* Combo boxes get their height from min-height, NEVER from vertical padding:
   Qt sizes the drop-down list from the combo's content height, so padding here
   made every popup exactly one row too short — a two-option dropdown had to be
   scrolled to reach the second option. */
QComboBox {{
    background: {_S3}; color: {_TEXT};
    border: 1px solid {_LINE}; border-radius: 7px;
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
    background: {_S3}; color: {_TEXT};
    selection-background-color: {_SEL};
    border: 1px solid {_LINE};
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
    background: {_S3}; color: {_TEXT};
    border: 1px solid {_LINE}; border-radius: 7px;
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
    background: {_S2}; color: {_TEXT};
    border: 1px solid {_LB}; border-radius: 8px;
    padding: 3px; outline: none;
    alternate-background-color: {_LHOV};
}}
QListWidget::item, QListView::item, QTreeView::item, QTableView::item {{
    padding: 4px 4px; border-radius: 5px;
    background: transparent; color: {_TEXT};
}}
QListWidget::item:alternate, QListView::item:alternate,
QTreeView::item:alternate, QTableView::item:alternate {{
    background: {_LHOV}; color: {_TEXT};
}}
QListWidget::item:selected, QListView::item:selected,
QTreeView::item:selected, QTableView::item:selected {{
    background: {_SEL}; color: {_TEXT};
    border-radius: 5px;
}}
QListWidget::item:hover, QListView::item:hover,
QTreeView::item:hover, QTableView::item:hover {{
    background: {_LHOV}; color: {_TEXT};
}}
QHeaderView::section {{
    background: {_S2}; color: {_DIM};
    border: none; border-bottom: 1px solid {_LB};
    padding: 5px 8px; font-size: 11px; font-weight: bold;
    letter-spacing: 1px;
}}

/* ── Text areas ─────────────────────────────────────────── */
QTextEdit, QPlainTextEdit {{
    background: {_S2}; color: {_TEXT};
    border: 1px solid {_LB}; border-radius: 8px;
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
    border: 1px solid {_LSTRONG}; background: {_S3};
}}
QCheckBox::indicator:hover {{ border-color: {_ACC}; }}
QCheckBox::indicator:checked {{
    background: {_ACC}; border: 1px solid {_ACC};
}}
QRadioButton {{ color: {_TEXT}; spacing: 8px; background: transparent; }}
QRadioButton::indicator {{
    width: 15px; height: 15px; border-radius: 8px;
    border: 1px solid {_LSTRONG}; background: {_S3};
}}
QRadioButton::indicator:hover {{ border-color: {_ACC}; }}
QRadioButton::indicator:checked {{ background: {_ACC}; border: 2px solid {_ACC}; }}

/* ── Group boxes ────────────────────────────────────────── */
QGroupBox {{
    background: {_GB};
    border: 1px solid {_LINE}; border-radius: 9px;
    margin-top: 22px; padding: 10px 8px 8px 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px; top: 5px;
    padding: 0 6px;
    color: {_FAINT}; font-size: 10px; font-weight: bold;
    letter-spacing: 2px;
}}

/* ── Scrollbars ─────────────────────────────────────────── */
/* 8 px, a neutral grey thumb (never the blue accent), square with no arrow
   buttons. QScrollArea's AsNeeded policy hides the bar when the content fits,
   so the grids/print settings only show it when there is actually more to see.
   The thumb's hover is one grey step up so it still responds under the
   pointer without ever reading as a selection. */
QScrollBar:vertical {{
    background: {_SCT}; width: 8px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {_SCB}; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {_SCROLL_HOV}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {_SCT}; height: 8px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {_SCB}; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {_SCROLL_HOV}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* The sidebar's own scroll surface gets the same 8px scrollbar explicitly,
   so it is never left with Fusion's thin 2px thumb. */
QScrollArea#sidebarScroll QScrollBar:vertical {{
    background: {_SCT}; width: 8px; margin: 2px;
}}
QScrollArea#sidebarScroll QScrollBar::handle:vertical {{
    background: {_SCB};
}}
QScrollArea#sidebarScroll QScrollBar::handle:vertical:hover {{ background: {_SCROLL_HOV}; }}
QScrollArea#sidebarScroll QScrollBar::add-line:vertical,
QScrollArea#sidebarScroll QScrollBar::sub-line:vertical {{ height: 0; }}

/* ── Slider ─────────────────────────────────────────────── */
QSlider::groove:horizontal {{
    background: {_IBD}; height: 4px; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {_ACC}; width: 14px; height: 14px;
    border-radius: 7px; margin: -5px 0;
    border: 2px solid {_S3};
}}
QSlider::sub-page:horizontal {{ background: {_ACC}; border-radius: 2px; }}

/* ── Separator ──────────────────────────────────────────── */
QFrame#separator {{ background: {_LINE}; max-height: 1px; min-height: 1px; }}

/* ── Tabs ───────────────────────────────────────────────── */
/* The document row has to read as one band across the whole window. QTabBar is
   only as wide as its tabs, and the corner widget only covers the right end —
   the strip between them fell through to the window background, so the band
   stopped dead after the last tab. Painting the QTabWidget itself fills that
   gap; the pane below covers everything else. */
/* Concept tab shape: rectangular, rounded on the right only, a 3 px gap between
   neighbours and none before the first. The active one is the raised surface
   with a 2 px accent bar along its top — the bar is a transparent border on
   every tab, so activating one moves nothing. */
QTabWidget {{ background: {_TAB_B}; }}
QTabWidget::pane {{
    border: none;
    background: {_TAB_P};
}}
QTabBar {{
    background: {_TAB_B};
    min-height: 40px; max-height: 40px;
}}
QTabBar::tab {{
    background: {_S2}; color: {_TAB_INACTIVE_TEXT};
    padding: 0px 12px 0px 12px;
    border: none;
    border-top: 2px solid transparent;
    border-top-left-radius: 0px;
    border-top-right-radius: 9px;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 9px;
    margin-left: 3px;
    min-width: 90px; font-size: 12.5px;
    min-height: 34px;
}}
QTabBar::tab:first {{ margin-left: 0; }}
/* Inactive tabs are a raised surface of their own so they read as tabs, not
   as part of the docbar band behind them (user issue #8 — previously the
   inactive fill was the rail's own colour and the tabs merged with it). */
QTabBar::tab:hover:!selected {{ background: {_TAB_HOV}; color: {_TEXT}; }}
QTabBar::tab:selected {{
    background: {_TAB_ACTIVE}; color: {_TEXT};
    font-weight: 700;
    border-top: 2px solid {_ACC};
}}
/* The per-tab close cross is a real QToolButton mounted on each tab (not the
   QSS subcontrol: styling QTabBar::close-button makes Qt draw only what the
   sheet specifies and, with no image, render nothing). It is shown on the
   active tab and on hover by the tab host. */
QToolButton#tabCloseBtn {{
    border: none; border-radius: 4px;
    background: transparent;
}}
QToolButton#tabCloseBtn:hover {{ background: {_CLOSE_HOV}; }}
QToolButton#tabCloseBtn:pressed {{ background: {_CLOSE_HOV}; }}

/* ── The document row ───────────────────────────────────── */
/* The tab bar IS the row: the actions ride in its right-hand corner, so one
   40 px strip carries what used to take a 46 px button bar above a tab strip. */
QWidget#docActions {{ background: {_TAB_B}; }}
QWidget#docRow {{
    background: {_TAB_B};
    border-bottom: 1px solid {_LSTRONG};
}}
/* The "+" new-tab button sits on the doc row beside the last tab and has to
   read as a button there. Its earlier SURFACE_2 fill + LINE border were within
   a few percent of the row's TAB_BAR in both themes (1.09:1 dark, 1.08:1 light)
   and the sheet-edge equalled the fill, so it rendered as a bare glyph on the
   row. A raised SURFACE_3 fill (one step off the row) and a strong 1px
   LINE_STRONG edge give it a card that actually reads as clickable. */
QPushButton#newtabBtn {{
    background: {_S3}; color: {_DIM};
    border: 1px solid {_LSTRONG}; border-radius: 7px;
    font-size: 14px; min-width: 26px; min-height: 26px;
}}
QPushButton#newtabBtn:hover {{ background: {_NEWTAB_HOV}; border-color: {_ACC}; color: {_TEXT}; }}
QPushButton#newtabBtn:pressed {{ background: {_SEL}; }}
QPushButton#docBtn {{
    background: {_S2}; color: {_TEXT};
    border: 1px solid {_LINE}; border-radius: 7px;
    padding: 0 13px; font-size: 12.5px; min-height: 30px;
}}
QPushButton#docBtn:hover {{ background: {_S3}; border-color: {_ACC}; }}
QPushButton#docBtn:pressed {{ background: {_S3}; }}
QPushButton#docBtn:disabled {{ color: {_BTN_DIS_T}; background: transparent; border-color: transparent; }}
/* No arrow of our own: the label already ends in one, and Qt's indicator would
   sit a second caret beside it. */
QPushButton#docBtn::menu-indicator {{ image: none; width: 0; }}
/* The Bearbeiten button is icon-only (docIconBtn) but still opens a menu; Qt
   would draw its own caret over the pencil otherwise. */
QPushButton#docIconBtn::menu-indicator {{ image: none; width: 0; }}
QPushButton#docIconBtn {{
    background: transparent; color: {_DIM};
    border: none; border-radius: 7px;
    padding: 0; font-size: 14px;
}}
QPushButton#docIconBtn:hover {{ background: {_S3}; color: {_TEXT}; }}
QPushButton#docIconBtn:pressed {{ background: {_S3}; color: {_TEXT}; }}
QPushButton#docIconBtn:disabled {{ color: {_BTN_DIS_T}; background: transparent; }}
QLineEdit#findEdit {{
    min-height: 28px; font-size: 12px; padding: 0 9px;
    border: 1px solid {_LINE}; border-radius: 7px;
    background: {_S2};
}}

/* ── Status bar (window level) ──────────────────────────── */
/* One bar under every view. The readings report; the centre message is the
   app's mouth and opens the Protokoll when clicked; the right end holds the
   ruler switch and the zoomer pill. */
QWidget#statusBar {{
    background: {_SB};
    border-top: 1px solid {_LSTRONG};
}}
QLabel#sbReading {{ color: {_DIM}; font-size: 12px; background: transparent;
    padding: 0 2px; }}
QLabel#sbSep {{ color: {_FAINT}; background: transparent; }}
QLabel#sbMsg {{
    color: {_DIM}; font-size: 12px; background: transparent;
    border-radius: 6px; padding: 0 14px;
}}
QLabel#sbMsg:hover {{ background: {_HOVER}; color: {_TEXT}; }}
QWidget#sbZoomer {{
    background: {_S2}; border: 1px solid {_LINE}; border-radius: 7px;
}}
QLabel#sbZoomVal {{ color: {_DIM}; font-size: 11px; background: transparent;
    font-variant-numeric: tabular-nums; }}
QWidget#sbZoomDiv {{ background: {_LINE}; }}
/* The centred page-nav cluster: the current page as a compact input, the
   total as a dim reading. The arrows reuse the icon-button look (grey, no
   border) and share its disabled state. */
QWidget#sbPageNav {{ background: transparent; }}
QLineEdit#sbPageField {{
    color: {_TEXT}; background: {_S3}; border: 1px solid {_LINE};
    border-radius: 5px; padding: 2px 0; font-size: 12px;
    min-height: 0px;
}}
QLineEdit#sbPageField:focus {{ border: 2px solid {_ACC}; }}
/* The page-nav field's inert state (Batch F): in dark the number dims a step
   past the generic disabled text and the border vanishes so the field reads as
   "off", not merely quieter (the audit: the old #667080 text + #28303f border
   kept it looking live against SURFACE_3). Light keeps the generic faded look,
   which the auditor already verified. */
QLineEdit#sbPageField:disabled {{
    color: {"#566070" if dark else _BTN_DIS_T};
    background: {_BTN_DIS};
    border: {"none" if dark else "1px solid " + _DIS_LINE};
}}
QLabel#sbNavTotal {{ color: {_DIM}; font-size: 12px; background: transparent; }}
/* The zoomer's buttons and the ruler switch share the icon-button look; the
   ruler is the only checkable one, and checked means the accent-soft ground
   every other active control sits on. */
QPushButton#iconBtn:checked {{ background: {_ACC_SOFT}; }}
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


def search_icon(colour: str):
    """A magnifier, drawn rather than typed.

    "⌕" (U+2315) is in almost no UI font and fell back to a glyph that rendered
    as a small circle — a button that looks like a rendering fault is worse than
    no button. Drawn here for the same reason the tab's close cross is: the app
    already owns its own small marks, and one that is always there deserves to
    be one of them."""
    pm = QPixmap(28, 28)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(colour), 2.2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(6, 6, 12, 12)
    p.drawLine(17, 17, 23, 23)
    p.end()
    return QIcon(pm)


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
