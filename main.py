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

# ── Moved into tools/shell/; imported back for main() ──────────────────────
from tools.shell.window import TOOLS, MainWindow
from tools.shell.titlebar import NavBtn, TitleBar
from tools.shell.settings import _total_ram_kb, _ram_percent_to_pages, _ram_percent_to_full_pages, _ram_percent_to_gb, AppSettings, _dlg_sep, _dlg_section, _dlg_buttons, AppearanceDialog, PerformanceDialog, GeneralDialog
from tools.shell.style import BG, SIDE, PANEL, ACC, TEXT, DIM, HOVER, LINE, _ACCENT, _build_style, STYLE, LIGHT_STYLE, AppStyle, _THEME_COLOURS, apply_theme_globally


# The accent, per theme: (base, hover, pressed). One blue family for both, but
# the dark theme needs the brighter end of it to separate from the navy chrome
# while the light theme needs the deeper end to stay readable as white-on-blue.


# ── RAM-Hilfsfunktion ─────────────────────────────────────────────────────────









# ── Stylesheets ───────────────────────────────────────────────────────────────






# ── Persistente Einstellungen ─────────────────────────────────────────────────



# ── Shared dialog helpers ─────────────────────────────────────────────────────





# ── Darstellung-Dialog ────────────────────────────────────────────────────────



# ── Leistung-Dialog ───────────────────────────────────────────────────────────



# ── Allgemein-Dialog ──────────────────────────────────────────────────────────



# ── Nav-Button ────────────────────────────────────────────────────────────────



# ── Custom Title Bar ──────────────────────────────────────────────────────────



# ── Hauptfenster ──────────────────────────────────────────────────────────────







_IPC_KEY = "copyshop_pdf_suite_single_instance"
# Marks a control line in the IPC message. A path can never begin with it.
_IPC_TOKEN_PREFIX = "\x01token="


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
    # Hand over our XDG activation token as well. The compositor gave it to
    # *this* process because the user just launched it, and it is the only thing
    # that lets the already-running instance legitimately raise itself on
    # Wayland. Sent as a control line so it can never be mistaken for a path.
    lines = list(paths)
    token = os.environ.get("XDG_ACTIVATION_TOKEN", "")
    if token:
        lines.insert(0, _IPC_TOKEN_PREFIX + token)
    # Always terminated by a newline so the receiver can tell "no files, just
    # raise the window" from a half-delivered message.
    sock.write(("\n".join(lines) + "\n").encode("utf-8"))
    # Make sure the bytes have actually left this process before the socket is
    # dropped: this call is immediately followed by the launcher exiting, and an
    # unflushed message means the file never opens in the running instance.
    # (waitForBytesWritten reports False when flush already sent everything, so
    # ask bytesToWrite instead of trusting its return value.)
    sock.flush()
    if sock.bytesToWrite():
        sock.waitForBytesWritten(2000)
    # Disconnect from this side. Waiting for the receiver to hang up first looks
    # tidier but does not work: the bytes only reach the other end's readyRead
    # once this socket closes, so waiting for a close that the receiver is
    # waiting on us for deadlocks until the timeout and the files never arrive.
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
        buf  = bytearray()
        done = []

        def _read():
            # The sender terminates its list with a newline; without buffering
            # until then, a list split across packets would be parsed as two
            # messages and the path straddling the split would be lost.
            if done:
                return
            buf.extend(bytes(conn.readAll()))
            if not buf.endswith(b"\n"):
                return
            done.append(True)
            data = bytes(buf).decode("utf-8", "replace"); buf.clear()
            token = ""
            paths = []
            for line in data.split("\n"):
                if line.startswith(_IPC_TOKEN_PREFIX):
                    token = line[len(_IPC_TOKEN_PREFIX):]
                elif line and os.path.isfile(line):
                    paths.append(line)
            # open_paths does the raising, so the window is activated exactly
            # once and with the token that came in with the files.
            win.open_paths(paths, token)
            conn.disconnectFromServer()

        def _finish():
            # Drain whatever arrived together with the close. A launcher that
            # writes and exits immediately can be gone before this side is even
            # scheduled — and if the event loop was busy at that moment (opening
            # the file the app was started with, say), the message was silently
            # dropped and that launch did nothing at all.
            _read()
            conn.deleteLater()

        conn.readyRead.connect(_read)
        # Let Qt reap the socket on its own event loop — deleting it while the
        # server is torn down mid-signal can take the process with it.
        conn.disconnected.connect(_finish)
        if conn.bytesAvailable():
            _read()      # data that arrived before readyRead was connected
        elif conn.state() == QLocalSocket.LocalSocketState.UnconnectedState:
            _finish()    # already closed before we got here

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
