"""
Theme, moved verbatim out of tools/page_viewer.py.
"""
import logging
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter
import weakref as _weakref


_DARK_TV = {
    'viewer_bg':  '#111827',
    'sidebar_bg': '#0f3460',
    'panel_bg':   '#16213e',
    'card_bg':    '#1a2a40',
    'input_bg':   '#162a4a',
    'border':     '#1e3a5a',
    'input_brd':  '#2a4a70',
    'hover':      '#1a4a80',
    'text':       '#eaeaea',
    'dim':        '#8892a4',
    'vdim':       '#556070',
    'acc':        '#4d8df5',
    'btn_bg':     '#16213e',
    'btn_brd':    '#2a4a70',
    'sel_bg':     '#1a4a80',
    'splitter':   '#1e3a5a',
}


_LIGHT_TV = {
    'viewer_bg':  '#e8edf3',
    'sidebar_bg': '#dce8f8',
    'panel_bg':   '#ffffff',
    'card_bg':    '#e4ecf6',
    'input_bg':   '#ffffff',
    'border':     '#b8cce0',
    'input_brd':  '#98b4cc',
    'hover':      '#c0d8f0',
    'text':       '#0f1925',
    'dim':        '#4a6080',
    'vdim':       '#6888a0',
    'acc':        '#1f6feb',
    'btn_bg':     '#eef4fc',
    'btn_brd':    '#98b4cc',
    'sel_bg':     '#b0ccec',
    'splitter':   '#b8cce0',
}


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
