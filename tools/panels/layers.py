"""
Ebenen (OCG) — list a file's optional-content groups, and flatten or drop
them.
"""
import os, subprocess, shutil
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QCheckBox, QScrollArea, QWidget
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._verify import _verify_pages_intact


# ══════════════════════════════════════════════════════════════════════════════
# LAYERS
# ══════════════════════════════════════════════════════════════════════════════
class LayersPanel(BasePanel):
    TITLE         = "Ebenen (OCG)"
    SUBTITLE      = "Optionale Inhaltsgruppen steuern."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        self._cbs=[]
        tb=QHBoxLayout()
        for txt,fn in [(tr("Ebenen laden"),self._load),(tr("Alle AN"),lambda:self._all(True)),(tr("Alle AUS"),lambda:self._all(False))]:
            b=QPushButton(txt); b.setObjectName("secondaryBtn"); b.clicked.connect(fn); tb.addWidget(b)
        tb.addStretch(); layout.addLayout(tb)
        scroll_content=QWidget(); self.cb_layout=QVBoxLayout(scroll_content)
        self.no_layers=make_label(tr("Keine Ebenen gefunden."),dim=True); self.cb_layout.addWidget(self.no_layers)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(scroll_content); scroll.setMinimumHeight(130)
        layout.addWidget(scroll)
        self.flatten=QCheckBox(tr("Ausgabe reduzieren")); self.flatten.setChecked(True); layout.addWidget(self.flatten)

    def _load(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        for _,cb in self._cbs: cb.deleteLater()
        self._cbs.clear()
        try:
            from pypdf import PdfReader; from pypdf.generic import ArrayObject
            reader=PdfReader(src, strict=False); root=reader.trailer["/Root"]; oc=root.get("/OCProperties")
            if not oc: self.no_layers.setVisible(True); self.log.log(tr("Keine Ebenen gefunden.")); return
            self.no_layers.setVisible(False)
            for ref in oc.get_object().get("/OCGs",ArrayObject()):
                obj=ref.get_object(); name=str(obj.get("/Name","(unbenannt)"))
                cb=QCheckBox("  "+name); cb.setChecked(True)
                self.cb_layout.addWidget(cb); self._cbs.append((ref.idnum, cb))
            self.log.log(tr('{p0} Ebene(n) gefunden.').format(p0=len(self._cbs)))
        except Exception as e: self.log.log(str(e),error=True)

    def _all(self,state):
        for _,cb in self._cbs: cb.setChecked(state)

    def _run_action(self):
        src=self.require_pdf()
        out=self.save_pdf("PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, NameObject
        reader = PdfReader(src, strict=False)
        writer = PdfWriter(); writer.append(reader)
        if self._cbs:
            root = writer.trailer["/Root"]
            oc = root.get("/OCProperties")
            if oc:
                oc_obj = oc.get_object()
                checked = {idnum for idnum, cb in self._cbs if cb.isChecked()}
                on_arr  = ArrayObject()
                off_arr = ArrayObject()
                for ref in oc_obj.get("/OCGs", ArrayObject()):
                    if ref.idnum in checked:
                        on_arr.append(ref)
                    else:
                        off_arr.append(ref)
                oc_obj[NameObject("/ON")] = on_arr
                if off_arr:
                    oc_obj[NameObject("/OFF")] = off_arr
        with open(out, "wb") as f: writer.write(f)
        if self.flatten.isChecked() and shutil.which("gs"):
            import tempfile
            fd, flat = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
            try:
                cmd=["gs","-sDEVICE=pdfwrite","-dCompatibilityLevel=1.5",
                     "-dNOPAUSE","-dBATCH","-dQUIET",f"-sOutputFile={flat}",out]
                r=subprocess.run(cmd,capture_output=True, text=True, errors="replace",timeout=300)
                if r.returncode!=0:
                    # -dQUIET: a failing Ghostscript often says nothing, and
                    # RuntimeError("") is an error dialog with no error in it.
                    raise RuntimeError(r.stderr.strip() or r.stdout.strip()
                                       or tr('Ghostscript beendet mit Code {p0}').format(p0=r.returncode))
                if not os.path.exists(flat) or os.path.getsize(flat) == 0:
                    raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))
                # Check before replacing: flattening layers runs the same
                # pdfwrite path that can black a page out while exiting 0, and
                # this one overwrites the file the user is about to be handed.
                import pikepdf as _pk
                with _pk.open(out) as _a, _pk.open(flat) as _b:
                    n_a, n_b = len(_a.pages), len(_b.pages)
                damaged = (_verify_pages_intact(out, flat, range(min(n_a, n_b)), None)
                           if n_a == n_b else {0: tr("Seitenzahl")})
                if damaged:
                    raise RuntimeError(tr(
                        'Reduzieren hat Seite(n) beschaedigt: {p0} — die '
                        'unreduzierte Datei wurde behalten.').format(
                            p0=", ".join(f"{i + 1} ({why})"
                                         for i, why in sorted(damaged.items()))))
                os.replace(flat, out)
            finally:
                try: os.remove(flat)
                except OSError: pass
        self.open_result(out,tr("Ebenen verarbeitet"))
        return tr("Ebenen verarbeitet")
