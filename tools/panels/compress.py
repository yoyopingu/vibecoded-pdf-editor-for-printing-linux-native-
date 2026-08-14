"""
Komprimieren — shrink a PDF through Ghostscript, and refuse a result that
came back damaged.
"""
import os, subprocess, shutil
from PyQt6.QtWidgets import QVBoxLayout, QPushButton, QComboBox, QGroupBox, QCheckBox
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import row
from tools.panels._verify import _verify_pages_intact


# ══════════════════════════════════════════════════════════════════════════════
# COMPRESS
# ══════════════════════════════════════════════════════════════════════════════
class CompressPanel(BasePanel):
    TITLE         = "Komprimieren"
    SUBTITLE      = "PDF-Dateigroesse reduzieren. Ergebnis wird als neuer Tab geoeffnet."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        gb = QGroupBox(tr("EINSTELLUNGEN")); gl = QVBoxLayout(gb)
        self.preset = QComboBox()
        self.preset.addItems([tr("Screen  (72 dpi — Web)"),
                              tr("Ebook   (150 dpi — Tablet)"),
                              tr("Drucker (300 dpi — Standard)"),
                              tr("Vordruck (300 dpi — Profidruck)")])
        self.preset.setCurrentIndex(2)
        gl.addLayout(row(tr("Qualitaetsstufe:"), self.preset))
        self.gs_check = QCheckBox(tr("Ghostscript verwenden (empfohlen)"))
        self.gs_check.setChecked(shutil.which("gs") is not None)
        gl.addWidget(self.gs_check)
        if not shutil.which("gs"):
            gl.addWidget(make_label("Ghostscript fehlt — sudo pacman -S ghostscript", dim=True))
        layout.addWidget(gb)

    def build_action_row(self, row_layout):
        row_layout.addStretch()
        self.run_btn = QPushButton(tr("  Komprimieren"))
        self.run_btn.setObjectName("actionBtn")
        self.run_btn.clicked.connect(self._safe_run)
        row_layout.addWidget(self.run_btn)

    def _run_action(self):
        # Widget values are read here, on the GUI thread; everything after that
        # is plain data handed to a worker. Ghostscript on a large file is
        # minutes of work, and it used to run right here — the window could not
        # repaint, report progress or be closed for the whole of it.
        src = self.require_pdf()
        out = self.save_pdf("Komprimierte PDF speichern als")
        if not out:
            raise ValueError(tr("Kein Ausgabepfad."))

        preset_map = ["/screen", "/ebook", "/printer", "/prepress"]
        gs_setting = preset_map[self.preset.currentIndex()]
        use_gs = self.gs_check.isChecked() and shutil.which("gs")

        self.run_async(
            lambda report: _compress(src, out, gs_setting, bool(use_gs), report),
            on_done=self._compress_done,
            busy_label="Komprimiere …",
        )
        return None

    def _compress_done(self, result):
        out, size_before, size_after = result
        ratio = (1 - size_after / size_before) * 100 if size_before else 0
        self.log.log(tr('Fertig. {p0} → {p1}  ({p2:+.1f}%)').format(
            p0=_fmt(size_before), p1=_fmt(size_after), p2=ratio))
        self.open_result(out, "Komprimiert")


def _compress(src, out, gs_setting, use_gs, report):
    """Compress `src` into `out` on a worker thread. Plain data only."""
    import pikepdf
    size_before = os.path.getsize(src)

    if use_gs:
        # Into a temp file first, so nothing Ghostscript mangled is handed
        # over: a smaller file is the whole point here, which makes "lost
        # content" indistinguishable from "worked well" by size alone.
        import tempfile
        fd, gs_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
        try:
            report(tr("Ghostscript: komprimiere …"))
            cmd = [
                "gs", "-o", gs_tmp,
                "-sDEVICE=pdfwrite",
                f"-dPDFSETTINGS={gs_setting}",
                "-dNOPAUSE", "-dBATCH", "-dQUIET",
                src,
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)
            if r.returncode != 0:
                raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(
                    p0=(r.stderr or r.stdout or f"exit {r.returncode}")[:400]))
            # -dQUIET means a Ghostscript that fails softly says nothing at
            # all; without this the size check below raised FileNotFoundError
            # instead of reporting that the compression failed.
            if not os.path.exists(gs_tmp) or os.path.getsize(gs_tmp) == 0:
                raise RuntimeError(tr(
                    "Ghostscript hat keine Ausgabedatei erzeugt."))
            with pikepdf.open(src) as _a, pikepdf.open(gs_tmp) as _b:
                n_src, n_out = len(_a.pages), len(_b.pages)
            if n_out != n_src:
                raise RuntimeError(tr(
                    'Komprimierung hat die Seitenzahl veraendert '
                    '({p0} → {p1}) — Datei nicht gespeichert.').format(
                        p0=n_src, p1=n_out))
            report(tr("Prüfe Seiten …"))
            damaged = _verify_pages_intact(src, gs_tmp, range(n_src), None)
            if damaged:
                raise RuntimeError(tr(
                    'Komprimierung hat Seite(n) beschaedigt: {p0} — Datei '
                    'nicht gespeichert.').format(
                        p0=", ".join(f"{i + 1} ({why})"
                                     for i, why in sorted(damaged.items()))))
            shutil.copyfile(gs_tmp, out)
        finally:
            try: os.remove(gs_tmp)
            except OSError: pass
    else:
        report(tr("Komprimiere Streams …"))
        pdf = pikepdf.open(src)
        try:
            pdf.save(out, compress_streams=True, recompress_flate=True)
        finally:
            pdf.close()

    return out, size_before, os.path.getsize(out)


def _fmt(b):
    if b<1024: return f"{b} B"
    elif b<1024**2: return f"{b/1024:.1f} KB"
    else: return f"{b/1024**2:.2f} MB"
