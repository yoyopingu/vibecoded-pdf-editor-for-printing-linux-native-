# CopyShop PDF Suite

A desktop PDF viewer, page manager and prepress toolkit built for copy shops —
the jobs you do before something goes on paper: reorder and rotate pages, merge
files, impose a booklet, check a document is printable, and send it to a
printer.

Written in Python with PyQt6. The interface ships in German and English
(Einstellungen ▸ Sprache).

![CopyShop PDF Suite](docs/screenshot.png)

> **Screenshot placeholder** — drop a PNG at `docs/screenshot.png` and this
> renders. Nothing else depends on it.

---

## What it does

**Viewer and page manager**

- Tabbed viewer with continuous zoom, page navigation and text selection.
- *Seiten verwalten* — a thumbnail grid for the whole document: reorder by drag
  and drop, rotate, delete, duplicate, cut/copy/paste, insert blank pages,
  insert pages from other files, undo/redo. Edits live in memory until you
  save, and every tool sees the document as the manager shows it.
- Opening several files at once goes straight to a sort preview, where they can
  be merged into one document or opened as separate tabs. Images and Office
  documents, vector drawings and PostScript files are converted on the way in.
- Printing with page ranges, copies and collation, paper size and tray,
  orientation, scaling, duplex and a colour mode — read from the queue's own
  CUPS defaults, and reopened on whatever you printed with last. Paper
  defaults to **Wie im Drucker eingestellt** ("as set on the printer"), which
  sends no size at all and lets the queue's own setting stand — the right answer whenever what you mean to print
  on is what is loaded, and the only one that works for oversized stock like
  SRA3 on a driverless press that reports no sizes to choose from. Name a size
  only to override that, or type one under **Benutzerdefiniert**. Picking a
  tray brings the size loaded in it along (read from the printer over IPP), so
  the job cannot ask for one size out of a tray holding another. The preview
  then shows the page alone — no sheet, no bleed marks — because none of that
  is known until a size is named. The spooled
  job is verified against what was requested.

**Tools** (left sidebar)

| Tool | What it does |
| --- | --- |
| N-Up Layout | several pages onto one sheet; crop marks follow the fitted page edges |
| Broschüre / Ausschießen | booklet imposition with correct sheet order |
| Komprimieren | Ghostscript-based size reduction, output validated |
| Zuschneiden / Skalieren | crop or resize to a format, with cut marks |
| Seitenzahlen | page numbers |
| Bild ↔ PDF | images to PDF and back |
| Graustufen | greyscale conversion that refuses to damage a page |
| Formulare / Reduzieren | fill and flatten AcroForms |
| OCR — Texterkennung | make scanned PDFs searchable |
| Druckvorstufenprüfung | preflight: bleed, fonts, image resolution, colour, layers |
| PDF/X-Export | one button: press-ready PDF/X-4 (or X-3), CMYK against the shop's output intent |
| Farbprofil / CMYK | colour profile conversion |
| Plugin-Manager | install your own tool panels |

---

## Requirements

**Python 3.9 or newer.**

### System binaries — pip cannot install these

The app shells out to several external programs. They are *not* Python packages
and will not appear in any `pip install`; without them the matching features
fail with a message telling you what is missing.

| Binary | Needed for | Required? |
| --- | --- | --- |
| `gs` (ghostscript) | Compress, Greyscale, Farbprofil/CMYK, print colour conversion, PostScript/EPS input | yes, for those tools |
| poppler (`pdftoppm`, `pdftocairo`, `pdftotext`, `pdftohtml`) | OCR rendering; image, vector, text/web and EPUB export | yes, for those conversions |
| `tesseract` + language data | OCR | yes, for OCR |
| `soffice` (LibreOffice) | opening Office/text/vector files; saving Office documents, presentations and drawings | only for those formats |
| CUPS (`lp`, `lpstat`) | printing | only for printing |
| `ocrmypdf` | optional OCR back end — adds true deskew and PDF/A | optional |

Without `ocrmypdf`, OCR still produces searchable PDFs by driving `tesseract`
directly.

### Python packages

See [`requirements.txt`](requirements.txt). Derived from the actual imports:
PyQt6, pypdf, pikepdf, pypdfium2, Pillow, img2pdf, reportlab, pdf2image.

---

## Install

### Arch / CachyOS

```bash
# system binaries
sudo pacman -S ghostscript poppler tesseract tesseract-data-deu tesseract-data-eng \
               cups libreoffice-still

# Python packages available in the official repos
sudo pacman -S python-pyqt6 python-pypdf python-pikepdf python-pillow \
               python-reportlab img2pdf

# these two are not in the official repos — AUR, or use the venv route below
paru -S python-pypdfium2 python-pdf2image
```

### Debian / Ubuntu

```bash
# system binaries
sudo apt install ghostscript poppler-utils tesseract-ocr tesseract-ocr-deu \
                 tesseract-ocr-eng cups libreoffice

# Python packages
sudo apt install python3-pyqt6 python3-pypdf python3-pikepdf python3-pil \
                 python3-reportlab python3-img2pdf
pip install pypdfium2 pdf2image      # usually not packaged
```

### Any distro — virtualenv

The reliable route. Install the system binaries with your package manager
first, then:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# optional OCR back end
pip install ocrmypdf
```

### As a package

```bash
pip install .            # provides a `copyshop-pdf` command
pip install '.[ocr]'     # …with the optional OCR back end
```

---

## Running

```bash
python3 main.py                    # empty
python3 main.py datei.pdf          # one file
python3 main.py *.pdf              # several — opens the sort/merge preview
```

Non-PDF arguments (images, Office documents, vector drawings and PostScript) are converted on open.

The app is single-instance: launching it again while a window is open hands the
files to that window instead of starting a second copy.

`install.sh` / `uninstall.sh` install a desktop entry and launcher for a
system-wide setup; they are optional and not needed to run from the repo.

### When something goes wrong

The app says so. An unexpected error raises a dialog with a plain-language
cause where it recognises one — an encrypted file, a missing Ghostscript, a
full disk — the full traceback behind **Show Details**, and a button to copy
the report. Unrecognised failures say they are unrecognised rather than
guessing, and the same fault repeating shows one dialog, not one per repaint.

A native crash cannot announce itself as it happens: a segfault leaves no
interpreter to build a dialog with. It is reported at the next start instead,
once, and cleared after.

**Help → Fehlerprotokoll anzeigen** ("Show error log") opens the folder holding
both log files. Started from the desktop entry the app has no terminal, so
these are the only record:

| File | What it holds |
|---|---|
| `copyshop.log` | Everything Python-level: errors, warnings, Qt's own diagnostics, and an unhandled exception from any thread. Rotates at 2 MB, three kept. |
| `copyshop-crash.log` | Native crashes — a segfault takes the interpreter out with no exception to report, so this is written by `faulthandler` from the signal handler itself. |

Both live in `${XDG_DATA_HOME:-~/.local/share}/copyshop_pdf_suite/`. Set
`COPYSHOP_LOG_DIR` to put them somewhere else.

A session that logged `starting` and never logged `exiting cleanly` is one that
died rather than quit — check `copyshop-crash.log` for the same timestamp.

---

## Tests

Regression suite — no fixtures on disk. It generates its own PDFs in a temp
directory, runs headless, and exits non-zero on failure.

```bash
python3 tests/run.py               # everything, no pytest needed
python3 tests/run.py zoom render   # only modules whose name matches
pytest tests/test_render.py        # if you have pytest
```

Both runners see the same 112 tests. `tests/run.py` exists because the
interpreter this app runs on has PyQt6, pypdfium2, pikepdf and reportlab but
not necessarily pytest, and a suite the app's own machine cannot run is not
much of a suite.

One caveat, stated plainly. A full run in a *single* process dies of a heap
fault about three times in eight — deep in, long after most tests have reported
PASS. Every test passes; a crashed run is one that got most of the way and fell
over. Both runners are affected, so it is the app or its libraries in a process
that has built and destroyed the whole GUI a hundred times over, not the
runner.

A single module has never been seen to crash, under either runner. So
`python3 tests/run.py` with no arguments runs **one process per module**:
8 clean runs out of 8, against 5 out of 8 for the same tests in one process.
`--one-process` keeps the old behaviour, because the next person to look at
this needs the reproduction.

That is a way around the fault, not a fix. Chasing it properly means ASAN or
valgrind against Qt and pdfium, which has not been done.

The tests are grouped by subject — `test_viewer_zoom.py`, `test_printing.py`,
`test_render.py` and so on — over `tests/support.py`, which holds the
QApplication, the fixture PDFs and the helpers more than one subject uses.

It tests the source tree in place, so run it from the repository root. Tests
that need an external binary (OCR) skip themselves and say so when it is
missing.

---

## Architecture

Four layers, and imports only ever point downwards.

**`tools/render/` — turning a PDF into pixels.** Depends on nothing above it.

| Module | Responsibility |
| --- | --- |
| `document_cache.py` | Open documents and their parsed pages, both pooled. Loading a page of a large PDF costs hundreds of milliseconds and hundreds of megabytes, so neither is done twice. Also owns the process-wide pdfium lock: libpdfium is not thread-safe even across different documents. |
| `raster.py` | One page, one bitmap, through pdfium's progressive API — so a render that is no longer wanted stops in milliseconds instead of finishing. |
| `region.py` | What to render for a viewport, and the whole-pixel geometry both sides agree on. Past a certain zoom the viewer renders only the window on screen. |
| `images.py` | Whole-page renders, and the arithmetic of what scale to render at. |
| `caches.py` | The thumbnail and full-page LRUs, evicted by what the user is looking at rather than by age. |
| `queue.py` | One worker, a priority heap, and the render tasks. A page turn preempts the thumbnail in flight. |

**`tools/viewer/` — showing it.** One module per part: `model`, `canvas`,
`single_page`, `page_grid`, `manage`, `merge`, `tab`, `panel`.

**`tools/printing/` — putting it on paper.** `dialog` decides what to print, on
what, and how; `preview` draws the sheet as the printer will produce it; `spool`
is the sending — Ghostscript and lp, or Qt. `spool` takes a path, a page model
and the settings, and touches no widget, which is what lets it be tested
directly: it is the part that has to be right when the shop is billing for the
output.

**`tools/panels/` — the tools.** One module per sidebar entry — N-Up,
imposition, compress, crop/scale, page numbers, image↔PDF, greyscale, forms,
OCR, preflight, PDF/X, colour profile — over shared helpers in `_shared.py`,
`_verify.py`, `_colour.py`, `_cropmarks.py`, `_icc.py`, `_prepress.py` and
`_imposition.py`.

**`tools/shell/` — everything around the documents.** `style` (palette,
stylesheets, window icon), `settings` (persisted preferences and their
dialogs), `titlebar`, `window` (`MainWindow` and the sidebar), `instance`
(a second launch hands its files to the running window), and `inputs` (click a
number field and its value is selected, ready to be typed over — one event
filter on the application rather than a subclass per field). `tools/app.py` is
the entry point; `main.py` at the root is a launcher for running from a
checkout, and is deliberately not installed.

And the rest of `tools/`:

| Module | Responsibility |
| --- | --- |
| `_base.py` | `BasePanel`, the contract every tool panel follows — current-file bar, log box, the run button, and `run_async()` for off-thread work. Also `displayed_pdf()`, which flattens the page manager's in-memory edits into a temp PDF so tools operate on what the user sees rather than what is on disk. |
| `jobs.py` | The one mechanism for background work: owned, cancellable, and waited for at shutdown. `Progress` is what a worker body is handed — a progress reporter that also carries the Stop flag and can run a subprocess that dies with it. |
| `app_state.py` | Small singleton holding the current document, page model and page, plus the signals other parts subscribe to (`pdf_changed`, `result_ready`, `status_message`). How a tool finds the open file without asking for one. |
| `input_formats.py` / `multi_open.py` | Input capabilities from Pillow and LibreOffice registries; shared file-dialog filters and PDF conversion through img2pdf, Ghostscript and LibreOffice. |
| `theme.py` | The live palette every widget paints with, and the switch between light and dark. Shared by the viewer, the panels, the print dialog and the window, so it belongs to none of them. |
| `colorspace.py` | Which colour spaces a page uses, read from the file's structure — declared spaces, image spaces, and the colour operators in the content streams, recursing into Form XObjects. Shared by the viewer's label, the Farbprofil tool and the greyscale scan, which each used to have their own copy and disagree. Read through one open of the file for the whole document, not one per page: opening a 500-page PDF costs 83 ms and reading every page out of it 181 ms, so asking page by page made a long document slow to browse precisely because it was long. |
| `i18n.py` | `tr()` and the German→English string table. German source strings are the keys. |
| `paper.py` | The paper sizes the application offers, in one place — built-ins plus whatever the shop adds in Einstellungen ▸ Druckvorstufe. Every size dropdown in the app is built from it: the tools, the print dialog and its fallback list. There were three separate tables before, and they had drifted: the tools reached A0 and had never heard of SRA3. Hiding a size takes it off the dropdowns without stopping it resolving, so a job saved with one keeps working. |
| `logging_setup.py` | The log files and everything that feeds them: rotation, the exception hooks for the main thread and every worker thread, Qt's own message handler, and `faulthandler` for native crashes. Installed from `app.py` before the panels are imported, so a failure while importing them is still recorded. Deliberately Qt-free — it hands failures to a registered reporter rather than drawing anything itself. |
| `shell/crash_report.py` | The other half: the dialog that shows a failure to the user, the plain-language guesses at its cause, and the report of a native crash the previous run died of. Marshals onto the GUI thread, since the thread that failed is rarely the one that may build a widget. |
| `plugin_manager.py` | Discovers `BasePanel` subclasses in `plugins/`, and the panel for installing them. |

Drop a `.py` file defining a `BasePanel` subclass with a `PLUGIN_NAME` into
`plugins/` and it appears in the sidebar on next start.

---

## License

GPL-3.0-only — see [LICENSE](LICENSE).

This is not a preference. The app links PyQt6, which Riverbank distributes under
the GPL v3, so a distributable combined work has to be GPL-compatible. If you
would rather allow later versions of the GPL, change the `license` field in
`pyproject.toml` to `GPL-3.0-or-later`; the licence text is the same either way.

### Automatic print orientation

With a paper size selected, **Automatic** turns each page in the outgoing PDF
or bitmap before submitting it to the printer. Mixed portrait/landscape jobs
use the same physical stock axes, and the app asks CUPS not to rotate them
again. Fit and Shrink still use the printer's actual printable area.

When paper is left to the printer, the app preserves the displayed page
orientation and lets the printer handle the unknown stock. Choosing a paper
size makes automatic rotation independent of printer-side rotation filters.
Print to PDF continues to produce portrait or landscape sheets per source page.

### Crop with the mouse

In **Crop / Scale**, click **Draw crop rectangle** and drag over the preview.
The full source page is shown while drawing, with live output width and height
in the status bar; releasing the mouse sets the four
crop margins and previews the result. Drag in either direction, press **Esc**
to cancel, or adjust the margin fields for precision. **Ctrl+Z** undoes a drawn
rectangle; **Ctrl+Shift+Z** or **Ctrl+Y** redoes it. Click **Run** to save the
cropped PDF. The existing page selection and **All PDF pages** option determine
which pages change. Drawing crops the content without scaling it.

### Save and convert

Opening files converts supported inputs to PDF, including from the command
line, drag and drop, the multi-file merge preview, and the image-to-PDF tool:

- Images: the installed Pillow readers determine the list, including JPEG,
  PNG, TIFF, GIF, WebP, JPEG 2000, AVIF, QOI, PSD, icons, portable bitmaps,
  TGA, PCX and scientific image formats. Identification-only plugins are
  excluded. JPEG keeps its original compressed data where possible and uses
  only its main picture; multipage TIFF and GIF retain their frames. Numeric
  images use a min/max display range when PDF cannot represent their samples.
- Office/text/vector documents: installed LibreOffice import filters with a
  PDF exporter determine the list. This includes Microsoft and OpenDocument
  documents, templates, macro-capable containers, flat XML variants, Apple
  Pages/Numbers/Keynote, older word processors and spreadsheets, Visio,
  Publisher, CorelDRAW, SVG/SVGZ and supported ebook formats such as FB2.
  Without LibreOffice installed, the picker shows the standard format list and
  opening one names the missing program. File-version support and rendering
  fidelity depend on LibreOffice; accepting an extension does not guarantee
  that every historical variant is readable.
- PostScript, EPS and PostScript-based Illustrator files use Ghostscript and
  preserve vectors and page geometry. PDF-compatible Illustrator files open
  directly. EPUB is an export option, not a LibreOffice input format.

Each converted input gets its own output directory and LibreOffice profile,
so same-name inputs cannot overwrite one another or reuse a stale conversion.

**Ctrl+S** saves the current PDF. **File → Save As (Ctrl+Shift+S)** also offers:

- Images: PNG, JPEG, multipage TIFF, WebP, BMP, GIF, JPEG 2000, AVIF, QOI,
  PPM, PGM, PBM, TGA and PCX. The image encoders available in the installed
  Pillow build determine which appear. Resolution is selectable (72–600 DPI).
- Vector/print output: SVG, EPS and PostScript (`pdftocairo`, from poppler).
- Text/web output: UTF-8 text, self-contained HTML and positioned-text XML
  (`pdftotext` / `pdftohtml`, from poppler). These extract existing text;
  scanned pages need OCR first.
- Office output: ODT, DOCX, DOC, flowing-text RTF, FODT, OTT; ODP, PPTX, PPT, FODP,
  OTP; ODG, FODG and OTG (`soffice` / LibreOffice). PDF import reconstructs
  text and drawing objects; the result may differ from the original editable
  document. Writer, Impress and Draw use their respective PDF importers.
  Spreadsheet/table reconstruction is not supplied by these PDF importers.
- EPUB 3 with fixed page layout, using SVG pages from `pdftocairo`. This
  preserves each page visually rather than guessing a reflowable book structure
  ([EPUB specification](https://www.w3.org/TR/epub-33/)).
  RTF uses a flowing HTML intermediate because rotated PDF text boxes do not
  survive a direct conversion to RTF.

All conversions respect the page manager's order, rotations and inserted or
imported pages. While the manager is visible, a partial page selection exports
only those pages, just like PDF Save As. Other formats export a copy and leave
the PDF tab and its edits intact. Formats that hold one page (including SVG,
EPS and the images other than TIFF) produce numbered files for a multipage
selection. The app shows their names before starting and asks before replacing
them. Conversion runs in a cancellable background job; temporary outputs are
staged beside the destination before the batch is published.
