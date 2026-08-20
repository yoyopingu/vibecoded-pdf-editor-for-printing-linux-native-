"""
Getting into a PDF that has a password on it.

"Password-protected" covers two different files, and this application treated
them as one and refused both.

Most of them are *restricted*: the owner set a password to stop copying or
printing and left the user password empty. Every viewer opens those — pikepdf
and pdfium open one with no password at all, which is how Acrobat, Evince and
a browser all show it without ever asking. This app checked
``PdfReader.is_encrypted``, which is true for both kinds, and turned them away
at the door.

The rest are *locked*: there is a real user password and nothing can read a
page without it. Those are the ones worth asking about, and now the app does —
once, with an explanation of what it needs and why.

Unlocking writes a decrypted copy to the temp directory and works on that.
Threading a password through the render cache, the tool panels and the print
path instead would put it in a dozen places that have no business knowing it,
and the copy is what "unlock" means to the person who typed the password. It
lives in the same temp directory as the flattened views and the print subsets,
and goes the same way.
"""
import logging
import os
import tempfile

from tools.i18n import tr


def encryption_state(path):
    """One of "open", "restricted" or "locked".

    "restricted" is encrypted but readable — an owner password with no user
    password. "locked" needs a password before any page can be read. Anything
    unreadable for another reason answers "open", so the caller's own error
    handling reports it rather than this claiming a password problem.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(path, strict=False)
        if not reader.is_encrypted:
            return "open"
        # A non-zero PasswordType means the empty password was accepted, as
        # either the user or the owner. That is the restricted case.
        return "restricted" if reader.decrypt("") else "locked"
    except Exception:
        logging.debug("could not read the encryption state of %s", path,
                      exc_info=True)
        return "open"


def is_locked(path):
    """Does this file need a password before anything can read it?"""
    return encryption_state(path) == "locked"


_UNLOCKED_COPIES: set = set()   # decrypted files this run wrote


def unlocked_dir():
    """Where decrypted copies live."""
    return os.path.join(tempfile.gettempdir(), "copyshop_unlocked")


def unlock_to_temp(path, password):
    """Write a decrypted copy of `path` and return where it went.

    Raises if the password is wrong — pikepdf's own PasswordError, which the
    caller turns into another attempt.

    The copy is the document without the protection its owner put on it, so
    it is written readable only by the user running the application and is
    deleted again when the tab closes or the application quits. It used to be
    written with whatever the umask gave — 0644 on this machine — into a
    directory every account on the machine can read, and then left there for
    good: a customer's protected file, unprotected, on the counter's shared
    computer long after the job was done.
    """
    import pikepdf
    out_dir = unlocked_dir()
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    # mkstemp creates with 0600 already, and creating it before pikepdf writes
    # leaves no moment where the decrypted bytes exist under a wider mode.
    fd, dest = tempfile.mkstemp(prefix=f"{stem}_", suffix=".pdf", dir=out_dir)
    os.close(fd)
    with pikepdf.open(path, password=password) as pdf:
        pdf.save(dest)
    try:
        os.chmod(dest, 0o600)      # pikepdf may have replaced the file
    except OSError:
        logging.debug("could not restrict %s", dest, exc_info=True)
    _UNLOCKED_COPIES.add(dest)
    return dest


def discard_unlocked_copy(path):
    """Delete one decrypted copy, if it is one of ours."""
    if not path or path not in _UNLOCKED_COPIES:
        return
    _UNLOCKED_COPIES.discard(path)
    try:
        os.remove(path)
    except OSError:
        logging.debug("could not remove the decrypted copy %s", path,
                      exc_info=True)


def discard_all_unlocked_copies():
    """Delete every decrypted copy this run wrote. For shutdown."""
    for path in list(_UNLOCKED_COPIES):
        discard_unlocked_copy(path)


def sweep_orphan_unlocked_copies():
    """Remove decrypted copies left behind by a run that could not clean up.

    A crash, a kill, or any version of this application from before these
    existed — which is every copy written before today. Unlike a view
    snapshot, one of these is a document with its protection taken off, so
    leaving it is worse than losing it: re-opening the file asks for the
    password again, which is the behaviour the owner expects anyway.
    """
    try:
        for name in os.listdir(unlocked_dir()):
            if not name.endswith(".pdf"):
                continue
            path = os.path.join(unlocked_dir(), name)
            if path in _UNLOCKED_COPIES:
                continue        # this run is using it
            try:
                os.remove(path)
            except OSError:
                logging.debug("could not sweep %s", path, exc_info=True)
    except FileNotFoundError:
        pass                    # nothing has ever been unlocked
    except Exception:
        logging.debug("could not sweep the decrypted copies", exc_info=True)


def ask_password(path, parent=None):
    """Ask for the password to `path`. Returns it, or None if cancelled.

    Says what it is asking for and why, because "Password:" over a text box on
    a file the user may not remember protecting is not a question anyone can
    answer confidently.
    """
    from PyQt6.QtWidgets import QInputDialog, QLineEdit
    text, ok = QInputDialog.getText(
        parent,
        tr("Passwort erforderlich"),
        tr('„{p0}“ ist mit einem Passwort geschützt.\n\n'
           'Ohne das Passwort lässt sich die Datei nicht anzeigen oder '
           'bearbeiten. Es wird nur zum Entsperren verwendet und nicht '
           'gespeichert.').format(p0=os.path.basename(path)),
        QLineEdit.EchoMode.Password)
    return text if ok else None


def ensure_openable(path, parent=None):
    """The path to work with, asking for a password only if one is needed.

    Returns `path` unchanged for a file that opens — including a restricted
    one, which every other viewer opens too. For a locked file, asks and
    returns the decrypted copy. Returns None if the user cancels, which means
    "do nothing", not "something failed".
    """
    if not is_locked(path):
        return path

    from PyQt6.QtWidgets import QMessageBox
    while True:
        password = ask_password(path, parent)
        if password is None:
            return None
        try:
            return unlock_to_temp(path, password)
        except Exception:
            logging.debug("unlocking %s failed", path, exc_info=True)
            again = QMessageBox.question(
                parent, tr("Passwort falsch"),
                tr("Das Passwort wurde nicht akzeptiert. Erneut versuchen?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if again != QMessageBox.StandardButton.Yes:
                return None
