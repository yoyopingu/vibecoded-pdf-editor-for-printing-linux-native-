"""
Formulare / Reduzieren — fill form fields and flatten them, and the
annotations with them, into the page content.
"""
import os, shutil, gc
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QVBoxLayout, QPushButton, QLineEdit, QGroupBox,
                             QCheckBox, QScrollArea, QWidget, QFrame, QLabel)
from tools.panels.base import BasePanel
from tools.i18n import tr
from tools.panels._shared import row


# ══════════════════════════════════════════════════════════════════════════════
# FORMS
# ══════════════════════════════════════════════════════════════════════════════
def _plain_ink(path):
    """Dark pixels per page when the file is rendered *without* form support —
    i.e. what a printer, a RIP or any non-interactive viewer puts on paper."""
    gc.disable()
    try:
        with _pdfium_lock:
            # The one place that must NOT go through open_document(). This
            # measures the page as something with no form support draws it —
            # a printer, a RIP, a plain viewer — because that is the whole
            # question flattening answers. Opened with a form environment it
            # would report the values pdfium paints from the field, which are
            # exactly the ones flattening exists to replace, and the check
            # would pass on a file where nothing had been baked in at all.
            import pypdfium2 as pdfium
            doc = pdfium.PdfDocument(path)      # no-forms: see above
            try:
                out = []
                for i in range(len(doc)):
                    im = doc[i].render(scale=0.5).to_pil().convert("L")
                    out.append(sum(1 for v in im.get_flattened_data() if v < 128))
                return out
            finally:
                doc.close()
    finally:
        gc.collect(); gc.enable()


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

    def build_ui(self, layout):
        self._fields = {}
        lb=QPushButton(tr("  Felder laden")); lb.setObjectName("secondaryBtn")
        lb.clicked.connect(self._load); layout.addWidget(lb)
        grp=QGroupBox(tr("FORMULARFELDER")); grp_vl=QVBoxLayout(grp); grp_vl.setContentsMargins(4,4,4,4)
        self.form_box=QWidget()
        self.form_vbox=QVBoxLayout(self.form_box); self.form_vbox.setContentsMargins(0,0,0,0)
        # A dim placeholder so an empty field list reads as "nothing to fill in"
        # rather than a large empty dark rectangle. It sits inside the same box
        # the fields are built into and is hidden the moment any row appears.
        self._empty_hint = QLabel(tr("Keine Formularfelder"))
        self._empty_hint.setObjectName("dimLabel")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setContentsMargins(0, 24, 0, 24)
        self.form_vbox.addWidget(self._empty_hint)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.form_box); scroll.setMinimumHeight(140)
        grp_vl.addWidget(scroll)
        layout.addWidget(grp)
        self.flatten=QCheckBox(tr("Nach dem Speichern reduzieren (fuer Druck)"))
        self.flatten.setChecked(True); layout.addWidget(self.flatten)

    def _load(self):
        # Reading the fields is I/O and pypdf parsing, which used to run right
        # here on the GUI thread — unnoticeable on a small form, a freeze on a
        # heavy one. Only the read goes to the worker: building QCheckBox/
        # QLineEdit widgets from the result has to stay on the GUI thread, so
        # that part happens in _load_done once the read comes back.
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        self.log.clear_log()
        self.run_async(
            lambda report: _read_fields(src, report),
            on_done=self._load_done,
            busy_label="Felder werden geladen …",
        )

    def _load_done(self, fields):
        self._fields.clear()
        # Drop every built field row but keep the placeholder label at the top.
        self._empty_hint.hide()
        self.form_vbox.removeWidget(self._empty_hint)
        while self.form_vbox.count():
            item = self.form_vbox.takeAt(0)
            if item.layout():
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget(): child.widget().deleteLater()
                item.layout().deleteLater()
            elif item.widget():
                item.widget().deleteLater()
        if not fields:
            self.form_vbox.addWidget(self._empty_hint)
            self._empty_hint.show()
            self.log.log(tr("Keine Formularfelder gefunden.")); return
        self.form_vbox.addWidget(self._empty_hint)
        self._empty_hint.hide()
        for name, ft, val in fields:
            if ft == "/Btn":
                w = QCheckBox(); w.setChecked(str(val) in ("/Yes", "/On", "Yes", "On"))
            else:
                w = QLineEdit(); w.setText(str(val) if val else "")
            self._fields[name] = w
            self.form_vbox.addLayout(row(name, w))
        self.log.log(tr('{p0} Feld(er) geladen.').format(p0=len(fields)))

    def _run_action(self):
        # The field values are read here; writing and the optional flatten —
        # which shells out to Ghostscript — go to a worker.
        src = self.require_pdf()
        out = self.save_pdf("Ausgefuelltes Formular speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        data = {name: ("/Yes" if (isinstance(w, QCheckBox) and w.isChecked()) else
                       "/Off" if isinstance(w, QCheckBox) else w.text())
                for name, w in self._fields.items()}
        flatten = self.flatten.isChecked()

        self.run_async(
            lambda report: _fill_form(src, out, data, flatten, report),
            on_done=self._form_done,
            busy_label="Formular wird ausgefüllt …",
        )
        return None

    def _form_done(self, result):
        out, n_fields = result
        self.log.log(tr('Formular ausgefuellt ({p0} Felder)').format(p0=n_fields))
        self.open_result(out, tr("Formular ausgefuellt"))


def _read_fields(src, report):
    """The form field names, types and current values in `src`, on a worker
    thread. Plain data only — the panel builds widgets from it on the GUI
    thread, in _load_done."""
    from pypdf import PdfReader
    report(tr("Lese Formularfelder …"))
    fields = PdfReader(src, strict=False).get_fields()
    if not fields:
        return []
    return [(name, field.get("/FT", ""), field.get("/V", ""))
            for name, field in fields.items()]


def _fill_form(src, out, data, flatten, report):
    """Write `src` with `data` filled in, optionally flattened. Plain data only."""
    import tempfile
    from pypdf import PdfReader, PdfWriter
    report(tr("Schreibe Datei …"))
    reader = PdfReader(src, strict=False); writer = PdfWriter(); writer.append(reader)
    if not data:
        # No fields were loaded, so there is nothing to fill in. pypdf answers
        # this with "No /AcroForm dictionary in PDF of PdfWriter Object", which
        # arrived as an unexpected exception and printed a traceback at someone
        # who simply picked a PDF that is not a form.
        raise ValueError(tr(
            "Diese PDF enthält keine Formularfelder.\n"
            "\"Felder laden\" zeigt, was ausfüllbar ist."))
    for page in writer.pages:
        writer.update_page_form_field_values(page, data)
    filled_fd, filled = tempfile.mkstemp(suffix=".pdf"); os.close(filled_fd)
    try:
        with open(filled, "wb") as f: writer.write(f)
        if flatten:
            report(tr("Ghostscript: reduziere …"))
            _flatten_form(filled, out,
                          has_values=any(str(v).strip() not in ("", "/Off")
                                         for v in data.values()))
        else:
            shutil.copyfile(filled, out)
    finally:
        try: os.remove(filled)
        except OSError: pass
    return out, len(data)
