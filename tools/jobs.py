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


class Cancelled(Exception):
    """The user pressed Stop. Not an error — nothing failed, it was stopped.

    Raised by Progress.check() and Progress.run(), and swallowed by the panel:
    a stopped job leaves no error dialog and no half-written output, only a
    line in the log saying it was stopped.
    """


class Progress:
    """What a run_async body is handed.

    It is callable, so every existing ``report("…")`` still works. It also
    knows whether the user has asked to stop, which is the part that makes a
    long tool interruptible:

        report("Seite 3 …")     # as before
        report.check()          # raises Cancelled if Stop was pressed
        report.run(cmd)         # subprocess that dies when Stop is pressed

    Checking is the body's job, and it has to be done between units of work
    rather than inside them: a half-converted page is worse than a whole one.
    """

    # Polling interval for subprocess communication. Shorter = more responsive
    # cancellation, longer = less overhead. Default 0.2s.
    POLL_INTERVAL = 0.2

    def __init__(self, job):
        self._job = job

    def __call__(self, message):
        self._job.report(message)

    @property
    def cancelled(self):
        return self._job.cancelled

    def check(self):
        if self._job.cancelled:
            raise Cancelled()

    def run(self, cmd, timeout=None, **kwargs):
        """subprocess.run, except that Stop kills the child.

        The whole reason this exists: the heavy tools spend their time inside
        Ghostscript or Tesseract, and a Stop button that cannot reach into
        that is a Stop button that does nothing for minutes. Polling rather
        than blocking in communicate() is what makes the flag visible.
        """
        import subprocess

        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        proc = subprocess.Popen(cmd, **kwargs)
        return _watch(proc, cmd, timeout, lambda: self._job.cancelled, self.POLL_INTERVAL)

    def run_many(self, cmds, timeout=None, max_workers=None, **kwargs):
        """run(), fanned out: several commands at once, and Stop kills all of
        them rather than one.

        Returns a CompletedProcess for every command, in the order the commands
        were given, whatever order they finished in. `timeout` is per command,
        the same meaning it has on run(). `max_workers` bounds how many are
        live at once; None runs them all, which is what the callers want —
        ghostscript.plan_chunks has already sized the list to the machine.

        One command falling over stops the others. The results of a chunked
        conversion are only useful all together, so leaving the rest to finish
        work that is about to be thrown away only makes the failure slower to
        report.

        Plain threads rather than concurrent.futures: a thread here does
        nothing but the poll loop run() already does, and the module is built
        on threading.Event throughout.
        """
        import subprocess

        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        results = [None] * len(cmds)
        failures = [None] * len(cmds)
        procs = {}
        procs_lock = threading.Lock()
        # Set by whichever command fails first; the rest read it and give up.
        abort = threading.Event()
        gate = threading.Semaphore(max_workers) if max_workers else None

        def stop_now():
            return self._job.cancelled or abort.is_set()

        def one(i, cmd):
            try:
                if gate is not None:
                    gate.acquire()
                try:
                    if stop_now():
                        return
                    proc = subprocess.Popen(cmd, **kwargs)
                    with procs_lock:
                        procs[i] = proc
                    results[i] = _watch(proc, cmd, timeout, stop_now, self.POLL_INTERVAL)
                finally:
                    if gate is not None:
                        gate.release()
            except Cancelled:
                pass                    # cancelled or abandoned; nothing to report
            except Exception as exc:    # noqa: BLE001 — reraised in the caller's thread
                failures[i] = exc
                abort.set()

        threads = [threading.Thread(target=one, args=(i, c), daemon=True,
                                    name=f"run_many-{i}")
                   for i, c in enumerate(cmds)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # A child that started between its thread's last check and Popen
        # returning would otherwise outlive the job that spawned it.
        if stop_now():
            with procs_lock:
                live = [p for p in procs.values() if p.poll() is None]
            for proc in live:
                _stop_process(proc)

        # Stop wins over a failure: the user asked, so nothing failed.
        if self._job.cancelled:
            raise Cancelled()
        for exc in failures:
            if exc is not None:
                raise exc
        return results


class _NoJob:
    """Stands in for a Job when the work is not running as one."""
    cancelled = False
    _done = True

    def report(self, message):
        pass

    @property
    def is_finished(self):
        return True


def null_progress():
    """A Progress that reports nowhere and is never cancelled.

    For calling a worker body outside a job — tests, and any path that runs
    the same function inline. It exists so the bodies can rely on `report`
    being a Progress and call ``report.check()`` unguarded, instead of every
    one of them testing whether it was handed a bare callable.
    """
    return Progress(_NoJob())


def _watch(proc, cmd, timeout, should_stop, poll_interval=0.2):
    """Wait for `proc`, polling so that stopping stays possible.

    Blocking in communicate() until the child is done is the one thing that
    must not happen here: the flag Stop sets would not be read until the work
    it is meant to interrupt had finished on its own. Shared by run() and
    run_many() so that one child and twelve are stopped by the same code.
    """
    import subprocess
    import time as _time

    deadline = None if timeout is None else _time.monotonic() + timeout
    while True:
        try:
            out, err = proc.communicate(timeout=poll_interval)
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            pass
        if should_stop():
            _stop_process(proc)
            raise Cancelled()
        if deadline is not None and _time.monotonic() > deadline:
            _stop_process(proc)
            raise subprocess.TimeoutExpired(cmd, timeout)


def _stop_process(proc):
    """Ask a child to stop, then insist. Ghostscript ignores nothing, but a
    wedged one would otherwise keep a core busy after the panel says idle."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
            proc.wait(timeout=3)
    except Exception:
        logging.debug("could not stop child process", exc_info=True)


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
    # None once Qt itself has been torn down, which is the case on the atexit
    # path: the pool is already gone, so there is nothing left to wait for.
    # Calling waitForDone on it raised an AttributeError that came out as an
    # error traceback on every exit without an event loop.
    pool = QThreadPool.globalInstance()
    if pool is not None:
        try:
            pool.waitForDone(wait_ms)
        except Exception:
            logging.debug("cancel_all: waitForDone raised during Qt teardown", exc_info=True)
    with _jobs_lock:
        _prune_locked()
    return len(victims)
