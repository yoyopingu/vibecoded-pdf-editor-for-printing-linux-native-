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
import uuid

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


def unlock_to_temp(path, password):
    """Write a decrypted copy of `path` and return where it went.

    Raises if the password is wrong — pikepdf's own PasswordError, which the
    caller turns into another attempt.
    """
    import pikepdf
    out_dir = os.path.join(tempfile.gettempdir(), "copyshop_unlocked")
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    dest = os.path.join(out_dir, f"{stem}_{uuid.uuid4().hex[:8]}.pdf")
    with pikepdf.open(path, password=password) as pdf:
        pdf.save(dest)
    return dest


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
