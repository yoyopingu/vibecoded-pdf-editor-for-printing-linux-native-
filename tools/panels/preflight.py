"""
Druckvorstufenprüfung — check a PDF for what tends to go wrong at the
press, and report it rather than changing anything.
"""
import os, gc
from tools.render.document_cache import PDFIUM_LOCK as _pdfium_lock
from PyQt6.QtWidgets import QVBoxLayout, QComboBox, QGroupBox, QCheckBox, QTextEdit
from tools._base import BasePanel, make_label
from tools.i18n import tr
from tools.panels._shared import PAPER_SIZES_PT, row
from tools.panels._colour import _colour_histogram, _hist_stats
from tools.panels._prepress import MIN_PRESS_DPI


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

        # What decides whether the file can go on a press at all, and what the
        # PDF/X export will have to do to it. Separate group because these are
        # about the file's fitness, not about it matching an expected format.
        xb = QGroupBox(tr("DRUCKFAEHIGKEIT")); xl = QVBoxLayout(xb)
        self.chk_bleed = QCheckBox(tr("Anschnitt / Endformat (TrimBox)"))
        self.chk_bleed.setChecked(True); xl.addWidget(self.chk_bleed)
        self.chk_fonts = QCheckBox(tr("Schriften eingebettet"))
        self.chk_fonts.setChecked(True); xl.addWidget(self.chk_fonts)
        self.chk_dpi = QCheckBox(tr("Bildauflösung ausreichend"))
        self.chk_dpi.setChecked(True); xl.addWidget(self.chk_dpi)
        self.chk_trans = QCheckBox(tr("Transparenz (wird beim Export reduziert)"))
        self.chk_trans.setChecked(True); xl.addWidget(self.chk_trans)
        self.chk_layers = QCheckBox(tr("Ebenen (OCG) auflisten"))
        self.chk_layers.setChecked(True); xl.addWidget(self.chk_layers)
        layout.addWidget(xb)
        layout.addWidget(make_label(tr("Bericht:"), dim=True))
        self.report=QTextEdit(); self.report.setReadOnly(True); self.report.setMinimumHeight(180)
        self.report.setPlaceholderText(tr("Pruefung starten...")); layout.addWidget(self.report)

    def _run_action(self):
        # The checks are read here; the page walk goes to a worker. It renders
        # every page at full size to look for colour, which is seconds per page
        # on a heavy document — 77 seconds for eight of them, during which the
        # window could not repaint. run_async also replaces the _preflighting
        # flag: it refuses a second run of its own accord.
        src = self.require_pdf()
        checks = {
            "size":   self.chk_size.isChecked(),
            "orient": self.chk_orient.isChecked(),
            "colour": self.chk_colour.isChecked(),
            "enc":    self.chk_enc.isChecked(),
            "bleed":  self.chk_bleed.isChecked(),
            "fonts":  self.chk_fonts.isChecked(),
            "dpi":    self.chk_dpi.isChecked(),
            "trans":  self.chk_trans.isChecked(),
            "layers": self.chk_layers.isChecked(),
        }
        target = PAPER_SIZES_PT.get(self.size_combo.currentText())

        self.run_async(
            lambda report: _preflight(src, checks, target, MIN_PRESS_DPI, report),
            on_done=self._preflight_done,
            busy_label="Pruefung laeuft …",
        )
        return None

    def _preflight_done(self, result):
        lines, verdict = result
        self.report.setPlainText("\n".join(lines))
        self.log.log(verdict)


def _press_readiness(src, checks, min_dpi, report):
    """The half of the report that is about going on a press.

    Returns (issues, oks). Separated from _preflight so the page-by-page walk
    below is not interleaved with four independent document-wide questions —
    and so each of these can be read next to what it means for the job.
    """
    from tools.panels import _prepress
    from tools.panels._prepress import standard_of
    from tools.shell.settings import AppSettings
    # Whether transparency is a problem depends on which profile the export
    # will write, so the report reflects the setting in force rather than
    # warning about something X-4 handles.
    flattens = standard_of(AppSettings.get().pdfx_standard())[0] != "x4"
    issues, oks = [], []

    if checks.get("bleed"):
        report(tr("Anschnitt …"))
        measured, bleeds, untrimmed = _prepress.page_bleed(src)
        if not measured:
            pass
        elif not bleeds:
            # Not an error: a flyer that ends at the paper edge has no bleed
            # and needs none. It is a fact the operator has to know, because
            # the export cannot invent one and neither can the shop.
            oks.append(tr("Kein Anschnitt definiert — Endformat = Seitenformat"))
        elif untrimmed:
            issues.append(tr('Anschnitt nur auf einem Teil der Seiten '
                             '(ohne TrimBox: {p0})').format(
                                 p0=", ".join(str(p) for p in untrimmed[:10])))
        else:
            low, high = min(bleeds), max(bleeds)
            where = (tr('Anschnitt: {p0:.1f} mm').format(p0=low) if high - low < 0.1
                     else tr('Anschnitt: {p0:.1f}–{p1:.1f} mm').format(p0=low, p1=high))
            # 3 mm is the usual minimum a shop asks for; less than 2 mm leaves
            # nothing for the cutter's tolerance.
            (oks if low >= 2.0 else issues).append(
                where if low >= 2.0 else
                tr('{p0} — knapp, ueblich sind 3 mm').format(p0=where))

    if checks.get("fonts"):
        report(tr("Schriften …"))
        missing = _prepress.unembedded_fonts(src)
        if missing:
            issues.append(tr('Schriften nicht eingebettet: {p0}').format(
                p0=", ".join(missing[:6])))
        else:
            oks.append(tr("Alle Schriften eingebettet"))

    if checks.get("dpi"):
        report(tr("Bildauflösung …"))
        low_res = _prepress.low_resolution_images(src, min_dpi)
        if low_res:
            issues.append(tr('Bilder unter {p0} dpi: {p1}').format(
                p0=min_dpi,
                p1=", ".join(tr('Seite {p0} ({p1} dpi)').format(p0=page, p1=dpi)
                             for page, dpi in low_res[:6])))
        else:
            oks.append(tr('Bildauflösung mindestens {p0} dpi').format(p0=min_dpi))

    if checks.get("trans"):
        report(tr("Transparenz …"))
        pages = _prepress.transparent_pages(src)
        if not pages:
            oks.append(tr("Keine Transparenz — Vektoren bleiben Vektoren"))
        elif flattens:
            # Only a problem under X-3, which is PDF 1.3 and has no
            # transparency: those pages get rendered to pixels, which is both
            # the quality cost and where the minutes go. Said before the
            # export rather than discovered after it.
            issues.append(tr(
                'Transparenz auf {p0} Seite(n) — PDF/X-3 kann sie nicht '
                'darstellen, diese Seiten werden in Pixel umgewandelt '
                '(Vektoren gehen dort verloren) und der Export dauert '
                'entsprechend laenger: {p1}').format(
                    p0=len(pages), p1=", ".join(str(p) for p in pages[:10])))
        else:
            oks.append(tr(
                'Transparenz auf {p0} Seite(n) — PDF/X-4 behaelt sie, '
                'nichts wird gerastert').format(p0=len(pages)))

    if checks.get("layers"):
        on, off = _prepress.layer_summary(src)
        if on or off:
            oks.append(tr('Ebenen: {p0} sichtbar, {p1} ausgeschaltet — beim '
                          'PDF/X-Export wird beides aufgeloest').format(
                              p0=len(on), p1=len(off)))
            for name in off[:6]:
                oks.append(tr('   ✗ {p0} — ausgeschaltet, entfaellt').format(p0=name))
    return issues, oks


def _preflight(src, checks, target, min_dpi, report):
    """Walk the document and describe what would go wrong at the press.

    Returns (report lines, one-line verdict). Plain data only — the panel puts
    it on screen back on the GUI thread.
    """
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    reader = PdfReader(src, strict=False)
    # See grayscale._scan_pages: PIL from to_pil() can be collected by cyclic GC
    # on the render worker's thread, firing pdfium finalizers without the lock.
    gc.disable()
    try:
        # PdfDocument/close are pdfium calls and need the process-wide lock — see
        # grayscale._scan_pages for the heap corruption an unlocked load caused.
        with _pdfium_lock:
            doc = pdfium.PdfDocument(src)
        try:
            n = len(reader.pages); issues = []; oks = []
            if checks["enc"]:
                if reader.is_encrypted: issues.append(tr("PDF ist passwortgeschuetzt"))
                else: oks.append(tr("Nicht verschluesselt"))
            orients = []; colour_pages = []
            for i in range(n):
                report(tr('Seite {i} / {total}…').format(i=i + 1, total=n))
                page = reader.pages[i]
                report.check()
                pw = float(page.mediabox.width); ph = float(page.mediabox.height)
                orients.append("Q" if pw > ph else "H")
                if checks["size"] and target:
                    tw, th = target
                    if not ((abs(pw-tw) < 5 and abs(ph-th) < 5)
                            or (abs(pw-th) < 5 and abs(ph-tw) < 5)):
                        issues.append(tr('Seite {p0}: {p1:.0f}x{p2:.0f}pt != {p3:.0f}x{p4:.0f}pt').format(
                            p0=i + 1, p1=pw, p2=ph, p3=tw, p4=th))
                if checks["colour"]:
                    with _pdfium_lock:
                        pil = doc[i].render(scale=0.4).to_pil().convert("RGB")
                    # Every pixel of a ~240px render, not a 64x64 squash of the
                    # page: averaging the render first hid small colour marks
                    # completely, and this check reporting "Keine Farbseiten
                    # erkannt" for a page that has them is a preflight that
                    # passes a job it should stop. scale=0.4 catches a
                    # half-point mark (verified by
                    # test_preflight_sees_a_tiny_colour_mark) while rendering
                    # ~6x faster than scale=1 on heavy vector pages.
                    if _hist_stats(_colour_histogram(pil), 20)[0] > 20:
                        colour_pages.append(i + 1)
            if checks["orient"] and orients:
                if len(set(orients)) > 1: issues.append(tr("Gemischte Ausrichtungen"))
                else: oks.append(tr("Einheitlich: {p0}").format(
                    p0=tr('Hochformat') if orients[0] == 'H' else tr('Querformat')))
            if checks["colour"]:
                if colour_pages: issues.append(f"Farbseiten: {colour_pages[:10]}")
                else: oks.append(tr("Keine Farbseiten erkannt"))
            press_issues, press_oks = _press_readiness(src, checks, min_dpi, report)
            issues += press_issues
            oks += press_oks
            lines = [f"BERICHT -- {os.path.basename(src)}",
                     tr('Seiten: {p0}  |  {p1} KB').format(
                         p0=n, p1=os.path.getsize(src) // 1024), ""]
            if issues: lines += [f"PROBLEME ({len(issues)}):"] + [f"  x  {x}" for x in issues] + [""]
            else: lines.append(tr("BESTANDEN -- Datei scheint druckfertig."))
            if oks: lines += ["BESTANDEN:"] + [f"  v  {x}" for x in oks]
        finally:
            with _pdfium_lock:
                doc.close()
    finally:
        gc.collect(); gc.enable()
    return lines, ("BESTANDEN" if not issues
                   else f"FEHLER ({len(issues)} Problem(e))")
