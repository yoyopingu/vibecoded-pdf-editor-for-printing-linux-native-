"""
The icon set, drawn rather than typed.

Every glyph button in the app used to show a text character (─ □ ✕ ⊞ ⟳), which
rendered differently on every font and read as a placeholder. The shapes here
are the ones prototyped in docs/gui-concept.html's sprite — one drawn set,
16 units square, stroke-only. When the concept's sprite changes, the bodies in
_ICONS change with it; that file is the source of truth and this is the port.

Colour is resolved at call time: the default is the live shell theme's TEXT,
so an icon built after a theme switch comes out in the new theme — but one
already handed to a widget keeps its colour until re-set. That is the same
deal the stylesheet offers, and the reason callers should build their icons
once and rebuild them on theme change (nothing does yet — no callers exist).
"""
from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from tools.app_state import theme_color


# Inner markup of each <symbol> in the concept's sprite, verbatim. The stroke
# colour and width live on the outer <svg> element that icon() wraps them in —
# the sprite's own CSS equivalent (stroke:currentColor, 1.7, round caps).
_ICONS = {
    "doc":      '<path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9.5 1.5V5H13"/>'
                '<path d="M6 8.5h4M6 11h4"/>',
    "search":   '<circle cx="7" cy="7" r="4.5"/><path d="m10.5 10.5 3.5 3.5"/>',
    "print":    '<path d="M4.5 6V1.5h7V6"/><rect x="2" y="6" width="12" height="6" rx="1.5"/>'
                '<path d="M4.5 11h7v3.5h-7z"/>',
    "save":     '<path d="M8 1.5v8"/><path d="m4.5 6 3.5 3.5L11.5 6"/>'
                '<path d="M2 11v2a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 14 13v-2"/>',
    "edit":     '<path d="M9.5 2.5 13.5 6.5 5.5 14.5H1.5v-4z"/><path d="m8 4 4 4"/>',
    "chev":     '<path d="m4 6 4 4 4-4"/>',
    "plus":     '<path d="M8 3v10M3 8h10"/>',
    "minus":    '<path d="M3 8h10"/>',
    "fit":      '<path d="M2 5V2h3M14 5V2h-3M2 11v3h3M14 11v3h-3"/>'
                '<rect x="5.5" y="5.5" width="5" height="5" rx="1"/>',
    "prev":     '<path d="m4 10 4-4 4 4"/>',
    "next":     '<path d="m4 6 4 4 4-4"/>',
    "close":    '<path d="m4 4 8 8M12 4l-8 8"/>',
    "min":      '<path d="M3 12h10"/>',
    "max":      '<rect x="3.5" y="3.5" width="9" height="9" rx="1"/>',
    "sun":      '<circle cx="8" cy="8" r="3.2"/>'
                '<path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1'
                'M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1"/>',
    "moon":     '<path d="M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7z"/>',
    "rotl":     '<path d="M3 8a5 5 0 1 0 1.5-3.5"/><path d="M3 2v3h3"/>',
    "rotr":     '<path d="M13 8a5 5 0 1 1-1.5-3.5"/><path d="M13 2v3h-3"/>',
    "trash":    '<path d="M2.5 4h11M6.5 4V2.5h3V4M4 4l.7 9.5h6.6L12 4M6.7 7v4M9.3 7v4"/>',
    "copy":     '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/>'
                '<path d="M10.5 5.5v-2a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>',
    "paste":    '<rect x="4.5" y="2.5" width="7" height="11" rx="1.5"/>'
                '<path d="M6.5 2.5V1.5h3v1"/><path d="M7 8.5h2M7 11h2"/>',
    "undo":     '<path d="M3.5 7H11a3.5 3.5 0 0 1 0 7H8"/><path d="M6.5 4 3.5 7l3 3"/>',
    "scissors": '<circle cx="4" cy="12.5" r="2"/><circle cx="4" cy="3.5" r="2"/>'
                '<path d="M5.7 4.7 13 13M5.7 11.3 13 3"/>',
    "fileplus": '<path d="M4 1.5h5.5L13 5v9.5H4z"/><path d="M9.5 1.5V5H13"/>'
                '<path d="M8 8v4M6 10h4"/>',
    "files":    '<path d="M5.5 1.5H11l2 2v8"/><path d="M3 5.5h5.5L11 8v6.5H3z"/>',
    "split":    '<path d="M2 8h4M10 8h4"/><path d="M8 2v3M8 11v3M6 5l2-3 2 3M6 11l2 3 2-3"/>',
    "grid":     '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/>'
                '<rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
    "crop":     '<path d="M5 1v10h10"/><path d="M1 5h10v10"/>',
    "marks":    '<path d="M2 5V2h3M14 5V2h-3M2 11v3h3M14 11v3h-3"/><path d="M8 5v6M5 8h6"/>',
    "check":    '<path d="M3.5 8.5 6.5 11.5 12.5 4.5"/>',
    "layers":   '<path d="m8 1.5 6.5 3.5L8 8.5 1.5 5z"/><path d="m2.5 8.5 5.5 3 5.5-3"/>'
                '<path d="m2.5 11.5 5.5 3 5.5-3"/>',
    "drop":     '<path d="M8 1.5s4.5 5 4.5 8a4.5 4.5 0 0 1-9 0c0-3 4.5-8 4.5-8z"/>',
    "num":      '<path d="M4.5 3.5 7 2v10"/><path d="M10 3.5h3.5L10.5 8h3"/>',
    "form":     '<rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M5 6h6M5 9h4"/>',
    "eye":      '<path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z"/>'
                '<circle cx="8" cy="8" r="2"/>',
    "shield":   '<path d="M8 1.5 13.5 3.5V8c0 3.5-2.5 5.5-5.5 6.5C5 13.5 2.5 11.5 2.5 8V3.5z"/>'
                '<path d="m5.8 7.8 1.6 1.6 3-3.2"/>',
    "export":   '<path d="M8 10V2M5 5l3-3 3 3"/>'
                '<path d="M2.5 10.5v2a1.5 1.5 0 0 0 1.5 1.5h8a1.5 1.5 0 0 0 1.5-1.5v-2"/>',
    "zip":      '<path d="M8 1.5v3M8 14.5v-3M1.5 8h3M14.5 8h-3"/>'
                '<rect x="5.5" y="5.5" width="5" height="5" rx="1"/>',
    "puzzle":   '<path d="M5.5 2.5a1.5 1.5 0 0 1 3 0c0 .5-.5 1-.5 1.5h4v4c.5 0 1-.5 1.5-.5'
                'a1.5 1.5 0 0 1 0 3c-.5 0-1-.5-1.5-.5v4h-4c0-.5.5-1 .5-1.5a1.5 1.5 0 0 0-3 0'
                'c0 .5.5 1 .5 1.5h-4v-11h4c-.5 0-1-.5-1-.5z" transform="translate(0 0)"/>',
    "ruler":    '<rect x="1.5" y="5" width="13" height="6" rx="1" transform="rotate(-45 8 8)"/>'
                '<path d="M6 6.5 7 7.5M8 4.5l1 1M10 6.5l1 1"/>',
    "select":   '<path d="M3 2.5 13 8.5l-4.5.8L5.5 13z"/>',
}


def _device_ratio():
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    ratios = [s.devicePixelRatio() for s in app.screens()]
    return max(ratios) if ratios else 1.0


def icon(name, colour=None, size=16):
    """One of the concept's glyphs as a QIcon.

    `name` is the sprite id minus its "i-" prefix ("doc", "rotl", …), `colour`
    a hex string defaulting to the live shell theme's TEXT, `size` the logical
    pixel size. The stroke stays 1.7 units in the 16-unit viewBox whatever the
    size — the concept's .sm/.lg variants scale the whole drawing too.
    """
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
        'fill="none" stroke="{}" stroke-width="1.7" '
        'stroke-linecap="round" stroke-linejoin="round">{}</svg>'
    ).format(theme_color("TEXT") if colour is None else colour, _ICONS[name])
    dpr = _device_ratio()
    pm = QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p)
    p.end()
    return QIcon(pm)
