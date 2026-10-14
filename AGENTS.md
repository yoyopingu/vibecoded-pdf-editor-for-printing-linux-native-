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
python3 tests/run.py tools_crop::test_crop_preview_reuses_both_screen_render_caches
python3 tests/run.py --timings # per-test and per-module timings
pytest tests/test_render.py    # one module, if pytest is installed
```

Run from the repository root. Tests are headless (`QT_QPA_PLATFORM=offscreen`),
build their own PDFs, and import the source tree in place. After a change, run
the module you touched — not the whole suite, unless the change is
cross-cutting. `tests/test_hygiene.py` is the one to run when you move imports.
After verification, commit completed work before reporting it done. Keep any
unrelated changes out of that commit.

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
| `tools/jobs.py` | Background jobs other than PDF raster work. Owned, cancellable, waited for at shutdown. Raster tasks use `tools/render/queue.py`. |
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
- Tool background work goes through `tools.jobs` (`run_async` on panels).
  PDF raster work goes through the existing single-worker `tools/render/queue.py`.
  Do not start a separate `threading.Thread` or `QThread` from a panel.
- Cancel owned jobs on tab close and window close (`cancel_owner()`).
- Widgets are touched on the GUI thread. A worker delivers results with a
  signal or the jobs callbacks. `QTimer.singleShot` created on a worker never
  fires — that thread has no event loop.
- Before touching a dialog a worker might outlive, check `_is_gone`
  (`sip.isdeleted`). `deleteLater` leaves a Python wrapper whose C++ half is
  already dead; using it does not raise, it takes the process down.

### What a tool is allowed to read
- Operations use the page-manager view (`ensure_view_snapshot()` /
  `displayed_pdf()`), not the file on disk. Reorder, rotate and insert live
  in `PageModel` until save. A screen preview may resolve each displayed
  position through `PageModel.page_source()` and `get_rotation()` instead of
  flattening the PDF; Crop/Scale does this to reuse the viewer's render cache.
  Never read the original path alone and lose unsaved page-manager edits.
- Do not guess a paper size. Unknown or "leave it to the printer" is `None` /
  empty, and the job sends no `media` option. Guessing A4 is how an SRA3 job
  printed an A4 area.

### Document input
- `tools/input_formats.py` owns accepted extensions: real Pillow readers,
  installed LibreOffice import filters whose document service can export PDF,
  and Ghostscript PostScript/EPS/Illustrator input. Keep every picker, drop
  target and conversion path on these shared sets. Header-only image plugins,
  media/archive placeholders and export-only formats are not PDF inputs.
- `tools/multi_open.py` is the shared converter for single and batch opens.
  Each input needs a unique output directory and isolated LibreOffice profile.
  Check the exact expected output and subprocess status; never pick the first
  unrelated PDF or accept stale output after a failed conversion.
- Preserve JPEG's main-image-only handling (including JPEG aliases); TIFF/GIF
  frames remain pages. Keep native image embedding when supported; decode only
  for unsupported alpha/colour modes or numeric sample display mapping.

### Document export
- `tools/export.py` owns the Save As format catalog and converters. Keep Ctrl+S
  as PDF save; Ctrl+Shift+S offers other formats. Non-PDF output is a copy and
  must not retarget the PDF tab or clear its model edits. Capture source pages,
  order and rotations on the GUI thread, then convert in a cancellable job
  owned by the tab. Honour partial selection only in the visible page manager.
- Office export must explicitly choose Writer/Impress/Draw's PDF input filter
  and use an isolated LibreOffice profile (RTF uses flowing HTML instead).
  EPUB uses Cairo SVG pages in an EPUB 3 fixed-layout package; Writer's EPUB
  exporter cannot handle PDF-imported drawing objects. Never infer PDF export
  support from the list of Office files the app can open. Missing binaries get
  a named error.
- Stage all outputs before publishing, preserve existing files on failure, and
  confirm replacement of numbered page files. Multipage TIFF is a single file;
  SVG/EPS and other images use one numbered file per page.

### Memory
- Render caches are bounded by bytes, not by count, and entries carry a file
  revision (`_stat_key`). Keep both.
- The large full-page cache and small thumbnail cache are shared by screen
  previews. `cached_screen_page()` can use either. Keep the print preview's
  render path separate: its form and annotation visibility differs from the
  screen view, so shared screen pixels could misrepresent the printed sheet.

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
- A tool that is only a form is a slim options column beside the single-page
  viewer (`BasePanel._setup`). It does not get its own preview pane. N-Up,
  Crop/Scale and Broschüre show their output sheets, and Graustufen keeps its
  own grid — those four build a `_tool_splitter` and take the main area. Do not
  fold Graustufen into the column; that preview is the tool.
- New widgets use `_TV` / `apply_theme_globally`. Check light and dark.
- Language selection saves the next launch's preference and shows an information
  dialog. Keep the current session and its translation language intact; never
  restart or close the app when a language is selected.
- Enter and keypad Enter run the visible tool's primary action, including
  when its shared page preview has focus. `BasePanel` owns these window
  shortcuts; hidden tools and modal dialogs must not trigger them. Respect
  the Run button's disabled state and disable shortcut auto-repeat. The
  ordinary viewer retains Enter page navigation when no tool is active.
- Thumbnail-grid shortcuts live in `tools/viewer/shortcuts.py`; global zoom
  routing is in `tools/viewer/panel.py`. `MainWindow._switch()` sets the active
  tool preview, so Ctrl+0 / Ctrl+1 act on the visible N-Up or Crop/Scale sheet
  instead of the hidden viewer. `PreviewPane` in `tools/panels/_shared.py`
  owns fit and physical-size zoom for those sheets. Do not add a conflict.
- Crop/Scale's mouse rectangle is measured on the full visible source page,
  including CropBox and model rotation. It writes the existing margin fields,
  cancels pending format reads and disables content fitting; export stays on
  the manual crop path. Never measure the cropped output as if it were the
  source. Cancel an active drag when its page or zoom coordinate map changes.
  Rectangles use Qt `QUndoStack` / `QUndoCommand`, one command per rectangle,
  restoring any format/fit settings they replaced. Scope actions to the tool,
  retain the PyQt action wrappers, preserve text-field undo, and clear the
  history on document changes. Do not add a second manual history stack.
  Live drag dimensions use cached page geometry and label updates only; never
  read a PDF or refresh/render the preview on mouse movement.
- Paper sizes come from `tools/paper.py`. No local size table.
- N-Up crop marks follow each occupied page's placed visible rectangle after
  fitting/centering, using `_nup_page_rect` in preview and export. Grid cells
  can contain unused padding and must not determine trim edges. Read each
  visible source's dimensions in a batched owned job for mixed-size previews;
  blank padding slots have no crop marks.
- N-Up's single-sided cut-and-stack source mode puts consecutive page runs
  into slot piles in reading order (left to right, then top to bottom).
  Preview and export share `_nup_sheet_pages`; retain blank padding so uneven
  jobs stack in order. Unlike sequential/repeat mode, a one-page cut-and-stack
  job places that page only once.
- The booklet sidebar preview walks `_booklet_sides` front/back faces, including
  padding, using `PreviewPane` for navigation and zoom. Resolve PageModel
  sources directly and reuse the shared screen caches; never flatten or
  rasterise an imposed PDF for this screen preview. Read page-tree dimensions
  in an owned job so complex page content cannot delay placeholder geometry.
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
  - Set `selection-color` together with `selection-background-color` on input
    fields. The native white selected text is unreadable on the light theme's
    pale selection background, including inside numeric editors.
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
- Custom stock dimensions may be entered landscape-first. Normalize their
  axes before applying Automatic/Portrait/Landscape in vector and Qt paths.
  Bitmap Print to PDF uses the selected sheet dimensions and centres the
  existing raster on that PDF page; the raster's own size is not the paper.
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
- `handling.preview_sheets` shares output geometry and page order with the
  preview. Render each visible source separately, preserving aspect ratio;
  never repeat the first page in every slot. Print-preview rasters are kept
  only for the visible sheet (at most 16 sources). Booklet navigation walks
  front and back faces, including padding and the selected sheet range.
- Apply Comments & Forms before imposition, then pass `DOCUMENT` downstream.
  Applying "form fields only" again to an imposed sheet erases its content.
- Booklet always fits pages into their half-sheet. Its automatic-rotation
  checkbox controls a quarter turn, not whether fitting is enabled.
- Booklet reuses `tools/panels/impose.py` (`_booklet_sides`, `_build_impose`).
  Do not reimplement saddle-stitch. Do not silently tick duplex.
- N-up reuses `_build_nup`. `border` defaults to false so the sidebar tool
  does not grow hairlines the operator did not ask for; the dialog passes
  its own checkbox.
- Poster tile counts must cover the full scaled source image. The first tile
  covers a full sheet; later tiles add sheet minus overlap, so overlap can
  add rows/columns (200% A4 on A4 is 2×2 without overlap, 3×3 with a small
  overlap). Preview and output share `poster_grid` / `poster_steps`. Apply
  `FIT_EPS_PT` in points, not to a dimensionless ratio that could hide real
  edge content. Cut marks point inward; the shared crop-mark helper draws
  outward and falls off the sheet.
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

Printing tests were split into behavior modules because piling `PrintDialog`s,
tab switches and pdfium renders into one process can fault. The isolated
`test_printing_dialog` module can still crash intermittently, including after
some PASS lines and before its summary. Rules:

- Close every dialog you open, in `finally`, and `deleteLater` the tab.
- Do not add a test that opens a dialog just to look at it. Assertion-only
  checks on `spool.py` / `handling.py` belong there or in
  `tests/test_print_handling.py`, not in another `PrintDialog`.
- Run `python3 tests/run.py printing` on its own. If a module crashes, report
  it separately from assertion failures and rerun that module to see whether
  it repeats. PASS lines before a crash do not make the run green. Investigate
  a repeatable crash or FAIL rather than attributing it to the old heap fault.
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
