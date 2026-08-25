"""
PDF/X-Export — turn a PDF into a press-ready PDF/X file.

PDF/X is the ISO 15930 subset of PDF that a print shop can accept without
opening it and checking: everything a RIP needs is in the file, and everything
it cannot resolve is forbidden. Whichever profile is written:

  * all colour is CMYK or greyscale, with one embedded ICC *output intent*
    naming the printing condition it was separated for;
  * every font is embedded, subset;
  * every page carries a TrimBox, so the RIP knows where the finished sheet
    ends;
  * /GTS_PDFXVersion in the document info says which profile it claims.

One button, and no options on it. Which press the shop separates for and how
much image resolution that press can use are properties of the press, not of
the job, so they live in Einstellungen → Druckvorstufe and are set once. What
a *file* brings with it — its bleed, its fonts, its image resolution — is
reporting, and belongs to "Druckvorstufenprüfung"; this panel converts.

It replaced the old "Ebenen (OCG)" tool. X-4 keeps optional content, so a
layered file comes through with its layers. X-3 has no optional content at
all, and there Ghostscript resolves it using the file's own default
configuration: a layer switched off in the source — a cutter contour, a
varnish plate, a "nicht drucken" guide layer — stays off and is gone from the
output. That is the behaviour the layers tool existed to guarantee, and the
export names the layers it dropped, because the one thing worse than a cutter
line on the plate is not being told it was there.

X-4 by default, X-3 for older equipment
---------------------------------------
The difference is transparency, and it decides both quality and speed.

X-3 is a PDF 1.3 format, and PDF 1.3 has no transparency at all. Every page
that uses any must therefore be *flattened*: rendered to pixels and embedded
as an image. On a page of transparent vector artwork that is the whole page
turned into a raster — on an A0 sheet, measured, 82 seconds and 17 MB, with
every path gone.

X-4 is PDF 1.6 and keeps transparency live, so nothing is rasterised. The same
A0 page takes 0.3 seconds and 185 KB with all 3600 paths intact. It is also
what modern presses ask for. So it is the default, and X-3 stays available for
a RIP too old to take it.

X-1a is offered by neither. It would be a one-word change to the version
string and a lie: it additionally forbids things Ghostscript does not
guarantee removing, and a file claiming a conformance it does not have is
rejected at the RIP, which is worse than a file that claims nothing. The same
scepticism applies to X-4 — the difference is that X-4's requirements are ones
this can check: PDF 1.6, an embedded CMYK output intent, every font embedded,
no encryption. Transparency, the thing gs cannot be trusted to *remove*, is
allowed there rather than forbidden.

There are deliberately no "embed fonts" or "convert to CMYK" switches. Both
are requirements of the standard, not preferences; a PDF/X export with them
turned off would be an ordinary PDF wearing a PDF/X label.
"""
import logging
import os
import shutil

from PyQt6.QtWidgets import QPushButton

from tools.panels.base import BasePanel, make_label
from tools.i18n import tr
from tools.panels import _images
from tools.ghostscript import (failed, ghostscript_binary, page_range_flags,
                               require_ghostscript, run_chunked, unlink)
from tools.panels._icc import (ICC_DIR, fallback_cmyk_icc, profile_by_key,
                               resolve_icc)
from tools.panels._prepress import (DEFAULT_STANDARD, PDFX_STANDARDS,
                                    highest_image_dpi, layer_summary,
                                    standard_of, transparent_pages)
from tools.panels._verify import _verify_pages_intact


def _describe_os_error(e: OSError) -> str:
    """Return a user-friendly description of an OSError."""
    if e.errno == 28:
        return tr("Kein Speicherplatz mehr auf dem Gerät.")
    if e.errno == 13:
        return tr("Keine Schreibberechtigung.")
    if e.errno == 2:
        return tr("Zielverzeichnis nicht gefunden.")
    return f"{e.strerror} (errno {e.errno})"


class PdfxPanel(BasePanel):
    TITLE         = "PDF/X-Export"
    SUBTITLE      = "Druckfertige PDF/X-Datei erzeugen."
    RUN_LABEL     = "  PDF/X exportieren"

    def build_ui(self, layout):
        layout.addWidget(make_label(tr(
            "Alle Farben in CMYK gegen das eingestellte Ausgabeprofil, alle "
            "Schriften eingebettet, Bilder auf Druckauflösung. Vektoren und "
            "Schrift bleiben scharf in jeder Größe. Das Ergebnis wird "
            "geprüft, bevor es gespeichert wird."), dim=True))
        layout.addWidget(make_label(tr(
            "Was die Datei mitbringt — Anschnitt, Schriften, Bildauflösung — "
            "zeigt „Druckvorstufenprüfung“."), dim=True))

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

        gs_ok = bool(ghostscript_binary())
        layout.addWidget(make_label(
            tr("✓  Ghostscript verfügbar") if gs_ok else
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
        _key, version, _lbl = standard_of(AppSettings.get().pdfx_standard())
        self._cond_lbl.setText(
            tr("{p0}   ·   {p1}   ·   Bilder: {p2} dpi").format(
                p0=version, p1=name, p2=dpi))

    def _open_settings(self):
        from tools.shell.settings import PrepressDialog
        PrepressDialog(self).exec()
        self._refresh_condition()

    # ── the export ───────────────────────────────────────────────────────────

    def _run_action(self):
        # Widgets are read here; Ghostscript and the verification go to a
        # worker — a prepress file is minutes of pdfwrite.
        src = self.require_pdf()
        require_ghostscript()

        from tools.shell.settings import AppSettings
        settings = AppSettings.get()
        full_label, candidates, oci, condition = profile_by_key(
            settings.pdfx_condition())
        label = full_label.split(" — ")[0]
        dpi = settings.pdfx_image_dpi()
        standard = standard_of(settings.pdfx_standard())[0]
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
            lambda report: _export_pdfx(src, out, icc, oci, condition, dpi,
                                        standard, report),
            on_done=lambda result: self._done(result, note),
            busy_label="PDF/X wird erzeugt …",
        )
        return None

    def _done(self, result, note):
        out, dropped, capped_to, version = result
        lines = [tr('PDF/X-Export abgeschlossen ({p0}).').format(p0=version), note]
        if capped_to:
            lines.append(tr(
                'Rasterauflösung auf {p0} dpi begrenzt — bei dieser Seitengröße '
                'wäre ein höherer Wert für Betrachter nicht mehr lesbar.')
                .format(p0=capped_to))
        if dropped:
            lines.append(tr("Aufgelöste Ebenen (nicht in der Ausgabe): {p0}")
                         .format(p0=", ".join(dropped)))
        self.log.log("\n".join(lines), hold=True)
        self.open_result(out, tr("PDF/X exportiert"))


# ── the worker ───────────────────────────────────────────────────────────────

def _pdfx_defs(icc_path, oci, condition, version=None):
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

    version = version or PDFX_STANDARDS[DEFAULT_STANDARD][0]
    return f"""%!
[ /GTS_PDFXVersion ({ps(version)})
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


def _check_conformance(path, standard=DEFAULT_STANDARD, exact_version=True):
    """Raise unless `path` really carries what PDF/X requires.

    pdfwrite exits 0 on plenty of files it did not fully convert, and the
    whole value of this tool is that its output can be handed over without
    being opened again. If the claim is not backed by the file, the file does
    not ship.

    These are *structural* checks, not certification. They cover the rules of
    X-3 that can be read straight out of the file — the marker, the output
    intent, the boxes, and the things the standard forbids outright. A real
    validator additionally checks the parts of the page description that only
    a rendering engine can see, and there is no packaged free PDF/X validator
    to defer to. Calling this "validated" would be the same false confidence
    as claiming X-1a, so it is not called that anywhere.
    """
    import pikepdf
    from tools.panels._prepress import unembedded_fonts

    with pikepdf.open(path) as pdf:
        # Check encryption first: a locked file cannot be verified for PDF/X.
        if pdf.is_encrypted:
            raise RuntimeError(tr("Die Ausgabe ist verschlüsselt."))
        _key, version, _label = standard_of(standard)
        claimed = str(pdf.docinfo.get("/GTS_PDFXVersion", ""))
        # Exact when checking our own output — it must say what we wrote.
        # By family when reading someone else's file, because PDF/X-4p and
        # PDF/X-3:2003 are the same profiles under different revisions, and
        # insisting on our spelling made every foreign file look unconverted.
        if (claimed != version) if exact_version else (_family(claimed) is None):
            raise RuntimeError(tr("Die Ausgabe trägt keine PDF/X-Kennung."))
        intents = pdf.Root.get("/OutputIntents")
        if not intents or len(intents) == 0:
            raise RuntimeError(tr("Die Ausgabe hat keinen Output-Intent."))
        intent = intents[0]
        profile = intent.get("/DestOutputProfile")
        if profile is None:
            raise RuntimeError(tr("Der Output-Intent enthält kein ICC-Profil."))
        if int(profile.get("/N", 0)) != 4:
            raise RuntimeError(tr("Das eingebettete Ausgabeprofil ist nicht CMYK."))
        missing = [i + 1 for i, p in enumerate(pdf.pages)
                   if "/TrimBox" not in p.obj and "/ArtBox" not in p.obj]
        if missing:
            raise RuntimeError(tr("Seiten ohne TrimBox: {p0}").format(
                p0=", ".join(str(i) for i in missing)))
        # Things PDF/X forbids outright. Each of these would be resolved at
        # the RIP by guessing, which is the one thing the standard exists to
        # prevent.
        # Optional content is forbidden in X-3 and permitted in X-4, which is
        # one of the reasons X-4 is cheaper: the layers do not have to be
        # resolved away.
        if standard != "x4" and "/OCProperties" in pdf.Root:
            raise RuntimeError(tr("Die Ausgabe enthält noch Ebenen (OCG)."))
        names = pdf.Root.get("/Names") or {}
        for key, why in (("/JavaScript", "JavaScript"),
                         ("/EmbeddedFiles", tr("eingebettete Dateien"))):
            if key in names:
                raise RuntimeError(tr("Die Ausgabe enthält {p0}.").format(p0=why))

    not_embedded = unembedded_fonts(path)
    if not_embedded:
        raise RuntimeError(tr("Schriften nicht eingebettet: {p0}").format(
            p0=", ".join(not_embedded[:6])))


# pdfium refuses an image of 2**28 pixels or so — measured on an A0 page of
# flattened artwork: 248 Mpx opened correctly, 314 Mpx came back mostly blank
# with the content shifted. Ghostscript renders both files identically, so the
# large one is a *valid* PDF that a RIP would print; it is our own viewer and
# our own verification that cannot read it. Shipping a file this application
# cannot display or check is not worth the extra detail, so the rasterising
# resolution is capped to fit. The margin below the real limit is deliberate.
MAX_RASTER_PIXELS = 240_000_000


def _flatten_dpi(src, requested):
    """(resolution to rasterise at, was it capped).

    Only pages that actually get flattened produce a raster, but -r is set
    once for the run, so the cap follows the largest page in the document. On
    anything up to A2 at 600 dpi this changes nothing; an A0 page comes down
    to about 390 dpi, which is still more resolution than a sheet that size is
    ever looked at from close enough to need.
    """
    import pikepdf
    allowed = requested
    with pikepdf.open(src) as pdf:
        for page in pdf.pages:
            try:
                box = [float(v) for v in page.obj["/MediaBox"]]
            except (KeyError, TypeError, ValueError):
                continue
            area_in2 = (abs(box[2] - box[0]) / 72.0) * (abs(box[3] - box[1]) / 72.0)
            if area_in2 <= 0:
                continue
            allowed = min(allowed, int((MAX_RASTER_PIXELS / area_in2) ** 0.5))
    allowed = max(72, allowed)
    return allowed, allowed < requested


def _family(version):
    """"x4", "x3", "x1a" or None, from whatever revision a file claims.

    Files in the wild carry PDF/X-4, PDF/X-4p, PDF/X-3:2002 and PDF/X-3:2003,
    and matching the exact string we happen to write means every file made by
    anything else is treated as needing conversion — which is how a file that
    was already fine ended up being re-separated.
    """
    text = str(version or "")
    for prefix, key in (("PDF/X-1a", "x1a"), ("PDF/X-3", "x3"), ("PDF/X-4", "x4")):
        if text.startswith(prefix):
            return key
    return None


def _conversion_reason(src, standard, oci, dpi):
    """Why `src` still has to be converted, or None if it already is what this
    export would produce.

    Re-exporting a file that is already PDF/X is not merely slow, it is lossy:
    every image is decoded and re-encoded, so a file that goes through twice
    is a generation worse than one that went through once. The honest answer
    to "make this PDF/X" for a file that already is one is that there is
    nothing to do.

    A *reason* rather than a yes/no because the answer is often surprising —
    a file can look like PDF/X in a viewer and still need work — and the
    operator has no way to find out why except being told.
    """
    import pikepdf
    # The ordinary case first, and in its own words: _check_conformance speaks
    # about "the output", which is right where it is used to vet what we just
    # wrote and wrong as an explanation of the file someone opened.
    try:
        with pikepdf.open(src) as pdf:
            if _family(pdf.docinfo.get("/GTS_PDFXVersion")) is None:
                return tr("noch kein PDF/X")
    except Exception:
        logging.debug("could not read %s", src, exc_info=True)
        return tr("nicht lesbar")

    try:
        _check_conformance(src, standard, exact_version=False)
    except RuntimeError as exc:
        return str(exc)
    except Exception:
        logging.debug("could not read %s as PDF/X", src, exc_info=True)
        return tr("nicht lesbar")

    try:
        with pikepdf.open(src) as pdf:
            claimed = _family(pdf.docinfo.get("/GTS_PDFXVersion"))
            if claimed != standard:
                return tr('Datei ist {p0}, gewünscht ist {p1}').format(
                    p0=str(pdf.docinfo.get("/GTS_PDFXVersion")),
                    p1=PDFX_STANDARDS[standard][0])
            intent = pdf.Root["/OutputIntents"][0]
            found = str(intent.get("/OutputConditionIdentifier", ""))
        # A generic setting is not an opinion about the press, so it must not
        # override one the file already carries: re-separating a file made for
        # a real printing condition down to generic CMYK loses information.
        # A *named* setting is an opinion, and a file made for another
        # condition genuinely has to be separated again.
        if oci != "Custom" and found != oci:
            return tr('Datei ist für {p0} separiert, eingestellt ist {p1}').format(
                p0=found or tr("unbenannt"), p1=oci)

        finest = highest_image_dpi(src, stop_above=dpi * 1.01)
        if finest is not None and finest > dpi * 1.01:
            return tr('Bilder mit bis zu {p0} dpi, Ziel sind {p1} dpi').format(
                p0=int(round(finest)), p1=dpi)
    except Exception:
        logging.debug("could not decide whether %s is already PDF/X", src,
                      exc_info=True)
        return tr("nicht lesbar")
    return None


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


def _export_pdfx(src, out, icc, oci, condition, dpi, standard, report):
    """Write `src` to `out` as PDF/X. Plain data only — runs on a worker."""
    import tempfile
    import pikepdf

    standard, version, _label = standard_of(standard)
    flattens = standard != "x4"
    gs_bin = require_ghostscript()

    # A file that is already this exact thing is left alone. Running it through
    # again would re-encode every image for no gain — see _conversion_reason,
    # which also says why a file that looks like PDF/X still needs work.
    reason = _conversion_reason(src, standard, oci, dpi)
    if reason is None:
        report(tr("Datei ist bereits {p0} — unverändert übernommen.")
               .format(p0=version))
        try:
            shutil.copyfile(src, out)
        except OSError as e:
            raise RuntimeError(tr("Datei konnte nicht kopiert werden: {p0}").format(p0=_describe_os_error(e)))
        return out, [], None, version
    report(tr("Wird konvertiert: {p0}").format(p0=reason))
    # X-4 keeps optional content, so there is nothing dropped to report; only
    # the flattening profile resolves layers away.
    on, off = layer_summary(src) if flattens else ([], [])
    capped = False
    if flattens:
        dpi, capped = _flatten_dpi(src, dpi)

    fd, defs = tempfile.mkstemp(suffix=".ps"); os.close(fd)
    fd, tmp = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    fd, staged = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    try:
        with open(defs, "w") as f:
            f.write(_pdfx_defs(icc, oci, condition, version))

        # Separate the images before Ghostscript sees them. Converting RGB to
        # CMYK is the whole cost of this export, and Ghostscript is about ten
        # times slower at it than littleCMS — see tools/panels/_images.py. What
        # it skips is still converted by Ghostscript, whose own colour
        # conversion stays switched on below, so this only ever changes how
        # long it takes.
        report(tr("Bilder werden separiert …"))
        converted, _skipped = _images.to_cmyk(src, staged, icc, report)
        gs_input = staged if converted else src

        def build(dest, first, last):
            # -dPDFX switches pdfwrite into PDF/X mode; the prologue has to be
            # read before the input, or the output intent lands in no document.
            #
            # Bare -dPDFX means X-3 and forces PDF 1.3 — no transparency, so
            # every page that uses any is rasterised. -dPDFX=4 with
            # CompatibilityLevel 1.6 keeps it live instead, which is why the
            # X-4 path needs no rasterising resolution at all and finishes in a
            # fraction of the time.
            cmd = [
                gs_bin,
                "-dPDFX" if flattens else "-dPDFX=4",
                "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dQUIET",
                "-sDEVICE=pdfwrite",
                "-dPDFSETTINGS=/prepress",
                "-sProcessColorModel=DeviceCMYK",
                "-sColorConversionStrategy=CMYK",
                "-dEmbedAllFonts=true",
                "-dSubsetFonts=true",
            ]
            if not flattens:
                cmd.append("-dCompatibilityLevel=1.6")
            if flattens:
                # The rasterising resolution, and the reason this is not slow.
                #
                # PDF/X-3 is PDF 1.3, which has no transparency, so any page
                # that uses it gets flattened — and flattening means rendering
                # that page to pixels. Ghostscript does that at 720 dpi by
                # default and then downsamples to the target, i.e. it renders
                # several times the detail it is about to throw away.
                # Rendering at the target instead is the same output for a
                # fraction of the work: an A0 page of transparent artwork went
                # from 116 s to 21 s at 300 dpi, measured, with a
                # byte-comparable result.
                #
                # It also makes the setting mean what it says. Without it,
                # asking for 600 dpi still produced 720 dpi flattening
                # downsampled to 300, because the preset's own limit won.
                #
                # X-4 rasterises nothing, so it gets no -r: the flag would only
                # slow down a run with no flattening in it.
                cmd.append(f"-r{dpi}")
            cmd += [
                # After /prepress, which keeps images at source resolution —
                # these override it (verified: a 600 dpi image comes back at
                # 300 and the file halves; a 200 dpi one is left alone rather
                # than upsampled). Resolution above what the press can image is
                # RIP time nobody sees on paper, and it is the main reason a
                # job is slow to print.
                "-dDownsampleColorImages=true", f"-dColorImageResolution={dpi}",
                "-dColorImageDownsampleType=/Bicubic",
                "-dDownsampleGrayImages=true", f"-dGrayImageResolution={dpi}",
                "-dGrayImageDownsampleType=/Bicubic",
                # Downsample anything above the target, not only what is half
                # again above it. Ghostscript's default threshold is 1.5, so a
                # 600 dpi setting actually left everything up to 900 dpi alone
                # — the setting did not mean what it says, and the export could
                # not reproduce its own output: re-running it found 840 dpi
                # images still above target and converted the whole file again.
                "-dColorImageDownsampleThreshold=1.0",
                "-dGrayImageDownsampleThreshold=1.0",
                # Bilevel line art is left alone: downsampling it is what makes
                # a scanned drawing come out ragged.
                "-dDownsampleMonoImages=false",
                f"-sOutputFile={dest}",
            ]
            # The page range applies to the next document Ghostscript opens;
            # the pdfmark prologue in between does not consume it.
            return cmd + page_range_flags(first, last) + [defs, gs_input]

        # Say which of the two jobs this is before it starts. Flattening
        # renders whole pages to pixels and is minutes on a large sheet, and a
        # progress line that only says "converting" makes that look like a
        # hang rather than the one unavoidable cost of PDF/X.
        report(tr("Transparenz wird reduziert — das dauert bei grossen Seiten …")
               if flattens and transparent_pages(src) else
               tr("Ghostscript: PDF/X-Konvertierung …"))
        with pikepdf.open(gs_input) as _i:
            n_input = len(_i.pages)
        # Only X-3 is split across processes. Its cost is rasterising whole
        # pages, which is per-page work and scales with the cores thrown at it.
        # X-4 rasterises nothing and is already a fraction of the time, so
        # there is little to win — and it is the profile that *keeps* optional
        # content, which lives in the catalog rather than on a page. Merging
        # chunks reassembles pages, so a chunked X-4 export would come back
        # with its layers silently dropped, which is the one thing this export
        # replaced the layers tool to avoid.
        r = failed(run_chunked(report, build, tmp, n_input if flattens else 1,
                               timeout=1800, carry_document_state=True))
        if r is not None:
            err = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")[:500]
            raise RuntimeError(tr('Ghostscript-Fehler:\n{p0}').format(p0=err))
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise RuntimeError(tr("Ghostscript hat keine Ausgabedatei erzeugt."))

        _check_conformance(tmp, standard)
        moved = _boxes_survived(src, tmp)
        if moved:
            raise RuntimeError(tr(
                'Der Beschnitt hat sich verändert auf Seite(n): {p0} — die '
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
        damaged = _verify_pages_intact(src, tmp, range(n_src), report) if not off else {}
        if damaged:
            raise RuntimeError(tr(
                'PDF/X-Export hat Seite(n) beschädigt: {p0} — die Datei wurde '
                'nicht gespeichert.').format(
                    p0=", ".join(f"{i + 1} ({why})" for i, why in sorted(damaged.items()))))
        shutil.copyfile(tmp, out)
    finally:
        unlink(defs, tmp, staged)
    return out, off, (dpi if capped else None), version
