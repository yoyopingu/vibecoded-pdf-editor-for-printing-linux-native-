"""
PDF/X-Export — turn a PDF into a press-ready PDF/X-3 file.

PDF/X is the ISO 15930 subset of PDF that a print shop can accept without
opening it and checking: everything a RIP needs is in the file, and everything
it cannot resolve is forbidden. Concretely, for the X-3 profile written here:

  * all colour is CMYK or greyscale, with one embedded ICC *output intent*
    naming the printing condition it was separated for;
  * every font is embedded, subset;
  * no transparency (Ghostscript flattens it for us — see below);
  * every page carries a TrimBox, so the RIP knows where the finished sheet
    ends;
  * /GTS_PDFXVersion in the document info says which profile it claims.

One button, and no options on it. Which press the shop separates for and how
much image resolution that press can use are properties of the press, not of
the job, so they live in Einstellungen → Druckvorstufe and are set once. What
a *file* brings with it — its bleed, its fonts, its image resolution — is
reporting, and belongs to "Druckvorstufenpruefung"; this panel converts.

It replaced the old "Ebenen (OCG)" tool. Optional-content groups are not part
of PDF/X-3 at all, and Ghostscript resolves them on the way out using the
file's own default configuration: a layer switched off in the source — a
cutter contour, a varnish plate, a "nicht drucken" guide layer — stays off and
is gone from the output. That is the behaviour the layers tool existed to
guarantee, now applied automatically, and the export names the layers it
dropped, because the one thing worse than a cutter line on the plate is not
being told it was there.

Why X-3 and nothing else
------------------------
Ghostscript's ``-dPDFX`` genuinely produces X-3, and forces PDF 1.3 while
doing it — which is also why transparency comes out flattened, and why the
version string here is "PDF/X-3:2002" (the PDF 1.3 revision) rather than the
1.4-based :2003. X-1a would be a one-word change to that string and a lie:
Ghostscript does not guarantee the extra X-1a restrictions, and a file that
claims a conformance it does not have is rejected at the RIP, which is worse
than a file that claims nothing. X-4 needs live transparency and a different
engine altogether.

There are deliberately no "embed fonts" or "convert to CMYK" switches. Both
are requirements of the standard, not preferences; a PDF/X export with them
turned off would be an ordinary PDF wearing a PDF/X label.
"""
import os
import shutil
import subprocess

from PyQt6.QtWidgets import QPushButton

from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._icc import (ICC_DIR, fallback_cmyk_icc, profile_by_key,
                               resolve_icc)
from tools.panels._verify import _verify_pages_intact


PDFX_VERSION = "PDF/X-3:2002"


def _layer_report(path):
    """(names of layers on, names of layers off) in `path`.

    "Off" means the file's default configuration switches it off, which is
    what Ghostscript honours when it resolves optional content away.
    """
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject
    reader = PdfReader(path, strict=False)
    oc = reader.trailer["/Root"].get("/OCProperties")
    if not oc:
        return [], []
    oc = oc.get_object()
    default = oc.get("/D")
    off_ids = set()
    if default is not None:
        for ref in default.get_object().get("/OFF", ArrayObject()):
            off_ids.add(ref.idnum)
    on, off = [], []
    for ref in oc.get("/OCGs", ArrayObject()):
        name = str(ref.get_object().get("/Name", tr("(unbenannt)")))
        (off if ref.idnum in off_ids else on).append(name)
    return on, off


class PdfxPanel(BasePanel):
    TITLE         = "PDF/X-Export"
    SUBTITLE      = "Druckfertige PDF/X-3 erzeugen."
    RUN_LABEL     = "  PDF/X exportieren"
    OPENS_NEW_TAB = True

    def build_ui(self, layout):
        layout.addWidget(make_label(tr(
            "Erzeugt {p0}: alle Farben in CMYK gegen das eingestellte "
            "Ausgabeprofil, alle Schriften eingebettet, Transparenz reduziert, "
            "Ebenen aufgeloest, Bilder auf Druckauflösung. Das Ergebnis wird "
            "geprueft, bevor es gespeichert wird.").format(p0=PDFX_VERSION),
            dim=True))
        layout.addWidget(make_label(tr(
            "Was die Datei mitbringt — Anschnitt, Schriften, Bildauflösung — "
            "zeigt „Druckvorstufenpruefung“."), dim=True))

        # The condition and the resolution belong to the press, not to the job,
        # so they live in Einstellungen and the panel is one button. This line
        # is here so the setting in force is never invisible at the moment it
        # is being applied.
        self._cond_lbl = make_label("", dim=True)
        layout.addWidget(self._cond_lbl)
        change = QPushButton(tr("Ausgabebedingung ändern…"))
        change.setObjectName("secondaryBtn")
        change.setMaximumWidth(260)
        change.clicked.connect(self._open_settings)
        layout.addWidget(change)

        gs_ok = bool(shutil.which("gs"))
        layout.addWidget(make_label(
            tr("✓  Ghostscript verfuegbar") if gs_ok else
            tr("✗  Ghostscript fehlt  →  sudo pacman -S ghostscript"), dim=True))
        layout.addStretch()
        self._refresh_condition()

    def _refresh_condition(self):
        from tools.shell.settings import AppSettings
        label, candidates, _oci, _cond = profile_by_key(
            AppSettings.get().pdfx_condition())
        dpi = AppSettings.get().pdfx_image_dpi()
        name = label.split(" — ")[0]
        if candidates and not resolve_icc(candidates):
            name = tr("{p0}  (Profil fehlt — generisches CMYK)").format(p0=name)
        self._cond_lbl.setText(
            tr("Ausgabebedingung: {p0}   ·   Bilder: {p1} dpi").format(
                p0=name, p1=dpi))

    def _open_settings(self):
        from tools.shell.settings import PrepressDialog
        PrepressDialog(self).exec()
        self._refresh_condition()

    # ── the export ───────────────────────────────────────────────────────────

    def _run_action(self):
        # Widgets are read here; Ghostscript and the verification go to a
        # worker — a prepress file is minutes of pdfwrite.
        src = self.require_pdf()
        if not shutil.which("gs"):
            raise RuntimeError(tr("Ghostscript nicht gefunden.\n"
                                  "Installation:  sudo pacman -S ghostscript"))

        from tools.shell.settings import AppSettings
        settings = AppSettings.get()
        full_label, candidates, oci, condition = profile_by_key(
            settings.pdfx_condition())
        label = full_label.split(" — ")[0]
        dpi = settings.pdfx_image_dpi()
        icc = resolve_icc(candidates)
        if icc is None:
            icc = fallback_cmyk_icc()
            if icc is None:
                raise RuntimeError(tr(
                    "Kein CMYK-ICC-Profil gefunden. PDF/X verlangt ein "
                    "eingebettetes Ausgabeprofil.\n.icc-Datei nach {p0} legen.")
                    .format(p0=ICC_DIR))
            if candidates:
                # Named condition asked for, profile not installed. Exporting
                # under its identifier while embedding a generic profile would
                # be a false claim, so the intent describes what was embedded.
                note = tr("⚠  Profil '{p0}' nicht installiert — generisches CMYK "
                          "eingebettet.\n   .icc-Datei nach {p1} legen.").format(
                              p0=label, p1=ICC_DIR)
                oci, condition = "Custom", "Generic CMYK, no characterised printing condition"
            else:
                note = tr("Ausgabebedingung: {p0}").format(p0=label)
        else:
            note = tr("Ausgabebedingung: {p0}  ({p1})").format(
                p0=label, p1=os.path.basename(icc))

        out = self.save_pdf("PDF/X speichern als")
        if not out:
            raise ValueError(tr("Kein Ausgabepfad angegeben."))

        self.run_async(
            lambda report: _export_pdfx(src, out, icc, oci, condition, dpi, report),
            on_done=lambda result: self._done(result, note),
            busy_label="PDF/X wird erzeugt …",
        )
        return None

    def _done(self, result, note):
        out, dropped = result
        lines = [tr('PDF/X-Export abgeschlossen ({p0}).').format(p0=PDFX_VERSION), note]
        if dropped:
            lines.append(tr("Aufgeloeste Ebenen (nicht in der Ausgabe): {p0}")
                         .format(p0=", ".join(dropped)))
        self.log.log("\n".join(lines))
        self.open_result(out, tr("PDF/X exportiert"))


# ── the worker ───────────────────────────────────────────────────────────────

def _pdfx_defs(icc_path, oci, condition):
    """The PostScript prologue that makes pdfwrite write an output intent.

    Ghostscript ships a sample of this (lib/PDFX_def.ps) that guesses the
    profile's component count from ColorConversionStrategy and warns that the
    guess is unreliable. We always separate to CMYK, so /N is simply 4 and the
    guessing goes away.
    """
    def ps(text):
        # Parentheses and backslashes delimit PostScript strings; a profile
        # path or a condition name containing one would end the string early.
        return str(text).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    return f"""%!
[ /GTS_PDFXVersion ({ps(PDFX_VERSION)})
  /Trapped /False
/DOCINFO pdfmark

/ICCProfile ({ps(icc_path)}) def

[/_objdef {{icc_PDFX}} /type /stream /OBJ pdfmark
[{{icc_PDFX}} << /N 4 >> /PUT pdfmark
[{{icc_PDFX}} ICCProfile (r) file /PUT pdfmark

[/_objdef {{OutputIntent_PDFX}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFX}} <<
  /Type /OutputIntent
  /S /GTS_PDFX
  /OutputCondition ({ps(condition)})
  /OutputConditionIdentifier ({ps(oci)})
  /RegistryName (http://www.color.org)
  /DestOutputProfile {{icc_PDFX}}
>> /PUT pdfmark
[{{Catalog}} <</OutputIntents [ {{OutputIntent_PDFX}} ]>> /PUT pdfmark
"""


def _check_conformance(path):
    """Raise unless `path` really carries what PDF/X requires.

    pdfwrite exits 0 on plenty of files it did not fully convert, and the
    whole value of this tool is that its output can be handed over without
    being opened again. If the claim is not backed by the file, the file does
    not ship.
    """
    import pikepdf
    with pikepdf.open(path) as pdf:
        if str(pdf.docinfo.get("/GTS_PDFXVersion", "")) != PDFX_VERSION:
            raise RuntimeError(tr("Die Ausgabe traegt keine PDF/X-Kennung."))
        intents = pdf.Root.get("/OutputIntents")
        if not intents or len(intents) == 0:
            raise RuntimeError(tr("Die Ausgabe hat keinen Output-Intent."))
        intent = intents[0]
        if intent.get("/DestOutputProfile") is None:
            raise RuntimeError(tr("Der Output-Intent enthaelt kein ICC-Profil."))
        missing = [i + 1 for i, p in enumerate(pdf.pages)
                   if "/TrimBox" not in p.obj and "/ArtBox" not in p.obj]
        if missing:
            raise RuntimeError(tr("Seiten ohne TrimBox: {p0}").format(
                p0=", ".join(str(i) for i in missing)))


def _boxes_survived(src, dst):
    """Page numbers whose trim geometry changed, described. Empty is good.

    The TrimBox is where the guillotine goes. Ghostscript does carry it and
    the BleedBox through — including swapping them correctly when it bakes a
    /Rotate into the page — so this is a check, not a repair. It exists
    because "Ghostscript preserves it" is an assumption about someone else's
    program, and the cost of that assumption being wrong one day is a job
    trimmed to the wrong size.

    A page whose MediaBox was rotated is compared against the rotated source
    box, since that is the same rectangle described in the new page space. A
    source page with no TrimBox is skipped: Ghostscript's TrimBox = MediaBox
    is the right answer for "no trim declared", and inventing one is how you
    crop a job that never asked for bleed.
    """
    import pikepdf

    def rect(page, key):
        if key not in page.obj:
            return None
        v = [float(x) for x in page.obj[key]]
        return (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))

    def swap(r):
        return None if r is None else (r[1], r[0], r[3], r[2])

    problems = []
    with pikepdf.open(src) as s_pdf, pikepdf.open(dst) as d_pdf:
        for i, (s_page, d_page) in enumerate(zip(s_pdf.pages, d_pdf.pages)):
            s_media, d_media = rect(s_page, "/MediaBox"), rect(d_page, "/MediaBox")
            if s_media is None or d_media is None:
                continue
            # Rotation baked in: the page is the same sheet described sideways.
            rotated = (abs((s_media[2] - s_media[0]) - (d_media[3] - d_media[1])) < 1.0
                       and abs((s_media[3] - s_media[1]) - (d_media[2] - d_media[0])) < 1.0
                       and abs((s_media[2] - s_media[0]) - (d_media[2] - d_media[0])) > 1.0)
            for key in ("/TrimBox", "/BleedBox"):
                want = rect(s_page, key)
                if want is None:
                    continue
                if rotated:
                    want = swap(want)
                got = rect(d_page, key)
                if got is None or any(abs(a - b) > 1.0 for a, b in zip(want, got)):
                    problems.append(tr('{p0} ({p1})').format(
                        p0=i + 1, p1=key.lstrip("/")))
    return problems


def _export_pdfx(src, out, icc, oci, condition, dpi, report):
    """Write `src` to `out` as PDF/X. Plain data only — runs on a worker."""
    import tempfile
    import pikepdf

    on, off = _layer_report(src)

    fd, defs = tempfile.mkstemp(suffix=".ps"); os.close(fd)
    fd, tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    try:
        with open(defs, "w") as f:
            f.write(_pdfx_defs(icc, oci, condition))

        # -dPDFX is what switches pdfwrite into PDF/X mode; the prologue has to
        # be read before the input, or the output intent lands in no document.
        cmd = [
            "gs", "-dPDFX", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dQUIET",
            "-sDEVICE=pdfwrite",
            "-dPDFSETTINGS=/prepress",
            "-sProcessColorModel=DeviceCMYK",
            "-sColorConversionStrategy=CMYK",
            "-dEmbedAllFonts=true",
            "-dSubsetFonts=true",
            # After /prepress, which keeps images at source resolution — these
            # override it (verified: a 600 dpi image comes back at 300 and the
            # file halves; a 200 dpi one is left alone rather than upsampled).
            # Resolution above what the press can image is RIP time nobody sees
            # on paper, and it is the main reason a job is slow to print.
            "-dDownsampleColorImages=true", f"-dColorImageResolution={dpi}",
            "-dColorImageDownsampleType=/Bicubic",
            "-dDownsampleGrayImages=true", f"-dGrayImageResolution={dpi}",
            "-dGrayImageDownsampleType=/Bicubic",
            # Bilevel line art is left alone: downsampling it is what makes a
            # scanned drawing come out ragged.
            "-dDownsampleMonoImages=false",
            f"-sOutputFile={tmp}",
            defs, src,
        ]
        report(tr("Ghostscript: PDF/X-Konvertierung …"))
        r = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=600)
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[:500]
            raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(p0=err))
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

        _check_conformance(tmp)
        moved = _boxes_survived(src, tmp)
        if moved:
            raise RuntimeError(tr(
                'Der Beschnitt hat sich veraendert auf Seite(n): {p0} — die '
                'Datei wurde nicht gespeichert.').format(p0=", ".join(moved)))

        # Same guard the CMYK and greyscale conversions use: pdfwrite can black
        # a page out while exiting 0, and on a prepress file nobody notices
        # until it is on press.
        with pikepdf.open(src) as _s, pikepdf.open(tmp) as _o:
            n_src, n_out = len(_s.pages), len(_o.pages)
        if n_src != n_out:
            raise RuntimeError(tr(
                'Seitenzahl stimmt nicht: {p0} statt {p1} — die Datei wurde '
                'nicht gespeichert.').format(p0=n_out, p1=n_src))
        report(tr("Prüfe Seiten …"))
        # Pages carrying only content from a switched-off layer legitimately
        # lose ink, so they are not evidence of damage.
        damaged = _verify_pages_intact(src, tmp, range(n_src), None) if not off else {}
        if damaged:
            raise RuntimeError(tr(
                'PDF/X-Export hat Seite(n) beschaedigt: {p0} — die Datei wurde '
                'nicht gespeichert.').format(
                    p0=", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items()))))
        shutil.copyfile(tmp, out)
    finally:
        for path in (defs, tmp):
            try:
                os.remove(path)
            except OSError:
                pass
    return out, off
