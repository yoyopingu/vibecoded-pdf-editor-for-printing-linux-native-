# AGENTS.md — CopyShop PDF Suite

A desktop PDF viewer, page manager and prepress toolkit for copy shops.
Python 3.9+ (PyQt6 GUI), shells out to Ghostscript, poppler, tesseract,
LibreOffice and CUPS. German is the source language; English is a translation
layer. License is GPL-3.0-only (PyQt6 requires it) — do not weaken it.

The README.md has a detailed, well-maintained architecture section; read it
for the full picture. This file is the condensed, agent-oriented version plus
the rules that keep changes from breaking things.

## Commands

```bash
python3 main.py                # run the app from a checkout (empty)
python3 main.py datei.pdf      # run with a file; multiple files → sort/merge preview

python3 tests/run.py           # full test suite, one process per module (preferred)
python3 tests/run.py zoom render   # only modules whose name matches
pytest tests/test_render.py    # single module / single test via pytest
```

- Tests run headless, generate their own PDF fixtures in a temp dir, and test
  the source tree in place — run them from the repository root.
- **Run the relevant test module(s) after every change**; run the full suite
  before considering work done.
- Known issue: a full run in a *single* process dies of a heap fault ~3 times
  in 8, late, after most tests have passed. This is why the default runner is
  one process per module. A crashed run is not a failed test — check which
  modules reported PASS. `--one-process` reproduces the fault on purpose.
- Tests needing an external binary (e.g. OCR / tesseract) skip themselves and
  say so when it is missing.

## Architecture

Four layers; **imports only ever point downwards** (higher layers may not be
imported by lower ones):

1. **`tools/render/`** — PDF → pixels. Depends on nothing above it.
   `document_cache` (pooled documents/pages + the process-wide pdfium lock),
   `raster` (progressive per-page bitmaps), `region` (viewport tiling at high
   zoom), `images`, `caches` (byte-bounded LRUs), `queue` (one worker,
   priority heap, page turns preempt thumbnails).
2. **`tools/viewer/`** — showing it. One module per part: `model`, `canvas`,
   `single_page`, `page_grid`, `manage` (page manager with in-memory edits),
   `merge`, `tab`, `tab_base`, `panel`, `rulers`, `shortcuts`.
3. **`tools/printing/`** — `dialog` (what/on what/how), `preview` (draws the
   sheet), `spool` (Ghostscript + lp or Qt; takes paths + settings, touches no
   widget, and is tested directly — it is the part that must be right when the
   shop bills for output), `handling`, `content`, `prefs`.
4. **`tools/panels/`** — one module per sidebar tool (nup, impose/booklet,
   compress, crop_resize, page_numbers, img_pdf, grayscale, forms, ocr,
   preflight, pdfx, colour_profile) over shared helpers in `_shared.py`,
   `_verify.py`, `_colour.py`, `_cropmarks.py`, `_icc.py`, `_prepress.py`,
   `_imposition.py`.

**`tools/shell/`** — everything around the documents: `style` (palette,
stylesheets), `settings` (persisted prefs), `titlebar`, `window` (MainWindow +
sidebar), `instance` (single-instance hand-off), `inputs` (app-wide number
field event filter), `crash_report` (user-facing error dialog).

Other `tools/` modules worth knowing:

| Module | Responsibility |
| --- | --- |
| `app.py` | Real entry point (`copyshop-pdf` script). Root `main.py` is only a dev launcher, not installed. Installs logging **before** importing panels so import failures are recorded. |
| `app_state.py` | Small singleton: current document, page model, page + signals (`pdf_changed`, `result_ready`, `status_message`). How a tool finds the open file. |
| `_base.py` | `BasePanel`, the contract every tool panel follows (current-file bar, log box, run button, `run_async()`). Also `ensure_view_snapshot()` / `displayed_pdf()`: flattens the page manager's in-memory edits into a temp PDF. |
| `jobs.py` | The one mechanism for background work: owned, cancellable, waited for at shutdown. `Progress` carries the Stop flag and can run a subprocess that dies with it. |
| `theme.py` | Live palette (`_TV`) and light/dark switching; shared by viewer, panels, print dialog, window. |
| `i18n.py` | `tr()` + the German→English table. German source strings are the keys. |
| `paper.py` | The one paper-size table used by every size dropdown in the app. |
| `colorspace.py` | Colour spaces of a page, read from file structure; one open per document, not per page. |
| `logging_setup.py` | Log files, rotation, exception hooks, Qt message handler, `faulthandler`. Qt-free; hands failures to a registered reporter. |
| `plugin_manager.py` | Discovers `BasePanel` subclasses with `PLUGIN_NAME` in `plugins/`. |
| `multi_open.py`, `pdf_access.py`, `ghostscript.py`, `pageverify.py` | Format conversion on open, lock detection, gs invocation, spool verification. |

## Hard rules

### Internationalization
- Every user-facing string goes through `tools.i18n.tr()`. **German is the
  key** — write `tr("Speichern")`, and add the English mapping to `_EN` in
  `tools/i18n.py`. Never hardcode untranslated strings in GUI code.
- Check both languages when touching layouts, tooltips, or messages.

### Concurrency
- **All pdfium calls happen under `PDFIUM_LOCK`** (`tools/render/document_cache.py`)
  — libpdfium is not thread-safe even across different documents. It must
  never be held at the same time as the registry lock.
- Background work goes through `tools.jobs` (`run_async` on panels, owned and
  cancellable). Never raw `threading.Thread` or `QThread` for jobs.
- Cancel owned jobs on tab close / window close via `cancel_owner()`.
- Anything touching widgets happens on the main thread; worker results marshal
  back via AppState signals or the jobs machinery.

### PDF access
- Tools must operate on what the user sees: go through
  `ensure_view_snapshot()` / `displayed_pdf()` (the page-manager view with
  reorder/rotate/insert applied), never the raw file on disk.
- Original path comes from `AppState.current_pdf`; the page model holds
  per-page rotation/source.

### Memory & caches
- Render caches are bounded by **bytes**, not count, and entries carry a file
  revision (`_stat_key`) to detect changes. Keep that property when touching
  them.

### Style of the codebase
- **Minimal diffs.** Never guess — read the actual code first. Preserve
  behaviour unless it is demonstrably a bug.
- The code is heavily comment-annotated with the *reasons* behind design
  decisions (e.g. why a lock exists, why a cache is byte-bounded). When you
  change behaviour, update the comment that explains why, and don't delete
  rationale you haven't replaced.
- Never disable or weaken a test to make it pass; fix the underlying issue.

### External binaries
Features shell out to `gs`, `pdftoppm`, `tesseract`, `soffice`, `lp`/`lpstat`
(optional: `ocrmypdf`). Missing binaries must surface a message naming what is
missing — not a traceback. Requirements live in `requirements.txt` and
README.md and nowhere else (don't restate dependency lists in code comments).

### UI / theming
- New widgets must respect the theming system (`_TV` palette in `theme.py`,
  `apply_theme_globally`); verify changes in both light and dark themes.
- Don't introduce keyboard shortcut conflicts; check `tools/viewer/shortcuts.py`.
- Paper sizes come from `tools/paper.py` — never build a local size table.
- The print dialog's visual design is frozen against `docs/print-dialog-concept.html`;
  keep spacing/geometry locked to it.

## Project layout quick reference

```
main.py                  dev launcher (not installed)
tools/app.py             real entry point
tools/render/            pdfium rasterization, caches, render queue
tools/viewer/            canvas, tabs, page manager, merge, rulers
tools/printing/          print dialog, preview, spooling (lp/ghostscript)
tools/panels/            sidebar tools (one module each) + shared helpers
tools/shell/             window, style, settings, single-instance, crash dialog
plugins/                 dropped-in BasePanel subclasses (user plugins)
tests/                   regression suite; tests/run.py = pytest-free runner
docs/                    concepts + notes (print dialog concept, i18n notes)
```
