"""
The "Farbprofil:" label and the work behind it.

Reading a page's colour spaces means walking every content stream — half a
second on a large one — so nothing here is allowed to stall the GUI thread:
the label answers at once when the result is cached, and otherwise a
background job fills it in when the answer arrives. A whole-document scan
runs once per file revision so page turns are free after the first.
"""
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QLabel

from tools.app_state import AppState
from tools.colorspace import (cached_page_colorspaces, describe,
                              document_revision, page_colorspaces,
                              scan_document)
from tools.i18n import tr


class ColourSpaceLabel(QObject):
    """Drives the label; ask it to update whenever the page on screen changes.

    ``resolver`` is called on each update and returns ``(src_path, orig)``
    for the page to describe, or None when there is nothing to describe.

    Every answer is also published on the status bus
    (``AppState.colorspace_changed``) as the bare profile name — the
    window-level status bar shows just "sRGB", while the per-tab label it
    drives here keeps its "Farbprofil: …" prefix.
    """

    def __init__(self, resolver, parent=None, on_scan_complete=None):
        super().__init__(parent)
        self._resolver = resolver
        self._on_scan_complete = on_scan_complete
        self._pending  = None
        self._job      = None
        self._scan_job = None
        self._scanned: set = set()
        self._profile  = ""
        self.widget = QLabel(tr("Farbprofil: —"))
        self.widget.setObjectName("dimLabel")

    @staticmethod
    def _publish(value):
        AppState.get().colorspace_changed.emit(value)

    def republish(self):
        """Re-emit the profile this label is showing — used when the tab or
        view that owns it becomes active, so the status bar re-reads it."""
        self._publish(self._profile)

    def update(self):
        try:
            where = self._resolver()
            if where is None:
                return
            src_path, orig = where
        except Exception:
            self._profile = ""
            self.widget.setText(tr("Farbprofil: —"))
            self._publish("")
            return

        # Read the whole file once, in the background, rather than a page at a
        # time as the user turns them. Cheap to ask for again — it is a stat
        # and two comparisons once the scan has been started.
        self._scan_document(src_path)

        known = cached_page_colorspaces(src_path, orig)
        if known is not None:
            # Nothing is outstanding, and the label now describes this page.
            self._pending = None
            profile = describe(known)
            self._profile = profile
            self.widget.setText(tr('Farbprofil: {p0}').format(p0=profile))
            self._publish(profile)
            return

        # The page this answer will belong to. By the time it comes back the
        # user may have turned to another one, and a label from the page before
        # is worse than no label.
        want = (src_path, orig)
        if self._pending == want:
            # Already asked for this page. Either the answer is still coming or
            # it came back unreadable — which page_colorspaces does not cache,
            # since "could not be read" is not an answer to remember — and
            # either way this runs after every render, so asking again would
            # mean re-reading an unreadable page for as long as it is on screen.
            return
        # Whatever was asked for the page before this one, nobody wants any
        # more. Left running, a turn through a long document queued one full
        # read of the file per page onto a pool with as many slots as there are
        # cores — so the answer for the page actually on screen waited behind
        # every page passed on the way to it, and the renders waited with it.
        self._cancel()
        self._pending = want
        self._profile = ""
        self.widget.setText(tr("Farbprofil: …"))
        self._publish("")
        from tools.jobs import submit
        job = submit(lambda job: page_colorspaces(src_path, orig),
                     owner=self, name="colorspace",
                     on_done=lambda names: self._apply(want, names))
        # Cleared on `finished` rather than on `done`, so a job that fails or is
        # cancelled releases the slot too — otherwise one such job would leave
        # the label convinced an answer was still coming and stop asking.
        job.signals.finished.connect(lambda j=job: self._job_finished(j))
        self._job = job

    def _cancel(self):
        if self._job is not None:
            self._job.cancel()
            self._job = None

    def _job_finished(self, job):
        if self._job is job:         # not one already replaced by a newer ask
            self._job = None

    def _apply(self, want, names):
        if self._pending != want:
            return                      # the user has moved on
        try:
            profile = describe(names)
            self._profile = profile
            self.widget.setText(
                tr('Farbprofil: {p0}').format(p0=profile))
            self._publish(profile)
        except RuntimeError:
            pass                        # the view is being torn down

    def _scan_document(self, src_path):
        """Read every page's colour spaces once, so page turns are free.

        The label used to read one page per turn, and reading a page means
        opening the file: 83 ms of cross-reference parsing against 0.36 ms to
        answer, on a 500-page document — and the 83 ms is the part that grows
        with the page count. So a long file was slow to browse precisely
        because it was long, and the whole document costs less than three of
        those turns (see tools.colorspace.scan_document).

        Once per file revision, and abandoned rather than finished if the user
        closes the tab: the pages read before that are kept, since they cost
        nothing to have.
        """
        revision = document_revision(src_path)
        if revision in self._scanned or self._scan_job is not None:
            return
        from tools.jobs import submit

        def scan(job):
            return scan_document(src_path, should_stop=lambda: job.cancelled)

        # Behind the page renders and behind the label's own page: this is work
        # for the turns to come, and nothing on screen is waiting for it.
        job = submit(scan, owner=self, name="colorspace-document", priority=-1,
                     on_done=lambda result: self._scan_done(revision, result))
        job.signals.finished.connect(lambda j=job: self._scan_finished(j))
        self._scan_job = job

    def _scan_finished(self, job):
        if self._scan_job is job:
            self._scan_job = None

    def _scan_done(self, revision, result):
        _names, complete = result
        if complete:
            # A set, not one revision: a merged tab draws its pages from
            # several files, and remembering only the last would have the two
            # re-scanning each other away every time the user crossed the join.
            self._scanned.add(revision)
        if self._on_scan_complete is not None:
            # The page counter re-aggregates over the whole tab once the scan
            # has filled the cache — complete or not, the pages it did read are
            # in, and the counter's own unknown-gate decides what to show.
            try:
                self._on_scan_complete()
            except RuntimeError:
                pass                    # the view is being torn down
