"""
CopyShop PDF Suite v3
======================
Starten:  python3 main.py
          python3 main.py datei.pdf

Abhängigkeiten (Arch/CachyOS):
  sudo pacman -S python-pyqt6 python-pypdf python-pikepdf python-pillow \
                 python-reportlab python-img2pdf python-pdf2image \
                 python-pytesseract python-pypdfium2 python-pip \
                 tesseract tesseract-data-deu tesseract-data-eng \
                 ghostscript poppler
  pip install ocrmypdf --break-system-packages

Abhängigkeiten (Ubuntu/Debian):
  sudo apt install python3-pyqt6 python3-pypdf python3-pikepdf python3-pil \
                   python3-reportlab python3-img2pdf python3-pdf2image \
                   python3-pytesseract python3-pypdfium2 python3-pip \
                   tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
                   ghostscript poppler-utils
  pip3 install ocrmypdf --break-system-packages
"""

import sys, os, traceback, logging

# Keep the log inside the app's data directory (the same folder the installer
# uses) so everything stays contained in one place and is removed together by
# uninstall.sh — instead of dumping copyshop_crash.log into the user's $HOME.
# Falls back to the system temp dir if that location is not writable, so
# logging setup can never block startup.
def _init_log_file():
    data_dir = os.path.join(
        os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
        "copyshop_pdf_suite")
    try:
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "copyshop.log")
    except Exception:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "copyshop.log")

try:
    logging.basicConfig(
        filename=_init_log_file(),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s"
    )
except Exception:
    # Last resort: never let logging configuration crash the app.
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(message)s")

def _excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logging.critical(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)

sys.excepthook = _excepthook

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QStackedWidget, QLabel, QFrame, QFileDialog,
    QSizePolicy, QDialog, QRadioButton, QCheckBox, QSpinBox,
    QFormLayout, QScrollArea, QMessageBox, QMenu, QComboBox
)
from PyQt6.QtCore import Qt, QSettings, pyqtSignal, QPoint
from PyQt6.QtGui import QKeySequence, QShortcut, QFont, QAction
try:
    # Imported here, not lazily: loading this extension module later — once the
    # render threads are running — can segfault inside the import machinery.
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
except ImportError:                       # QtNetwork not installed
    QLocalServer = QLocalSocket = None

from tools.i18n          import tr, get_language, load_language, set_language
from tools.app_state     import AppState
from tools.page_viewer   import PageViewerPanel, shutdown_render_queue
from tools.all_tools     import (
    CompressPanel, CropResizePanel, PageNumbersPanel,
    ImgPdfPanel, GrayscalePanel, FormsPanel,
    OcrPanel, PreflightPanel, LayersPanel, ColourProfilePanel,
    ImposePanel, NUpPanel
)
from tools.plugin_manager import PluginManagerPanel, discover_plugins

# ── Farbpalette (Dunkel) ──────────────────────────────────────────────────────
BG     = "#1a1a2e"
SIDE   = "#0f3460"
PANEL  = "#16213e"
ACC    = "#3d82f0"
TEXT   = "#eaeaea"
DIM    = "#8892a4"
HOVER  = "#1a4a80"
LINE   = "#1e3a5a"

# The accent, per theme: (base, hover, pressed). One blue family for both, but
# the dark theme needs the brighter end of it to separate from the navy chrome
# while the light theme needs the deeper end to stay readable as white-on-blue.
_ACCENT = {
    True:  ("#4d8df5", "#6ba1f8", "#3a76d8"),   # dark
    False: ("#1f6feb", "#1a5fd0", "#1550b4"),   # light
}

TOOLS = [
    ("N-Up Layout",               NUpPanel),
    ("Broschüre / Ausschießen",   ImposePanel),
    ("Komprimieren",              CompressPanel),
    ("Zuschneiden / Skalieren",   CropResizePanel),
    ("Seitenzahlen",              PageNumbersPanel),
    ("Bild ↔ PDF",                ImgPdfPanel),
    ("Graustufen",                GrayscalePanel),
    ("Formulare / Reduzieren",    FormsPanel),
    ("OCR — Texterkennung",       OcrPanel),
    ("Druckvorstufenprüfung",     PreflightPanel),
    ("Ebenen (OCG)",              LayersPanel),
    ("Farbprofil / CMYK",         ColourProfilePanel),
    ("Plugin-Manager",            PluginManagerPanel),
]

# ── RAM-Hilfsfunktion ─────────────────────────────────────────────────────────

def _total_ram_kb() -> int:
    """Read total physical RAM in KB from /proc/meminfo, fallback 4 GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 4 * 1024 * 1024


def _ram_percent_to_pages(percent: int) -> int:
    """Convert a RAM-% to a thumbnail cache page count (~200 KB per thumb)."""
    allowed_kb = _total_ram_kb() * percent // 100
    return max(50, allowed_kb // 200)


def _ram_percent_to_full_pages(percent: int) -> int:
    """Convert a RAM-% to a full-page render cache count.
    Each full-page render is roughly 70 MB (4000 x 5660 px RGB).
    We reserve 1/4 of the allowed RAM for this cache and cap at 12
    so the app never monopolises memory on large files.
    """
    allowed_kb = _total_ram_kb() * percent // 100
    quarter_kb = allowed_kb // 4
    return max(2, min(12, quarter_kb // (70 * 1024)))


def _ram_percent_to_gb(percent: int) -> float:
    """Convert a RAM-% to the equivalent GB."""
    return _total_ram_kb() * percent / 100 / (1024 * 1024)


# ── Stylesheets ───────────────────────────────────────────────────────────────

def _build_style(dark: bool) -> str:
    if dark:
        _BG    = "#151520"; _PANEL = "#1c2340"; _TEXT  = "#e8eaf0"
        _DIM   = "#7a8699"; _HOVER = "#1e4d82"; _LINE  = "#1e3354"
        _IB    = "#1a2038"; _IBD   = "#2a4470"
        _LB    = "#2a4470"
        _SEL   = "#1e4d82"; _LHOV  = "#1a3050"
        _TAB_B = "#151520"; _TAB_P = "#1c2340"
        _SCB   = "#2e4e78"; _SCT   = "#151520"
        _SB    = "#0e2d58"; _SB_LOGO = "#0b1e3a"
        _NB_TEXT = "#7a8699"; _NB_HOV = "#162848"; _NB_ACT_TEXT = "#e8eaf0"
        _VB_BG = "#162848"; _VB_HOV = "#1e3a60"
        _BTN_DIS = "#2c2c3e"; _BTN_DIS_T = "#55607a"
        _GB    = "#1a2038"   # group box bg
    else:
        _BG    = "#edf1f7"; _PANEL = "#ffffff"; _TEXT  = "#0d1a28"
        _DIM   = "#556070"; _HOVER = "#d4e4f8"; _LINE  = "#c0d0e4"
        _IB    = "#ffffff"; _IBD   = "#a8c0d8"
        _LB    = "#a8c0d8"
        _SEL   = "#d4e4f8"; _LHOV  = "#eaf2fc"
        _TAB_B = "#edf1f7"; _TAB_P = "#ffffff"
        _SCB   = "#a8c0d8"; _SCT   = "#e0eaf4"
        _SB    = "#d8e8f8"; _SB_LOGO = "#bcd0ec"
        _NB_TEXT = "#446080"; _NB_HOV = "#a8c8ec"; _NB_ACT_TEXT = "#0d1a28"
        _VB_BG = "#c8ddf0"; _VB_HOV = "#b4ccec"
        _BTN_DIS = "#c0cede"; _BTN_DIS_T = "#88a0b4"
        _GB    = "#f6f9fd"

    _ACC, _ACC_HOV, _ACC_PRS = _ACCENT[dark]
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
QPushButton#titleBarBtn:last-child:hover {{ background: #c0392b; color: #ffffff; }}

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
    background: {_ACC}; color: #ffffff; border: none;
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

        def _cross(size=16, colour="#8892a4", alpha=255):
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
                    icon.addPixmap(_cross(colour="#e05260"), QIcon.Mode.Active)
                    return icon
                return super().standardIcon(standardIcon, option, widget)

        return _Style(QStyleFactory.create("Fusion"))


# ── Persistente Einstellungen ─────────────────────────────────────────────────

class AppSettings:
    """Singleton wrapper around QSettings — persists across restarts."""
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self._qs = QSettings("CopyShop", "PDFSuite")

    # Appearance
    def theme(self) -> str:
        return self._qs.value("appearance/theme", "dark")

    def set_theme(self, val: str):
        self._qs.setValue("appearance/theme", val)

    # Performance
    # Rendering speed preset: "balanced" | "fast" | "max"
    # Maps to (page_threads, thumb_threads)
    _SPEED_PRESETS = {
        "balanced": (2, 4),
        "fast":     (4, 6),
        "max":      (8, 8),
    }

    def prerender(self) -> bool:
        return self._qs.value("performance/prerender", True, type=bool)

    def set_prerender(self, val: bool):
        self._qs.setValue("performance/prerender", bool(val))

    def speed_preset(self) -> str:
        return self._qs.value("performance/speed_preset", "balanced")

    def set_speed_preset(self, val: str):
        self._qs.setValue("performance/speed_preset", val)

    def render_threads(self) -> int:
        return self._SPEED_PRESETS.get(self.speed_preset(), (2, 4))[0]

    def thumb_threads(self) -> int:
        return self._SPEED_PRESETS.get(self.speed_preset(), (2, 4))[1]

    def ram_percent(self) -> int:
        return int(self._qs.value("performance/ram_percent", 25))

    def set_ram_percent(self, val: int):
        self._qs.setValue("performance/ram_percent", int(val))

    # General
    def reopen_last(self) -> bool:
        return self._qs.value("general/reopen_last", False, type=bool)

    def set_reopen_last(self, val: bool):
        self._qs.setValue("general/reopen_last", bool(val))

    def last_file(self) -> str:
        return self._qs.value("general/last_file", "")


# ── Shared dialog helpers ─────────────────────────────────────────────────────

def _dlg_sep():
    f = QFrame(); f.setObjectName("separator")
    f.setFrameShape(QFrame.Shape.HLine)
    return f

def _dlg_section(layout, title):
    lbl = QLabel(tr(title))
    lbl.setObjectName("sectionLabel")
    lbl.setContentsMargins(0, 4, 0, 2)
    layout.addWidget(lbl)

def _dlg_buttons(layout, save_slot):
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    cancel = QPushButton(tr("Abbrechen"))
    cancel.setObjectName("secondaryBtn")
    save = QPushButton(tr("Speichern"))
    save.setObjectName("actionBtn")
    save.clicked.connect(save_slot)
    btn_row.addWidget(cancel)
    btn_row.addWidget(save)
    layout.addLayout(btn_row)
    return cancel  # caller connects cancel.clicked.connect(self.reject)


# ── Darstellung-Dialog ────────────────────────────────────────────────────────

class AppearanceDialog(QDialog):
    theme_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Darstellung"))
        self.setMinimumWidth(380)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "DARSTELLUNG")

        theme_row = QHBoxLayout()
        theme_row.setSpacing(20)
        lbl = QLabel(tr("Farbschema:"))
        lbl.setMinimumWidth(160)
        theme_row.addWidget(lbl)
        self._dark_rb  = QRadioButton(tr("Dunkel"))
        self._light_rb = QRadioButton(tr("Hell"))
        if self._s.theme() == "light":
            self._light_rb.setChecked(True)
        else:
            self._dark_rb.setChecked(True)
        theme_row.addWidget(self._dark_rb)
        theme_row.addWidget(self._light_rb)
        theme_row.addStretch()
        outer.addLayout(theme_row)

        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _save(self):
        s = self._s
        new_theme = "light" if self._light_rb.isChecked() else "dark"
        changed = (new_theme != s.theme())
        s.set_theme(new_theme)
        if changed:
            self.theme_changed.emit(new_theme)
        self.accept()


# ── Leistung-Dialog ───────────────────────────────────────────────────────────

class PerformanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Leistung"))
        self.setMinimumWidth(500)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "LEISTUNG")

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)

        self._prerender_cb = QCheckBox(tr("Seiten im Hintergrund vorab rendern"))
        self._prerender_cb.setChecked(self._s.prerender())
        form.addRow(tr("Vorab-Rendering:"), self._prerender_cb)

        self._speed_combo = QComboBox()
        _speed_items = [
            ("balanced", tr("Ausgewogen  (empfohlen)")),
            ("fast",     tr("Schnell")),
            ("max",      tr("Maximum  (alle Kerne)")),
        ]
        for key, label in _speed_items:
            self._speed_combo.addItem(label, key)
        cur_preset = self._s.speed_preset()
        idx = next((i for i, (k, _) in enumerate(_speed_items) if k == cur_preset), 0)
        self._speed_combo.setCurrentIndex(idx)
        form.addRow(tr("Rendering-Geschwindigkeit:"), self._speed_combo)

        self._ram_spin = QSpinBox()
        self._ram_spin.setRange(5, 80)
        self._ram_spin.setSingleStep(5)
        self._ram_spin.setValue(self._s.ram_percent())
        self._ram_spin.setSuffix("  % RAM")
        self._ram_spin.setMaximumWidth(140)
        self._ram_hint = QLabel()
        self._ram_hint.setObjectName("dimLabel")
        self._ram_spin.valueChanged.connect(self._update_ram_hint)
        self._update_ram_hint(self._ram_spin.value())
        cache_col = QVBoxLayout()
        cache_col.setSpacing(2)
        cache_col.addWidget(self._ram_spin)
        cache_col.addWidget(self._ram_hint)
        form.addRow(tr("Thumbnail-Cache:"), cache_col)

        outer.addLayout(form)
        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _update_ram_hint(self, pct: int):
        self._ram_hint.setText(f"≈ {_ram_percent_to_gb(pct):.1f} GB")

    def _save(self):
        s = self._s
        s.set_prerender(self._prerender_cb.isChecked())
        s.set_speed_preset(self._speed_combo.currentData())
        s.set_ram_percent(self._ram_spin.value())
        from tools.page_viewer import apply_performance_settings
        apply_performance_settings(
            prerender       = s.prerender(),
            render_threads  = s.render_threads(),
            thumb_threads   = s.thumb_threads(),
            cache_size      = _ram_percent_to_pages(s.ram_percent()),
            full_page_cache = _ram_percent_to_full_pages(s.ram_percent()),
        )
        self.accept()


# ── Allgemein-Dialog ──────────────────────────────────────────────────────────

class GeneralDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Allgemein"))
        self.setMinimumWidth(380)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "ALLGEMEIN")

        self._reopen_cb = QCheckBox(tr("Letzte Datei beim Programmstart automatisch oeffnen"))
        self._reopen_cb.setChecked(self._s.reopen_last())
        outer.addWidget(self._reopen_cb)

        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _save(self):
        self._s.set_reopen_last(self._reopen_cb.isChecked())
        self.accept()


# ── Nav-Button ────────────────────────────────────────────────────────────────

class NavBtn(QPushButton):
    def __init__(self, text, viewer=False, parent=None):
        super().__init__(text, parent)
        self.setObjectName("viewerBtn" if viewer else "navBtn")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("active", "false")

    def set_active(self, active: bool):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self); self.style().polish(self)


# ── Custom Title Bar ──────────────────────────────────────────────────────────

class TitleBar(QWidget):
    """Frameless custom title bar: drag area + menu bar + window controls."""

    def __init__(self, window: "MainWindow", parent=None):
        super().__init__(parent)
        self._win = window
        self._drag_pos = None
        self.setObjectName("titleBar")
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(0)

        # App title
        title = QLabel(tr("CopyShop PDF Suite"))
        title.setObjectName("titleBarLabel")
        layout.addWidget(title)

        layout.addSpacing(16)

        # Menu bar embedded in title bar
        self.menu_bar = self._build_menu()
        layout.addWidget(self.menu_bar)

        layout.addStretch()

        # Window controls
        for symbol, tip, slot in [
            ("─", "Minimieren",    window.showMinimized),
            ("□", "Maximieren",    self._toggle_max),
            ("✕", "Schließen",     window.close),
        ]:
            btn = QPushButton(symbol)
            btn.setObjectName("titleBarBtn")
            btn.setToolTip(tr(tip))
            btn.setFixedSize(42, 42)
            btn.clicked.connect(slot)
            layout.addWidget(btn)

    def _build_menu(self):
        from PyQt6.QtWidgets import QMenuBar
        mb = QMenuBar(self)
        mb.setObjectName("titleMenuBar")

        # Datei
        menu_file = mb.addMenu(tr("Datei"))
        act_open = QAction(tr("PDF öffnen…"), self)
        act_open.setShortcut(QKeySequence("Ctrl+O"))
        act_open.triggered.connect(self._win._open_dialog)
        menu_file.addAction(act_open)
        act_multi = QAction(tr("Mehrere PDFs öffnen…"), self)
        act_multi.triggered.connect(self._win._open_multi_dialog)
        menu_file.addAction(act_multi)
        menu_file.addSeparator()
        # Saving belongs to the document, not to the page manager — it used to
        # live only in that sidebar, so it was unreachable from the normal view
        # even though Strg+S was already wired up there.
        # Resolved when triggered, not now: the title bar is built before
        # MainWindow creates .viewer.
        act_save = QAction(tr("Speichern"), self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(lambda: self._win.viewer._save_current())
        menu_file.addAction(act_save)
        act_save_as = QAction(tr("Speichern unter…"), self)
        act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        act_save_as.triggered.connect(lambda: self._win.viewer._save_as_current())
        menu_file.addAction(act_save_as)
        menu_file.addSeparator()
        act_quit = QAction(tr("Beenden"), self)
        act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        act_quit.triggered.connect(self._win.close)
        menu_file.addAction(act_quit)

        # Einstellungen
        menu_settings = mb.addMenu(tr("Einstellungen"))
        act_appear = QAction(tr("Darstellung…"), self)
        act_appear.triggered.connect(self._win._open_appearance)
        menu_settings.addAction(act_appear)
        act_perf = QAction(tr("Leistung…"), self)
        act_perf.setShortcut(QKeySequence("Ctrl+,"))
        act_perf.triggered.connect(self._win._open_performance)
        menu_settings.addAction(act_perf)
        act_general = QAction(tr("Allgemein…"), self)
        act_general.triggered.connect(self._win._open_general)
        menu_settings.addAction(act_general)

        # Sprache submenu
        menu_lang = menu_settings.addMenu(tr("Sprache"))
        act_de = QAction(tr("Deutsch"), self)
        act_de.setCheckable(True)
        act_de.setChecked(get_language() == "de")
        act_de.triggered.connect(lambda: self._win._set_language("de"))
        menu_lang.addAction(act_de)
        act_en = QAction(tr("English"), self)
        act_en.setCheckable(True)
        act_en.setChecked(get_language() == "en")
        act_en.triggered.connect(lambda: self._win._set_language("en"))
        menu_lang.addAction(act_en)

        # Hilfe
        menu_help = mb.addMenu(tr("Hilfe"))
        act_about = QAction(tr("Über CopyShop PDF Suite"), self)
        act_about.triggered.connect(self._win._show_about)
        menu_help.addAction(act_about)

        return mb

    def _toggle_max(self):
        if self._win.isMaximized():
            self._win.showNormal()
        else:
            self._win.showMaximized()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            if self._win.isMaximized():
                ratio = e.globalPosition().x() / max(1, self._win.width())
                self._win.showNormal()
                self._drag_pos = QPoint(int(self._win.width() * ratio), self.height() // 2)
            self._win.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._toggle_max()


# ── Hauptfenster ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, open_file=None, open_files=None):
        super().__init__()
        self.setWindowTitle(tr("CopyShop PDF Suite"))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(1000, 640)
        self.resize(1280, 760)
        self._build()
        if open_file:
            self._switch(0)
            self.viewer.open_file(open_file)
        elif open_files:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(100, lambda: self._open_multi(open_files))

    def open_paths(self, paths):
        """Open files in THIS window — one file becomes a tab, several go through
        the open/merge chooser. Called when another launch forwards its files to
        the running instance (see _listen_for_open_requests)."""
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return
        self._raise_to_front()
        if len(paths) == 1:
            self._switch(0)
            self.viewer.open_file(paths[0])
        else:
            self._open_multi(paths)

    def _raise_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show(); self.raise_(); self.activateWindow()

    def _open_multi(self, files):
        """Several files at once go straight to the merge preview, which offers
        both "merge" and "open separately".

        There used to be a modal chooser in front of it. Clicking its merge
        button faster than the preview could be built queued the click again
        and stacked up one merge tab per click, and confirming a second merge
        while the first was still converting destroyed a running QThread —
        an instant abort. The preview is the only step now."""
        from tools.multi_open import classify
        files = [f for f in files if os.path.isfile(f) and classify(f)]
        if not files:
            return
        self._switch(0)
        if len(files) == 1:
            self.viewer.open_file(files[0])
        else:
            self.viewer.show_merge_tab(files)

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Custom Title Bar ──────────────────────────────────────────────────
        self._title_bar = TitleBar(self)
        outer.addWidget(self._title_bar)

        # ── Body (sidebar + main) ─────────────────────────────────────────────
        body = QWidget()
        root = QHBoxLayout(body)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addWidget(body, 1)

        # ── Seitenleiste ─────────────────────────────────────────────────────
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(210)
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(0, 6, 0, 0)
        sb.setSpacing(0)

        self._btns  = []
        self._stack = QStackedWidget()

        # Page Viewer Button (hervorgehoben)
        vb = NavBtn(tr("Seiten-Viewer"), viewer=True)
        vb.clicked.connect(lambda: self._switch(0))
        sb.addWidget(vb); self._btns.append(vb)

        self.viewer = PageViewerPanel()
        self._stack.addWidget(self.viewer)
        self.viewer.tab_opened.connect(lambda: self._switch(0))
        self.viewer.switch_to_viewer   = lambda: self._switch(0)
        self.viewer.get_main_stack_idx = lambda: self._stack.currentIndex()
        self.viewer.restore_main_idx   = lambda idx: self._switch(idx)

        # Tools
        tl = QLabel("  " + tr("WERKZEUGE"))
        tl.setObjectName("sectionLabel")
        tl.setContentsMargins(14, 8, 0, 2)
        sb.addWidget(tl)

        for i, (label, PanelClass) in enumerate(TOOLS):
            btn = NavBtn(tr(label))
            idx = i + 1
            btn.clicked.connect(lambda c, x=idx: self._switch(x))
            sb.addWidget(btn); self._btns.append(btn)
            self._stack.addWidget(PanelClass())

        # Plugins
        plugins = discover_plugins()
        if plugins:
            sep = QFrame(); sep.setObjectName("separator")
            sep.setFrameShape(QFrame.Shape.HLine); sb.addWidget(sep)
            pl = QLabel("  " + tr("PLUGINS")); pl.setObjectName("sectionLabel")
            pl.setContentsMargins(14, 10, 0, 4); sb.addWidget(pl)
            base = len(TOOLS) + 1
            for pi, (plabel, PCls) in enumerate(plugins):
                btn = NavBtn(plabel.strip())
                idx = base + pi
                btn.clicked.connect(lambda c, x=idx: self._switch(x))
                sb.addWidget(btn); self._btns.append(btn)
                self._stack.addWidget(PCls())

        # ── Version am unteren Rand ───────────────────────────────────────────
        sb.addStretch()

        ver = QLabel(tr("v3.0  —  open source"))
        ver.setObjectName("dimLabel"); ver.setContentsMargins(14, 0, 0, 8)
        sb.addWidget(ver)

        root.addWidget(sidebar)
        self._sidebar = sidebar   # keep reference for manage-mode hide/show

        # ── Hauptbereich ─────────────────────────────────────────────────────
        main_col = QVBoxLayout()
        main_col.setContentsMargins(0, 0, 0, 0)
        main_col.setSpacing(0)
        main_col.addWidget(self._stack)

        wrapper = QWidget(); wrapper.setLayout(main_col)
        root.addWidget(wrapper, 1)

        # Wire sidebar hide/show into the page viewer
        self.viewer.hide_sidebar = lambda: self._sidebar.setVisible(False)
        self.viewer.show_sidebar = lambda: self._sidebar.setVisible(True)

        self._switch(0)

    def _switch(self, idx: int):
        for i, btn in enumerate(self._btns):
            btn.set_active(i == idx)
        self._stack.setCurrentIndex(idx)

    def _open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("PDF öffnen"), "", tr("PDF Dateien (*.pdf)"))
        if path:
            self._switch(0)
            self.viewer.open_file(path)

    def _open_multi_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Mehrere PDFs öffnen"), "", tr("PDF Dateien (*.pdf)"))
        if paths:
            if len(paths) == 1:
                self._switch(0)
                self.viewer.open_file(paths[0])
            else:
                self._open_multi(paths)

    def _show_about(self):
        QMessageBox.about(
            self,
            tr("Über CopyShop PDF Suite"),
            "<b>CopyShop PDF Suite v3</b><br><br>"
            + tr("Professionelles PDF-Werkzeug für Copyshops und Druckvorstufe.") + "<br><br>"
            + tr("Entwickelt mit Python · PyQt6 · pypdfium2 · pikepdf · pypdf") + "<br><br>"
            "<i>" + tr("open source") + "</i>"
        )

    def _set_language(self, lang: str):
        set_language(lang)
        import os
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _open_appearance(self):
        dlg = AppearanceDialog(self)
        dlg.theme_changed.connect(self._apply_theme)
        dlg.exec()

    def _open_performance(self):
        PerformanceDialog(self).exec()

    def _open_general(self):
        GeneralDialog(self).exec()

    def _apply_theme(self, theme: str):
        apply_theme_globally(theme)


_THEME_COLOURS = {
    "light": {"BG": "#edf1f7", "SIDE": "#d8e8f8", "PANEL": "#ffffff",
              "ACC": "#1f6feb", "TEXT": "#0d1a28", "DIM": "#556070",
              "HOVER": "#d4e4f8", "LINE": "#c0d0e4"},
    "dark":  {"BG": "#151520", "SIDE": "#0e2d58", "PANEL": "#1c2340",
              "ACC": "#4d8df5", "TEXT": "#e8eaf0", "DIM": "#7a8699",
              "HOVER": "#1e4d82", "LINE": "#1e3354"},
}


def apply_theme_globally(theme: str):
    """Push `theme` into every place that holds colours: the app stylesheet, the
    viewer palette (_TV, which re-styles the registered panels) and
    app_state.THEME.

    Startup used to set only the first two, so on a light-mode launch every
    widget styled through theme_color() kept the dark defaults — the Greyscale
    preview area most visibly."""
    QApplication.instance().setStyleSheet(LIGHT_STYLE if theme == "light" else STYLE)
    import tools.app_state as _as
    _as.THEME.update(_THEME_COLOURS["light" if theme == "light" else "dark"])
    from tools.page_viewer import set_viewer_theme
    set_viewer_theme(theme)          # last: it re-runs every panel's _apply_theme


_IPC_KEY = "copyshop_pdf_suite_single_instance"


def _forward_to_running_instance(paths) -> bool:
    """If the app is already running, hand the files to that instance and return
    True — opening a PDF from the file manager should add a tab to the window
    that is already open, not start a second copy of the app."""
    if QLocalSocket is None:
        return False
    sock = QLocalSocket()
    sock.connectToServer(_IPC_KEY)
    if not sock.waitForConnected(300):
        return False
    # Always terminated by a newline so the receiver can tell "no files, just
    # raise the window" from a half-delivered message.
    sock.write(("\n".join(paths) + "\n").encode("utf-8"))
    # Make sure the bytes have actually left this process before the socket is
    # dropped: this call is immediately followed by the launcher exiting, and an
    # unflushed message means the file never opens in the running instance.
    # (waitForBytesWritten reports False when flush already sent everything, so
    # ask bytesToWrite instead of trusting its return value.)
    sock.flush()
    if sock.bytesToWrite():
        sock.waitForBytesWritten(2000)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(1000)
    return True


def _listen_for_open_requests(win):
    """Serve the other end of the above: every later launch delivers its file
    list here and we open it in this window."""
    if QLocalServer is None:
        return None

    def _on_connection():
        # Drain every queued connection: two launches in quick succession (e.g.
        # double-clicking two PDFs) can land before this handler runs, and
        # taking only one per signal silently dropped the other file.
        while server.hasPendingConnections():
            _serve(server.nextPendingConnection())

    def _serve(conn):
        if conn is None: return
        buf = bytearray()

        def _read():
            # The sender terminates its list with a newline; without buffering
            # until then, a list split across packets would be parsed as two
            # messages and the path straddling the split would be lost.
            buf.extend(bytes(conn.readAll()))
            if not buf.endswith(b"\n"):
                return
            data = bytes(buf).decode("utf-8", "replace"); buf.clear()
            win._raise_to_front()
            paths = [p for p in data.split("\n") if p and os.path.isfile(p)]
            if paths:
                win.open_paths(paths)
            conn.disconnectFromServer()

        conn.readyRead.connect(_read)
        # Let Qt reap the socket on its own event loop — deleting it while the
        # server is torn down mid-signal can take the process with it.
        conn.disconnected.connect(conn.deleteLater)
        if conn.bytesAvailable():
            _read()      # data that arrived before readyRead was connected

    # A crashed instance leaves the socket file behind; removeServer clears it.
    QLocalServer.removeServer(_IPC_KEY)
    server = QLocalServer()
    server.newConnection.connect(_on_connection)
    server.listen(_IPC_KEY)
    win._ipc_server = server          # keep it alive with the window
    return server


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("CopyShop PDF Suite")
    app.setStyle(AppStyle.create())

    # Stop the render worker before anything gets torn down, so a task can't
    # finish and emit into receivers we are about to delete below.
    app.aboutToQuit.connect(shutdown_render_queue)

    # Hand the files to an already-running instance and quit, before building
    # any UI. With no files this just raises the existing window — the app is
    # tab-based, so a second launch should never mean a second window.
    _cli_files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    try:
        if _forward_to_running_instance(_cli_files):
            return
    except Exception:
        pass   # no running instance reachable — carry on and open normally

    load_language()   # must be after QApplication — QSettings needs it

    # Apply persisted settings before building the window
    s = AppSettings.get()
    apply_theme_globally(s.theme())

    from tools.page_viewer import apply_performance_settings
    apply_performance_settings(
        prerender        = s.prerender(),
        render_threads   = s.render_threads(),
        thumb_threads    = s.thumb_threads(),
        cache_size       = _ram_percent_to_pages(s.ram_percent()),
        full_page_cache  = _ram_percent_to_full_pages(s.ram_percent()),
    )

    # App-Icon
    from PyQt6.QtGui import QPixmap, QPainter, QColor, QPen, QBrush, QIcon, QPolygon
    from PyQt6.QtCore import QPoint
    icon_pm = QPixmap(64, 64)
    icon_pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(icon_pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor("#1a1a2e")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, 64, 64, 10, 10)
    p.setBrush(QBrush(QColor("#eaeaea")))
    p.drawRoundedRect(12, 8, 32, 42, 3, 3)
    p.setBrush(QBrush(QColor("#1a1a2e")))
    p.drawPolygon(QPolygon([QPoint(36,8), QPoint(44,8), QPoint(44,16)]))
    p.setBrush(QBrush(QColor("#cccccc")))
    p.drawPolygon(QPolygon([QPoint(36,8), QPoint(44,16), QPoint(36,16)]))
    p.setPen(QPen(QColor(ACC), 2))
    p.drawLine(18, 24, 38, 24)
    p.drawLine(18, 30, 38, 30)
    p.drawLine(18, 36, 30, 36)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(ACC)))
    p.drawEllipse(38, 42, 14, 14)
    p.end()
    app.setWindowIcon(QIcon(icon_pm))

    args = _cli_files

    # Reopen last file if the setting is enabled and no file was passed on CLI
    if not args and s.reopen_last() and s.last_file() and os.path.isfile(s.last_file()):
        win = MainWindow(open_file=s.last_file())
    elif len(args) > 1:
        win = MainWindow(open_files=args)
    elif len(args) == 1:
        win = MainWindow(open_file=args[0])
    else:
        win = MainWindow()

    win.show()
    try:
        _listen_for_open_requests(win)
    except Exception:
        pass   # QtNetwork unavailable — the app still works, just not shared
    rc = app.exec()

    # Tear the widget tree down here, while the event loop and the interpreter
    # are both still healthy. Left alive, it was PyQt's own cleanup_on_exit
    # atexit hook that destroyed it during interpreter finalisation, and that
    # walk hit a wrapper whose C++ object was already gone — every single quit
    # ended in a segfault inside sip_api_get_address. deleteLater + one event
    # loop pass disposes of it in the normal Qt order instead.
    win.deleteLater()
    app.processEvents()
    del win
    sys.exit(rc)


if __name__ == "__main__":
    main()
