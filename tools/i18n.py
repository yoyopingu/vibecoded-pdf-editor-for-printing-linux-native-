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
    "Ebenen": "Layers",
    "Ebenen (OCG)": "Layers (OCG)",
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
    "PDF-Ebenen verwalten":
        "Manage PDF layers",
    "Optionale Inhaltsgruppen steuern.":
        "Control optional content groups.",
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
    "Andere Datei...": "Other file...",
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
    "Thumbnails": "Thumbnails",
    "AUSWAHL": "SELECTION",
    "Alle auswaehlen  (Strg+A)": "Select all  (Ctrl+A)",
    "Auswahl aufheben  (Strg+D)": "Deselect all  (Ctrl+D)",
    "DREHEN": "ROTATE",
    "90° im Uhrzeigersinn": "90° clockwise",
    "90° gegen Uhrzeigersinn": "90° counter-clockwise",
    "OPERATIONEN": "OPERATIONS",
    "Loeschen  (Entf)": "Delete  (Del)",
    "Kopieren  (Strg+C)": "Copy  (Ctrl+C)",
    "Einfuegen  (Strg+V)": "Paste  (Ctrl+V)",
    "Rueckgaengig  (Strg+Z)": "Undo  (Ctrl+Z)",
    "Extrahieren...": "Extract...",
    "Als neuen Tab oeffnen": "Open as new tab",
    "Leere Seite einfuegen": "Insert blank page",
    "Aus Datei einfuegen...": "Insert from file...",
    "SPEICHERN": "SAVE",
    "Speichern": "Save",
    "Speichern als...": "Save as...",
    "ZUSAMMENFUEHREN": "MERGE",
    "PDFs zusammenfuehren...": "Merge PDFs...",
    "TRENNEN": "SPLIT",
    "Jede Seite als Datei": "Each page as file",
    "Alle N Seiten...": "Every N pages...",
    "Nach Bereichen...": "By ranges...",
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
    "Sortiert": "Collated",
    "Farbkonvertierung:": "Color conversion:",
    "Unverändert (Druckertreiber entscheidet)": "Unchanged (printer driver decides)",
    "→ CMYK (für CMYK-Drucker)": "→ CMYK (for CMYK printers)",
    "→ sRGB (für RGB-Drucker)": "→ sRGB (for RGB printers)",
    "  Drucken  ": "  Print  ",

    # ── Settings dialog ───────────────────────────────────────────────────────
    "Leistung": "Performance",
    "Design:": "Theme:",
    "Hell": "Light",
    "Dunkel": "Dark",
    "Vorschau-Threads:": "Preview threads:",
    "Voraus-Rendering:": "Pre-rendering:",
    "Seiten": "Pages",
    "Thumbnail-Cache:": "Thumbnail cache:",
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
    "Modus:": "Mode:",
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
    "pytesseract (schnell)": "pytesseract (fast)",
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
    "Spalten (N-up):": "Columns (N-up):",
    "Zeilen (N-up):": "Rows (N-up):",
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
    "Fehlende Positionen mit Leerseiten auffüllen": "Fill empty positions with blank pages",
    "Fehlende Seiten mit Leerseiten auffüllen": "Fill missing pages with blank pages",
    "AUSGABEFORMAT": "OUTPUT FORMAT",
    "DIN A4  (210 × 297 mm)": "A4  (210 × 297 mm)",
    "DIN A3  (297 × 420 mm)": "A3  (297 × 420 mm)",
    "DIN A5  (148 × 210 mm)": "A5  (148 × 210 mm)",
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
    "Seiten normalisieren und auf größere Bögen anordnen.":
        "Normalize pages and arrange on larger sheets.",
    "Broschüre / Sattelheftung  (A4 → A3)": "Booklet / Saddle Stitch  (A4 → A3)",
    "2-up  (zwei Seiten nebeneinander)": "2-up  (two pages side by side)",
    "N-up Raster  (Spalten × Zeilen)": "N-up Grid  (Columns × Rows)",
    "SEITEN NORMALISIEREN": "NORMALIZE PAGES",
    "Alle Seiten werden zuerst auf dieselbe Größe gebracht\n(Proportionen bleiben erhalten, Rand wird aufgefüllt).":
        "All pages are first brought to the same size\n(proportions preserved, edges filled).",
    "Vor dem Ausschießen normalisieren": "Normalize before imposing",
    "Größte Seite im Dokument  (automatisch)": "Largest page in document  (auto)",
    "Zielgröße:": "Target size:",
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
    "Vorab-Rendering:": "Pre-rendering:",
    "Rendering-Geschwindigkeit:": "Rendering speed:",
    "Ausgewogen  (empfohlen)": "Balanced  (recommended)",
    "Schnell": "Fast",
    "Maximum  (alle Kerne)": "Maximum  (all cores)",
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
    "N-up Raster (Spalten x Zeilen)": "N-up grid (columns x rows)",

    # ── Layers panel ─────────────────────────────────────────────────────────
    "Ebenen laden": "Load layers",
    "Alle AN": "All ON",
    "Alle AUS": "All OFF",
    "Keine Ebenen gefunden.": "No layers found.",
    "Ausgabe reduzieren": "Flatten output",

    # ── Color profile panel ───────────────────────────────────────────────────
    "  Farbprofil pruefen": "  Check color profile",
    "Farbprofil-Info erscheint hier...": "Color profile info appears here...",
    "Konvertiert via Ghostscript nach DeviceCMYK. Qualitaetsstufe: Prepress (hoechste Qualitaet, alle Fonts eingebettet).":
        "Converts to DeviceCMYK via Ghostscript. Quality: Prepress (highest quality, all fonts embedded).",
    "✓  Ghostscript verfuegbar": "✓  Ghostscript available",
    "✗  Ghostscript fehlt  →  sudo pacman -S ghostscript":
        "✗  Ghostscript missing  →  sudo pacman -S ghostscript",
}
