"""
The flow behind "mehrere Dateien oeffnen": a sort preview for several picked
files, conversion of what needs converting, and the two ways out — merge into
one document, or open each as its own tab.

Extracted from PageViewerPanel; the panel remains the host. Everything here
reaches the tab strip, the info line and the open-a-tab paths through the
panel it is given, and owns nothing but this one flow.
"""
import logging
import os
import shutil

from PyQt6.QtWidgets import QMessageBox

from tools.app_state import AppState
from tools.i18n import tr
from tools.viewer.merge import MergeOrderWidget


class MergeFlow:
    def __init__(self, panel):
        self.panel = panel

    def show_merge_tab(self, file_paths):
        """Preview for several picked files, shown as a tab in the same style as
        the page manager: sort them, then either merge them into one document or
        open them as separate tabs."""
        import tempfile
        file_paths = [p for p in file_paths if os.path.isfile(p)]
        if not file_paths:
            return
        self.panel._reveal_tabs()
        logging.debug(f"show_merge_tab: {len(file_paths)} Dateien: {file_paths}")

        # A repeat call with the same files is a double click or a re-sent open
        # request, not a second job. Raise the tab that is already open instead
        # of stacking an identical one behind it — that stack was how a fast
        # click ended up merging twice at once.
        for i in range(self.panel.tabs.count()):
            w = self.panel.tabs.widget(i)
            if isinstance(w, MergeOrderWidget) and w.source_paths == file_paths:
                self.panel.tabs.setCurrentIndex(i)
                return

        widget = MergeOrderWidget(file_paths)   # records file_paths as source_paths
        # One conversion directory per tab. A single panel-wide one was wiped by
        # whichever merge tab was cancelled first, taking the output another tab
        # was still using with it.
        widget.tmp_dir = tempfile.mkdtemp(prefix="copyshop_")
        idx = self.panel.tabs.addTab(widget, tr("  📂  Dateien oeffnen  "))
        self.panel.tabs.setCurrentIndex(idx)
        self.panel._update_toolbar()
        self.panel._viewer_info.setText(tr("Dateien sortieren, zusammenfuehren oder einzeln oeffnen"))

        def _on_confirmed(paths):
            logging.debug(f"_on_confirmed empfangen: {len(paths)} Dateien")
            self._convert_and_merge(paths, widget)

        def _on_separately(paths):
            self._convert_and_open(paths, widget)

        def _on_cancelled():
            wi = self.panel.tabs.indexOf(widget)
            if wi >= 0:
                self.panel.tabs.removeTab(wi)
            self.panel._update_toolbar()
            try: shutil.rmtree(widget.tmp_dir, ignore_errors=True)
            except Exception: pass   # ignore_errors already handles the file-level failures

        widget.merge_confirmed.connect(_on_confirmed)
        widget.open_separately.connect(_on_separately)
        widget.cancelled.connect(_on_cancelled)

    def _start_conversion(self, file_paths, merge_widget, on_done):
        """Convert the picked files to PDF in the merge tab's own temp dir and
        hand the results to `on_done(pdfs, failures)`. Shared by merge and
        open-separately.

        The conversion is a plain function on a pool job now. It used to be a
        QThread whose reference the panel had to keep alive by hand, in a set,
        until QThread.finished said the thread had really stopped — get that
        wrong and Qt aborts the process. tools/jobs.py owns the job instead, and
        it is tied to the merge tab so closing the tab stops it.
        """
        from tools.jobs import submit
        from tools.multi_open import convert_files
        self.panel._viewer_info.setText(tr("Konvertiere Dateien..."))
        # Tab-Titel via Widget-Referenz setzen (sicher gegen Index-Shifts)
        wi = self.panel.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.panel.tabs.setTabText(wi, tr("  ⏳  Konvertiere...  "))

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
        AppState.get().status_message.emit(
            tr('{p0} Datei(en) konnten nicht konvertiert werden').format(p0=len(failures)))
        QMessageBox.warning(
            self.panel, tr("Nicht konvertierte Dateien"),
            tr("Diese Dateien fehlen im Ergebnis:\n\n{p0}").format(p0=detail))

    def _conversion_failed(self, merge_widget, failures=()):
        """No file survived conversion — put the tab back the way it was so the
        user can change the list and try again instead of being stuck."""
        self.panel._viewer_info.setText(tr("Fehler: Keine Dateien konvertiert"))
        wi = self.panel.tabs.indexOf(merge_widget)
        if wi >= 0:
            self.panel.tabs.setTabText(wi, tr("  ✗  Fehler  "))
        merge_widget.set_busy(False)
        self._report_conversion_failures(failures)

    def _convert_and_merge(self, file_paths, merge_widget):
        """Konvertiert Dateien und fuegt sie zusammen."""
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
            except Exception as e:
                logging.exception("merge failed")
                self.panel._viewer_info.setText(tr('Fehler: {p0}').format(p0=e))
                merge_widget.set_busy(False)
                return
            wi = self.panel.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.panel.tabs.removeTab(wi)
            self.panel._open_result_tab(out, tr("Zusammengefuehrt"))
            self.panel._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)

    def _convert_and_open(self, file_paths, merge_widget):
        """"Einzeln oeffnen" — same conversion as the merge, but every file
        becomes its own tab instead of one combined document."""
        logging.debug(f"_convert_and_open: {len(file_paths)} Dateien")

        def _on_done(pdfs, failures):
            valid = [p for p in pdfs if p]
            if not valid:
                self._conversion_failed(merge_widget, failures)
                return
            wi = self.panel.tabs.indexOf(merge_widget)
            if wi >= 0:
                self.panel.tabs.removeTab(wi)
            failures = list(failures)
            for path in valid:
                try:
                    self.panel._open(path)
                except Exception as e:
                    logging.exception("open failed: %s", path)
                    failures.append((path, str(e)))
            self.panel._update_toolbar()
            self._report_conversion_failures(failures)

        self._start_conversion(file_paths, merge_widget, _on_done)
