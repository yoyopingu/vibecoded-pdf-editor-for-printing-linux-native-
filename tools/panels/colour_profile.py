"""
ColourProfilePanel, moved verbatim out of tools/all_tools.py.
See tools/panels/__init__.py.
"""
import os, shutil
from PyQt6.QtWidgets import QVBoxLayout, QPushButton, QComboBox, QGroupBox, QTextEdit
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import row
from tools.panels._verify import _verify_pages_intact


# ══════════════════════════════════════════════════════════════════════════════
# COLOUR PROFILE
# ══════════════════════════════════════════════════════════════════════════════
class ColourProfilePanel(BasePanel):
    TITLE         = "Farbprofil / CMYK"
    SUBTITLE      = "ICC-Profile pruefen und in CMYK umwandeln."
    OPENS_NEW_TAB = True

    # (label incl. real profile name + paper/use-case, candidate .icc filenames).
    # The generic option needs no ICC file; the named ones use the matching .icc
    # via Ghostscript when present (drop them in ~/.local/share/copyshop_pdf_suite/icc/).
    CMYK_PROFILES = [
        (tr("Standard (generisch) — universell, ohne ICC-Datei"), None),
        (tr("ISO Coated v2 (FOGRA39) — gestrichenes Papier, EU-Offset-Standard"),
            ("ISOcoated_v2_eci.icc", "ISOcoated_v2_300_eci.icc")),
        (tr("PSO Coated v3 (FOGRA51) — modernes gestrichenes Papier, Premium-Offset"),
            ("PSOcoated_v3.icc",)),
        (tr("PSO Uncoated v3 (FOGRA52) — ungestrichenes/Naturpapier, Bücher & Briefbögen"),
            ("PSOuncoated_v3_FOGRA52.icc", "PSO_Uncoated_ISO12647_eci.icc")),
        (tr("U.S. Web Coated (SWOP) v2 — US-Rollenoffset, Magazine (gestrichen)"),
            ("USWebCoatedSWOP.icc",)),
        (tr("Coated GRACoL 2006 — US-Bogenoffset, hochwertiges gestrichenes Papier"),
            ("GRACoL2006_Coated1v2.icc", "CGATS21_CRPC6.icc")),
    ]

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
        for label, cands in self.CMYK_PROFILES:
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

    def _resolve_icc(self, candidates):
        """Return the path to the first available .icc among `candidates`
        (searching the app icc dir + common system dirs), or None."""
        if not candidates:
            return None
        dirs = [
            os.path.expanduser("~/.local/share/copyshop_pdf_suite/icc/"),
            "/usr/share/color/icc/",
            "/usr/share/color/icc/colord/",
        ]
        for name in candidates:
            for d in dirs:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p
        return None

    def _inspect(self):
        try: src=self.require_pdf()
        except ValueError as e: self.log.log(str(e),error=True); return
        try:
            import pikepdf
            CS_MAP={
                "/DeviceRGB":"RGB", "/DeviceCMYK":"CMYK", "/DeviceGray":"Graustufen",
                "/CalRGB":"Kal. RGB", "/CalGray":"Kal. Grau", "/ICCBased":"ICC",
                "/Lab":"CIE Lab", "/Separation":tr("Sonderfarbe"), "/DeviceN":"DeviceN",
            }
            pdf=pikepdf.open(src)
            found=set()

            def _cs_name(obj):
                try:
                    if isinstance(obj, pikepdf.Array): return str(obj[0])
                    return str(obj)
                except Exception: return ""

            def _scan(res):
                if res is None: return
                if "/ColorSpace" in res:
                    cs_dict=res["/ColorSpace"]
                    if isinstance(cs_dict, pikepdf.Dictionary):
                        for v in cs_dict.values():
                            n=_cs_name(v)
                            if n in CS_MAP: found.add(n)
                    else:
                        n=_cs_name(cs_dict)
                        if n in CS_MAP: found.add(n)
                if "/XObject" in res:
                    xobj=res["/XObject"]
                    if isinstance(xobj, pikepdf.Dictionary):
                        for v in xobj.values():
                            try:
                                if v.get("/Subtype")==pikepdf.Name("/Image") and "/ColorSpace" in v:
                                    n=_cs_name(v["/ColorSpace"])
                                    if n in CS_MAP: found.add(n)
                                elif v.get("/Subtype")==pikepdf.Name("/Form"):
                                    if "/Resources" in v: _scan(v["/Resources"])
                            except Exception: pass

            for page in pdf.pages:
                res=page.get("/Resources")
                if res: _scan(res)
                # Content Stream scannen für direkte Farboperatoren (GS-Vektoren)
                if not found or found == {"/DeviceGray"}:
                    try:
                        import re
                        stream_bytes = b""
                        contents = page.get("/Contents")
                        if contents is not None:
                            if isinstance(contents, pikepdf.Array):
                                for c in contents: stream_bytes += bytes(c.read_bytes())
                            else:
                                stream_bytes = bytes(contents.read_bytes())
                        text = stream_bytes.decode("latin-1", errors="replace")
                        if re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+[kK]\b', text):
                            found.add("/DeviceCMYK")
                        if re.search(r'[\d.]+\s+[\d.]+\s+[\d.]+\s+r[gG]\b', text):
                            found.add("/DeviceRGB")
                        if re.search(r'[\d.]+\s+[gG]\b', text):
                            found.add("/DeviceGray")
                    except Exception: pass
            n_pages = len(pdf.pages)
            pdf.close()

            readable=[CS_MAP.get(c, c) for c in found]
            is_cmyk="/DeviceCMYK" in found
            is_rgb=bool(found & {"/DeviceRGB","/CalRGB","/ICCBased"})
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
        import subprocess as sp

        src = self.require_pdf()

        if not shutil.which("gs"):
            raise RuntimeError(tr(
                "Ghostscript nicht gefunden.\n"
                "Installation:  sudo pacman -S ghostscript"))

        out = self.save_pdf("CMYK-PDF speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad angegeben."))

        # Bewährter GS-Befehl für RGB→CMYK ohne ICC-Profil-Problematik.
        # -dEncodeColorImages=false / -dEncodeGrayImages=false verhindert
        # Neukomprimierung und Qualitätsverlust bei Bildern.
        # -dPDFSETTINGS=/prepress: höchste Qualität, Fonts eingebettet.
        # Selected CMYK target profile (None = generic). Use its .icc if present.
        candidates = self.profile_combo.currentData()
        icc = self._resolve_icc(candidates)
        prof_label = self.profile_combo.currentText().split(" — ")[0]

        # Ghostscript writes to a temp file, never straight to `out`: the result
        # is checked page by page first, exactly as the greyscale conversion is.
        # pdfwrite can black out a transparency group while exiting 0, and for a
        # prepress file nobody notices until it is on press.
        import tempfile, contextlib, pikepdf
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

            r = sp.run(cmd, capture_output=True, text=True, errors="replace", timeout=300)

            if r.returncode != 0:
                err = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[:500]
                raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(p0=err))
            if not os.path.exists(cmyk_tmp) or os.path.getsize(cmyk_tmp) == 0:
                raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

            with pikepdf.open(src) as _s, pikepdf.open(cmyk_tmp) as _c:
                n_ok = min(len(_s.pages), len(_c.pages))
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
        try:
            import pikepdf
            found_rgb = False
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
                            pass
            pdf_out.close()
            verify = tr("⚠  Einige RGB-Bilder noch vorhanden (eingebettete Profile).") if found_rgb else tr("✓  Farbraum erfolgreich in CMYK konvertiert.")
        except Exception:
            verify = tr("(Verifikation nicht möglich)")
        if damaged:
            verify += "\n⚠  " + tr(
                'ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt '
                'und blieben deshalb unveraendert: {p1}').format(
                    p0=len(damaged),
                    p1=", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items())))

        if icc:
            prof_note = f"Profil: {prof_label}  ({os.path.basename(icc)})"
        elif candidates:
            prof_note = (tr("⚠  Profil '{p0}' nicht installiert — generische CMYK-Konvertierung verwendet.\n   .icc-Datei nach ~/.local/share/copyshop_pdf_suite/icc/ legen.").format(p0=prof_label))
        else:
            prof_note = "Profil: Standard (generisch)"

        self.open_result(out, tr("CMYK konvertiert"))
        return tr('Konvertierung abgeschlossen.\n{p0}\n{p1}').format(p0=prof_note, p1=verify)
