"""
Every colour the application paints with, and the widgets that follow them.

The one source. There used to be four palettes: module constants in
tools/shell/style.py, a second set of literals inside _build_style, a third in
_THEME_COLOURS, and a fourth here — and the first two disagreed about what dark
blue means. Everything derives from PALETTE now; nothing restates it.

Each theme has two vocabularies, because two different things read them:

  "shell"   what the stylesheet is built from, and what theme_color() answers
            with. The eight names the tool panels use (BG, SIDE, PANEL, ACC,
            TEXT, DIM, HOVER, LINE) plus the shades only the stylesheet needs.
  "viewer"  what the viewer's own widgets paint with at run time — the page
            canvas, the thumbnail grids, the print preview. Exposed as _TV.

_TV is the live viewer palette. It is mutated in place rather than rebound, so a
widget holding a reference sees the change; set_viewer_theme swaps its contents
and then re-styles every panel that registered itself with _register_themed. The
registry is weak, so a closed tab does not keep its panels alive.
"""
import logging
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter
import weakref as _weakref


PALETTE = {
    # The concept's neutral palette (docs/gui-concept.html :root). Five
    # surfaces and two line weights carry the whole chrome; blue is only
    # ever the accent. SURFACE_2 is the button ground and the raised card,
    # SURFACE_3 the input ground and every hover target — the stylesheet
    # still shares INPUT_BG/HOVER between fields and buttons, so both sit
    # one step apart rather than exactly on their concept values until the
    # QSS rewrite splits them.
    "dark": {
        "shell": {
            # The eight the tool panels and theme_color() know by name.
            "BG":    "#0f1115",   "SIDE":  "#15181e",
            "PANEL": "#15181e",   "ACC":   "#4d8df5",
            "TEXT":  "#e8ebf2",   "DIM":   "#949cad",
            "HOVER": "#222834",   "LINE":  "#272e3a",
            "FAINT": "#667080",
            # The concept's surface steps and edges.
            "SURFACE_2":    "#1b2028", "SURFACE_3":   "#222834",
            "CANVAS":       "#0b0d11", "LINE_STRONG": "#343d4d",
            # The accent under the pointer and under the finger.
            "ACC_HOVER":   "#6ba1f8", "ACC_PRESSED": "#3a76d8",
            # Shades only the stylesheet uses.
            "INPUT_BG":    "#1b2028", "INPUT_BORDER": "#272e3a",
            "LIST_BORDER": "#272e3a", "SELECTION":    "#1e2b40",
            "LIST_HOVER":  "#222834", "TAB_BAR":      "#15181e",
            "TAB_PANE":    "#0f1115", "SCROLL_BAR":   "#343d4d",
            "SCROLL_TRACK": "#0f1115", "SIDEBAR_LOGO": "#15181e",
            "NAV_TEXT":    "#949cad", "NAV_HOVER": "#222834",
            "NAV_ACTIVE_TEXT": "#e8ebf2",
            "VIEW_BTN":    "#1b2028", "VIEW_BTN_HOVER": "#222834",
            "BTN_DISABLED": "#1b2028", "BTN_DISABLED_TEXT": "#667080",
            "GROUP_BOX":   "#1b2028",
            # White on the accent-coloured button, and the red the window's
            # close button turns under the pointer (the concept's own value).
            "ON_ACCENT":   "#ffffff", "CLOSE_BTN": "#c0392b",
        },
        "viewer": {
            "viewer_bg":  "#0b0d11",
            "sidebar_bg": "#15181e",
            "panel_bg":   "#15181e",
            "card_bg":    "#1b2028",
            "input_bg":   "#222834",
            "border":     "#272e3a",
            "input_brd":  "#272e3a",
            "hover":      "#222834",
            "text":       "#e8ebf2",
            "dim":        "#949cad",
            "vdim":       "#667080",
            "acc":        "#4d8df5",
            "btn_bg":     "#1b2028",
            "btn_brd":    "#272e3a",
            "sel_bg":     "#1e2b40",
            "splitter":   "#272e3a",
            "placeholder": "#1b2028",
            # The scroll rail's groove. Its own colour rather than the canvas's:
            # the rail sits on sidebar_bg, and a groove painted in viewer_bg is
            # near-invisible against it in the light theme, where the two are
            # a few per cent apart.
            "track":      "#0a0c10",
            # The status bar's preflight light. Semantic, not decorative: green
            # means the document could go on a press, amber means something is
            # worth knowing first.
            "ok":         "#4caf7d",
            "warn":       "#e9a23b",
            "surface_2":  "#1b2028",
            "surface_3":  "#222834",
            "canvas":     "#0b0d11",
            "line_strong": "#343d4d",
        },
    },
    "light": {
        "shell": {
            "BG":    "#e7ebf1",   "SIDE":  "#f6f8fa",
            "PANEL": "#f6f8fa",   "ACC":   "#1f6feb",
            "TEXT":  "#1a2029",   "DIM":   "#5c6675",
            "HOVER": "#ffffff",   "LINE":  "#d0d6e0",
            "FAINT": "#8b95a5",
            "SURFACE_2":    "#edeff4", "SURFACE_3":   "#ffffff",
            "CANVAS":       "#dbe1e9", "LINE_STRONG": "#b9c2d0",
            "ACC_HOVER":   "#1a5fd0", "ACC_PRESSED": "#1550b4",
            "INPUT_BG":    "#edeff4", "INPUT_BORDER": "#d0d6e0",
            "LIST_BORDER": "#d0d6e0", "SELECTION":    "#e4eefd",
            "LIST_HOVER":  "#ffffff", "TAB_BAR":      "#f6f8fa",
            "TAB_PANE":    "#e7ebf1", "SCROLL_BAR":   "#b9c2d0",
            "SCROLL_TRACK": "#e7ebf1", "SIDEBAR_LOGO": "#f6f8fa",
            "NAV_TEXT":    "#5c6675", "NAV_HOVER": "#ffffff",
            "NAV_ACTIVE_TEXT": "#1a2029",
            "VIEW_BTN":    "#edeff4", "VIEW_BTN_HOVER": "#ffffff",
            "BTN_DISABLED": "#e4e9f1", "BTN_DISABLED_TEXT": "#8b95a5",
            "GROUP_BOX":   "#edeff4",
            "ON_ACCENT":   "#ffffff", "CLOSE_BTN": "#c0392b",
        },
        "viewer": {
            "viewer_bg":  "#dbe1e9",
            "sidebar_bg": "#f6f8fa",
            "panel_bg":   "#f6f8fa",
            "card_bg":    "#edeff4",
            "input_bg":   "#ffffff",
            "border":     "#d0d6e0",
            "input_brd":  "#d0d6e0",
            "hover":      "#ffffff",
            "text":       "#1a2029",
            "dim":        "#5c6675",
            "vdim":       "#8b95a5",
            "acc":        "#1f6feb",
            "btn_bg":     "#edeff4",
            "btn_brd":    "#d0d6e0",
            "sel_bg":     "#e4eefd",
            "splitter":   "#d0d6e0",
            "placeholder": "#c9d1dd",
            "track":      "#c6ccd6",
            "ok":         "#1e8a57",
            "warn":       "#b87309",
            "surface_2":  "#edeff4",
            "surface_3":  "#ffffff",
            "canvas":     "#dbe1e9",
            "line_strong": "#b9c2d0",
        },
    },
}

# Two things that do not follow the theme, because they do not describe the
# application. They describe what comes out of the printer: the preview panes
# paint a sheet of paper and the marks a blade will cut along, and paper is
# white and ink is black whether or not you like your screen dark.
#
# The two previews disagreed about white — the N-Up sheet was #f5f5f5 and the
# Crop/Scale sheet #ffffff, side by side in the same application.
PAPER = "#ffffff"
INK   = "#000000"
# The marks on that sheet: a border, a header bar, the fainter body lines.
# Same values in both themes for the same reason as PAPER and INK.
PAPER_LINE  = "#c9cedb"
PAPER_LINE2 = "#aab3c6"

# What a colour means rather than where it sits, so these hold across both
# themes: each is a mid-tone chosen to stay legible on a near-black panel and
# on a white one. The greyscale report paints its legend and its rows from
# these, and a drag carrying a copy marks itself with `copy`.
STATUS = {
    "converted": "#3a8a3a",   # the page was turned grey
    "forced":    "#2176ae",   # …because you told it to, not because it looked grey
    "skipped":   "#e67e22",   # left alone
    "colour":    "#c0392b",   # still colour, and will be billed as colour
    "copy":      "#3a8a3a",   # the "+" on a drag that copies instead of moves
}

# The find highlights, painted over the rendered page. Fixed like PAPER and INK
# and for the same reason: they are marks on the sheet, not part of the
# application's chrome, and a highlighter is yellow on a white page whatever the
# interface around it is set to. The alpha is what keeps the text readable
# through them — the match under the cursor is the stronger of the two so it can
# be picked out of a page full of hits.
FIND = {
    "hit":     (255, 214, 0, 96),
    "current": (255, 138, 0, 150),
    "edge":    (196, 120, 0, 200),
}

# The eight names that leave this module: theme_color() answers for these, and
# tool panels ask for them by name. The rest of "shell" is the stylesheet's own
# business.
SHELL_KEYS = ("BG", "SIDE", "PANEL", "ACC", "TEXT", "DIM", "HOVER", "LINE")


def shell_colours(theme):
    """Everything the stylesheet is built from, for one theme."""
    return PALETTE.get(theme, PALETTE["dark"])["shell"]


def viewer_colours(theme):
    """What the viewer's own widgets paint with, for one theme."""
    return PALETTE.get(theme, PALETTE["dark"])["viewer"]


# Kept as names because the viewer and several panels import them directly.
_DARK_TV  = viewer_colours("dark")
_LIGHT_TV = viewer_colours("light")
_TV: dict = dict(_DARK_TV)   # current live theme — mutated by set_viewer_theme()

_TOP_BTN_W = 132
_PREV_BTN  = (34, 26)

_DROP_THICKNESS = 7      # px across the slim axis
_DROP_HALO      = 4      # px of glow around the body


def _paint_drop_marker(p, x, y, length, horizontal=False):
    """Paint the drop slot. (x, y) is its top-left; `length` runs along the
    card edge it marks — the card height for a column of cards, the card width
    for a row."""
    w, h = (length, _DROP_THICKNESS) if horizontal else (_DROP_THICKNESS, length)
    body = QRectF(x, y, w, h)
    acc  = QColor(_TV['acc'])
    halo = QColor(acc); halo.setAlpha(70)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(halo)
    glow = body.adjusted(-_DROP_HALO, -_DROP_HALO, _DROP_HALO, _DROP_HALO)
    r_glow = min(glow.width(), glow.height()) / 2.0
    p.drawRoundedRect(glow, r_glow, r_glow)
    p.setBrush(acc)
    r_body = min(body.width(), body.height()) / 2.0
    p.drawRoundedRect(body, r_body, r_body)
    p.restore()


_theme_panels: list = []      # weakrefs to panels that have _apply_theme()


def _register_themed(panel) -> None:
    _theme_panels[:] = [r for r in _theme_panels if r() is not None]
    _theme_panels.append(_weakref.ref(panel))


def set_viewer_theme(theme: str) -> None:
    """Update live theme colours and re-style all registered panels."""
    _TV.clear()
    _TV.update(_DARK_TV if theme == "dark" else _LIGHT_TV)
    dead = []
    for ref in _theme_panels:
        obj = ref()
        if obj is not None:
            try:
                obj._apply_theme()
            except Exception:
                logging.exception(f"_apply_theme failed on {obj!r}")
        else:
            dead.append(ref)
    for d in dead:
        _theme_panels.remove(d)
