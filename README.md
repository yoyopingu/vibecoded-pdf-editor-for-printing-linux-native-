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
  documents are converted on the way in.
- Printing with page ranges, duplex, N-up and a monochrome option, with the
  spooled job verified against what was requested.

**Tools** (left sidebar)

| Tool | What it does |
| --- | --- |
| N-Up Layout | several pages onto one sheet |
| Broschüre / Ausschießen | booklet imposition with correct sheet order |
| Komprimieren | Ghostscript-based size reduction, output validated |
| Zuschneiden / Skalieren | crop or resize to a format, with cut marks |
| Seitenzahlen | page numbers |
| Bild ↔ PDF | images to PDF and back |
| Graustufen | greyscale conversion that refuses to damage a page |
| Formulare / Reduzieren | fill and flatten AcroForms |
| OCR — Texterkennung | make scanned PDFs searchable |
| Druckvorstufenprüfung | preflight: colour, resolution, fonts, boxes |
| Ebenen (OCG) | inspect and flatten optional content groups |
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
| `gs` (ghostscript) | Compress, Greyscale, Farbprofil/CMYK, print colour conversion | yes, for those tools |
| poppler (`pdftoppm`) | backs `pdf2image`, used by the OCR page renderer | yes, for OCR |
| `tesseract` + language data | OCR | yes, for OCR |
| `soffice` (LibreOffice) | opening Office, text, CSV, HTML and SVG files | only for those formats |
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

Non-PDF arguments (images, Office documents) are converted on open.

The app is single-instance: launching it again while a window is open hands the
files to that window instead of starting a second copy.

`install.sh` / `uninstall.sh` install a desktop entry and launcher for a
system-wide setup; they are optional and not needed to run from the repo.

---

## Tests

Regression suite — no fixtures on disk. It generates its own PDFs in a temp
directory, runs headless, and exits non-zero on failure.

```bash
python3 tests/run.py              # everything, no pytest needed
python3 tests/run.py zoom render  # only modules whose name matches
pytest tests/ -x -k thumbnail     # if you have pytest
```

Both runners see the same 90 tests. `tests/run.py` exists because the
interpreter this app runs on has PyQt6, pypdfium2, pikepdf and reportlab but
not necessarily pytest, and a suite the app's own machine cannot run is not
much of a suite.

One caveat, stated plainly: running all 90 in a *single* pytest process aborts
in glibc's allocator about a third of the time, after every test has reported
PASS. Each module on its own is clean (`pytest tests/test_render.py`), and
`tests/run.py` is clean over the full suite — the same 90 tests, the same order,
in one process — so this is something about the state pytest keeps alive across
a long run, not a failing test. `tests/run.py` is the runner to trust for a
full pass; pytest is the nicer one for working on a single module.

The tests are grouped by subject — `test_viewer_zoom.py`, `test_printing.py`,
`test_render.py` and so on — over `tests/support.py`, which holds the
QApplication, the fixture PDFs and the helpers more than one subject uses.

It tests the source tree in place, so run it from the repository root. Tests
that need an external binary (OCR) skip themselves and say so when it is
missing.

---

## Architecture

`main.py` owns the application shell: the frameless window and title bar, the
sidebar that switches between the viewer and the tool panels, settings, theming,
and the single-instance IPC that hands files from a second launch to the running
window.

Everything else lives in `tools/`:

| Module | Responsibility |
| --- | --- |
| `page_viewer.py` | The viewer, and the largest module by far. Page rendering and its caches, the priority render queue, the single-page view, the thumbnail page manager (`PageGrid`/`ManagePanel`), the multi-file sort preview (`FileGrid`/`MergeOrderWidget`), `PageModel` (the in-memory page order, rotations and sources), tabs, and the print dialog. |
| `all_tools.py` | Every tool panel: N-Up, imposition, compress, crop/scale, page numbers, image↔PDF, greyscale, forms, OCR, preflight, layers, colour profile. One class per sidebar entry. |
| `_base.py` | `BasePanel`, the contract every tool panel follows — current-file bar, log box, the run button, and `run_async()` for off-thread work. Also `displayed_pdf()`, which flattens the page manager's in-memory edits into a temp PDF so tools operate on what the user sees rather than what is on disk. |
| `app_state.py` | Small singleton holding the current document, page model and page, plus the signals other parts subscribe to (`pdf_changed`, `result_ready`, `status_message`). How a tool finds the open file without asking for one. |
| `multi_open.py` | Which file formats can become a PDF, the file-dialog filter built from that list, and `ConvertWorker` — the background thread that runs img2pdf and LibreOffice. |
| `i18n.py` | `tr()` and the German→English string table. German source strings are the keys. |
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
