"""
CopyShop PDF Suite — Internationalization
Supported: de (Deutsch), en (English)

Usage:
    from tools.i18n import tr, get_language, load_language, set_language
    label = tr("Speichern")   # → "Save" in English mode
"""
from PyQt6.QtCore import QSettings

_CURRENT_LANG: str = "de"


def load_language() -> None:
    """Read persisted language from QSettings. Call once at startup."""
    global _CURRENT_LANG
    lang = QSettings("CopyShop", "PDFSuite").value("locale/language", "de")
    _CURRENT_LANG = lang if lang in ("de", "en") else "de"


def get_language() -> str:
    return _CURRENT_LANG


def set_language(lang: str) -> None:
    global _CURRENT_LANG
    _CURRENT_LANG = lang if lang in ("de", "en") else "de"
    s = QSettings("CopyShop", "PDFSuite")
    s.setValue("locale/language", _CURRENT_LANG)
    s.sync()   # flush to disk before os.execv replaces the process


def tr(text: str) -> str:
    """Return translated string; falls back to the original (German) text."""
    if _CURRENT_LANG == "de" or not text:
        return text
    return _EN.get(text, text)


# ── English translations (key = German source string) ─────────────────────────
_EN: dict[str, str] = {

    # ── Menu bar ──────────────────────────────────────────────────────────────
    "Datei": "File",
    "PDF öffnen…": "Open PDF…",
    "Mehrere PDFs öffnen…": "Open Multiple PDFs…",
    "Beenden": "Quit",
    "Einstellungen": "Settings",
    "Leistung…": "Performance…",
    "Darstellung…": "Appearance…",
    "Allgemein…": "General…",
    "Darstellung": "Appearance",
    "Allgemein": "General",
    "Sprache": "Language",
    "Hilfe": "Help",
    "Über CopyShop PDF Suite": "About CopyShop PDF Suite",

    # ── Sidebar ───────────────────────────────────────────────────────────────
    "Seiten-Viewer": "Page Viewer",
    "WERKZEUGE": "TOOLS",
    "PLUGINS": "PLUGINS",
    "v3.0  —  open source": "v3.0  —  open source",

    # ── Tool names (sidebar nav) ──────────────────────────────────────────────
    "Komprimieren": "Compress",
    "Zuschneiden / Skalieren": "Crop / Scale",
    "Seitenzahlen": "Page Numbers",
    "Bild → PDF": "Image → PDF",
    "Bild ↔ PDF": "Image ↔ PDF",
    "Graustufen": "Grayscale",
    "Formulare / Reduzieren": "Forms / Flatten",
    "OCR": "OCR",
    "OCR — Texterkennung": "OCR — Text Recognition",
    "Druckvorstufenprüfung": "Preflight Check",
    "Farbprofil": "Color Profile",
    "Farbprofil / CMYK": "Color Profile / CMYK",
    "Aufschlag / Ausschuss": "Imposition",
    "Ausschießen (2-up)": "Impose (2-up)",

    # ── Panel TITLE fields ────────────────────────────────────────────────────
    "Zusammenfuehren / Trennen": "Merge / Split",
    "Bild zu/von PDF": "Image to/from PDF",
    "Graustufen-Konvertierung": "Grayscale Conversion",
    "Ausschießen (2-up / Broschüre)": "Impose (2-up / Booklet)",
    "OCR -- Texterkennung": "OCR — Text Recognition",
    "Druckvorstufenpruefung": "Preflight Check",

    # ── Tool subtitles ────────────────────────────────────────────────────────
    "PDF-Dateigrösse reduzieren — Ghostscript oder pikepdf":
        "Reduce PDF file size — Ghostscript or pikepdf",
    "PDF-Dateigroesse reduzieren. Ergebnis wird als neuer Tab geoeffnet.":
        "Reduce PDF file size. Result opens as a new tab.",
    "PDFs zusammenfuehren oder in Teile aufteilen.":
        "Merge PDFs or split into parts.",
    "Seiten zuschneiden oder auf ein anderes Format skalieren":
        "Crop or scale pages to a different format",
    "Seitenzahlen automatisch einfügen":
        "Automatically insert page numbers",
    "Seitenzahlen auf jede Seite stempeln.":
        "Stamp page numbers onto every page.",
    "Bilder zu einer PDF zusammenfügen":
        "Combine images into a PDF",
    "Bilder in PDF umwandeln oder PDF-Seiten als Bilder exportieren.":
        "Convert images to PDF or export PDF pages as images.",
    "PDF in Graustufen umwandeln":
        "Convert PDF to grayscale",
    "Visuell graue Seiten in echtes DeviceGray umwandeln. Farbseiten bleiben unveraendert.":
        "Convert visually gray pages to true DeviceGray. Color pages remain unchanged.",
    "Formularfelder aus PDF entfernen / einbetten":
        "Remove / embed form fields from PDF",
    "Formularfelder ausfuellen und einbetten.":
        "Fill in and embed form fields.",
    "Text aus Scans erkennen (OCR)":
        "Recognize text from scans (OCR)",
    "Gescannte PDFs durchsuchbar machen.":
        "Make scanned PDFs searchable.",
    "PDF auf Druckprobleme prüfen":
        "Check PDF for print issues",
    "PDF auf Drucktauglichkeit pruefen.":
        "Check PDF for print readiness.",
    "ICC-Farbprofil einbetten oder entfernen":
        "Embed or remove ICC color profile",
    "ICC-Profile pruefen und in CMYK umwandeln.":
        "Check ICC profiles and convert to CMYK.",
    "Seiten für den Druck anordnen":
        "Arrange pages for printing",
    "Mehrere Seiten auf groessere Boegen anordnen.":
        "Arrange multiple pages on larger sheets.",

    # ── BasePanel / CurrentFileBar ────────────────────────────────────────────
    "PDF:": "PDF:",
    "Keine Datei geöffnet — öffne zuerst eine PDF im Page Viewer":
        "No file open — open a PDF in the Page Viewer first",
    "Log...": "Log...",
    "  Ausführen": "  Run",

    # ── Page Viewer toolbar ───────────────────────────────────────────────────
    "  Öffnen...": "  Open...",
    "  Seiten verwalten  [Tab]": "  Manage Pages  [Tab]",
    "  Drucken": "  Print",

    # ── Manage panel ──────────────────────────────────────────────────────────
    "Seiten verwalten": "Manage Pages",
    "Auswahl  (z.B. 1, 3, 5-8)": "Selection  (e.g. 1, 3, 5–8)",
    "z.B. 1, 3, 5-8, 12  →  Enter": "e.g. 1, 3, 5–8, 12  →  Enter",
    "ANSICHT": "VIEW",
    "AUSWAHL": "SELECTION",
    "Alle auswaehlen  (Strg+A)": "Select all  (Ctrl+A)",
    "Auswahl aufheben  (Strg+D)": "Deselect all  (Ctrl+D)",
    "Thumbnails verkleinern": "Smaller thumbnails",
    "Thumbnails vergroessern": "Larger thumbnails",
    "Zoom zuruecksetzen": "Reset zoom",
    "Auswahl 90° gegen den Uhrzeigersinn drehen": "Rotate selection 90° counter-clockwise",
    "Auswahl 90° im Uhrzeigersinn drehen": "Rotate selection 90° clockwise",
    "OPERATIONEN": "OPERATIONS",
    "Loeschen  (Entf)": "Delete  (Del)",
    "Kopieren  (Strg+C)": "Copy  (Ctrl+C)",
    "Einfuegen  (Strg+V)": "Paste  (Ctrl+V)",
    "Rueckgaengig  (Strg+Z)": "Undo  (Ctrl+Z)",
    "Extrahieren...": "Extract...",
    "Als neuen Tab oeffnen": "Open as new tab",
    "Speichern unter…": "Save as…",
    "{p0} Seite(n) in einem neuen Tab oeffnen.": "Open {p0} page(s) in a new tab.",
    "Sollen die Seiten hier ebenfalls bleiben?": "Should the pages also stay here?",
    "Kopieren  (hier behalten)": "Copy  (keep here)",
    "Verschieben  (hier entfernen)": "Move  (remove here)",
    "{p0} Seite(n) in neuen Tab verschoben.": "Moved {p0} page(s) into a new tab.",
    "Leere Seite einfuegen": "Insert blank page",
    "Aus Datei(en) einfuegen...": "Insert from file(s)...",
    "Speichern": "Save",
    "TRENNEN": "SPLIT",
    "Jede Seite als Datei": "Each page as file",
    "Alle N Seiten...": "Every N pages...",
    "◀  Einzelansicht  [Tab / Esc]": "◀  Single view  [Tab / Esc]",

    # ── Print dialog ──────────────────────────────────────────────────────────
    "Drucken": "Print",
    "Drucker:": "Printer:",
    "Seiten:": "Pages:",
    "Alle": "All",
    "Kopien:": "Copies:",
    "Duplex:": "Duplex:",
    "Aus": "Off",
    "Lange Seite": "Long edge",
    "Kurze Seite": "Short edge",
    "Lange Seite (Buch)": "Long edge (book)",
    "Kurze Seite (Notizblock)": "Short edge (notepad)",
    "Sortiert": "Collated",
    "Farbkonvertierung:": "Color conversion:",
    "Unverändert (Druckertreiber entscheidet)": "Unchanged (printer driver decides)",
    "→ CMYK (für CMYK-Drucker)": "→ CMYK (for CMYK printers)",
    "→ sRGB (für RGB-Drucker)": "→ sRGB (for RGB printers)",
    "  Drucken  ": "  Print  ",

    # ── Print dialog (full) ─────────────────────────────────────────────────────
    "DRUCKER": "PRINTER",
    "SEITEN": "PAGES",
    "SEITENHANDHABUNG": "PAGE HANDLING",
    "Alle Seiten  (1 – {n})": "All pages  (1 – {n})",
    "Aktuelle Seite": "Current page",
    "z.B.  1-3, 5, 7-9": "e.g.  1-3, 5, 7-9",
    "Skalierung:": "Scaling:",
    "Feste Größe": "Fixed size",
    "Druckt in der Größe daneben — 100 % ist 1:1, Inhalt kann am Rand beschnitten werden":
        "Prints at the size beside it — 100 % is 1:1, content may be clipped at the edge",
    "Größe relativ zum Original. 100 % druckt 1:1.": "Size relative to the original. 100 % prints 1:1.",
    "Auf bedruckbaren Bereich verkleinern": "Shrink to printable area",
    "Skaliert hoch und runter — Seite füllt den Druckbereich vollständig (Acrobat: Fit Page)":
        "Scales up and down — the page completely fills the print area (Acrobat: Fit Page)",
    "Verkleinert nur wenn nötig, vergrössert nie (Acrobat: Shrink to Printable Area)":
        "Shrinks only when necessary, never enlarges (Acrobat: Shrink to Printable Area)",
    "Automatisch": "Automatic",
    "Papier:": "Paper:",
    "Sortieren  (1,2,3 / 1,2,3)": "Collate  (1,2,3 / 1,2,3)",
    "Farbe:": "Color:",
    "Unverändert": "Unchanged",
    "→ CMYK  (für CMYK-Drucker)": "→ CMYK  (for CMYK printers)",
    "→ sRGB  (für RGB-Drucker)": "→ sRGB  (for RGB printers)",
    "Unverändert: Druckertreiber entscheidet (empfohlen mit ICC-Profilen)\n"
    "→ CMYK: Vor dem Druck in CMYK umrechnen\n"
    "→ sRGB: Vor dem Druck in sRGB umrechnen":
        "Unchanged: printer driver decides (recommended with ICC profiles)\n"
        "→ CMYK: convert to CMYK before printing\n"
        "→ sRGB: convert to sRGB before printing",
    "Beidseitig drucken  (Duplex)": "Print double-sided  (duplex)",
    "Lange Seite: Rückseite steht gleich herum wie die Vorderseite "
    "(Bindung an der langen Kante, wie ein Buch).\n"
    "Kurze Seite: Rückseite ist um 180° gedreht "
    "(Bindung an der kurzen Kante, wie ein Notizblock).":
        "Long edge: the back is the same way up as the front "
        "(bound on the long edge, like a book).\n"
        "Short edge: the back is rotated 180° "
        "(bound on the short edge, like a notepad).",
    "Drucker werden geladen…": "Loading printers…",
    "Kein Drucker gefunden": "No printer found",
    "Dieser Drucker unterstützt kein Duplex-Drucken.":
        "This printer does not support duplex printing.",
    "Dieser Drucker druckt nur in Graustufen.":
        "This printer prints in grayscale only.",
    "Randloser Druck — bei gleichem Seitenformat keine Skalierung":
        "Borderless printing — no scaling when the paper size matches",
    "Dieser Drucker unterstützt randlosen Druck (full-bleed). "
    "'An Seite anpassen' ändert eine A4-Seite auf A4-Papier nicht.":
        "This printer supports borderless (full-bleed) printing. "
        "'Fit to page' does not change an A4 page on A4 paper.",
    "Druckrand: ca. {m:.1f} mm  (roter Rahmen in Vorschau)":
        "Print margin: approx. {m:.1f} mm  (red frame in preview)",
    "Ca. {m:.1f} mm Hardware-Rand kann nicht bedruckt werden. "
    "'An Seite anpassen' verkleinert den Inhalt auf den bedruckbaren Bereich.":
        "Approx. {m:.1f} mm hardware margin cannot be printed. "
        "'Fit to page' shrinks the content to the printable area.",
    "Ungültiger Seitenbereich — bitte Zahlen zwischen 1 und {n} eingeben.":
        "Invalid page range — please enter numbers between 1 and {n}.",
    "Kein gültiger Seitenbereich für {n}-seitige Datei.":
        "No valid page range for a {n}-page file.",
    "Keine Seiten ausgewählt.": "No pages selected.",
    "Kein Drucker verfügbar.": "No printer available.",
    "Fehler: PDF ist passwortgeschützt — bitte zuerst entsperren.":
        "Error: PDF is password-protected — please unlock it first.",
    "Sende {n} Seite(n) an »{name}«…": "Sending {n} page(s) to »{name}«…",
    "Druckauftrag wird vorbereitet…": "Preparing print job…",
    "GS-Pfad fehlgeschlagen — Versuche Qt-Fallback…":
        "GS path failed — trying Qt fallback…",
    "Druckfehler:": "Print error:",
    "Seiten zusammenstellen… ({count})": "Collecting pages… ({count})",
    "Ghostscript: Normalisierung und Skalierung…":
        "Ghostscript: normalising and scaling…",
    "Sende an Drucker…": "Sending to printer…",
    "Keine Seiten konnten gelesen werden (Datei beschädigt?).":
        "No pages could be read (file corrupted?).",
    "Rendere Seite {i} / {total}…": "Rendering page {i} / {total}…",
    "Keine Seiten konnten gerendert werden.": "No pages could be rendered.",
    "QPainter konnte nicht gestartet werden.": "QPainter could not be started.",
    "Qt-Druckfehler: {e}": "Qt print error: {e}",
    "Re-centre: keine Seiten.": "Re-centre: no pages.",
    "Druckauftrag gesendet: {pages} Seite(n) × {copies} Kopie(n) = {total} Blatt.":
        "Print job sent: {pages} page(s) × {copies} copy/copies = {total} sheet(s).",
    "  (Übersprungen: S. {skipped})": "  (Skipped: p. {skipped})",
    "Fertig — an Drucker gesendet.": "Done — sent to printer.",

    # ── Manage panel / save & split dialogs / main buttons ──────────────────────
    "Öffnen...": "Open...",
    "Auswahl als Datei speichern": "Save selection as file",
    "Zuerst Seiten auswaehlen.": "Select pages first.",
    "Auswahl speichern als": "Save selection as",
    "Zielordner waehlen": "Choose target folder",
    "Seite(n) gespeichert.": "page(s) saved.",
    "Dateien erstellt": "files created",
    "Seiten pro Teil": "Pages per part",
    "Wie viele Seiten pro Datei?": "How many pages per file?",
    "Seitenbereiche": "Page ranges",
    "Bereiche eingeben": "Enter ranges",
    "Seiten gesamt": "pages total",

    # ── Tools: grayscale / N-Up / imposition / CMYK / workers ───────────────────
    "Vorgang läuft bereits — bitte warten.": "Operation already running — please wait.",
    "Pruefung laeuft bereits — bitte warten.": "Check already running — please wait.",
    "Keine PDF geoeffnet": "No PDF open",
    "Seite": "Page",
    "Blatt": "Sheet",
    "Schreibe Datei …": "Writing file …",
    "Seiten zusammenstellen …": "Collecting pages …",
    "rendert …": "rendering …",
    "Ghostscript: Graustufen-Konvertierung …": "Ghostscript: grayscale conversion …",
    "Graustufen-PDF speichern als": "Save grayscale PDF as",
    "Seite(n) konvertiert (vektorbasiert)": "page(s) converted (vector-based)",
    "Ghostscript (gs) nicht gefunden — für verlustfreie, vektorbasierte Graustufen erforderlich. Bitte 'ghostscript' installieren.":
        "Ghostscript (gs) not found — required for lossless, vector-based grayscale. Please install 'ghostscript'.",
    "Benutzerdefiniert (mm)": "Custom (mm)",
    "B": "W",
    "H": "H",
    "Auf Format zuschneiden / skalieren": "Crop / scale to format",
    "Nur Schnittmarken setzen": "Only add crop marks",
    "  Felder laden": "  Load fields",
    "CMYK-Profil:": "CMYK profile:",
    "Benannte Profile nutzen die passende .icc-Datei aus ~/.local/share/copyshop_pdf_suite/icc/ — fehlt sie, wird generisch konvertiert.":
        "Named profiles use the matching .icc file from ~/.local/share/copyshop_pdf_suite/icc/ — if it is missing, a generic conversion is used.",
    "Rand oben": "Top margin",
    "unten": "bottom",
    "Rand links": "Left margin",
    "rechts": "right",
    "Abstand H": "Gap H",
    "V": "V",
    "Quellseiten in Originalgröße (100 %)": "Source pages at original size (100%)",
    "Seiten werden nicht verkleinert. Das Ausgabeformat muss groß "
    "genug sein, sonst meldet das Werkzeug, wie groß es sein müsste.":
        "Pages are not scaled down. The output format has to be large enough; "
        "if it is not, the tool reports the size it would need.",
    "Bei 100 % ist kein Platz: Seite {p0}×{p1} mm, Feld {p2}×{p3} mm. "
    "Nötig wäre ein Ausgabeformat von mindestens {p4}×{p5} mm.":
        "No room at 100%: page {p0}×{p1} mm, slot {p2}×{p3} mm. "
        "The output format would have to be at least {p4}×{p5} mm.",
    "Schnittmarken hinzufügen": "Add crop marks",
    "N-Up PDF speichern als": "Save N-Up PDF as",
    "Jede Seite wiederholt (ein Blatt je Seite)": "Repeat each page (one sheet per page)",
    "Abstände zu groß — kein Platz für Inhalt.": "Gaps too large — no room for content.",
    "Ränder zu groß — von der Seite bleibt nichts übrig.":
        "Margins too large — nothing left of the page.",

    # ── Static UI strings: menus, buttons, dialogs, merge/plugin panels ─────────
    "CopyShop PDF Suite": "CopyShop PDF Suite",
    "Deutsch": "Deutsch",
    "English": "English",
    "PDF (*.pdf)": "PDF (*.pdf)",
    "Python-Dateien (*.py)": "Python files (*.py)",
    "Mehrere PDFs öffnen": "Open multiple PDFs",
    "Datei oeffnen": "Open file",
    "PDF(s) einfuegen": "Insert PDF(s)",
    "PDFs einfuegen": "Insert PDFs",
    "Extrahieren als": "Extract as",
    "Speichern als": "Save as",
    "Gehe zu Seite": "Go to page",
    "Kopieren": "Copy",
    "Alles auswählen": "Select all",
    "VORSCHAU": "PREVIEW",
    "⚠ Inhalt wird beschnitten": "⚠ Content will be clipped",
    "Seite 1 / 1": "Page 1 / 1",
    "Masse: —": "Size: —",
    "Farbprofil: —": "Color profile: —",
    "Leere Seite eingefuegt.": "Blank page inserted.",
    "Nichts zum Einfuegen.  Zuerst Strg+C.": "Nothing to paste.  Press Ctrl+C first.",
    "Nichts zum Rueckgaengig.": "Nothing to undo.",
    "Rueckgaengig.  Strg+Y = Wiederholen.": "Undone.  Ctrl+Y = Redo.",
    "Nichts zum Wiederholen.": "Nothing to redo.",
    "Wiederholt.": "Redone.",
    "OCR läuft …": "OCR running …",
    "Fertig.": "Done.",
    "Fehler.": "Error.",
    "Bild-Konvertierung fehlgeschlagen": "Image conversion failed",
    "LibreOffice fehlt": "LibreOffice missing",
    "LibreOffice wird benoetigt um Office-Dateien zu oeffnen.\nInstallation: sudo pacman -S libreoffice-still":
        "LibreOffice is required to open Office files.\nInstallation: sudo pacman -S libreoffice-still",
    "Konvertierung fehlgeschlagen": "Conversion failed",
    "Office-Konvertierung fehlgeschlagen": "Office conversion failed",
    "Alle unterstuetzten Dateien (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.pages);;PDF (*.pdf);;Bilder (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;Office (*.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.pages)":
        "All supported files (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.pages);;PDF (*.pdf);;Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;Office (*.docx *.doc *.xlsx *.xls *.pptx *.ppt *.odt *.ods *.odp *.rtf *.pages)",
    "z.B. Seite ": "e.g. page ",
    "z.B.  / {gesamt}": "e.g.  / {gesamt}",
    # Merge / multi-open panels
    "Dateien zusammenfuehren": "Merge files",
    "Dateien oeffnen — CopyShop PDF Suite": "Open files — CopyShop PDF Suite",
    "Dateien sortieren und zusammenfuehren": "Sort and merge files",
    "Dateien sortieren, zusammenfuehren oder einzeln oeffnen":
        "Sort files, then merge them or open them separately",
    "Dateien oeffnen": "Open files",
    "OEFFNEN": "OPEN",
    "Was soll mit den Dateien passieren?": "What should happen with the files?",
    "Keine Auswahl": "No selection",
    "Drag & Drop zum Umsortieren": "Drag & drop to reorder",
    "  Drag & Drop zum Umsortieren  |  Klick zum Auswaehlen  |  Entf zum Entfernen":
        "  Drag & drop to reorder  |  Click to select  |  Del to remove",
    "Drag & Drop zum Umsortieren  ·  Strg/Shift zum Mehrfachauswaehlen":
        "Drag & drop to reorder  ·  Ctrl/Shift for multiple selection",
    "DATEI-INFO": "FILE INFO",
    "Entfernen  (Entf)": "Remove  (Del)",
    "✗  Abbrechen": "✗  Cancel",
    "{p0} Dateien": "{p0} files",
    "  Zusammenfuehren": "  Merge",
    "  Zusammenfuehren...": "  Merge...",
    "  Einzeln oeffnen": "  Open separately",
    "  🔗  Zusammenfuehren  ": "  🔗  Merge  ",
    "  📂  Dateien oeffnen  ": "  📂  Open files  ",
    "  ⏳  Konvertiere...  ": "  ⏳  Converting...  ",
    "  ✗  Fehler  ": "  ✗  Error  ",
    "Konvertiere Dateien...": "Converting files...",
    "Fehler: Keine Dateien konvertiert": "Error: no files converted",
    # ── Open dialogs / supported formats ──────────────────────────────────────
    "Datei öffnen…": "Open File…",
    "Mehrere Dateien öffnen…": "Open Multiple Files…",
    "Datei öffnen": "Open File",
    "Mehrere Dateien öffnen": "Open Multiple Files",
    "Alle unterstuetzten Dateien": "All supported files",
    "Bilder": "Images",
    "Office & Text": "Office & Text",
    "Alle Dateien": "All files",
    "Konvertiere {p0} …": "Converting {p0} …",
    "LibreOffice hat nicht innerhalb von 120 Sekunden geantwortet.":
        "LibreOffice did not respond within 120 seconds.",
    "LibreOffice hat keine PDF erzeugt.": "LibreOffice produced no PDF.",
    "Datei nicht gefunden": "File not found",
    "Die Datei existiert nicht mehr:\n{p0}": "This file no longer exists:\n{p0}",
    "Format nicht unterstuetzt": "Format not supported",
    'CopyShop kann "{p0}" nicht oeffnen.': 'CopyShop cannot open "{p0}".',
    "Datei konnte nicht geoeffnet werden": "Could not open file",
    "Ergebnis konnte nicht geoeffnet werden": "Could not open result",
    "{p0}\n\n{p1}": "{p0}\n\n{p1}",
    # ── Plugin manager ────────────────────────────────────────────────────────
    "Plugin ist bereits installiert.": "Plugin is already installed.",
    "Plugin ersetzen?": "Replace plugin?",
    '"{p0}" ist bereits installiert. Ersetzen?': '"{p0}" is already installed. Replace it?',
    "Installation fehlgeschlagen": "Installation failed",
    "Installation fehlgeschlagen: {p0}": "Installation failed: {p0}",
    "Ordner konnte nicht geoeffnet werden: {p0}\n{p1}":
        "Could not open folder: {p0}\n{p1}",
    # ── OCR ───────────────────────────────────────────────────────────────────
    "✓  tesseract  —  erzeugt durchsuchbare PDFs\n"
    "○  ocrmypdf fehlt  →  optional, bringt echtes Deskew "
    "und PDF/A:  pip install ocrmypdf --break-system-packages":
        "✓  tesseract  —  produces searchable PDFs\n"
        "○  ocrmypdf missing  →  optional, adds true deskew "
        "and PDF/A:  pip install ocrmypdf --break-system-packages",
    "✗  Kein OCR-Programm  →  sudo pacman -S tesseract tesseract-data-deu":
        "✗  No OCR program  →  sudo pacman -S tesseract tesseract-data-deu",
    "Sprachpakete: {p0}": "Language packs: {p0}",
    "Sprachpaket fehlt: {p0}\nVerfuegbar: {p1}\n"
    "Installation z.B.:  sudo pacman -S tesseract-data-{p0}":
        "Language pack missing: {p0}\nAvailable: {p1}\n"
        "Install e.g.:  sudo pacman -S tesseract-data-{p0}",
    "Seiten konnten nicht gerendert werden (poppler installiert?):\n{p0}":
        "Could not render pages (is poppler installed?):\n{p0}",
    "Das PDF enthaelt keine Seiten.": "This PDF has no pages.",
    "Die Datei ist beschaedigt oder keine gueltige PDF.\n{p0}":
        "This file is damaged or not a valid PDF.\n{p0}",
    "Zeitueberschreitung bei Seite {p0}.": "Timed out on page {p0}.",
    "Seite {p0}: {p1}": "Page {p0}: {p1}",
    "tesseract beendet mit Code {p0}": "tesseract exited with code {p0}",
    "OCR hat keine Seiten erzeugt.": "OCR produced no pages.",
    "OCR abgeschlossen ({p0}) — {p1} Seite(n) erkannt":
        "OCR complete ({p0}) — {p1} page(s) recognised",
    ", {p0} bereits mit Text uebersprungen": ", {p0} already had text and were skipped",
    "Deutsch": "German", "Englisch": "English", "Französisch": "French",
    "Spanisch": "Spanish", "Italienisch": "Italian", "Niederländisch": "Dutch",
    "Portugiesisch": "Portuguese", "Afrikaans": "Afrikaans", "Türkisch": "Turkish",
    "Polnisch": "Polish", "Russisch": "Russian",
    "Ausschneiden  (Strg+X)": "Cut  (Ctrl+X)",
    "Zuerst Dateien auswaehlen.": "Select files first.",
    "Zwischenablage ist leer.": "Clipboard is empty.",
    "Rueckgaengig.": "Undone.",
    "Wiederhergestellt.": "Redone.",
    "Nichts rueckgaengig zu machen.": "Nothing to undo.",
    "Nichts zu wiederholen.": "Nothing to redo.",
    "{p0} Datei(en) kopiert.": "{p0} file(s) copied.",
    "{p0} Datei(en) eingefuegt.": "{p0} file(s) pasted.",
    "Auswahl speichern als ({p0} Seiten)": "Save selection as ({p0} pages)",
    "{p0} Seite(n) gespeichert als: {p1}": "{p0} page(s) saved as: {p1}",
    "Nicht konvertierte Dateien": "Files that were not converted",
    "Diese Dateien fehlen im Ergebnis:\n\n{p0}":
        "These files are missing from the result:\n\n{p0}",
    "{p0} Datei(en) konnten nicht konvertiert werden":
        "{p0} file(s) could not be converted",
    "Seiten: ?": "Pages: ?",
    "Seiten: nach Konvertierung": "Pages: after conversion",
    "Seiten: nach Konvertierung bekannt": "Pages: known after conversion",
    # Plugin manager
    "WIE PLUGINS FUNKTIONIEREN": "HOW PLUGINS WORK",
    "Plugin-Ordner öffnen": "Open plugin folder",
    "Plugin auswählen für Details...": "Select a plugin for details...",
    "Plugin installieren (.py)...": "Install plugin (.py)...",
    "Liste aktualisieren": "Refresh list",
    "  (keine Plugins installiert)": "  (no plugins installed)",
    "Plugin auswählen": "Select plugin",

    # ── Exceptions, tool status, combo items, section labels (final pass) ────────
    "Keine PDF geöffnet.\nÖffne zuerst eine PDF im Page Viewer (linke Seite).":
        "No PDF open.\nOpen a PDF in the Page Viewer first (left side).",
    "Die Datei konnte nicht geschrieben werden:\n{p0}\n\n{p1}":
        "The file could not be written:\n{p0}\n\n{p1}",
    "Die Datei konnte nicht geschrieben werden:\n{p0}":
        "The file could not be written:\n{p0}",
    "Diese PDF enthält keine Formularfelder.\n\"Felder laden\" zeigt, was ausfüllbar ist.":
        "This PDF has no form fields.\n\"Felder laden\" shows what can be filled in.",
    "Passwort erforderlich": "Password required",
    "Passwort falsch": "Wrong password",
    "Das Passwort wurde nicht akzeptiert. Erneut versuchen?":
        "That password was not accepted. Try again?",
    "„{p0}“ ist mit einem Passwort geschützt.\n\n"
    "Ohne das Passwort lässt sich die Datei nicht anzeigen oder "
    "bearbeiten. Es wird nur zum Entsperren verwendet und nicht "
    "gespeichert.":
        "\u201c{p0}\u201d is protected with a password.\n\n"
        "Without it the file cannot be shown or edited. It is used only to "
        "unlock the file and is not stored.",
    "Diese PDF ist passwortgeschützt.\nBitte zuerst entsperren (Passwort entfernen), dann erneut öffnen.":
        "This PDF is password-protected.\nPlease unlock it first (remove the password), then reopen.",
    "Lineale und Hilfslinien": "Rulers and guides",
    "Hilfslinien dieser Seite entfernen": "Clear guides on page",
    "Alle Hilfslinien entfernen": "Clear all guides",
    '  Datei pruefen':
        '  Check file',
    'PDF/X-Pruefbericht erscheint hier...':
        'PDF/X check report appears here...',
    'AUSGABEBEDINGUNG':
        'OUTPUT CONDITION',
    'PDF/X exportiert':
        'PDF/X exported',
    'Ghostscript: PDF/X-Konvertierung …':
        'Ghostscript: PDF/X conversion …',
    '(unbenannt)':
        '(unnamed)',
    'Das Ausgabeprofil beschreibt, fuer welche Druckbedingung die Datei separiert wurde. Es wird als Output-Intent eingebettet — die Druckerei liest daran ab, ob die Datei zu ihrer Maschine passt.':
        'The output profile records which printing condition the file was separated for. It is embedded as an output intent — the print shop reads it to tell whether the file suits their press.',
    'Ausgabeprofil:':
        'Output profile:',
    'Benannte Profile nutzen die passende .icc-Datei aus ~/.local/share/copyshop_pdf_suite/icc/ — fehlt sie, wird ein generisches CMYK-Profil eingebettet und im Bericht vermerkt.':
        'Named profiles use the matching .icc file from ~/.local/share/copyshop_pdf_suite/icc/ — without it a generic CMYK profile is embedded and the report says so.',
    'Die Ausgabe traegt keine PDF/X-Kennung.':
        'The output carries no PDF/X identifier.',
    'Die Ausgabe hat keinen Output-Intent.':
        'The output has no output intent.',
    'Der Output-Intent enthaelt kein ICC-Profil.':
        'The output intent contains no ICC profile.',
    '⚠  RGB vorhanden — wird beim Export nach CMYK konvertiert.':
        '⚠  RGB present — will be converted to CMYK on export.',
    '✓  Keine RGB-Farben gefunden.':
        '✓  No RGB colours found.',
    '✓  Jede Seite hat eine TrimBox.':
        '✓  Every page has a TrimBox.',
    'Ebenen (in PDF/X-3 nicht zulaessig, werden aufgeloest):':
        'Layers (not permitted in PDF/X-3, resolved on export):',
    '✓  Keine Ebenen (OCG).':
        '✓  No layers (OCG).',
    'Ausgabebedingung: {p0}  ({p1})':
        'Output condition: {p0}  ({p1})',
    'PDF/X-Export abgeschlossen ({p0}).':
        'PDF/X export finished ({p0}).',
    'Der Export erzeugt {p0}: alle Farben in CMYK, alle Schriften eingebettet, Transparenz reduziert, TrimBox auf jeder Seite. Ebenen (OCG) werden dabei gemaess ihrer Standard-Sichtbarkeit aufgeloest — ausgeschaltete Ebenen sind in der Ausgabe nicht mehr enthalten.':
        'The export produces {p0}: all colour in CMYK, all fonts embedded, transparency flattened, a TrimBox on every page. Layers (OCG) are resolved by their default visibility — a layer switched off is not in the output.',
    "⚠  Profil '{p0}' nicht installiert — generisches CMYK eingebettet.\n   .icc-Datei nach {p1} legen.":
        "⚠  Profile '{p0}' is not installed — generic CMYK embedded.\n   Put the .icc file in {p1}.",
    'Ausgabebedingung: {p0}':
        'Output condition: {p0}',
    'Aufgeloeste Ebenen (nicht in der Ausgabe): {p0}':
        'Resolved layers (not in the output): {p0}',
    'Seiten ohne TrimBox: {p0}':
        'Pages without a TrimBox: {p0}',
    'Seitenzahl stimmt nicht: {p0} statt {p1} — die Datei wurde nicht gespeichert.':
        'Page count does not match: {p0} instead of {p1} — the file was not saved.',
    'PDF/X-Export hat Seite(n) beschaedigt: {p0} — die Datei wurde nicht gespeichert.':
        'The PDF/X export damaged page(s): {p0} — the file was not saved.',
    '⚠  {p0} Seite(n) ohne TrimBox — wird beim Export ergaenzt.':
        '⚠  {p0} page(s) without a TrimBox — added on export.',
    '   ✓ {p0} — sichtbar, wird eingerechnet':
        '   ✓ {p0} — visible, will be included',
    '   ✗ {p0} — ausgeschaltet, entfaellt':
        '   ✗ {p0} — switched off, dropped',
    'Kein CMYK-ICC-Profil gefunden. PDF/X verlangt ein eingebettetes Ausgabeprofil.\n.icc-Datei nach {p0} legen.':
        'No CMYK ICC profile found. PDF/X requires an embedded output profile.\nPut the .icc file in {p0}.',
    "⚠  Profil '{p0}' nicht installiert — generische CMYK-Konvertierung verwendet.\n   .icc-Datei nach {p1} legen.":
        "⚠  Profile '{p0}' is not installed — generic CMYK conversion used.\n   Put the .icc file in {p1}.",
    'PDF/X-Export':
        'PDF/X export',
    'Druckfertige PDF/X-3 erzeugen.':
        'Produce a press-ready PDF/X-3.',
    '  PDF/X exportieren':
        '  Export PDF/X',
    'PDF/X wird erzeugt …':
        'Producing PDF/X …',
    'Ausgabebedingung ändern…':
        'Change output condition…',
    'Was die Datei mitbringt — Anschnitt, Schriften, Bildauflösung — zeigt „Druckvorstufenpruefung“.':
        'What the file brings with it — bleed, fonts, image resolution — is shown by “Druckvorstufenpruefung”.',
    '{p0}  (Profil fehlt — generisches CMYK)':
        '{p0}  (profile missing — generic CMYK)',
    'Ausgabebedingung: {p0}   ·   Bilder: {p1} dpi':
        'Output condition: {p0}   ·   Images: {p1} dpi',
    'Erzeugt {p0}: alle Farben in CMYK gegen das eingestellte Ausgabeprofil, alle Schriften eingebettet, Transparenz reduziert, Ebenen aufgeloest, Bilder auf Druckauflösung. Das Ergebnis wird geprueft, bevor es gespeichert wird.':
        'Produces {p0}: all colour in CMYK against the configured output profile, all fonts embedded, transparency flattened, layers resolved, images at press resolution. The result is checked before it is saved.',
    'Der Beschnitt hat sich veraendert auf Seite(n): {p0} — die Datei wurde nicht gespeichert.':
        'The trim geometry changed on page(s): {p0} — the file was not saved.',
    '{p0} ({p1})':
        '{p0} ({p1})',
    'Druckvorstufe':
        'Prepress',
    'Ausgabebedingung:':
        'Output condition:',
    'Bildauflösung:':
        'Image resolution:',
    'Profil installieren…':
        'Install profile…',
    'Bilder über der eingestellten Auflösung werden beim PDF/X-Export\nreduziert — 300 dpi ist, was die Maschine belichten kann. Bilder\ndarunter bleiben unverändert.':
        'Images above the configured resolution are downsampled on PDF/X export\n— 300 dpi is what the press can image. Images below it are left\nunchanged.',
    'ICC-Profil auswählen':
        'Choose an ICC profile',
    'Profil installiert':
        'Profile installed',
    'Generisches CMYK — kein ICC-Profil nötig.':
        'Generic CMYK — no ICC profile needed.',
    'Kein Profil nötig':
        'No profile needed',
    'Die generische Ausgabebedingung braucht keine ICC-Datei.':
        'The generic output condition needs no ICC file.',
    'Kein CMYK-Profil':
        'Not a CMYK profile',
    'Profil installieren':
        'Install profile',
    'Installiert nach {p0}':
        'Installed to {p0}',
    '✓  Profil installiert: {p0}':
        '✓  Profile installed: {p0}',
    '✗  Profil fehlt ({p0}). Der Export nutzt so lange generisches CMYK und vermerkt das im Bericht. Mit „Profil installieren…“ eine heruntergeladene .icc-Datei hinzufügen.':
        '✗  Profile missing ({p0}). Until it is there the export uses generic CMYK and says so in the report. Use “Install profile…” to add a downloaded .icc file.',
    'Diese Datei ist kein CMYK-Profil ({p0}). Ein Profil in einem anderen Farbraum würde jeden Export falsch separieren.':
        'This file is not a CMYK profile ({p0}). A profile in another colour space would separate every export wrongly.',
    '„{p0}“ als Profil für {p1} installieren?':
        'Install “{p0}” as the profile for {p1}?',
    'unlesbar':
        'unreadable',
    'Druckvorstufe…':
        'Prepress…',
    'DRUCKFAEHIGKEIT':
        'PRESS READINESS',
    'Anschnitt / Endformat (TrimBox)':
        'Bleed / trim size (TrimBox)',
    'Bildauflösung ausreichend':
        'Image resolution sufficient',
    'Ebenen (OCG) auflisten':
        'List layers (OCG)',
    'Anschnitt …':
        'Bleed …',
    'Schriften …':
        'Fonts …',
    'Bildauflösung …':
        'Image resolution …',
    'Alle Schriften eingebettet':
        'All fonts embedded',
    'Kein Anschnitt definiert — Endformat = Seitenformat':
        'No bleed defined — trim size = page size',
    'Schriften nicht eingebettet: {p0}':
        'Fonts not embedded: {p0}',
    'Bilder unter {p0} dpi: {p1}':
        'Images below {p0} dpi: {p1}',
    'Bildauflösung mindestens {p0} dpi':
        'Image resolution at least {p0} dpi',
    'Ebenen: {p0} sichtbar, {p1} ausgeschaltet — beim PDF/X-Export wird beides aufgeloest':
        'Layers: {p0} visible, {p1} switched off — the PDF/X export resolves both',
    'Anschnitt nur auf einem Teil der Seiten (ohne TrimBox: {p0})':
        'Bleed on only some pages (no TrimBox on: {p0})',
    'Anschnitt: {p0:.1f} mm':
        'Bleed: {p0:.1f} mm',
    'Anschnitt: {p0:.1f}–{p1:.1f} mm':
        'Bleed: {p0:.1f}–{p1:.1f} mm',
    '{p0} — knapp, ueblich sind 3 mm':
        '{p0} — tight; 3 mm is usual',
    'Seite {p0} ({p1} dpi)':
        'page {p0} ({p1} dpi)',
    'Das eingebettete Ausgabeprofil ist nicht CMYK.':
        'The embedded output profile is not CMYK.',
    'Die Ausgabe enthaelt noch Ebenen (OCG).':
        'The output still contains layers (OCG).',
    'Die Ausgabe ist verschluesselt.':
        'The output is encrypted.',
    'eingebettete Dateien':
        'embedded files',
    'Die Ausgabe enthaelt {p0}.':
        'The output contains {p0}.',
    'Transparenz wird reduziert — das dauert bei grossen Seiten …':
        'Flattening transparency — this takes a while on large pages …',
    'Rasterauflösung auf {p0} dpi begrenzt — bei dieser Seitengroesse waere ein hoeherer Wert fuer Betrachter nicht mehr lesbar.':
        'Raster resolution capped at {p0} dpi — at this page size a higher value would be unreadable to viewers.',
    'Transparenz (wird beim Export reduziert)':
        'Transparency (flattened on export)',
    'Transparenz …':
        'Transparency …',
    'Keine Transparenz — Vektoren bleiben Vektoren':
        'No transparency — vectors stay vectors',
    'Transparenz auf {p0} Seite(n) — beim PDF/X-Export werden diese Seiten in Pixel umgewandelt (Vektoren gehen dort verloren) und der Export dauert entsprechend laenger: {p1}':
        'Transparency on {p0} page(s) — the PDF/X export converts those pages to pixels (vectors are lost there) and takes correspondingly longer: {p1}',
    'Alle Farben in CMYK gegen das eingestellte Ausgabeprofil, alle Schriften eingebettet, Bilder auf Druckauflösung. Vektoren und Schrift bleiben scharf in jeder Groesse. Das Ergebnis wird geprueft, bevor es gespeichert wird.':
        'All colour in CMYK against the configured output profile, all fonts embedded, images at press resolution. Vectors and type stay sharp at any size. The result is checked before it is saved.',
    '{p0}   ·   {p1}   ·   Bilder: {p2} dpi':
        '{p0}   ·   {p1}   ·   Images: {p2} dpi',
    'Transparenz auf {p0} Seite(n) — PDF/X-3 kann sie nicht darstellen, diese Seiten werden in Pixel umgewandelt (Vektoren gehen dort verloren) und der Export dauert entsprechend laenger: {p1}':
        'Transparency on {p0} page(s) — PDF/X-3 cannot represent it, so those pages are converted to pixels (vectors are lost there) and the export takes correspondingly longer: {p1}',
    'Transparenz auf {p0} Seite(n) — PDF/X-4 behaelt sie, nichts wird gerastert':
        'Transparency on {p0} page(s) — PDF/X-4 keeps it, nothing is rasterised',
    'Standard:':
        'Standard:',
    'Gilt für zwei Dinge: Bilder über diesem Wert werden reduziert\n(darunterliegende bleiben unverändert), und Seiten mit Transparenz\nwerden mit dieser Auflösung in Pixel umgewandelt. Vektoren und\nSchrift bleiben davon unberührt und bleiben in jeder Größe scharf.\nHöher heißt größere Dateien und längere Exportzeiten.':
        'Applies to two things: images above this value are downsampled (those below\nare left alone), and — under PDF/X-3 only — pages with transparency are\nconverted to pixels at this resolution. Vectors and type are unaffected and\nstay sharp at any size. Higher means larger files and longer exports.',
    'PDF/X-4 — moderne Pressen, Transparenz bleibt erhalten':
        'PDF/X-4 — modern presses, transparency preserved',
    'PDF/X-3 — aeltere RIPs, Transparenz wird reduziert':
        'PDF/X-3 — older RIPs, transparency flattened',
    'Druckfertige PDF/X-Datei erzeugen.':
        'Produce a press-ready PDF/X file.',
    'Stopp':
        'Stop',
    'Wird abgebrochen …':
        'Stopping …',
    'Abgebrochen — es wurde nichts geschrieben.':
        'Stopped — nothing was written.',
    'Bild {p0} / {p1} wird separiert …':
        'Separating image {p0} / {p1} …',
    'Bilder werden separiert …':
        'Separating images …',
    '  Komprimieren':
        '  Compress',
    "Keine PDF geladen.": "No PDF loaded.",
    "Keine Seiten ausgewaehlt.": "No pages selected.",
    "Keine Seiten ausgewaehlt": "No pages selected",
    "Kein Ausgabepfad angegeben.": "No output path specified.",
    "Mindestens eine PDF hinzufuegen.": "Add at least one PDF.",
    "Bilder hinzufuegen.": "Add images.",
    "Keine Formularfelder gefunden.": "No form fields found.",
    "Rendere Seiten …": "Rendering pages …",
    "Starte ocrmypdf …": "Starting ocrmypdf …",
    "N-Up läuft …": "N-Up running …",
    "Pruefung abgeschlossen.": "Check complete.",
    "Ghostscript-Fehler": "Ghostscript error",
    "Seite {i} / {total}…": "Page {i} / {total}…",
    "Seiten analysieren …": "Analysing pages …",
    "Pruefung laeuft …": "Checking …",
    "Formular wird ausgefüllt …": "Filling in the form …",
    "Bilder werden exportiert …": "Exporting images …",
    "Ghostscript: komprimiere …": "Ghostscript: compressing …",
    "Ghostscript: CMYK-Konvertierung …": "Ghostscript: converting to CMYK …",
    "Ghostscript: reduziere …": "Ghostscript: flattening …",
    "Komprimiere Streams …": "Compressing streams …",
    "Prüfe Seiten …": "Checking pages …",
    "Komprimiere …": "Compressing …",
    "CMYK-Konvertierung …": "Converting to CMYK …",
    "Ghostscript hat keine Ausgabedatei erzeugt.": "Ghostscript produced no output file.",
    "Konvertierte Seiten prüfen …": "Verifying converted pages …",
    "Drucker-Standard": "Printer default",
    "Papierfach:": "Paper source:",
    "  (Standard)": "  (default)",
    "Aus welchem Schacht das Papier gezogen wird.\nDrucker-Standard: keine Vorgabe senden — die Warteschlange entscheidet.":
        "Which tray the paper is drawn from.\nPrinter default: send no preference — the queue decides.",
    "Drucker-Standard: keine Vorgabe senden — der Drucker bzw. die Warteschlange entscheidet.\nDie Farbinformation bleibt in jedem Fall in der Datei erhalten.":
        "Printer default: send no preference — the printer or queue decides.\nThe colour information is kept in the file either way.",
    "Dieser Drucker meldet nur Graustufen — die Farbe bleibt in der Datei erhalten und kann anderswo gedruckt werden.":
        "This printer reports greyscale only — the colour stays in the file and can be printed elsewhere.",
    "Aktuelle Seite kann nicht ermittelt werden — bitte »Alle Seiten« oder einen Bereich waehlen.":
        "Cannot determine the current page — please choose \u00abAll pages\u00bb or a range.",
    "Hinweis: Der Fallback kann die gewaehlte Farbraum-Umwandlung nicht ausfuehren — es wird ohne sie gedruckt.":
        "Note: the fallback cannot perform the selected colour conversion — printing without it.",
    "{p0} Dateien zusammengefuehrt ({p1} Seiten)": "{p0} files merged ({p1} pages)",
    "Keine der gewaehlten Dateien enthielt Seiten.": "None of the selected files contained any pages.",
    "Ohne Seiten uebersprungen: {p0}": "Skipped, no pages: {p0}",
    "Reduzieren hat Seiteninhalt entfernt — die Datei wurde nicht gespeichert. Bitte ohne 'Reduzieren' erneut speichern.":
        "Flattening removed page content — the file was not saved. Please save again without 'flatten'.",
    "Reduzieren hat die ausgefuellten Werte nicht ins Seitenbild uebernommen — die Datei wurde nicht gespeichert. Bitte ohne 'Reduzieren' erneut speichern.":
        "Flattening did not transfer the filled-in values onto the page — the file was not saved. Please save again without 'flatten'.",
    "Komprimierung hat die Seitenzahl veraendert ({p0} → {p1}) — Datei nicht gespeichert.":
        "Compression changed the page count ({p0} → {p1}) — file not saved.",
    "Komprimierung hat Seite(n) beschaedigt: {p0} — Datei nicht gespeichert.":
        "Compression damaged page(s): {p0} — file not saved.",
    "Reduzieren hat Seite(n) beschaedigt: {p0} — die unreduzierte Datei wurde behalten.":
        "Flattening damaged page(s): {p0} — the unflattened file was kept.",
    "Seitenzahl": "page count",
    "Farbumwandlung hat Seite(n) {p0} geschwaerzt — es wird ohne Umwandlung gedruckt.":
        "Colour conversion blacked out page(s) {p0} — printing without conversion.",
    "ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt und blieben deshalb unveraendert: {p1}":
        "WARNING: {p0} page(s) were damaged by the conversion and were therefore left unchanged: {p1}",
    "Prüfe Seite {p0} / {p1} …{p2}": "Verifying page {p0} / {p1} …{p2}",
    "Seite {p0} erneut versuchen …": "Retrying page {p0} …",
    "{p0} Seite(n) einzeln nachkonvertiert.": "{p0} page(s) re-converted individually.",
    "ACHTUNG: {p0} Seite(n) wurden bei der Konvertierung beschädigt und blieben deshalb unveraendert farbig: {p1}":
        "WARNING: {p0} page(s) were damaged by the conversion and were therefore left unchanged, in colour: {p1}",
    "Ghostscript beendet mit Code {p0}": "Ghostscript exited with code {p0}",
    "Ghostscript hat nach 15 Minuten nicht geantwortet und wurde abgebrochen. Die PDF ist vermutlich beschädigt oder sehr groß.":
        "Ghostscript did not respond within 15 minutes and was aborted. The PDF is probably damaged or very large.",
    "{p0} Seite(n) konnte Ghostscript nicht umwandeln und blieben farbig.":
        "Ghostscript could not convert {p0} page(s); they were left in colour.",
    "Dokument hat nur {p0} Seiten.": "Document only has {p0} pages.",
    "OCR hat keine Ausgabedatei erzeugt.": "OCR produced no output file.",
    "ocrmypdf beendet mit Code {p0}": "ocrmypdf exited with code {p0}",
    "Kein OCR-Programm gefunden.\nInstallation:  pip install ocrmypdf --break-system-packages\n           oder:  sudo pacman -S tesseract tesseract-data-deu":
        "No OCR program found.\nInstallation:  pip install ocrmypdf --break-system-packages\n           or:  sudo pacman -S tesseract tesseract-data-deu",
    "Ghostscript nicht gefunden.\nInstallation:  sudo pacman -S ghostscript":
        "Ghostscript not found.\nInstallation:  sudo pacman -S ghostscript",
    "Zusammengefuehrt": "Merged",
    "Mit Seitenzahlen": "With page numbers",
    "Aus Bildern": "From images",
    "Ebenen verarbeitet": "Layers processed",
    "CMYK konvertiert": "CMYK converted",
    "Formular ausgefuellt": "Form filled",
    "Französisch": "French",
    "Sonderfarbe": "Spot color",
    "Standard (generisch) — universell, ohne ICC-Datei": "Standard (generic) — universal, without ICC file",
    "ISO Coated v2 (FOGRA39) — gestrichenes Papier, EU-Offset-Standard": "ISO Coated v2 (FOGRA39) — coated paper, EU offset standard",
    "PSO Coated v3 (FOGRA51) — modernes gestrichenes Papier, Premium-Offset": "PSO Coated v3 (FOGRA51) — modern coated paper, premium offset",
    "PSO Uncoated v3 (FOGRA52) — ungestrichenes/Naturpapier, Bücher & Briefbögen": "PSO Uncoated v3 (FOGRA52) — uncoated/natural paper, books & letterheads",
    "U.S. Web Coated (SWOP) v2 — US-Rollenoffset, Magazine (gestrichen)": "U.S. Web Coated (SWOP) v2 — US web offset, magazines (coated)",
    "Coated GRACoL 2006 — US-Bogenoffset, hochwertiges gestrichenes Papier": "Coated GRACoL 2006 — US sheet offset, high-quality coated paper",
    "PDF ist passwortgeschuetzt": "PDF is password-protected",
    "Nicht verschluesselt": "Not encrypted",
    "Gemischte Ausrichtungen": "Mixed orientations",
    "Einheitlich: {p0}": "Uniform: {p0}",
    "Keine Farbseiten erkannt": "No color pages detected",
    "Farbraum: {p0}": "Color space: {p0}",
    "nicht erkennbar": "not recognizable",
    "BESTANDEN -- Datei scheint druckfertig.": "PASSED -- file appears print-ready.",
    "⚠  Gemischt (RGB + CMYK) — vor Profidruck vollständig in CMYK umwandeln.": "⚠  Mixed (RGB + CMYK) — convert fully to CMYK before professional printing.",
    "⚠  RGB — vor Profidruck in CMYK umwandeln.": "⚠  RGB — convert to CMYK before professional printing.",
    "ℹ  Farbraum nicht eindeutig erkennbar.": "ℹ  Color space not clearly recognizable.",
    "⚠  Einige RGB-Bilder noch vorhanden (eingebettete Profile).": "⚠  Some RGB images still present (embedded profiles).",
    "✓  Farbraum erfolgreich in CMYK konvertiert.": "✓  Color space successfully converted to CMYK.",
    "(Verifikation nicht möglich)": "(Verification not possible)",
    "REIHENFOLGE": "ORDER",
    "▲  Hoch": "▲  Up",
    "▼  Runter": "▼  Down",
    "✕  Entfernen  (Entf)": "✕  Remove  (Del)",
    "am Ende": "at the end",
    "  —  {p0} zu konvertieren": "  —  {p0} to convert",
    "LibreOffice nicht gefunden.\nsudo pacman -S libreoffice-still": "LibreOffice not found.\nsudo pacman -S libreoffice-still",
    "Nicht unterstuetzt: {p0}": "Not supported: {p0}",
    "Verarbeite: {p0}": "Processing: {p0}",
    # Plugin manager
    "Installierte Plugins:": "Installed plugins:",
    "Installiert. Nach Neustart aktiv.": "Installed. Active after restart.",
    "Buttons oben verwenden.": "Use the buttons above.",
    "Plugin-Ordner:  {p0}": "Plugin folder:  {p0}",
    "Lege eine .py-Datei mit einer BasePanel-Unterklasse und PLUGIN_NAME in den Plugin-Ordner. Nach einem Neustart erscheint das Plugin in der Seitenleiste.":
        "Place a .py file with a BasePanel subclass and PLUGIN_NAME in the plugin folder. After a restart the plugin appears in the sidebar.",
    "Name:    {p0}\nTitel:   {p1}\nInfo:    {p2}": "Name:    {p0}\nTitle:   {p1}\nInfo:    {p2}",
    # Tool names, window controls, plugin subtitle (tr'd at use-site)
    "N-Up Layout": "N-Up Layout",
    "Plugin-Manager": "Plugin Manager",
    "Minimieren": "Minimize",
    "Maximieren": "Maximize",
    "Schließen": "Close",
    "Eigene Werkzeuge installieren und verwalten.": "Install and manage custom tools.",

    # ── Dynamic status / progress messages (f-string templates) ─────────────────
    "Fehler: {p0}": "Error: {p0}",
    "Ghostscript-Fehler:\n{p0}": "Ghostscript error:\n{p0}",
    "Vorschau: {p0}": "Preview: {p0}",
    "Ergebnis geöffnet: {p0}": "Result opened: {p0}",
    "Gespeichert: {p0}": "Saved: {p0}",
    "Gespeichert als: {p0}": "Saved as: {p0}",
    "Gespeichert: {p0} Seiten": "Saved: {p0} pages",
    "Gespeichert: {p0} Seiten -> {p1}": "Saved: {p0} pages -> {p1}",
    "Text gespeichert: {p0}": "Text saved: {p0}",
    "Seite {p0}": "Page {p0}",
    "Seite {p0} / {p1}": "Page {p0} / {p1}",
    "Seite {p0}   ({p1} / {p2} ausgewählt)": "Page {p0}   ({p1} / {p2} selected)",
    "Seite (1 – {p0}):": "Page (1 – {p0}):",
    "nach Seite {p0}": "after page {p0}",
    "{p0} Seiten": "{p0} pages",
    "+{p0} Seiten": "+{p0} pages",
    "{p0} Seiten ausgewaehlt": "{p0} pages selected",
    "{p0} Seiten: {p1}": "{p0} pages: {p1}",
    "{p0} Seite(n) bearbeitet.": "{p0} page(s) processed.",
    "{p0} Seite(n) geloescht.  Strg+Z = Rueckgaengig.": "{p0} page(s) deleted.  Ctrl+Z = Undo.",
    "{p0} Seite(n) kopiert.  Strg+V = Einfuegen (auch in anderen Tabs).": "{p0} page(s) copied.  Ctrl+V = Paste (also in other tabs).",
    "{p0} Seite(n) ausgeschnitten.  Strg+V = Einfuegen.": "{p0} page(s) cut.  Ctrl+V = Paste.",
    "{p0} Seite(n) eingefuegt.": "{p0} page(s) pasted.",
    "{p0} Seite(n) extrahiert.": "{p0} page(s) extracted.",
    "{p0} Seite(n) als neuer Tab geoeffnet.": "{p0} page(s) opened as a new tab.",
    "OK: {p0} Seite(n) {p1} eingefuegt": "OK: {p0} page(s) {p1} inserted",
    "Datei {p0}": "File {p0}",
    "Datei {p0} von {p1}": "File {p0} of {p1}",
    "Datei:   {p0}": "File:   {p0}",
    "Seiten: {p0}": "Pages: {p0}",
    "Seiten:  {p0}": "Pages:  {p0}",
    "Seiten: {p0}  |  {p1} KB": "Pages: {p0}  |  {p1} KB",
    "Groesse: {p0:.0f} KB": "Size: {p0:.0f} KB",
    "Masse: {p0:.0f} × {p1:.0f} mm": "Size: {p0:.0f} × {p1:.0f} mm",
    "Farbprofil: {p0}": "Color profile: {p0}",
    "{p0} Datei(en)": "{p0} file(s)",
    "{p0} Datei(en) ausgewaehlt": "{p0} file(s) selected",
    "{p0} Dateien ausgewaehlt": "{p0} files selected",
    "{p0} Dateien zusammengefuehrt": "{p0} files merged",
    "In {p0} Dateien aufgeteilt": "Split into {p0} files",
    "Teil 1 von {p0}": "Part 1 of {p0}",
    "  Zusammenfuehren  ({p0})": "  Merge  ({p0})",
    "  Einzeln oeffnen  ({p0})": "  Open separately  ({p0})",
    "  —  {p0} werden noch konvertiert": "  —  {p0} still converting",
    "  🔗  Konvertieren & zusammenfuehren  ({p0})": "  🔗  Convert & merge  ({p0})",
    "Konvertierung fehlgeschlagen:\n{p0}": "Conversion failed:\n{p0}",
    "Konvertierung abgeschlossen.\n{p0}\n{p1}": "Conversion complete.\n{p0}\n{p1}",
    "PDF aus {p0} Bildern erstellt": "PDF created from {p0} images",
    "{p0} Bilder exportiert": "{p0} images exported",
    "Seitenzahlen auf {p0} Seiten hinzugefuegt": "Page numbers added to {p0} pages",
    "Schnittmarken auf {p0} Seite(n) gesetzt ({p1:.0f}×{p2:.0f} mm).": "Crop marks added to {p0} page(s) ({p1:.0f}×{p2:.0f} mm).",
    "Broschüre: {p0} Seiten auf {p1} Blattseiten.": "Booklet: {p0} pages on {p1} sheet sides.",
    "{p0}×{p1}-up: {p2} Seiten auf {p3} Bögen.": "{p0}×{p1}-up: {p2} pages on {p3} sheets.",
    "Fertig. {p0} Seiten auf {p1} Blatt ({p2}×{p3}).": "Done. {p0} pages on {p1} sheets ({p2}×{p3}).",
    "Fertig. {p0} → {p1}  ({p2:+.1f}%)": "Done. {p0} → {p1}  ({p2:+.1f}%)",
    "Formular ausgefuellt ({p0} Felder)": "Form filled ({p0} fields)",
    "{p0} Feld(er) geladen.": "{p0} field(s) loaded.",
    "{p0} Ebene(n) gefunden.": "{p0} layer(s) found.",
    "OCR Seite {p0} / {p1} …": "OCR page {p0} / {p1} …",
    "--- Seite {p0} ---\n": "--- Page {p0} ---\n",
    "OCR abgeschlossen ({p0})": "OCR complete ({p0})",
    "OCR abgeschlossen — Text gespeichert ({p0})": "OCR complete — text saved ({p0})",
    "Seite {p0}: max={p1}, farbig={p2:.2f}%": "Page {p0}: max={p1}, color={p2:.2f}%",
    "Seite {p0}: {p1:.0f}x{p2:.0f}pt != {p3:.0f}x{p4:.0f}pt": "Page {p0}: {p1:.0f}x{p2:.0f}pt != {p3:.0f}x{p4:.0f}pt",
    "⚠  Profil '{p0}' nicht installiert — generische CMYK-Konvertierung verwendet.\n   .icc-Datei nach ~/.local/share/copyshop_pdf_suite/icc/ legen.":
        "⚠  Profile '{p0}' not installed — using generic CMYK conversion.\n   Place the .icc file in ~/.local/share/copyshop_pdf_suite/icc/.",

    # ── Settings dialog ───────────────────────────────────────────────────────
    "Leistung": "Performance",
    "Design:": "Theme:",
    "Hell": "Light",
    "Dunkel": "Dark",
    "Vorschau-Threads:": "Preview threads:",
    "Voraus-Rendering:": "Pre-rendering:",
    "Seiten": "Pages",
    "% RAM": "% RAM",
    "Abbrechen": "Cancel",

    # ── Common section labels ─────────────────────────────────────────────────
    "EINGABE": "INPUT",
    "EINSTELLUNGEN": "SETTINGS",
    "OPTIONEN": "OPTIONS",
    "AUSGABE": "OUTPUT",
    "ERGEBNIS": "RESULT",
    "PRÜFUNGEN": "CHECKS",
    "PRUEFUNGEN": "CHECKS",
    "FORMULARFELDER": "FORM FIELDS",
    "ZUSAMMENFUEHREN": "MERGE",
    "MODUS": "MODE",
    "BILDER  nach  PDF": "IMAGES  to  PDF",
    "PDF  nach  BILDER  (aktuell geoeffnete PDF)": "PDF  to  IMAGES  (currently open PDF)",
    "IN CMYK UMWANDELN": "CONVERT TO CMYK",

    # ── Compress panel ────────────────────────────────────────────────────────
    "Methode:": "Method:",
    "Ghostscript (empfohlen)": "Ghostscript (recommended)",
    "pikepdf (schnell, verlustfrei)": "pikepdf (fast, lossless)",
    "GS Qualität:": "GS Quality:",
    "Screen (72 dpi)": "Screen (72 dpi)",
    "Ebook (150 dpi)": "Ebook (150 dpi)",
    "Printer (300 dpi)": "Printer (300 dpi)",
    "Prepress (300 dpi, Farbtreue)": "Prepress (300 dpi, color accurate)",
    "Default": "Default",
    "Ausgabe:": "Output:",
    "Neue Datei erstellen": "Create new file",
    "Original überschreiben": "Overwrite original",
    "Screen  (72 dpi — Web)": "Screen  (72 dpi — Web)",
    "Ebook   (150 dpi — Tablet)": "Ebook   (150 dpi — Tablet)",
    "Drucker (300 dpi — Standard)": "Printer (300 dpi — Standard)",
    "Vordruck (300 dpi — Profidruck)": "Prepress (300 dpi — Professional)",

    # ── Crop / Scale panel ────────────────────────────────────────────────────
    "Format:": "Format:",
    "Breite (mm):": "Width (mm):",
    "Höhe (mm):": "Height (mm):",
    "Ausrichtung:": "Orientation:",
    "Hochformat": "Portrait",
    "Querformat": "Landscape",
    "Skalieren (Inhalt anpassen)": "Scale (fit content)",
    "Zuschneiden (Ränder abschneiden)": "Crop (cut margins)",
    "Alle Seiten": "All pages",
    "Auswahl...": "Selection...",

    # ── Page numbers panel ────────────────────────────────────────────────────
    "Position:": "Position:",
    "Unten Mitte": "Bottom center",
    "Unten Rechts": "Bottom right",
    "Unten Links": "Bottom left",
    "Oben Mitte": "Top center",
    "Oben Rechts": "Top right",
    "Oben Links": "Top left",
    "Schriftgrösse:": "Font size:",
    "Startnummer:": "Start number:",
    "Arabisch (1, 2, 3)": "Arabic (1, 2, 3)",
    "Römisch (i, ii, iii)": "Roman (i, ii, iii)",
    "Präfix:": "Prefix:",
    "Suffix:": "Suffix:",

    # ── Image → PDF panel ─────────────────────────────────────────────────────
    "Bilder hinzufügen": "Add images",
    "Entfernen": "Remove",
    "Hoch": "Up",
    "Runter": "Down",
    "Seitenformat:": "Page format:",
    "An Seite anpassen": "Fit to page",

    # ── Grayscale panel ───────────────────────────────────────────────────────
    "Ghostscript": "Ghostscript",
    "pikepdf + Pillow": "pikepdf + Pillow",

    # ── Forms panel ───────────────────────────────────────────────────────────
    "Aktion:": "Action:",
    "Formularfelder einbetten (flatten)": "Flatten form fields",
    "Formularfelder entfernen": "Remove form fields",
    "Felder laden": "Load fields",

    # ── OCR panel ─────────────────────────────────────────────────────────────
    "Sprache:": "Language:",
    "Deutsch (deu)": "German (deu)",
    "Englisch (eng)": "English (eng)",
    "Deutsch + Englisch": "German + English",
    "Engine:": "Engine:",
    "ocrmypdf (empfohlen)": "ocrmypdf (recommended)",
    "DPI:": "DPI:",
    "Durchsuchbare PDF erstellen": "Create searchable PDF",
    "Nur Text extrahieren": "Extract text only",

    # ── Preflight panel ───────────────────────────────────────────────────────
    "Schriften eingebettet": "Fonts embedded",
    "Transparenz vorhanden": "Transparency present",
    "Überdrucken aktiv": "Overprint active",
    "CMYK-Farbraum": "CMYK color space",
    "Bildauflösung < 150 dpi": "Image resolution < 150 dpi",
    "Anschnitt vorhanden": "Bleed present",
    "Seitenformat prüfen": "Check page format",
    "Prüfen": "Check",

    # ── Color profile panel ───────────────────────────────────────────────────
    "Profil:": "Profile:",
    "Profil einbetten": "Embed profile",
    "Profil entfernen": "Remove profile",
    "Aktuelles Profil:": "Current profile:",
    "Kein Profil eingebettet": "No profile embedded",

    # ── Imposition panel ──────────────────────────────────────────────────────
    "Layout:": "Layout:",
    "2-seitig (Booklet)": "2-up (Booklet)",
    "4-seitig": "4-up",
    "Ausschuss (Drucker)": "Imposition (printer)",
    "Rand (mm):": "Margin (mm):",

    # ── File dialog strings ───────────────────────────────────────────────────
    "PDF öffnen": "Open PDF",
    "PDF Dateien (*.pdf)": "PDF Files (*.pdf)",
    "PDF speichern als": "Save PDF as",
    "Ausgabe-Ordner wählen": "Select output folder",
    "PDFs öffnen": "Open PDFs",
    "Bilder öffnen": "Open images",
    "Bilder (*.png *.jpg *.jpeg *.tiff *.tif *.bmp *.webp)":
        "Images (*.png *.jpg *.jpeg *.tiff *.tif *.bmp *.webp)",

    # ── all_tools.py panel strings ────────────────────────────────────────────
    "PDFs hinzufuegen. Reihenfolge per Drag & Drop.":
        "Add PDFs. Drag & drop to reorder.",
    "+ Hinzufuegen": "+ Add",
    "- Entfernen": "- Remove",
    "Hoch": "Up",
    "Runter": "Down",
    "  Zusammenfuehren und speichern...": "  Merge and save...",
    "Verwendet die aktuell im Viewer geoeffnete PDF.":
        "Uses the currently open PDF in the viewer.",
    "Jede Seite als eigene Datei": "Each page as separate file",
    "Nach Seitenbereichen  (z.B. 1-3, 5, 7-9)": "By page ranges  (e.g. 1-3, 5, 7-9)",
    "Alle N Seiten": "Every N pages",
    "z.B. 1-3, 5, 7-9": "e.g. 1-3, 5, 7-9",
    "Seitenbereiche:": "Page ranges:",
    "Seiten pro Teil:": "Pages per part:",
    "  Trennen und in Ordner speichern...": "  Split and save to folder...",
    "Qualitaetsstufe:": "Quality level:",
    "Ghostscript verwenden (empfohlen)": "Use Ghostscript (recommended)",
    "  Komprimieren": "  Compress",
    "Ausfuehren": "Run",
    "Bilder hinzufuegen. Reihenfolge per Drag & Drop.":
        "Add images. Drag & drop to reorder.",
    "+ Bilder": "+ Images",
    "  In PDF umwandeln...": "  Convert to PDF...",
    "Ausgabeformat:": "Output format:",
    "Aufloesung:": "Resolution:",
    "  Seiten als Bilder exportieren...": "  Export pages as images...",
    "  Seiten analysieren": "  Analyze pages",
    "Ergebnis  (Grau = wird konvertiert, Rot = bleibt unveraendert):":
        "Result  (grey = will be converted, red = stays unchanged):",
    "Praefix:": "Prefix:",
    "Suffix:": "Suffix:",
    "Startnummer:": "Start number:",
    "Erste N Seiten ueberspringen:": "Skip first N pages:",
    "Schriftgroesse (pt):": "Font size (pt):",
    "Abstand vom Rand (pt):": "Distance from edge (pt):",
    "Alle gleich": "All equal",
    "Auf Null setzen": "Reset to zero",
    "Inhalt skalieren": "Scale content",
    "Proportionen beibehalten": "Keep proportions",
    "Alle PDF-Seiten": "All PDF pages",
    "Modus:": "Mode:",
    "Letzte Seite mit Leerseiten auffuellen": "Fill last page with blank pages",
    "Nach dem Speichern reduzieren (fuer Druck)": "Flatten after saving (for printing)",
    "Felder laden": "Load fields",
    "Sprache:": "Language:",
    "Erwartetes Format:": "Expected format:",
    "  Seiten analysieren": "  Analyze pages",
    "Seiten pro Teil:": "Pages per part:",
    "Seiten begradigen": "Deskew pages",
    "Seiten mit Text ueberspringen": "Skip pages with text",
    "Beliebige Groesse": "Any size",
    "Erwartetes Format:": "Expected format:",
    "Seitenformat korrekt": "Page format correct",
    "Einheitliche Ausrichtung": "Consistent orientation",
    "Farbige Seiten erkennen": "Detect color pages",
    "Nicht passwortgeschuetzt": "Not password protected",
    "Bericht:": "Report:",
    "Pruefung starten...": "Start check...",

    # ── NUp panel ─────────────────────────────────────────────────────────────
    "Mehrere Seiten auf einem Blatt — mit Rand- und Abstandssteuerung.":
        "Multiple pages per sheet — with margin and gap controls.",
    "QUELLSEITEN": "SOURCE PAGES",
    "Alle Seiten der Reihe nach": "All pages in sequence",
    "Nur aktuelle Seite (wiederholt)": "Current page only (repeated)",
    "Seitenbereich:": "Page range:",
    "Quelle:": "Source:",
    "Bereich:": "Range:",
    "RASTER": "GRID",
    "Spalten:": "Columns:",
    "Zeilen:": "Rows:",
    "Quellseite um 90° drehen": "Rotate source page 90°",
    "AUSGABEFORMAT": "OUTPUT FORMAT",
    "Wie Quellseite × Raster  (automatisch)": "Match source × grid  (auto)",
    "ABSTÄNDE": "SPACING",
    "Rand oben:": "Top margin:",
    "Rand unten:": "Bottom margin:",
    "Rand links:": "Left margin:",
    "Rand rechts:": "Right margin:",
    "Abstand H:": "H gap:",
    "Abstand V:": "V gap:",
    "Alle Ränder gleich": "All margins equal",
    "Beide Abstände gleich": "Both gaps equal",
    "Vorschau (erstes Blatt)": "Preview (first sheet)",
    "N-Up erstellen": "Create N-Up",
    "Keine PDF geöffnet": "No PDF open",
    "z.B.  1-3, 5": "e.g.  1-3, 5",

    # ── Imposition panel (ImposePanel) ────────────────────────────────────────
    "Broschüre / Ausschießen": "Booklet / Imposition",
    "Seiten in Falzreihenfolge auf größere Bögen ausschießen.":
        "Impose pages onto larger sheets in folding order.",
    "SEITEN NORMALISIEREN": "NORMALIZE PAGES",
    "Wie Quellseite × 2  (automatisch)": "Same as source page × 2  (automatic)",
    "Der Bogen, auf den gedruckt wird. Zwei Seiten liegen nebeneinander\n"
    "darauf — A3 quer nimmt also zwei A4-Seiten auf.":
        "The sheet that goes through the printer. Two pages sit side by side "
        "on it — A3 landscape therefore takes two A4 pages.",
    "Alle Seiten werden auf ihre Hälfte des Bogens gebracht\n"
    "(Proportionen bleiben erhalten, Rand wird aufgefüllt).":
        "Every page is fitted to its half of the sheet\n"
        "(proportions are kept, the remainder is padded).",
    "Seiten in Falzreihenfolge auf größere Bögen ausschießen": "Impose pages onto larger sheets in folding order",
    "Vor dem Ausschießen normalisieren": "Normalize before imposing",
    "Die PDF hat keine Seiten.": "The PDF has no pages.",
    "Ausschießen …": "Imposing …",
    "Ausgeschossene PDF speichern als": "Save imposed PDF as",
    "Kein Ausgabepfad.": "No output path.",

    # ── Grayscale panel (enhanced) ────────────────────────────────────────────
    "Visuell graue Seiten in echtes DeviceGray umwandeln.":
        "Convert visually gray pages to true DeviceGray.",
    "Grün = konvertiert": "Green = converted",
    "Blau = erzwungen": "Blue = forced",
    "Orange = übersprungen": "Orange = skipped",
    "Rot = Farbe": "Red = color",
    "← PDF öffnen um Vorschau zu laden": "← Open PDF to load preview",
    "1 farbiger Pixel = Farbseite": "1 colored pixel = color page",
    "Nach Anteil farbiger Pixel": "By ratio of colored pixels",
    "Farb-Schwellwert": "Color threshold",
    "Abstand vom Grau pro Pixel:": "Color distance per pixel:",
    "Mindest-Anteil farbiger Pixel": "Minimum ratio of colored pixels",
    "Ab wieviel % gilt die Seite als Farbseite?":
        "From what % is the page considered color?",
    "SW": "B&W",
    "Farbe": "Color",
    "Gesamt": "Total",
    "Seite(n) werden konvertiert": "page(s) will be converted",
    "bleiben unveraendert": "remain unchanged",
    "Bitte zuerst eine PDF öffnen.": "Please open a PDF first.",
    "Keine Seiten zum Konvertieren ausgewählt.": "No pages selected for conversion.",
    "Seite(n) konvertiert": "page(s) converted",
    "unveraendert": "unchanged",
    "erzwungen": "forced",
    "uebersprungen": "skipped",

    # ── About dialog ──────────────────────────────────────────────────────────
    "Professionelles PDF-Werkzeug für Copyshops und Druckvorstufe.":
        "Professional PDF tool for copy shops and print pre-press.",
    "Entwickelt mit Python · PyQt6 · pypdfium2 · pikepdf · pypdf":
        "Built with Python · PyQt6 · pypdfium2 · pikepdf · pypdf",
    "open source": "open source",

    # ── Settings dialog (SettingsDialog) ─────────────────────────────────────
    "DARSTELLUNG": "APPEARANCE",
    "Farbschema:": "Color scheme:",
    "LEISTUNG": "PERFORMANCE",
    "Seiten im Hintergrund vorab rendern": "Pre-render pages in background",
    "Gerendert wird in einem Thread: pdfium ist nicht threadsicher, und\n"
    "alle Aufrufe teilen sich eine Sperre. Mehr Threads wären hier ohne\n"
    "Wirkung.":
        "Rendering runs on one thread: pdfium is not thread-safe and every\n"
        "call shares one lock. More threads would make no difference here.",
    "Seitenspeicher:": "Page memory:",
    "≈ {p0:.1f} GB — etwa {p1} Seiten üblicher Größe":
        "≈ {p0:.1f} GB — about {p1} pages of a typical size",
    "Vorab-Rendering:": "Pre-rendering:",
    "Thumbnail-Cache:": "Thumbnail cache:",
    "ALLGEMEIN": "GENERAL",
    "Letzte Datei beim Programmstart automatisch oeffnen":
        "Automatically open last file on startup",

    # ── Crop/Scale panel ─────────────────────────────────────────────────────
    "Vorschau": "Preview",
    "Format": "Format",
    "— Kein —": "— None —",
    "Raender  [+ schneiden / - erweitern]": "Margins  [+ crop / - expand]",
    "Oben": "Top",
    "Unten": "Bottom",
    "Links": "Left",
    "Rechts": "Right",

    # ── Grayscale panel ───────────────────────────────────────────────────────
    "Erkennungs-Modus": "Detection mode",
    "Einzelner Pixel reicht — schon 1 farbiger Pixel = Farbseite":
        "Single pixel suffices — even 1 colored pixel = color page",
    "Nach Anteil — erst ab X% farbiger Pixel = Farbseite":
        "By ratio — only if X% of pixels are colored = color page",
    "Farb-Schwellwert  (Farbabstand pro Pixel)": "Color threshold  (color distance per pixel)",
    "Wie stark muss ein Pixel von Grau abweichen um als 'farbig' zu gelten?":
        "How much must a pixel deviate from gray to be considered 'colored'?",
    "Streng": "Strict",
    "Tolerant": "Tolerant",
    "Mindest-Anteil farbiger Pixel  (fuer Modus 'Nach Anteil')":
        "Minimum ratio of colored pixels  (for 'By ratio' mode)",
    "Wieviel % der Pixel muss farbig sein damit die Seite als Farbseite gilt?":
        "What % of pixels must be colored for the page to count as a color page?",

    # ── Imposition panel ─────────────────────────────────────────────────────
    "2-up (zwei Seiten nebeneinander)": "2-up (two pages side by side)",
    "Broschuere (Sattelheftung)": "Booklet (saddle stitch)",

    # ── Layers panel ─────────────────────────────────────────────────────────

    # ── Color profile panel ───────────────────────────────────────────────────
    "  Farbprofil pruefen": "  Check color profile",
    "Farbprofil-Info erscheint hier...": "Color profile info appears here...",
    "Konvertiert via Ghostscript nach DeviceCMYK. Qualitaetsstufe: Prepress (hoechste Qualitaet, alle Fonts eingebettet).":
        "Converts to DeviceCMYK via Ghostscript. Quality: Prepress (highest quality, all fonts embedded).",
    "✓  Ghostscript verfuegbar": "✓  Ghostscript available",
    "✗  Ghostscript fehlt  →  sudo pacman -S ghostscript":
        "✗  Ghostscript missing  →  sudo pacman -S ghostscript",
}
