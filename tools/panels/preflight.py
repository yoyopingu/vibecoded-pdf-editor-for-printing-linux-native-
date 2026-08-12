"""
PreflightPanel, moved verbatim out of tools/all_tools.py.
See tools/panels/__init__.py.
"""
import os
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from PyQt6.QtWidgets import QVBoxLayout, QComboBox, QGroupBox, QCheckBox, QTextEdit, QApplication
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import PAPER_SIZES_PT, row
from tools.panels._colour import _colour_histogram, _hist_stats


# ══════════════════════════════════════════════════════════════════════════════
# PREFLIGHT
# ══════════════════════════════════════════════════════════════════════════════
class PreflightPanel(BasePanel):
    TITLE    = "Druckvorstufenpruefung"
    SUBTITLE = "PDF auf Drucktauglichkeit pruefen."

    def build_ui(self, layout):
        cb=QGroupBox(tr("PRUEFUNGEN")); cl=QVBoxLayout(cb)
        self.size_combo=QComboBox(); self.size_combo.addItem(tr("Beliebige Groesse"))
        self.size_combo.addItems(list(PAPER_SIZES_PT.keys()))
        cl.addLayout(row(tr("Erwartetes Format:"), self.size_combo))
        self.chk_size=QCheckBox(tr("Seitenformat korrekt")); self.chk_size.setChecked(True); cl.addWidget(self.chk_size)
        self.chk_orient=QCheckBox(tr("Einheitliche Ausrichtung")); self.chk_orient.setChecked(True); cl.addWidget(self.chk_orient)
        self.chk_colour=QCheckBox(tr("Farbige Seiten erkennen")); self.chk_colour.setChecked(True); cl.addWidget(self.chk_colour)
        self.chk_enc=QCheckBox(tr("Nicht passwortgeschuetzt")); self.chk_enc.setChecked(True); cl.addWidget(self.chk_enc)
        layout.addWidget(cb)
        layout.addWidget(make_label(tr("Bericht:"), dim=True))
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMinimumHeight(180)
        self.report.setPlaceholderText(tr("Pruefung starten...")); layout.addWidget(self.report)

    def _run_action(self):
        if getattr(self, '_preflighting', False):
            raise RuntimeError(tr("Pruefung laeuft bereits — bitte warten."))
        self._preflighting = True
        try:
            return self._do_preflight()
        finally:
            self._preflighting = False

    def _do_preflight(self):
        src=self.require_pdf()
        from pypdf import PdfReader; import pypdfium2 as pdfium
        reader=PdfReader(src, strict=False); doc=pdfium.PdfDocument(src)
        try:
            n=len(reader.pages); issues=[]; oks=[]
            if self.chk_enc.isChecked():
                if reader.is_encrypted: issues.append(tr("PDF ist passwortgeschuetzt"))
                else: oks.append(tr("Nicht verschluesselt"))
            target=PAPER_SIZES_PT.get(self.size_combo.currentText())
            orients=[]; colour_pages=[]
            for i in range(n):
                QApplication.processEvents()
                page=reader.pages[i]; pw=float(page.mediabox.width); ph=float(page.mediabox.height)
                orients.append("Q" if pw>ph else "H")
                if self.chk_size.isChecked() and target:
                    tw,th=target
                    if not ((abs(pw-tw)<5 and abs(ph-th)<5) or (abs(pw-th)<5 and abs(ph-tw)<5)):
                        issues.append(tr('Seite {p0}: {p1:.0f}x{p2:.0f}pt != {p3:.0f}x{p4:.0f}pt').format(p0=i + 1, p1=pw, p2=ph, p3=tw, p4=th))
                if self.chk_colour.isChecked():
                    with _pdfium_lock:
                        pil=doc[i].render(scale=1).to_pil().convert("RGB")
                    # Every pixel, not a 64x64 squash of the page: averaging the
                    # render first hid small colour marks completely, and this
                    # check reporting "Keine Farbseiten erkannt" for a page that
                    # has them is a preflight that passes a job it should stop.
                    if _hist_stats(_colour_histogram(pil), 20)[0] > 20:
                        colour_pages.append(i+1)
            if self.chk_orient.isChecked() and orients:
                if len(set(orients))>1: issues.append(tr("Gemischte Ausrichtungen"))
                else: oks.append(tr("Einheitlich: {p0}").format(p0=tr('Hochformat') if orients[0]=='H' else tr('Querformat')))
            if self.chk_colour.isChecked():
                if colour_pages: issues.append(f"Farbseiten: {colour_pages[:10]}")
                else: oks.append(tr("Keine Farbseiten erkannt"))
            lines=[f"BERICHT -- {os.path.basename(src)}",tr('Seiten: {p0}  |  {p1} KB').format(p0=n, p1=os.path.getsize(src) // 1024),""]
            if issues: lines+=[f"PROBLEME ({len(issues)}):"]+ [f"  x  {x}" for x in issues]+[""]
            else: lines.append(tr("BESTANDEN -- Datei scheint druckfertig."))
            if oks: lines+=["BESTANDEN:"]+[f"  v  {x}" for x in oks]
            self.report.setPlainText("\n".join(lines))
        finally:
            doc.close()
        return f"{'BESTANDEN' if not issues else f'FEHLER ({len(issues)} Problem(e))'}"
