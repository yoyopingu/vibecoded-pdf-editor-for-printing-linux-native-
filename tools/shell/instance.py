"""
One app, one window: a second launch hands its files to the running
instance over a local socket instead of opening another window.
"""
import os
try:
    # Imported here, not lazily: loading this extension module later — once the
    # render threads are running — can segfault inside the import machinery.
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket
except ImportError:                       # QtNetwork not installed
    QLocalServer = QLocalSocket = None


_IPC_KEY = "copyshop_pdf_suite_single_instance"


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
