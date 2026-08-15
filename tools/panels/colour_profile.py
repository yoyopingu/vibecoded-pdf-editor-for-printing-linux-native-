"""
Farbprofil / CMYK — report which colour spaces a file uses, and convert
it to CMYK or sRGB through Ghostscript, checking the result before it ships.
"""
import logging
import os, shutil
from PyQt6.QtWidgets import QVBoxLayout, QPushButton, QComboBox, QGroupBox, QTextEdit
from tools._base import BasePanel, make_label
from tools.colorspace import document_colorspaces, has_cmyk, has_rgb
from tools.i18n import tr
from tools.panels._icc import CMYK_PROFILES, ICC_DIR, resolve_icc
from tools.panels._shared import row
from tools.panels._verify import _verify_pages_intact


# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PROFILE
# ══════════════════════════════════════════════════════════════════════════════
class ColourProfilePanel(BasePanel):
    TITLE         = "Farbprofil / CMYK"
    SUBTITLE      = "ICC-Profile pruefen und in CMYK umwandeln."
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        ib=QPushButton(tr("  Farbprofil pruefen")); ib.setObjectName("secondaryBtn")
        ib.clicked.connect(self._inspect); layout.addWidget(ib)
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMaximumHeight(150)
        self.report.setPlaceholderText(tr("Farbprofil-Info erscheint hier...")); layout.addWidget(self.report)

        cb=QGroupBox(tr("IN CMYK UMWANDELN")); cl=QVBoxLayout(cb)
        cl.addWidget(make_label(tr(
            "Konvertiert via Ghostscript nach DeviceCMYK. "
            "Qualitaetsstufe: Prepress (hoechste Qualitaet, alle Fonts eingebettet)."), dim=True))
        self.profile_combo = QComboBox()
        for label, cands, _oci, _condition in CMYK_PROFILES:
            self.profile_combo.addItem(tr(label), cands)
        cl.addLayout(row(tr("CMYK-Profil:"), self.profile_combo))
        cl.addWidget(make_label(tr(
            "Benannte Profile nutzen die passende .icc-Datei aus "
            "~/.local/share/copyshop_pdf_suite/icc/ — fehlt sie, wird generisch "
            "konvertiert."), dim=True))
        gs_ok = bool(shutil.which("gs"))
        status = tr("✓  Ghostscript verfuegbar") if gs_ok else tr("✗  Ghostscript fehlt  →  sudo pacman -S ghostscript")
        cl.addWidget(make_label(status, dim=True))
        layout.addWidget(cb)

    def _inspect(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        try:
            import pikepdf
            # Presentation only — the scanning is tools/colorspace.py, shared
            # with the viewer's label and the greyscale scan.
            CS_MAP={
                "/DeviceRGB":"RGB", "/DeviceCMYK":"CMYK", "/DeviceGray":"Graustufen",
                "/CalRGB":"Kal. RGB", "/CalGray":"Kal. Grau", "/ICCBased":"ICC",
                "/Lab":"CIE Lab", "/Separation":tr("Sonderfarbe"), "/DeviceN":"DeviceN",
            }
            found = set(document_colorspaces(src))
            with pikepdf.open(src) as pdf:
                n_pages = len(pdf.pages)

            readable=[CS_MAP[c] for c in found if c in CS_MAP]
            is_cmyk=has_cmyk(found)
            is_rgb=has_rgb(found)
            lines=[
                tr('Datei:   {p0}').format(p0=os.path.basename(src)),
                tr('Seiten:  {p0}').format(p0=n_pages),
                "",
                tr("Farbraum: {p0}").format(p0=', '.join(sorted(readable)) if readable else tr('nicht erkennbar')),
                "",
            ]
            if is_cmyk and not is_rgb:
                lines.append("✓  CMYK — druckfertig.")
            elif is_rgb and is_cmyk:
                lines.append(tr("⚠  Gemischt (RGB + CMYK) — vor Profidruck vollständig in CMYK umwandeln."))
            elif is_rgb:
                lines.append(tr("⚠  RGB — vor Profidruck in CMYK umwandeln."))
            else:
                lines.append(tr("ℹ  Farbraum nicht eindeutig erkennbar."))
            self.report.setPlainText("\n".join(lines))
            self.log.log(tr("Pruefung abgeschlossen."))
        except Exception as e:
            self.log.log(str(e), error=True)

    def _run_action(self):
        # Everything read from a widget is read here; the Ghostscript run and
        # the page-by-page verification go to a worker. On a heavy file that is
        # minutes during which the window used to be unable to repaint.
        src = self.require_pdf()

        if not shutil.which("gs"):
            raise RuntimeError(tr(
                "Ghostscript nicht gefunden.\n"
                "Installation:  sudo pacman -S ghostscript"))

        out = self.save_pdf("CMYK-PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad angegeben."))

        # Selected CMYK target profile (None = generic). Use its .icc if present.
        candidates = self.profile_combo.currentData()
        icc = resolve_icc(candidates)
        prof_label = self.profile_combo.currentText().split(" — ")[0]
        if icc:
            prof_note = f"Profil: {prof_label}  ({os.path.basename(icc)})"
        elif candidates:
            prof_note = (tr("⚠  Profil '{p0}' nicht installiert — generische CMYK-Konvertierung verwendet.\n   .icc-Datei nach {p1} legen.").format(p0=prof_label, p1=ICC_DIR))
        else:
            prof_note = "Profil: Standard (generisch)"

        self.run_async(
            lambda report: _to_cmyk(src, out, icc, report),
            on_done=lambda result: self._cmyk_done(result, prof_note),
            busy_label="CMYK-Konvertierung …",
        )
        return None

    def _cmyk_done(self, result, prof_note):
        out, damaged, found_rgb, verified = result
        if not verified:
            verify = tr("(Verifikation nicht möglich)")
        else:
            verify = (tr("⚠  Einige RGB-Bilder noch vorhanden (eingebettete Profile).")
                      if found_rgb else
                      tr("✓  Farbraum erfolgreich in CMYK konvertiert."))
        if damaged:
            verify += "\n⚠  " + tr(
                'ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt '
                'und blieben deshalb unveraendert: {p1}').format(
                    p0=len(damaged),
                    p1=", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items())))
        self.log.log(tr('Konvertierung abgeschlossen.\n{p0}\n{p1}').format(
            p0=prof_note, p1=verify))
        self.open_result(out, tr("CMYK konvertiert"))


def _to_cmyk(src, out, icc, report):
    """Convert `src` to CMYK into `out` on a worker thread.

    Returns (out, damaged, found_rgb, verified). Plain data only — the panel
    turns it into a report back on the GUI thread.
    """
    import subprocess as sp
    import tempfile, contextlib, pikepdf

    # Bewährter GS-Befehl für RGB→CMYK ohne ICC-Profil-Problematik.
    # -dEncodeColorImages=false / -dEncodeGrayImages=false verhindert
    # Neukomprimierung und Qualitätsverlust bei Bildern.
    # -dPDFSETTINGS=/prepress: höchste Qualität, Fonts eingebettet.
    #
    # Ghostscript writes to a temp file, never straight to `out`: the result
    # is checked page by page first, exactly as the greyscale conversion is.
    # pdfwrite can black out a transparency group while exiting 0, and for a
    # prepress file nobody notices until it is on press.
    fd, cmyk_tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    try:
        cmd = [
            "gs",
            "-o", cmyk_tmp,
            "-sDEVICE=pdfwrite",
            "-dPDFSETTINGS=/prepress",
            "-dEncodeColorImages=false",
            "-dEncodeGrayImages=false",
            "-dEncodeMonoImages=false",
            "-sProcessColorModel=DeviceCMYK",
            "-sColorConversionStrategy=CMYK",
            "-sColorConversionStrategyForImages=CMYK",
        ]
        if icc:
            cmd.append(f"-sOutputICCProfile={icc}")   # convert to this named CMYK space
        cmd.append(src)

        report(tr("Ghostscript: CMYK-Konvertierung …"))
        r = sp.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)

        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[:500]
            raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(p0=err))
        if not os.path.exists(cmyk_tmp) or os.path.getsize(cmyk_tmp) == 0:
            raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

        with pikepdf.open(src) as _s, pikepdf.open(cmyk_tmp) as _c:
            n_ok = min(len(_s.pages), len(_c.pages))
        report(tr("Prüfe Seiten …"))
        damaged = _verify_pages_intact(src, cmyk_tmp, range(n_ok), None)
        if damaged:
            # Keep the untouched original for those pages rather than hand
            # over a file with black rectangles in it.
            with contextlib.ExitStack() as stack:
                s_pdf = stack.enter_context(pikepdf.open(src))
                c_pdf = stack.enter_context(pikepdf.open(cmyk_tmp))
                o_pdf = stack.enter_context(pikepdf.Pdf.new())
                for i in range(n_ok):
                    o_pdf.pages.append(
                        s_pdf.pages[i] if i in damaged else c_pdf.pages[i])
                o_pdf.save(out)
        else:
            shutil.copyfile(cmyk_tmp, out)
    finally:
        try: os.remove(cmyk_tmp)
        except OSError: pass

    # Ergebnis verifizieren
    found_rgb = False
    try:
        import pikepdf
        pdf_out = pikepdf.open(out)
        for page in pdf_out.pages:
            res = page.get("/Resources")
            if not res: continue
            xobj = res.get("/XObject")
            if xobj and isinstance(xobj, pikepdf.Dictionary):
                for v in xobj.values():
                    try:
                        if v.get("/Subtype") == pikepdf.Name("/Image"):
                            cs = v.get("/ColorSpace")
                            if cs:
                                name = str(cs[0]) if isinstance(cs, pikepdf.Array) else str(cs)
                                if name in ("/DeviceRGB", "/CalRGB"):
                                    found_rgb = True
                    except Exception:
                        logging.debug("colour profile: could not read an image's "
                                      "colour space", exc_info=True)
        pdf_out.close()
        verified = True
    except Exception:
        logging.debug("colour profile: verification failed", exc_info=True)
        verified = False

    return out, damaged, found_rgb, verified
