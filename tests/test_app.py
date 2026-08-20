"""
App.
"""
import os, sys, time
from tools.panels.compress import CompressPanel
from tools.panels.crop_resize import CropResizePanel
from tools.panels.grayscale import GrayscalePanel
from tools.panels.nup import NUpPanel
from tools.panels.page_numbers import PageNumbersPanel
import tools.app as MAIN
import tools.shell.instance as INSTANCE
from tests.support import FX, _TMP, _app, _open, _spin


_HOST_SCRIPT = '''
import sys, os
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
app = QApplication([])
import tools.app as MAIN
import tools.shell.instance as INSTANCE
INSTANCE._IPC_KEY = {key!r}   # own socket, so a real running app is untouched
win = MAIN.MainWindow(open_file={src!r})
win.show()
seen = []
win._open_multi = lambda files: (seen.append(len(files)),
                                 print("MULTI", len(files), flush=True))
raises = []
_raise = win._raise_to_front
def _raise_to_front(activation_token=""):
    raises.append(activation_token)
    print("TOKEN", activation_token or "-", "x%d" % len(raises), flush=True)
    _raise(activation_token)
win._raise_to_front = _raise_to_front
# open_paths defers the actual open to the next event-loop turn, so report the
# tab count from there rather than straight after the call.
_fwd = win._open_forwarded
def _open_forwarded(paths):
    _fwd(paths)
    if len(paths) == 1:
        print("TABS", win.viewer.tabs.count(), "raises=%d" % len(raises), flush=True)
win._open_forwarded = _open_forwarded
MAIN._listen_for_open_requests(win)
print("READY", int(win._ipc_server.isListening()), flush=True)
QTimer.singleShot(20000, lambda: os._exit(2))
app.exec()
'''


_EXIT_SCRIPT = '''
import sys, os
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.argv = ["copyshop", {src!r}]
import tools.app as MAIN
import tools.shell.instance as INSTANCE
INSTANCE._IPC_KEY = {key!r}   # own socket, so a real running app is untouched
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

# Quit the way a user does, once the window is up and rendering.
_show = MAIN.MainWindow.show
def show(self):
    _show(self)
    QTimer.singleShot(1500, QApplication.instance().quit)
MAIN.MainWindow.show = show
try:
    MAIN.main()
except SystemExit as e:
    print("RC", e.code, flush=True)
'''


def test_app_exits_without_crashing():
    """Quitting must not dump core.

    The widget tree used to survive until interpreter finalisation, where PyQt's
    own cleanup_on_exit atexit hook destroyed it — that walk dereferenced a
    wrapper whose C++ object had already gone and segfaulted inside
    sip_api_get_address. It hit *every* quit, and only showed up as a core dump
    after the window had already vanished. Has to run as a real process: the
    fault is in how this one shuts down."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(_TMP, "exit_host.py")
    with open(script, "w") as f:
        f.write(_EXIT_SCRIPT.format(repo=repo, src=FX["normal"],
                                    key=f"copyshop_exit_{os.getpid()}"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    runs = []
    for _ in range(3):          # the crash was racy — one clean run proves little
        p = subprocess.run([sys.executable, "-u", script], env=env,
                           capture_output=True, text=True, timeout=120)
        runs.append(p.returncode)
    bad = [rc for rc in runs if rc != 0]
    assert not bad, (f"app exited with {runs} "
                     f"(negative = killed by signal, -11 = SIGSEGV)")
    return f"{len(runs)} clean exits"


def _expect_line(proc, prefix, timeout=25.0):
    """Read the child's stdout until a line starts with `prefix`."""
    end = time.time() + timeout
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith(prefix):
            return line
    raise AssertionError(f"child never reported {prefix!r}")


def test_single_instance_forwards_files():
    """A second launch must hand its files to the already-running window instead
    of opening another one. Driven as two real processes, which is the case that
    actually broke: opening a PDF from the file manager started a new app."""
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(_TMP, "ipc_host.py")
    # A private key: the developer's own copy of the app may well be running,
    # and it would answer on the production socket.
    key = f"copyshop_test_{os.getpid()}"
    # Patched where it is read: _forward_to_running_instance resolves
    # _IPC_KEY in its own module, so rebinding main's copy would leave the
    # test talking to the production socket — and to the developer's own
    # running app, which would answer.
    real_key, INSTANCE._IPC_KEY = INSTANCE._IPC_KEY, key
    with open(script, "w") as f:
        f.write(_HOST_SCRIPT.format(repo=repo, src=FX["normal"], key=key))

    # No instance running yet → forwarding must fail so the app opens normally.
    assert MAIN._forward_to_running_instance([FX["normal"]]) is False

    host = subprocess.Popen([sys.executable, "-u", script],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert _expect_line(host, "READY") == "READY 1", "host is not listening"
        # one file → a tab in the window that is already open, and the launcher's
        # XDG activation token travels with it. Without that token a Wayland
        # compositor refuses to let the running instance raise itself, which is
        # why the file used to open silently in the background.
        os.environ["XDG_ACTIVATION_TOKEN"] = "tok-abc123"
        try:
            assert MAIN._forward_to_running_instance([FX["single"]]) is True
        finally:
            os.environ.pop("XDG_ACTIVATION_TOKEN", None)
        got = _expect_line(host, "TOKEN").split()[1]
        assert got == "tok-abc123", f"activation token not forwarded (got {got!r})"
        tab_line = _expect_line(host, "TABS").split()
        tabs = int(tab_line[1])
        # tabs == 2 also proves the token line was not mistaken for a path.
        assert tabs == 2, f"forwarded file did not become a second tab ({tabs})"
        # Exactly one raise. A second, tokenless requestActivate() landing right
        # behind the good one is what left the window blinking instead of
        # coming forward on Wayland.
        raises = int(tab_line[2].split("=")[1])
        assert raises == 1, f"window was raised {raises}x for one delivery"
    finally:
        host.kill(); host.wait(timeout=10)
        INSTANCE._IPC_KEY = real_key


def test_open_paths_routes_by_count():
    """One file goes straight to a tab, several go to the sort/merge preview.
    Driven against the real method with a stand-in window, so it stays
    deterministic (a second MainWindow in this process is not)."""
    class FakeWindow:
        # The real _open_forwarded, so the deferred hand-off is exercised too.
        _open_forwarded = MAIN.MainWindow._open_forwarded
        def __init__(self):
            self.raised = []; self.opened = []; self.multi = None
            self.viewer = self
        def _raise_to_front(self, activation_token=""):
            self.raised.append(activation_token)
        def _switch(self, idx): pass
        def open_file(self, path): self.opened.append(path)
        def _open_multi(self, files): self.multi = list(files)

    def _deliver(w, paths, token=""):
        MAIN.MainWindow.open_paths(w, paths, token)
        _spin(10, 0.0)      # open_paths defers the open by one event-loop turn

    w = FakeWindow()
    _deliver(w, [FX["normal"]], "tok-1")
    assert w.opened == [FX["normal"]] and w.multi is None
    assert w.raised == ["tok-1"], f"raised {w.raised}, expected one raise with the token"

    w = FakeWindow()
    _deliver(w, [FX["normal"], FX["single"]])
    assert w.multi == [FX["normal"], FX["single"]] and w.opened == []
    assert len(w.raised) == 1, f"raised {len(w.raised)}x for one delivery"

    w = FakeWindow()
    _deliver(w, [os.path.join(_TMP, "does_not_exist.pdf")])
    assert w.opened == [] and w.multi is None, "missing files must be ignored"


def _popup_fully_visible(cb):
    """How many of a combo's items are fully inside its open popup."""
    cb.showPopup()
    _app.processEvents(); time.sleep(0.02); _app.processEvents()
    view = cb.view(); vp = view.viewport(); model = view.model()
    n = 0
    for i in range(cb.count()):
        r = view.visualRect(model.index(i, 0))
        if r.top() >= 0 and r.bottom() <= vp.height():
            n += 1
    cb.hidePopup()
    return n


def test_dropdowns_show_all_their_options():
    """Vertical padding on QComboBox makes Qt size the drop-down one row short,
    so even a two-option dropdown had to be scrolled. Check the real stylesheet
    against a range of item counts and against every combo in the tool panels."""
    from PyQt6.QtWidgets import QComboBox, QWidget, QVBoxLayout
    old = _app.styleSheet()
    try:
        for style in (MAIN.STYLE, MAIN.LIGHT_STYLE):
            _app.setStyleSheet(style)
            for count in (2, 3, 5, 11, 14):
                host = QWidget(); lay = QVBoxLayout(host)
                cb = QComboBox(); cb.addItems([f"Option {i+1}" for i in range(count)])
                lay.addWidget(cb); host.resize(320, 80); host.show()
                _app.processEvents()
                visible = _popup_fully_visible(cb)
                host.hide()
                assert visible == count, \
                    f"{count}-item dropdown shows only {visible} items — it scrolls"
        # and the combos the tools actually use
        _app.setStyleSheet(MAIN.STYLE)
        _open(FX["normal"])
        for panel_cls in (NUpPanel, CropResizePanel, PageNumbersPanel, CompressPanel):
            p = panel_cls(); p.resize(900, 600); p.show(); _app.processEvents()
            for cb in p.findChildren(QComboBox):
                if cb.count() < 2: continue
                visible = _popup_fully_visible(cb)
                assert visible == cb.count(), \
                    (f"{panel_cls.__name__}: a {cb.count()}-option dropdown shows "
                     f"only {visible} — it scrolls")
            p.hide()
    finally:
        _app.setStyleSheet(old)


def test_light_theme_reaches_every_colour_source():
    """Startup used to set the stylesheet and the viewer palette but leave
    app_state.THEME on its dark defaults, so anything drawn with theme_color()
    — the Greyscale preview area above all — stayed dark in light mode."""
    import tools.app_state as _as
    old = _app.styleSheet()
    try:
        MAIN.apply_theme_globally("light")
        assert _as.THEME["BG"] == "#edf1f7", f"THEME not switched: {_as.THEME['BG']}"
        from tools.theme import _TV
        assert _TV["viewer_bg"] == "#e8edf3"
        # the greyscale preview follows a *runtime* switch as well
        _open(FX["normal"])
        g = GrayscalePanel(); g.resize(900, 600); g.show()
        g._build_preview(3)
        _app.processEvents()
        light_css = g._preview_box.styleSheet()
        assert "#e8edf3" in light_css, light_css
        MAIN.apply_theme_globally("dark")
        _app.processEvents()
        dark_css = g._preview_box.styleSheet()
        assert dark_css != light_css and "#111827" in dark_css, dark_css
        g.hide()
    finally:
        MAIN.apply_theme_globally("dark")
        _app.setStyleSheet(old)


def test_preview_canvas_follows_the_theme():
    """The preview panes paint their own backdrop into a pixmap and put that
    over the label. The backdrop was the dark card colour spelled out as a
    literal, so in light mode the label underneath went light and the pixmap
    covering it stayed dark — and nothing repainted it on a switch, because
    _apply_theme only ever restyled the label."""
    from tools.theme import _TV
    from PyQt6.QtGui import QColor
    old = _app.styleSheet()
    try:
        _open(FX["normal"])
        p = NUpPanel(); p.resize(1000, 700); p.show()
        _spin(30)
        seen = {}
        for theme in ("light", "dark"):
            MAIN.apply_theme_globally(theme)
            _spin(40)
            img = p._preview.pixmap().toImage()
            # bottom-right: the sheet is drawn to (w-1, h-1), so the last row
            # and column are the only backdrop the N-Up preview leaves showing.
            corner = QColor(img.pixel(img.width() - 1, img.height() - 1)).name()
            assert corner == _TV["card_bg"], (theme, corner, _TV["card_bg"])
            seen[theme] = corner
        assert seen["light"] != seen["dark"], seen
        p.hide()
    finally:
        MAIN.apply_theme_globally("dark")
        _app.setStyleSheet(old)


def test_clicking_a_number_field_selects_its_value():
    """Click a number field and the value is selected, ready to be typed over —
    then click again and the caret goes where you clicked, so it can be edited.

    Every other application does this. This one put a caret between two digits,
    so changing 20 to 5 meant selecting by hand or three backspaces.

    Text fields are deliberately left alone: a file path or a page range is
    usually edited, not replaced."""
    from PyQt6.QtWidgets import QWidget, QVBoxLayout, QSpinBox, QDoubleSpinBox, QLineEdit
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtTest import QTest
    from tools.shell.inputs import install

    filt = install(_app)
    w = QWidget(); lay = QVBoxLayout(w)
    spin = QDoubleSpinBox(); spin.setRange(0, 999); spin.setValue(20.0)
    other = QSpinBox(); other.setRange(0, 999); other.setValue(7)
    path = QLineEdit("/home/somebody/a file.pdf")
    for x in (spin, other, path):
        lay.addWidget(x)
    # Other tests in this process have left windows around; without activating
    # this one the click never delivers focus to it. And focus has to start
    # somewhere else, or showing the window hands it to the first spin box and
    # the test measures that rather than the click.
    w.show(); w.raise_(); w.activateWindow()
    path.setFocus(); _spin(4, 0.0)
    assert not spin.lineEdit().hasFocus()

    def click(widget):
        le = widget.lineEdit() if hasattr(widget, "lineEdit") else widget
        QTest.mouseClick(le, Qt.MouseButton.LeftButton,
                         pos=QPoint(6, le.height() // 2))
        _spin(6, 0.0)

    try:
        click(spin)
        assert spin.lineEdit().selectedText(), "first click selected nothing"

        QTest.keyClicks(spin.lineEdit(), "5"); _spin(4, 0.0)
        assert spin.lineEdit().text() == "5", \
            f"typing did not replace the value ({spin.lineEdit().text()!r})"

        click(spin)
        assert spin.lineEdit().selectedText() == "", \
            "the second click re-selected everything — the field cannot be edited"

        click(other)
        assert other.lineEdit().selectedText(), "moving to another field selected nothing"

        click(path)
        assert path.selectedText() == "", "a text field should not select-all"
    finally:
        _app.removeEventFilter(filt)
        import tools.shell.inputs as _inputs
        _inputs._filter = None
        w.deleteLater(); _app.processEvents()


def test_a_forwarded_file_survives_a_garbage_collection():
    """The accepted socket has to stay referenced from Python until it is done.

    nextPendingConnection() returns a QLocalSocket that C++ owns — parented to
    the server — so the socket survives. Its Python wrapper did not: nothing
    referenced it once _serve returned, and collecting it took the connections
    to readyRead and disconnected with it. The bytes still arrived and nobody
    was listening, so the launch silently did nothing.

    Timing decided whether a collection landed in that window, which made it an
    intermittent failure rather than an obvious one. Here the collection is
    forced into exactly that window, so the bug is deterministic."""
    import gc
    from PyQt6.QtNetwork import QLocalSocket

    class _Win:
        def __init__(self): self.got = []
        def open_paths(self, paths, token=""): self.got.append((list(paths), token))

    key = f"copyshop_gc_{os.getpid()}"
    real_key, INSTANCE._IPC_KEY = INSTANCE._IPC_KEY, key
    win = _Win()
    server = INSTANCE._listen_for_open_requests(win)
    try:
        assert server is not None and server.isListening(), "listener did not start"
        sock = QLocalSocket()
        sock.connectToServer(key)
        assert sock.waitForConnected(2000), "client could not connect"

        # Let the server accept it. _serve runs here, with nothing to read yet —
        # the state the failure needed.
        _spin(20)
        gc.collect()

        sock.write((FX["single"] + "\n").encode("utf-8"))
        sock.flush()
        if sock.bytesToWrite():
            sock.waitForBytesWritten(2000)
        sock.disconnectFromServer()
        _spin(40)

        assert win.got, "the forwarded path never arrived"
        paths, _token = win.got[0]
        assert paths == [FX["single"]], f"wrong payload: {win.got!r}"
    finally:
        try:
            server.close()
        except Exception:
            pass
        INSTANCE._IPC_KEY = real_key
    return "accepted socket survived a collection"


def test_every_performance_setting_changes_something():
    """A control that does nothing is worse than no control.

    The dialog offered "Rendering-Geschwindigkeit: Ausgewogen / Schnell /
    Maximum (alle Kerne)", which mapped to a render_threads/thumb_threads pair
    that apply_performance_settings ignored on purpose — and could not have
    used, because PDFIUM_LOCK serialises every pdfium call in the process.
    It promised all your cores and did nothing at all.

    What is left has to be real, so this drives each one and checks the thing
    it claims to control actually moves."""
    from tools.render.queue import apply_performance_settings, prerender_enabled
    from tools.render.caches import _ThumbnailCache, _FullPageCache
    from tools.shell.settings import AppSettings, PerformanceDialog

    before = (_ThumbnailCache.MAX_BYTES, _FullPageCache.MAX_BYTES,
              prerender_enabled())
    try:
        apply_performance_settings(prerender=False, thumb_bytes=9 * 1024 * 1024,
                                   full_page_bytes=17 * 1024 * 1024)
        assert prerender_enabled() is False, "the pre-render switch did nothing"
        assert _ThumbnailCache.MAX_BYTES == 9 * 1024 * 1024, \
            "the thumbnail budget did nothing"
        assert _FullPageCache.MAX_BYTES == 17 * 1024 * 1024, \
            "the full-page budget did nothing"

        apply_performance_settings(prerender=True, thumb_bytes=before[0],
                                   full_page_bytes=before[1])
        assert prerender_enabled() is True
    finally:
        apply_performance_settings(prerender=before[2], thumb_bytes=before[0],
                                   full_page_bytes=before[1])

    # And nothing is left in the dialog that AppSettings cannot answer for.
    dlg = PerformanceDialog()
    try:
        s = AppSettings.get()
        for gone in ("speed_preset", "render_threads", "thumb_threads"):
            assert not hasattr(s, gone), f"{gone} survived the removal"
        assert not hasattr(dlg, "_speed_combo"), "the inert control is still there"
        assert hasattr(dlg, "_prerender_cb") and hasattr(dlg, "_ram_spin")
    finally:
        dlg.deleteLater(); _app.processEvents()
    return "pre-render and cache size both bite; the thread count is gone"


def test_the_wheel_does_not_change_a_setting_it_is_only_passing_over():
    """Scrolling a dialog must not alter the settings the pointer crosses, and
    must still scroll the dialog.

    Scrolling down the print dialog changed the paper size, the duplex edge or
    the colour mode on the way past. The first attempt at stopping that broke
    scrolling altogether, twice over: it matched QAbstractSlider, which
    QScrollBar inherits — so it ate the wheel on the scroll bars themselves —
    and it consumed the event rather than passing it on, so even a panel with
    no scroll bar under the pointer stopped moving. Both halves are asserted
    here."""
    from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QComboBox, QSpinBox,
                                 QSlider, QScrollArea)
    from PyQt6.QtCore import Qt, QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from tools.shell.inputs import install

    install(_app)
    inner = QWidget(); lay = QVBoxLayout(inner)
    combo = QComboBox(); combo.addItems(["A4", "A3", "Letter"]); combo.setCurrentIndex(0)
    spin  = QSpinBox(); spin.setRange(0, 99); spin.setValue(5)
    slide = QSlider(Qt.Orientation.Horizontal); slide.setRange(0, 50); slide.setValue(10)
    for x in (combo, spin, slide):
        lay.addWidget(x)
    for i in range(40):                       # enough content to need scrolling
        lay.addWidget(QSpinBox())
    area = QScrollArea(); area.setWidgetResizable(True); area.setWidget(inner)
    area.resize(300, 200); area.show(); area.raise_(); area.activateWindow()
    _spin(6, 0.0)

    def wheel(widget):
        centre = widget.rect().center()
        ev = QWheelEvent(QPointF(centre), QPointF(widget.mapToGlobal(centre)),
                         QPoint(0, -120), QPoint(0, -120), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        _app.sendEvent(widget, ev)
        _spin(2, 0.0)

    bar = area.verticalScrollBar()
    assert bar.maximum() > 0, "the fixture does not actually scroll"

    # Over each control: the setting is untouched and the panel still scrolls.
    for widget, read in ((combo, combo.currentIndex),
                         (spin,  spin.value),
                         (slide, slide.value)):
        before_value = read()
        before_scroll = bar.value()
        wheel(widget)
        assert read() == before_value, \
            f"{type(widget).__name__} changed under a wheel that was passing over it"
        assert bar.value() > before_scroll, \
            f"the panel stopped scrolling with the pointer over a {type(widget).__name__}"

    # And the scroll bar itself is not a control to be protected from the wheel.
    before_scroll = bar.value()
    wheel(bar)
    assert bar.value() != before_scroll, "the scroll bar itself stopped scrolling"

    area.deleteLater(); _app.processEvents()
    return "settings untouched, panel still scrolls, scroll bar still works"


def test_a_restricted_pdf_opens_and_a_locked_one_asks_for_the_password():
    """"Password-protected" is two different files and this refused both.

    Most of them are *restricted*: an owner password stops copying or printing
    and the user password is empty. pikepdf and pdfium open one with no
    password at all, which is how every other viewer shows it. This app tested
    PdfReader.is_encrypted, true of both kinds, and turned them away at the
    door — so files that open everywhere else did not open here.

    The rest are *locked*: a real user password, and nothing can read a page
    without it. Those are worth asking about, and now it asks."""
    from tools.pdf_access import encryption_state, is_locked, unlock_to_temp
    import tools.pdf_access as ACCESS
    from tools.viewer.panel import PageViewerPanel
    from pypdf import PdfReader

    assert encryption_state(FX["normal"]) == "open"
    assert encryption_state(FX["restricted"]) == "restricted", \
        "an owner-password file was not recognised as merely restricted"
    assert encryption_state(FX["encrypted"]) == "locked"
    assert not is_locked(FX["restricted"]), \
        "a file every other viewer opens was treated as needing a password"

    vp = PageViewerPanel(); vp.resize(800, 600); vp.show()
    asked = []
    real_ask = ACCESS.ask_password
    try:
        ACCESS.ask_password = lambda path, parent=None: asked.append(path)
        # Restricted: opens, and nothing is asked.
        vp.open_file(FX["restricted"])
        _spin(40)
        assert vp.tabs.count() == 1, "a restricted PDF still refused to open"
        assert not asked, "a restricted PDF asked for a password it does not need"

        # Locked, answered correctly: opens the decrypted copy.
        ACCESS.ask_password = lambda path, parent=None: (asked.append(path), "u")[1]
        vp.open_file(FX["encrypted"])
        _spin(60)
        assert asked, "a locked PDF opened without asking for anything"
        assert vp.tabs.count() == 2, "the right password did not open the file"
        opened = vp.tabs.currentWidget().pdf_path
        assert not PdfReader(opened, strict=False).is_encrypted, \
            "the tab is on a file that still needs a password"
    finally:
        ACCESS.ask_password = real_ask
        vp.deleteLater(); _app.processEvents()

    # And the wrong password is refused rather than half-opening something.
    try:
        unlock_to_temp(FX["encrypted"], "not the password")
        raise AssertionError("a wrong password was accepted")
    except AssertionError:
        raise
    except Exception:
        pass
    return "restricted opens silently, locked asks once, wrong password refused"


def test_enter_saves_a_settings_dialog_rather_than_discarding_it():
    """Enter runs the tool in every panel; in a settings dialog it threw the
    settings away.

    Every QPushButton in a QDialog is autoDefault, so Qt made the first in tab
    order the default — and _dlg_buttons adds Cancel before Save, so Cancel
    was it in three of the four. The prepress dialog was worse: its default
    was "Profil installieren…" further up the page, so Enter opened a file
    picker. Found while giving the print dialog its Enter back; the same
    defect, in a helper all four share.
    """
    from PyQt6.QtWidgets import QPushButton
    from tools.shell import settings as S

    for name in ("AppearanceDialog", "PerformanceDialog",
                 "PrepressDialog", "GeneralDialog"):
        dlg = getattr(S, name)()
        try:
            dlg.show(); _app.processEvents()
            defaults = [b.text().strip() for b in dlg.findChildren(QPushButton)
                        if b.isDefault()]
            assert defaults == ["Speichern"], \
                f"{name}: Enter would press {defaults}, not Save"
            # Cancel keeps its own Enter, so tabbing to it and pressing Enter
            # still means cancel rather than save.
            cancel = [b for b in dlg.findChildren(QPushButton)
                      if b.text().strip() == "Abbrechen"]
            assert cancel and cancel[0].autoDefault(), \
                f"{name}: a focused Cancel would save instead of cancelling"
        finally:
            dlg.close(); dlg.deleteLater(); _app.processEvents()
    return "all four settings dialogs save on Enter"
