"""
MergeSplitPanel, moved verbatim out of tools/all_tools.py.
See tools/panels/__init__.py.
"""
import os
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QSpinBox, QGroupBox, QRadioButton, QApplication
from tools._base import BasePanel, FileDropList, make_label
from tools.i18n import tr
from tools.panels._shared import row


# ══════════════════════════════════════════════════════════════════════════════
# MERGE / SPLIT
# ══════════════════════════════════════════════════════════════════════════════
class MergeSplitPanel(BasePanel):
    TITLE    = "Zusammenfuehren / Trennen"
    SUBTITLE = "PDFs zusammenfuehren oder in Teile aufteilen."

    def build_ui(self, layout):
        mg = QGroupBox(tr("ZUSAMMENFUEHREN")); ml = QVBoxLayout(mg)
        ml.addWidget(make_label(tr("PDFs hinzufuegen. Reihenfolge per Drag & Drop."), dim=True))
        self.merge_list = FileDropList(); ml.addWidget(self.merge_list)
        br = QHBoxLayout()
        for txt, fn in [(tr("+ Hinzufuegen"), self._add_merge),
                        (tr("- Entfernen"), self.merge_list.remove_selected),
                        (tr("Hoch"), self.merge_list.move_up),
                        (tr("Runter"), self.merge_list.move_down)]:
            b = QPushButton(txt); b.setObjectName("secondaryBtn")
            b.clicked.connect(fn); br.addWidget(b)
        br.addStretch(); ml.addLayout(br)
        mb = QPushButton(tr("  Zusammenfuehren und speichern..."))
        mb.setObjectName("actionBtn"); mb.clicked.connect(self._do_merge); ml.addWidget(mb)
        layout.addWidget(mg)

        sp = QGroupBox(tr("TRENNEN")); sl = QVBoxLayout(sp)
        sl.addWidget(make_label(tr("Verwendet die aktuell im Viewer geoeffnete PDF."), dim=True))
        self.radio_each  = QRadioButton(tr("Jede Seite als eigene Datei"))
        self.radio_range = QRadioButton(tr("Nach Seitenbereichen  (z.B. 1-3, 5, 7-9)"))
        self.radio_n     = QRadioButton(tr("Alle N Seiten"))
        self.radio_each.setChecked(True)
        for r in (self.radio_each, self.radio_range, self.radio_n): sl.addWidget(r)
        self.range_input = QLineEdit(); self.range_input.setPlaceholderText(tr("z.B. 1-3, 5, 7-9"))
        sl.addLayout(row(tr("Seitenbereiche:"), self.range_input))
        self.n_spin = QSpinBox(); self.n_spin.setRange(1, 9999); self.n_spin.setValue(1)
        sl.addLayout(row(tr("Seiten pro Teil:"), self.n_spin))
        spb = QPushButton(tr("  Trennen und in Ordner speichern..."))
        spb.setObjectName("actionBtn"); spb.clicked.connect(self._do_split); sl.addWidget(spb)
        layout.addWidget(sp)

    def build_action_row(self, row_layout): pass

    def _add_merge(self):
        paths = self.pick_pdfs()
        if paths: self.merge_list.add_files(paths)

    def _do_merge(self):
        if getattr(self, '_merging', False): return
        self._merging = True
        try:
            self._do_merge_impl()
        finally:
            self._merging = False

    def _do_merge_impl(self):
        from pypdf import PdfWriter, PdfReader
        paths = self.merge_list.get_paths()
        if not paths: self.log.log(tr("Mindestens eine PDF hinzufuegen."), error=True); return
        out = self.save_pdf("Zusammengefuehrte PDF speichern als")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            writer = PdfWriter()
            empty = []
            for p in paths:
                n_before = len(writer.pages)
                for page in PdfReader(p, strict=False).pages: writer.add_page(page)
                if len(writer.pages) == n_before:
                    empty.append(os.path.basename(p))
            if not writer.pages:
                raise ValueError(tr("Keine der gewaehlten Dateien enthielt Seiten."))
            with open(out, "wb") as f: writer.write(f)
            # Report what actually came out, not how many files were picked: a
            # file that contributed nothing used to be counted as merged.
            self.log.log(tr('{p0} Dateien zusammengefuehrt ({p1} Seiten)').format(
                p0=len(paths) - len(empty), p1=len(writer.pages)))
            if empty:
                self.log.log(tr('Ohne Seiten uebersprungen: {p0}').format(
                    p0=", ".join(empty)), error=True)
            self.open_result(out, tr("Zusammengefuehrt"))
        except Exception as e: self.log.log(str(e), error=True)

    def _do_split(self):
        if getattr(self, '_splitting', False): return
        self._splitting = True
        try:
            self._do_split_impl()
        finally:
            self._splitting = False

    def _do_split_impl(self):
        from pypdf import PdfReader, PdfWriter
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            reader = PdfReader(src, strict=False); stem = os.path.splitext(os.path.basename(src))[0]
            n = len(reader.pages); saved = []
            if self.radio_each.isChecked():
                for i, page in enumerate(reader.pages):
                    w = PdfWriter(); w.add_page(page)
                    p = os.path.join(out_dir, f"{stem}_seite{i+1:03d}.pdf")
                    with open(p, "wb") as f: w.write(f); saved.append(p)
            elif self.radio_range.isChecked():
                groups = self._parse_ranges(self.range_input.text(), n)
                for gi, pages in enumerate(groups, 1):
                    w = PdfWriter()
                    for p in pages: w.add_page(reader.pages[p])
                    path = os.path.join(out_dir, f"{stem}_teil{gi:02d}.pdf")
                    with open(path, "wb") as f: w.write(f); saved.append(path)
            else:
                nv = self.n_spin.value(); chunk = 0
                for start in range(0, n, nv):
                    chunk += 1; w = PdfWriter()
                    for i in range(start, min(start+nv, n)): w.add_page(reader.pages[i])
                    path = os.path.join(out_dir, f"{stem}_teil{chunk:03d}.pdf")
                    with open(path, "wb") as f: w.write(f); saved.append(path)
            self.log.log(tr('In {p0} Dateien aufgeteilt').format(p0=len(saved)))
            if saved: self.open_result(saved[0], tr('Teil 1 von {p0}').format(p0=len(saved)))
        except Exception as e: self.log.log(str(e), error=True)

    def _parse_ranges(self, raw, n):
        groups = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                if "-" in part:
                    a, b = part.split("-", 1)
                    pages = list(range(int(a.strip())-1, int(b.strip())))
                else:
                    pages = [int(part)-1]
            except ValueError:
                continue
            pages = [p for p in pages if 0 <= p < n]
            if pages: groups.append(pages)
        return groups

    def _run_action(self): pass
