"""
App.
"""
import os, sys, time
import tools.all_tools as T
import main as MAIN
import tools.shell.instance as INSTANCE
from tests.support import FX, _TMP, _app, _open, _spin


_HOST_SCRIPT = '''
import sys, os
sys.path.insert(0, {repo!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
app = QApplication([])
import main as MAIN
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
import main as MAIN
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
        for panel_cls in (T.NUpPanel, T.CropResizePanel, T.PageNumbersPanel, T.CompressPanel):
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
        from tools.page_viewer import _TV
        assert _TV["viewer_bg"] == "#e8edf3"
        # the greyscale preview follows a *runtime* switch as well
        _open(FX["normal"])
        g = T.GrayscalePanel(); g.resize(900, 600); g.show()
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
