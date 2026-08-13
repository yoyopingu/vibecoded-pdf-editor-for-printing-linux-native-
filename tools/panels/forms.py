"""
Formulare / Reduzieren — fill form fields and flatten them, and the
annotations with them, into the page content.
"""
import os, shutil
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from PyQt6.QtWidgets import QVBoxLayout, QPushButton, QLineEdit, QGroupBox, QCheckBox, QScrollArea, QWidget, QFrame
from tools._base import BasePanel
from tools.i18n import tr
from tools.panels._shared import row


# ══════════════════════════════════════════════════════════════════════════════
# FORMS
# ══════════════════════════════════════════════════════════════════════════════
def _plain_ink(path):
    """Dark pixels per page when the file is rendered *without* form support —
    i.e. what a printer, a RIP or any non-interactive viewer puts on paper."""
    import pypdfium2 as pdfium
    with _pdfium_lock:
        doc = pdfium.PdfDocument(path)
        try:
            out = []
            for i in range(len(doc)):
                im = doc[i].render(scale=0.5).to_pil().convert("L")
                out.append(sum(1 for v in im.get_flattened_data() if v < 128))
            return out
        finally:
            doc.close()


def _flatten_form(filled, out, has_values):
    """Bake the filled-in values into the page so they survive printing.

    "Reduzieren (fuer Druck)" used to be `del page["/Annots"]`, which deletes the
    widget annotations — and the widget is *where the value is drawn*. The field
    value stayed in the AcroForm dictionary, so the file still looked filled to
    anything that inspects fields, while the page itself rendered blank. Ticking
    the box that says "for printing" produced an empty form.

    Flattening properly means drawing each widget's appearance stream into the
    page content. pypdf writes a correct appearance when it fills a field, but it
    also sets /NeedAppearances, and qpdf refuses to flatten form fields while
    that flag is set — so it has to be cleared first, which is safe precisely
    because the appearance we are about to bake in is the one pypdf just wrote."""
    import pikepdf
    before = _plain_ink(filled)
    with pikepdf.open(filled) as pdf:
        acro = pdf.Root.get("/AcroForm")
        if acro is not None and "/NeedAppearances" in acro:
            del acro["/NeedAppearances"]
        pdf.flatten_annotations("all")
        pdf.save(out)
    after = _plain_ink(out)
    # Refuse to hand over a form that lost what was typed into it.
    if len(after) == len(before):
        if any(a < b for a, b in zip(after, before)):
            raise RuntimeError(tr(
                "Reduzieren hat Seiteninhalt entfernt — die Datei wurde nicht "
                "gespeichert. Bitte ohne 'Reduzieren' erneut speichern."))
        if has_values and sum(after) <= sum(before):
            raise RuntimeError(tr(
                "Reduzieren hat die ausgefuellten Werte nicht ins Seitenbild "
                "uebernommen — die Datei wurde nicht gespeichert. Bitte ohne "
                "'Reduzieren' erneut speichern."))


class FormsPanel(BasePanel):
    TITLE         = "Formulare / Reduzieren"
    SUBTITLE      = "Formularfelder ausfuellen und einbetten."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        self._fields = {}
        lb=QPushButton(tr("  Felder laden")); lb.setObjectName("secondaryBtn")
        lb.clicked.connect(self._load); layout.addWidget(lb)
        grp=QGroupBox(tr("FORMULARFELDER")); grp_vl=QVBoxLayout(grp); grp_vl.setContentsMargins(4,4,4,4)
        self.form_box=QWidget()
        self.form_vbox=QVBoxLayout(self.form_box); self.form_vbox.setContentsMargins(0,0,0,0)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.form_box); scroll.setMinimumHeight(140)
        grp_vl.addWidget(scroll)
        layout.addWidget(grp)
        self.flatten=QCheckBox(tr("Nach dem Speichern reduzieren (fuer Druck)"))
        self.flatten.setChecked(True); layout.addWidget(self.flatten)

    def _load(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        self._fields.clear()
        while self.form_vbox.count():
            item = self.form_vbox.takeAt(0)
            if item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()
                item.layout().deleteLater()
            elif item.widget():
                item.widget().deleteLater()
        try:
            from pypdf import PdfReader
            fields=PdfReader(src, strict=False).get_fields()
            if not fields: self.log.log(tr("Keine Formularfelder gefunden.")); return
            for name,field in fields.items():
                ft=field.get("/FT",""); val=field.get("/V","")
                if ft=="/Btn":
                    w=QCheckBox(); w.setChecked(str(val) in ("/Yes","/On","Yes","On"))
                else:
                    w=QLineEdit(); w.setText(str(val) if val else "")
                self._fields[name]=w
                self.form_vbox.addLayout(row(name, w))
            self.log.log(tr('{p0} Feld(er) geladen.').format(p0=len(fields)))
        except Exception as e: self.log.log(str(e),error=True)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("Ausgefuelltes Formular speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        import tempfile
        from pypdf import PdfReader, PdfWriter
        reader=PdfReader(src, strict=False); writer=PdfWriter(); writer.append(reader)
        data={name: ("/Yes" if (isinstance(w,QCheckBox) and w.isChecked()) else
                     "/Off" if isinstance(w,QCheckBox) else w.text())
              for name,w in self._fields.items()}
        for page in writer.pages: writer.update_page_form_field_values(page,data)
        filled_fd, filled = tempfile.mkstemp(suffix=".pdf"); os.close(filled_fd)
        try:
            with open(filled,"wb") as f: writer.write(f)
            if self.flatten.isChecked():
                _flatten_form(filled, out,
                              has_values=any(str(v).strip() not in ("", "/Off")
                                             for v in data.values()))
            else:
                shutil.copyfile(filled, out)
        finally:
            try: os.remove(filled)
            except OSError: pass
        self.open_result(out,tr("Formular ausgefuellt"))
        return tr('Formular ausgefuellt ({p0} Felder)').format(p0=len(data))
