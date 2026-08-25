"""
The flow behind "mehrere Dateien öffnen": a sort preview for several picked
files, conversion of what needs converting, and the two ways out — merge into
one document, or open each as its own tab.

Extracted from PageViewerPanel; the panel remains the host. Everything here
reaches the tab strip, the info line and the open-a-tab paths through the
panel it is given, and owns nothing but this one flow.
"""
import logging
import os
import shutil
import uuid

from PyQt6.QtWidgets import QMessageBox

from tools.app_state import AppState
from tools.i18n import tr
from tools.viewer.merge import MergeOrderWidget


class MergeFlow:
    def __init__(self, panel):
        self.panel = panel

    def show_merge_tab(self, file_paths):
        """Sort/merge preview for several picked files.

        Phase 7.3 (decision 3): this is the fourth view of the main chrome, not
        a doc-bar tab. The merge pane mounts into the main sidebar via the
        SidebarHost and the FileGrid fills the main area; one pending merge
        batch at a time. A repeat request with the same files raises the
        current preview; a different set replaces it."""
        import tempfile
        file_paths = [p for p in file_paths if os.path.isfile(p)]
        if not file_paths:
            return

        # One batch at a time (decision 3). A repeat call with the same files
        # is a double click or a re-sent open request — raise the open preview
        # rather than starting a second one. A different file set replaces the
        # current batch.
        cur = self.panel._merge_widget
        if cur is not None and cur.source_paths == file_paths:
            self.panel._enter_merge(cur)
            return
        if cur is not None:
            self.panel._exit_merge_preview()

        widget = MergeOrderWidget(file_paths)   # records file_paths as source_paths
        # One conversion directory per batch. A single panel-wide one was wiped
        # by whichever merge was cancelled first, taking the output another
        # batch was still using with it.
        widget.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")

        def _on_confirmed(paths):
            logging.debug(f"_on_confirmed empfangen: {len(paths)} Dateien")
            self._convert_and_merge(paths, widget)

        def _on_separately(paths):
            self._convert_and_open(paths, widget)

        def _on_cancelled():
            self.panel._exit_merge_preview()

        widget.merge_confirmed.connect(_on_confirmed)
        widget.open_separately.connect(_on_separately)
        widget.cancelled.connect(_on_cancelled)

        self.panel._enter_merge(widget)

    def _start_conversion(self, file_paths, merge_widget, on_done):
        """Convert the picked files to PDF in the merge batch's own temp dir and
        hand the results to `on_done(pdfs, failures)`. Shared by merge and
        open-separately.

        The conversion is a plain function on a pool job now. It used to be a
        QThread whose reference the panel had to keep alive by hand, in a set,
        until QThread.finished said the thread had really stopped — get that
        wrong and Qt aborts the process. tools/jobs.py owns the job instead, and
        it is tied to the merge widget so exiting the merge view stops it.
        """
        from tools.jobs import submit
        from tools.multi_open import convert_files
        self.panel._viewer_info.setText(tr("Konvertiere Dateien..."))

        # A file that cannot be converted is dropped from the result, so
        # convert_files hands back which ones and why — the user is told rather
        # than left with a document quietly missing pages.
        return submit(
            lambda job: convert_files(file_paths, merge_widget.tmp_dir, job),
            owner=merge_widget, name="convert-files",
            on_progress=self.panel._viewer_info.setText,
            on_done=lambda result: on_done(result[0], result[1]))

    def _report_conversion_failures(self, failures):
        """Never let files vanish from a merge in silence."""
        if not failures:
            return
        detail = "\n".join(f"{os.path.basename(p)}  —  {m}" for p, m in failures)
        logging.error("conversion failed:\n%s", detail)
        n = len(failures)
        AppState.get().status_message.emit(
            tr("{p0} Datei konnte nicht konvertiert werden").format(p0=n) if n == 1
            else tr("{p0} Dateien konnten nicht konvertiert werden").format(p0=n))
        QMessageBox.warning(
            self.panel, tr("Nicht konvertierte Dateien"),
            tr("Diese Dateien fehlen im Ergebnis:\n\n{p0}").format(p0=detail))

    def _conversion_failed(self, merge_widget, failures=()):
        """No file survived conversion — put the preview back the way it was so
        the user can change the list and try again instead of being stuck."""
        self.panel._viewer_info.setText(tr("Fehler: Keine Dateien konvertiert"))
        merge_widget.set_busy(False)
        self._report_conversion_failures(failures)

    def _persist_one(self, path, tmp_dir):
        """Move a finished output out of the merge temp dir into a persistent
        temp dir, so that cleaning the merge tmp dir on exit cannot orphan the
        file a result tab still points at.

        Only files that live *inside* `tmp_dir` are moved — a source PDF is
        returned untouched, because a tab opened from it must keep pointing at
        the user's original file (and the file itself must stay put).

        One persistent dir per panel, registered for removal at exit — the same
        pattern _open_office uses for its converted files."""
        import atexit
        import tempfile
        if not path or not tmp_dir:
            return path
        try:
            if os.path.commonpath([os.path.abspath(path),
                                   os.path.abspath(tmp_dir)]) != os.path.abspath(tmp_dir):
                return path   # an original source file — never move it
        except ValueError:
            return path       # different drives / not a shared path
        d = getattr(self.panel, "_merge_out_dir", None)
        if d is None:
            d = tempfile.mkdtemp(prefix="copyshop_out_")
            atexit.register(shutil.rmtree, d, ignore_errors=True)
            self.panel._merge_out_dir = d
        # Two merges both write "zusammengefuehrt.pdf", and two source files
        # from different directories can share a basename — the previous batch's
        # result would silently be overwritten. Uniquify so every persisted
        # output keeps its own file for the life of the session.
        stem, ext = os.path.splitext(os.path.basename(path))
        dest = os.path.join(d, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
        if os.path.abspath(dest) != os.path.abspath(path):
            shutil.move(path, dest)
        return dest

    def _convert_and_merge(self, file_paths, merge_widget):
        """Konvertiert Dateien und fügt sie zusammen."""
        logging.debug(f"_convert_and_merge: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            try:
                from pypdf import PdfWriter, PdfReader
                writer = PdfWriter()
                for path in valid:
                    for page in PdfReader(path, strict=False).pages:
                        writer.add_page(page)
                out = os.path.join(merge_widget.tmp_dir, "zusammengefuehrt.pdf")
                with open(out, "wb") as f:
                    writer.write(f)
                # The merged file must survive the temp-dir cleanup that leaving
                # the merge view triggers, so move it out before opening it.
                out = self._persist_one(out, merge_widget.tmp_dir)
            except Exception as e:
                logging.exception("merge failed")
                self.panel._viewer_info.setText(tr('Fehler: {p0}').format(p0=e))
                merge_widget.set_busy(False)
                return
            # Open the result FIRST — the merged file lives in the merge temp
            # dir, and _exit_merge_preview removes that dir. _open_result_tab
            # reads the file while it still exists; adding the tab then trips
            # _on_tab_changed, which leaves the merge view and cleans up. The
            # explicit exit below is the safety net when no tab change fired.
            self.panel._open_result_tab(out, tr("Zusammengeführt"))
            self.panel._update_toolbar()
            self._report_conversion_failures(failures)
            self.panel._exit_merge_preview()

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _convert_and_open(self, file_paths, merge_widget):
        """"Einzeln öffnen" — same conversion as the merge, but every file
        becomes its own tab instead of one combined document."""
        logging.debug(f"_convert_and_open: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            # Every converted PDF becomes its own tab, so each one must survive
            # the temp-dir cleanup: move the generated ones out of the merge
            # dir first (original source PDFs stay where they are).
            valid = [self._persist_one(p, merge_widget.tmp_dir) for p in valid]
            # Suppress the doc-tab-change auto-exit while opening: the converted
            # PDFs live in the merge temp dir, and _exit_merge_preview removes
            # that dir. Every file must be read before the cleanup runs.
            self.panel._merge_leaving = True
            failures = list(failures)
            for path in valid:
                try:
                    self.panel._open(path)
                except Exception as e:
                    logging.exception("open failed: %s", path)
                    failures.append((path, str(e)))
            self.panel._merge_leaving = False
            self.panel._update_toolbar()
            self._report_conversion_failures(failures)
            self.panel._exit_merge_preview()

        self._start_conversion(file_paths, merge_widget, _on_done)
