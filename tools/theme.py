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
    "dark": {
        "shell": {
            # The eight the tool panels and theme_color() know by name.
            "BG":    "#151520",   "SIDE":  "#0e2d58",
            "PANEL": "#1c2340",   "ACC":   "#4d8df5",
            "TEXT":  "#e8eaf0",   "DIM":   "#7a8699",
            "HOVER": "#1e4d82",   "LINE":  "#1e3354",
            # The accent under the pointer and under the finger.
            "ACC_HOVER":   "#6ba1f8", "ACC_PRESSED": "#3a76d8",
            # Shades only the stylesheet uses.
            "INPUT_BG":    "#1a2038", "INPUT_BORDER": "#2a4470",
            "LIST_BORDER": "#2a4470", "SELECTION":    "#1e4d82",
            "LIST_HOVER":  "#1a3050", "TAB_BAR":      "#151520",
            "TAB_PANE":    "#1c2340", "SCROLL_BAR":   "#2e4e78",
            "SCROLL_TRACK": "#151520", "SIDEBAR_LOGO": "#0b1e3a",
            "NAV_TEXT":    "#7a8699", "NAV_HOVER":    "#162848",
            "NAV_ACTIVE_TEXT": "#e8eaf0",
            "VIEW_BTN":    "#162848", "VIEW_BTN_HOVER": "#1e3a60",
            "BTN_DISABLED": "#2c2c3e", "BTN_DISABLED_TEXT": "#55607a",
            "GROUP_BOX":   "#1a2038",
        },
        "viewer": {
            "viewer_bg":  "#111827",
            "sidebar_bg": "#0f3460",
            "panel_bg":   "#16213e",
            "card_bg":    "#1a2a40",
            "input_bg":   "#162a4a",
            "border":     "#1e3a5a",
            "input_brd":  "#2a4a70",
            "hover":      "#1a4a80",
            "text":       "#eaeaea",
            "dim":        "#8892a4",
            "vdim":       "#556070",
            "acc":        "#4d8df5",
            "btn_bg":     "#16213e",
            "btn_brd":    "#2a4a70",
            "sel_bg":     "#1a4a80",
            "splitter":   "#1e3a5a",
        },
    },
    "light": {
        "shell": {
            "BG":    "#edf1f7",   "SIDE":  "#d8e8f8",
            "PANEL": "#ffffff",   "ACC":   "#1f6feb",
            "TEXT":  "#0d1a28",   "DIM":   "#556070",
            "HOVER": "#d4e4f8",   "LINE":  "#c0d0e4",
            "ACC_HOVER":   "#1a5fd0", "ACC_PRESSED": "#1550b4",
            "INPUT_BG":    "#ffffff", "INPUT_BORDER": "#a8c0d8",
            "LIST_BORDER": "#a8c0d8", "SELECTION":    "#d4e4f8",
            "LIST_HOVER":  "#eaf2fc", "TAB_BAR":      "#edf1f7",
            "TAB_PANE":    "#ffffff", "SCROLL_BAR":   "#a8c0d8",
            "SCROLL_TRACK": "#e0eaf4", "SIDEBAR_LOGO": "#bcd0ec",
            "NAV_TEXT":    "#446080", "NAV_HOVER":    "#a8c8ec",
            "NAV_ACTIVE_TEXT": "#0d1a28",
            "VIEW_BTN":    "#c8ddf0", "VIEW_BTN_HOVER": "#b4ccec",
            "BTN_DISABLED": "#c0cede", "BTN_DISABLED_TEXT": "#88a0b4",
            "GROUP_BOX":   "#f6f9fd",
        },
        "viewer": {
            "viewer_bg":  "#e8edf3",
            "sidebar_bg": "#dce8f8",
            "panel_bg":   "#ffffff",
            "card_bg":    "#e4ecf6",
            "input_bg":   "#ffffff",
            "border":     "#b8cce0",
            "input_brd":  "#98b4cc",
            "hover":      "#c0d8f0",
            "text":       "#0f1925",
            "dim":        "#4a6080",
            "vdim":       "#6888a0",
            "acc":        "#1f6feb",
            "btn_bg":     "#eef4fc",
            "btn_brd":    "#98b4cc",
            "sel_bg":     "#b0ccec",
            "splitter":   "#b8cce0",
        },
    },
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
_DARK_TV = PALETTE["dark"]["viewer"]
_LIGHT_TV = PALETTE["light"]["viewer"]


_TV: dict = dict(_DARK_TV)   # current live theme — mutated by set_viewer_theme()


_TOP_BTN_W = 132


_PREV_BTN = (34, 26)


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
                import logging
                logging.exception(f"_apply_theme failed on {obj!r}")
        else:
            dead.append(ref)
    for d in dead:
        _theme_panels.remove(d)
