"""
AppState — Zentraler PDF-Kontext
=================================
Singleton der den aktuellen Zustand der App hält.
Alle Tools holen sich die aktuelle PDF von hier,
statt selbst eine Datei-Dialog zu öffnen.

Verwendung:
    state = AppState.get()
    path  = state.current_pdf      # aktuell geöffnete PDF
    state.open_pdf(path)           # neue PDF öffnen
    state.open_result(path, title) # Ergebnis in neuem Tab öffnen
"""
from PyQt6.QtCore import QObject, pyqtSignal
from tools.i18n import tr

# Farbpalette — wird von main.py bei Theme-Wechsel aktualisiert
THEME = {
    "BG":    "#151520",
    "SIDE":  "#0e2d58",
    "PANEL": "#1c2340",
    "ACC":   "#4d8df5",
    "TEXT":  "#e8eaf0",
    "DIM":   "#7a8699",
    "HOVER": "#1e4d82",
    "LINE":  "#1e3354",
}

def theme_color(key: str) -> str:
    return THEME.get(key, "#000000")


class AppState(QObject):
    # Signals
    pdf_changed         = pyqtSignal(str)          # neue PDF geöffnet
    result_ready        = pyqtSignal(str, str)      # (pfad, tab_titel) — öffne in neuem Tab
    status_message      = pyqtSignal(str)           # Statuszeile aktualisieren
    current_page_changed = pyqtSignal(int)          # aktuell angezeigte Seite gewechselt (0-basiert)

    _instance = None

    def __init__(self):
        super().__init__()
        self._current_pdf  = ""
        self.page_model    = None   # aktuelles PageModel
        self._current_page = 0     # aktuell angezeigte Seite (0-basiert)

    @classmethod
    def get(cls) -> "AppState":
        if cls._instance is None:
            cls._instance = AppState()
        return cls._instance

    @property
    def current_pdf(self) -> str:
        return self._current_pdf

    @current_pdf.setter
    def current_pdf(self, path: str):
        self._current_pdf = path
        self.pdf_changed.emit(path)

    @property
    def current_page(self) -> int:
        return self._current_page

    @current_page.setter
    def current_page(self, idx: int):
        # Emit only on an actual change so panels (e.g. Crop/N-Up previews)
        # can refresh when the page picked in "Seiten verwalten" changes.
        changed = (idx != self._current_page)
        self._current_page = idx
        if changed:
            self.current_page_changed.emit(idx)

    def open_pdf(self, path: str):
        self.current_pdf = path

    def open_result(self, path: str, title: str = ""):
        """Signal an den Viewer: öffne dieses Ergebnis in einem neuen Tab."""
        if not title:
            import os
            title = os.path.basename(path)
        self.result_ready.emit(path, title)
        self.status_message.emit(tr('Ergebnis geöffnet: {p0}').format(p0=title))

    def has_pdf(self) -> bool:
        import os
        return bool(self._current_pdf) and os.path.isfile(self._current_pdf)
