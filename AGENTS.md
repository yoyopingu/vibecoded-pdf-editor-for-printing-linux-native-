# AGENTS.md — CopyShop PDF Suite

A desktop PDF viewer, page manager and prepress toolkit for copy shops.
Python 3.9+ (PyQt6), shells out to Ghostscript, poppler, tesseract,
LibreOffice and CUPS. German is the source language; English is a translation
layer. License is GPL-3.0-only (PyQt6 requires it) — do not weaken it.

`README.md` has the full architecture and install notes. This file is the
short version plus the rules that keep an edit from undoing a previous fix.
Read the comment above the function you are about to change: the reasons are
written there, and a lot of them exist because the obvious version was wrong.

## Commands

```bash
python3 main.py                # run from a checkout
python3 main.py datei.pdf      # one file; several files → sort/merge preview

python3 tests/run.py           # full suite, one process per module
python3 tests/run.py printing  # only modules whose name matches
pytest tests/test_render.py    # one module, if pytest is installed
```

Run from the repository root. Tests are headless (`QT_QPA_PLATFORM=offscreen`),
build their own PDFs, and import the source tree in place. After a change, run
the module you touched — not the whole suite, unless the change is
cross-cutting. `tests/test_hygiene.py` is the one to run when you move imports.

## Architecture

Imports only point downwards.

1. **`tools/render/`** — PDF → pixels. Depends on nothing above it.
   `document_cache` (pooled documents + the process-wide pdfium lock),
   `raster`, `region`, `images`, `caches`, `queue` (one worker; a page turn
   preempts thumbnails).
2. **`tools/viewer/`** — `model`, `canvas`, `single_page`, `page_grid`,
   `manage` (in-memory page-manager edits), `merge`, `tab`, `tab_base`,
   `panel`, `rulers`, `shortcuts`.
3. **`tools/printing/`** — `dialog` (what / on what / how), `preview` (draws
   the sheet), `handling` (poster, n-up, booklet PDFs; no Qt), `content`
   (Comments & Forms), `spool` (Ghostscript + `lp`, or Qt; no widgets — this
   is what gets tested when the shop is billed for the output), `prefs`.
4. **`tools/panels/`** — one module per sidebar tool, over `_shared.py`,
   `_verify.py`, `_colour.py`, `_cropmarks.py`, `_icc.py`, `_prepress.py`,
   `_imposition.py`.

**`tools/shell/`** — `style`, `settings`, `titlebar`, `window`, `instance`,
`inputs`, `crash_report`.

| Module | What it is |
| --- | --- |
| `tools/app.py` | Real entry point. Logging is installed before the panels import. Root `main.py` is only a dev launcher. |
| `tools/app_state.py` | Current document, page model, page, and the signals tools subscribe to. |
| `tools/_base.py` | `BasePanel`. `ensure_view_snapshot()` / `displayed_pdf()` flatten the page manager into a temp PDF. |
| `tools/jobs.py` | The only background-work mechanism. Owned, cancellable, waited for at shutdown. |
| `tools/theme.py` | Live palette `_TV`. Mutated in place; do not rebind it. |
| `tools/i18n.py` | `tr()`. German source strings are the keys; English is `_EN`. |
| `tools/paper.py` | The one paper-size table. Hiding a size removes it from dropdowns but it still resolves. |
| `tools/colorspace.py` | Colour spaces of a page, one open per document, not per page. |

## Hard rules

### Internationalization
- Every user-facing string goes through `tr("Deutsch")`, and the English line
  goes in `_EN`. A string missing from `_EN` is shown in German in both
  languages.
- `QGroupBox` treats `&` as a mnemonic. A title that should show an ampersand
  is written with `&&` (`SEITENGRÖSSE & HANDHABUNG`).

### Concurrency
- Every pdfium call takes `PDFIUM_LOCK` in `tools/render/document_cache.py`.
  libpdfium is not thread-safe across documents either. Never hold that lock
  and the registry lock at the same time.
- Background work goes through `tools.jobs` (`run_async` on panels). Not
  `threading.Thread`, not `QThread`.
- Cancel owned jobs on tab close and window close (`cancel_owner()`).
- Widgets are touched on the GUI thread. A worker delivers results with a
  signal or the jobs callbacks. `QTimer.singleShot` created on a worker never
  fires — that thread has no event loop.
- Before touching a dialog a worker might outlive, check `_is_gone`
  (`sip.isdeleted`). `deleteLater` leaves a Python wrapper whose C++ half is
  already dead; using it does not raise, it takes the process down.

### What a tool is allowed to read
- Operate on the page-manager view (`ensure_view_snapshot()` /
  `displayed_pdf()`), not the file on disk. Reorder, rotate and insert live
  in `PageModel` until save.
- Do not guess a paper size. Unknown or "leave it to the printer" is `None` /
  empty, and the job sends no `media` option. Guessing A4 is how an SRA3 job
  printed an A4 area.

### Memory
- Render caches are bounded by bytes, not by count, and entries carry a file
  revision (`_stat_key`). Keep both.

### Comments and tests
- Comments explain why, not what. When the behaviour changes, update the
  comment. Do not delete a rationale you have not replaced.
- Do not weaken a test to make it pass.
- `tests/test_hygiene.py` fails the tree for an unused import, a name read
  from module scope but never bound, a function-level import that repeats the
  module's own import, and an import cycle — including imports inside
  functions. `__init__.py` re-exports are exempt. A name that exists only to
  force an import order carries `# noqa` and a comment (see `tests/support.py`
  importing `tools.app`: loading `QtNetwork` later, once render threads exist,
  segfaults).

### External binaries
Missing `gs`, `pdftoppm`, `tesseract`, `soffice`, `lp` / `lpstat` (optional
`ocrmypdf`) must produce a message that names the missing program, not a
traceback. Dependency lists live in `requirements.txt` and `README.md` only.

### UI
- New widgets use `_TV` / `apply_theme_globally`. Check light and dark.
- Shortcuts: `tools/viewer/shortcuts.py`. Do not add a conflict.
- Paper sizes come from `tools/paper.py`. No local size table.
- Stylesheet traps that have already shipped as bugs:
  - A `QCheckBox` / `QRadioButton` without `background: transparent` paints
    an opaque bar across the group box.
  - Indicator `border` is drawn *outside* `width` / `height`, and the row
    does not grow to fit it. A ticked control is then clipped top and bottom.
    Draw the mark inside the box, and give the control a `min-height` that
    holds it.
  - `QSpinBox` up/down buttons: any paint property (even a transparent
    background) replaces the native arrows with empty slivers. Width only.
  - `QComboBox` gets its height from `min-height`, never from vertical
    padding — padding makes the popup one row short. Do not restyle
    `::drop-down` or the arrow disappears.
  - An unscoped `QWidget { background }` paints every plain widget, including
    panes inside a group box. A transparent override has to name the object
    (`QWidget#printPaneHost { background: transparent }`). A stylesheet with
    no selector does not do that job.
  - The app stylesheet sets `QSpinBox` to 36 px. The print dialog overrides
    that to 30 px so the percent box does not span two radio rows.

## Print dialog

`docs/print-dialog-concept.html` is the original arrangement (preview left,
settings right, text tabs for Größe / Poster / Mehrere / Broschüre, no Page
Setup, no odd/even subset, copy order as `1,2,3,1,2,3` vs `1,1,2,2,3,3`).
The live dialog has moved on purpose. Do not revert these to match the HTML:

- No hover tips. Nothing in `tools/printing/dialog.py` calls `setToolTip`.
- No Farbraum dropdown. Kommentare & Formulare sits in the sheet-options
  footer. Graustufen on a real printer is a CUPS option; the spooled file
  keeps its colour so the job can be re-routed. Print to PDF is the
  exception: gray is baked into the file, because the file is the output.
- **Feste Größe** percent is not in `prefs.REMEMBERED`. `PrintDialog.done`
  sets it back to 100. Do not restore it from saved settings.
- **In PDF drucken** (`PDF_PRINTER` in `dialog.py`) is not a queue. It is
  always in the printer list. Drucken asks for a path; `build_print_pdf`
  writes the vector job and `write_raster_pdf` writes it when Als Bitmap is
  on. Copies, duplex and the tray are disabled for that target. Fit, Shrink
  and the percentage are applied in the file — there is no CUPS pass
  afterwards. `oriented_sheet_pt` is shared with `print_via_gs` so the sheet
  matches.
- The window must fit the screen **including the title bar**. A frame inset
  of 0 means the window manager has not answered yet, not that there is no
  title bar. `_fit_on_screen` reserves one and runs a second pass.
- The action bar is outside the scroll area and tall enough for the button's
  `sizeHint` plus its margins. A shorter bar lets Drucken paint past the
  window.
- `QScrollArea` with `widgetResizable` shrinks its child to the viewport
  whenever the viewport is taller than the child's *minimum*. The last row
  is then clipped and no scrollbar appears. `_content_height` raises the
  settings pane's minimum to its size hint so a short screen scrolls instead.
- Preview width steps down on a narrow window (316 / 260 / 210). Combos use
  `AdjustToMinimumContentsLengthWithIcon` so the longest printer name does
  not force the dialog off the screen. The page-range field may shrink; it
  is not fixed at 148 px.
- The preview is a composite pixmap. Do not rasterize the imposed poster /
  n-up / booklet PDF inside the preview worker. That double render, in the
  same process as the printing tests, trips the heap fault. The spooler
  prints the real PDF from `handling.py`.
- Booklet reuses `tools/panels/impose.py` (`_booklet_sides`, `_build_impose`).
  Do not reimplement saddle-stitch. Do not silently tick duplex.
- N-up reuses `_build_nup`. `border` defaults to false so the sidebar tool
  does not grow hairlines the operator did not ask for; the dialog passes
  its own checkbox.
- Poster tile count uses `FIT_EPS_PT`. Overlap does not add tiles. Cut marks
  point inward; the shared crop-mark helper draws outward and falls off the
  sheet.
- Imposed jobs are spooled at scale index fixed and 100 %, because the page
  is already the sheet. Booklet forces the orientation the sheet actually has.
- Collate is the `collate` property (bool), not a checkbox. `orient_idx` is
  0 auto, 1 portrait, 2 landscape.
- Enter prints (`_make_enter_print`). The preview's page arrows are
  `QPushButton`s and would otherwise become the dialog's default button.
  A focused Abbrechen still cancels.
- Tests reach into the dialog by attribute name (`scale_fit`, `printer_combo`,
  `by_page_size_check`, `handling`, …). Grep `tests/` before renaming one.
- Form fields: `prepare_print_pdf` in `content.py`. Ghostscript ignores
  `/NeedAppearances`, and a naive page subset drops `/AcroForm`. Both used
  to print empty boxes.

Sidebar **N-Up Layout** and **Broschüre / Ausschießen** stay. The dialog's
imposition is print-time only.

## Tests, the heap fault

A full suite in one process dies of a heap fault about three times in eight,
late, after most tests have passed. `python3 tests/run.py` with no arguments
is one process per module for that reason. `--one-process` reproduces it on
purpose. The runner calls `os._exit` so Qt/pdfium teardown cannot turn a
green run into a non-zero status.

`tests/test_printing.py` is the module that still faults if you pile
`PrintDialog`s, tab switches, and pdfium renders into one process. Rules:

- Close every dialog you open, in `finally`, and `deleteLater` the tab.
- Do not add a test that opens a dialog just to look at it. Assertion-only
  checks on `spool.py` / `handling.py` belong there or in
  `tests/test_print_handling.py`, not in another `PrintDialog`.
- Run `python3 tests/run.py printing` on its own. A segfault after the PASS
  lines, during interpreter shutdown, is the known fault. A segfault with no
  PASS line, or a FAIL, is a real failure — read which test.
- `tests/support.py` sets `COPYSHOP_LOG_DIR` to a temp dir before importing
  the app. Do not point test logs at the user's real log.

## Project layout

```
main.py                  dev launcher (not installed)
tools/app.py             real entry point
tools/render/            pdfium, caches, render queue
tools/viewer/            canvas, tabs, page manager, merge, rulers
tools/printing/          print dialog, preview, spooling
tools/panels/            sidebar tools + shared helpers
tools/shell/             window, style, settings, single-instance
plugins/                 dropped-in BasePanel subclasses
tests/                   tests/run.py is the pytest-free runner
docs/                    print-dialog concept, i18n notes
```
