"""
One mechanism for fire-and-forget background work.

Before this there were three: QThread subclasses, bare
``threading.Thread(daemon=True)``, and _RenderQueue's own loop. The bare
threads were the worst of them — nothing held a reference to them, nothing
could cancel them, and nothing waited for them. A print job started on one and
the window closed underneath it kept running against a half-deleted dialog, and
at exit the interpreter simply stopped mid-subprocess.

A Job is a QRunnable on Qt's global pool with three properties the bare threads
lacked:

* **Owned.** ``setAutoDelete(False)`` plus a module registry, so the object
  outlives ``run()`` no matter what the caller does with its return value. This
  is the crash class the comment on _keep_worker describes: a QThread whose last
  Python reference goes out of scope while the thread is still inside run() is
  destroyed by Qt with "QThread: Destroyed while thread is still running", and
  that is an abort, not an exception.
* **Cancellable.** Every job carries a flag; long bodies check
  ``job.cancelled`` at points where stopping is safe. cancel_owner() takes a
  whole group at once — everything a tab or a dialog started — and cancel_all()
  runs at shutdown.
* **Waited for.** cancel_all() joins the pool, so nothing is still touching Qt
  objects while the widget tree is being torn down.

_RenderQueue is deliberately *not* built on this; see its own comment.
"""

import logging
import threading

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal


class JobSignals(QObject):
    """Results, delivered on the thread that created the job (the GUI thread)."""
    done     = pyqtSignal(object)   # return value of the work function
    error    = pyqtSignal(object)   # the exception, if it raised
    progress = pyqtSignal(str)
    finished = pyqtSignal()         # always, cancelled or not


class Job(QRunnable):
    """Work function running on the pool. ``fn(job)`` receives the job itself so
    it can check :attr:`cancelled` and emit progress."""

    def __init__(self, fn, owner=None, name=""):
        super().__init__()
        # Qt must not delete this behind our back: the registry owns it.
        self.setAutoDelete(False)
        self._fn      = fn
        self.owner    = owner
        self.name     = name or getattr(fn, "__name__", "job")
        self.signals  = JobSignals()
        self._cancel  = threading.Event()
        self._done    = threading.Event()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    @property
    def is_finished(self):
        return self._done.is_set()

    def cancel(self):
        self._cancel.set()

    def report(self, message):
        if not self.cancelled:
            self.signals.progress.emit(str(message))

    def run(self):
        try:
            if self.cancelled:
                return
            result = self._fn(self)
            if not self.cancelled:
                self.signals.done.emit(result)
        except Exception as exc:                       # noqa: BLE001
            logging.exception("background job %r failed", self.name)
            if not self.cancelled:
                self.signals.error.emit(exc)
        finally:
            # Order matters: mark finished, then signal. Never drop the registry
            # entry here — that is the only strong reference, and releasing it
            # from inside run() would free the object we are executing.
            self._done.set()
            try:
                self.signals.finished.emit()
            except RuntimeError:
                pass       # receiver already gone


_jobs: "set" = set()
_jobs_lock = threading.Lock()


def _prune_locked():
    for job in [j for j in _jobs if j.is_finished]:
        _jobs.discard(job)


def submit(fn, owner=None, name="", priority=0,
           on_done=None, on_error=None, on_progress=None, on_finished=None):
    """Run ``fn(job)`` on the global pool. Returns the :class:`Job`.

    `owner` groups jobs so they can be cancelled together — pass the widget that
    started the work, and cancel_owner(widget) when it closes.

    Pass the callbacks here rather than connecting to ``job.signals`` after this
    returns: the job may already be running by then, and a short one can finish
    and emit before the caller has connected anything.
    """
    job = Job(fn, owner=owner, name=name)
    if on_done     is not None: job.signals.done.connect(on_done)
    if on_error    is not None: job.signals.error.connect(on_error)
    if on_progress is not None: job.signals.progress.connect(on_progress)
    if on_finished is not None: job.signals.finished.connect(on_finished)
    with _jobs_lock:
        _prune_locked()
        _jobs.add(job)
    QThreadPool.globalInstance().start(job, priority)
    return job


def active_jobs():
    with _jobs_lock:
        return [j for j in _jobs if not j.is_finished]


def cancel_owner(owner):
    """Cancel everything a given tab or dialog started. Does not wait: the
    bodies stop at their next checkpoint, and their results are dropped."""
    if owner is None:
        return 0
    with _jobs_lock:
        victims = [j for j in _jobs if j.owner is owner and not j.is_finished]
    for job in victims:
        job.cancel()
    return len(victims)


def cancel_all(wait_ms=2000):
    """Cancel every job and wait for the pool to drain. Called at shutdown,
    before the widget tree is taken apart — a job still running then would be
    emitting into half-deleted receivers."""
    with _jobs_lock:
        victims = list(_jobs)
    for job in victims:
        job.cancel()
    QThreadPool.globalInstance().waitForDone(wait_ms)
    with _jobs_lock:
        _prune_locked()
    return len(victims)
