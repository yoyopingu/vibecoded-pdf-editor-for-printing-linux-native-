"""
Makes the suite importable under pytest.

Importing tests.support is the whole job: it puts the repo on sys.path, creates
the QApplication, imports the app in the order that does not segfault, and
builds the fixture PDFs. Doing it here means it has happened before pytest
imports the first test module, whichever one that is.

The suite does not require pytest — see tests/run.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import support        # noqa: F401,E402  — imported for its side effects


def pytest_sessionfinish(session, exitstatus):
    """Shut the app's background work down before Qt tears itself apart.

    The app does this on QApplication.aboutToQuit, which never fires here
    because no test runs an event loop. Without it the render thread and the
    job pool are still live while Qt destroys the QApplication, and the process
    segfaults after the results have been printed — a green run reporting
    exit 139, about one time in two.

    tests/run.py sidesteps the same problem with os._exit; pytest needs its
    reporting to finish, so this stops the threads instead of the process.
    """
    from tools.page_viewer import shutdown_render_queue
    shutdown_render_queue()
