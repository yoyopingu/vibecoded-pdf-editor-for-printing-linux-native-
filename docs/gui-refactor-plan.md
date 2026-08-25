# Folio GUI refactor — implementation plan

The app is being rebuilt to the design prototyped in `docs/gui-concept.html`
(analysis of the old GUI: `docs/gui-analysis.md`). This file is the standing
plan: the chat it was written in may be compacted away, this file stays.
Read it before touching `tools/shell/`, `tools/viewer/`, `tools/panels/`
or `tools/printing/`.

## Ground rules

- One step = one visual or structural change. The app must run and the tests
  must be green after every step.
- Dev loop: `python3 tests/run.py quick`. Before calling any phase done:
  full `python3 tests/run.py` (~300 tests, ~4 min).
- Run `test_hygiene` after ANY change under `tools/` — it catches names
  unbound by moved imports, and this refactor moves a lot of imports.
- Screenshot both themes after each phase (pattern: `/tmp/opencode/shots*.py`
  scripts + headless Firefox for the concept file).
- The AGENTS.md "which tests after which edits" table applies throughout.

## Settled decisions (from the design review — do not relitigate)

1. **Print dialog: Farbkonvertierung is removed.** Delete the combo and all
   its wiring (dialog.py:443–457, :563, :954, :993, :1008, :1208, :1350,
   :1381, :1478; `prefs.REMEMBERED`'s `"colorconv"` entry; the
   `-sColorConversionStrategy` branch in spool.py:735–739 becomes unreachable
   from the UI but stays — colour conversion remains a tool-panel job
   (colour_profile.py), pre-print).
2. **Tool progress: "option a" + Protokoll window.** No LogBox anywhere.
   A running tool reports through the single center status line, updated in
   place ("Graustufen: Seite 3 / 6 …"); the final result (and errors) stay
   until the next message AND disappear when the tool is exited (back
   button, tools toggle, view switch — the view's default message returns).
   The Stop button sits beside the tool's Apply button and exists only
   while a job runs. The center message line is CLICKABLE: it opens the
   Protokoll window — a scrollable log of every app message (timestamp +
   level INFO/OK/ERR: file opens, saves, prints, merges, tool progress
   lines, errors). Every transient message is also logged there; logging
   is the memory, the status line is only the present. The Hilfe menu's
   "Fehlerprotokoll anzeigen" is RENAMED to "Logs anzeigen" and opens the
   same exact window — one implementation, two entry points. See the
   concept file: pick a tool, press its apply button, then click the
   status line.
3. **Merge: concept-identical fourth view.** "+" always opens the file
   dialog first (single file → opens directly; multi-select → merge view).
   Merge is a view in the main chrome: its pane (Auswahl / Ansicht /
   Reihenfolge / Operationen / Öffnen with Zusammenführen + Einzeln öffnen)
   mounts into the main sidebar via `SidebarHost`; FileGrid + shared rail
   fill the main area; no separate left column, no hidden tool list.
   ViewSwitch gains no entry (entered via "+" only). Escape/cancel/
   Zusammenführen return to the previous view; the merge temp-dir cleanup
   (open_flow.py:63–69) moves with it. Consequence: one pending merge batch
   at a time (replaces the old "several merge tabs" behaviour).
4. **Zoom: per tab AND per view.** Preview, manage grid and layout sheets
   each keep their own zoom state per tab (already true in the code —
   SinglePageView / PageGrid / PreviewPane own theirs). The window-level
   zoomer in the StatusBar is a remote control: routes −/+/fit to the active
   view of the active tab, re-syncs its label on every tab and view switch.
5. **Colour/greyscale counter: structure scan.** Fed by
   `tools/colorspace.py scan_document()` (per-page `is_grey_only`), running
   in the background `tools/jobs` job the viewer's `ColourSpaceLabel`
   (viewer/color_label.py) already starts — cached by `(revision, page)`,
   so it is essentially free and refreshes after edits. When the greyscale
   tool's pixel scan (`grayscale.py _scan_pages`) has run for the current
   file, its accurate numbers override. No new pool, no GUI-thread scanning.

## Phases

### Phase 0 — Groundwork (no visual change)
| # | Step | Files | Tests |
|---|---|---|---|
| 0.1 | Fix stale AGENTS.md (window is `tools/shell/window.py`, not `app.py`; document the sidebar-slot protocol, rail delegation, the two theme vocabularies) | `AGENTS.md` | — |
| 0.2 | Baseline: full suite green + dark/light screenshots as before/after reference | — | full suite |
| 0.3 | New `tools/shell/icons.py`: the concept's SVG set as a QIcon factory (theme-aware, stroke-coloured), no callers yet | new file | `test_hygiene` |

### Phase 1 — One palette (values only, no structure)
Keep BOTH access mechanisms — QSS via `shell_colours()` and runtime painting
via `viewer_colours()`/`_TV` — only change the values. This restyles ~20
self-painting widgets without touching logic.
| # | Step | Files | Tests |
|---|---|---|---|
| 1.1 | Add `SURFACE_2`, `SURFACE_3`, `CANVAS`, `LINE_STRONG` keys to both vocabularies; remap existing keys to the concept's neutral values (dark + light). *Done — plus `FAINT` in shell (concept's --faint: group headings, section labels) and `surface_2/3`, `canvas`, `line_strong` in viewer. Shell `INPUT_BG`=SURFACE_2 + `HOVER`=SURFACE_3 as the values-only compromise (QSS still shares them between fields and buttons); viewer `input_bg`=SURFACE_3 (fields only).* | `tools/theme.py` | `test_tools_colour` |
| 1.2 | Rewrite `_build_style()` QSS: neutral chrome, 1px borders, tabs rounded right-only + top-accent active state, new button/input/scrollbar/menu styling. objectNames unchanged | `tools/shell/style.py` | `test_app`, eyeball both themes |
| 1.3 | Retint self-painting widgets to the new keys: `paint_card` (page_grid/merge), canvas bg, `#navSide`/`#infoBar`, CurrentFileBar, LogBox (until Phase 5 removes it), PreviewPane, layout inline styles, print dialog/preview inline styles | `page_grid.py`, `merge.py`, `canvas.py`, `single_page.py`, `panels/base.py`, `panels/_shared.py`, `layout_view.py`, `printing/dialog.py`, `printing/preview.py` | `quick` |

### Phase 2 — One status bar (full width, window level)
Replaces the per-tab `#infoBar` (single_page.py:253–351). Readings left
(format · colorspace · colour/greyscale counter · preflight), transient
message center (decision 2's target), ruler toggle + zoomer right.
Page count lives in the rail (already does).
| # | Step | Files | Tests |
|---|---|---|---|
| 2.1 | New `tools/shell/statusbar.py`: `StatusBar` (QSS-styled, subscribes to a status bus; owns ruler btn + zoomer). *Done — incl. QSS section in style.py (#statusBar/#sbMsg/#sbZoomer…), `#iconBtn:checked` accent-soft, elided clickable centre message (`_MessageLabel`), transient-vs-held message API, `set_rulers_checked` blockSignals, theme-registered (icons rebuilt via `_apply_theme`).* | new file | `test_hygiene` |
| 2.2 | Status bus: extend `AppState` with signals (`zoom_changed`, `page_metrics_changed`, `colorspace_changed`, `preflight_changed`, `saved_changed`); `SinglePageView` emits instead of writing its 8 labels. *Done — `saved_changed` became unnecessary (`set_saved_at` stays per-tab until 2.4 decides); emitters: single_page (zoom×3, metrics, preflight), color_label (colorspace, bare profile name). #infoBar still exists — deletion is 2.4.* | `app_state.py`, `single_page.py`, `color_label.py` | `test_viewer_zoom` |
| 2.3 | Colour/greyscale counter: aggregate `is_grey_only` per page from the scan `ColourSpaceLabel` already runs (decision 5); greyscale tool publishes its pixel-scan numbers as override. *Done — `count_grey_pages(pages)` (structure verdicts, unknown-gate, single-file pixel override), `set_pixel_counts`/`pixel_counts` keyed (path, revision) with stale-prune, bus signal `colour_counts_changed(object)`, StatusBar subscribes to all five readings signals, `ColourSpaceLabel(on_scan_complete=…)`, `SinglePageView.publish_colour_counts()` on load/refresh/scan-done/`_sync_rail_count`, grayscale `_publish_counts()` after `_reclassify` (records override + emits only when the scanned file is the open one; `_scanned_path` now set BEFORE `_reclassify`). 4 new tests in `test_tools_colour.py`.* | `colorspace.py`, `statusbar.py`, `grayscale.py`, `app_state.py`, `color_label.py`, `single_page.py`, `tab.py` | `test_tools_colour` |
| 2.4 | Mount `StatusBar` in `MainWindow` below the body (full width); delete `#infoBar`; re-route `show_status()` + the 6 s timer to the center message; center message resets to the view default when a tool is exited | `shell/window.py`, `single_page.py`, `panel.py` | `test_viewer_zoom`, `test_manage`, `test_app` |
| 2.5 | Protokoll window (decision 2): log store + modal (timestamp, INFO/OK/ERR), opened by clicking the center message; every action message routed through one `notify(msg, level)` that sets the line AND logs; tool progress lines log individually; Leeren + Schließen in the footer. **The Hilfe menu's "Fehlerprotokoll anzeigen" is renamed to "Logs anzeigen" (titlebar.py:129, crash_report.py:78, crash_report.py:195, i18n.py:63) and opens the SAME window — one widget, one log store, two entry points, zero duplicated code.** | `statusbar.py`, `panels/base.py` (replaces LogBox), message sites, `titlebar.py`, `crash_report.py`, `i18n.py` | `test_hygiene`, `test_tools_misc`, `test_crash_report` |

### Phase 3 — Full-width doc bar (tab bar survives every view)
Today the `QTabWidget` lives inside `PageViewerPanel` (stack page 0), which
is why tabs vanish in Layout/tool panels. Move tab bar + doc-actions row to
`MainWindow`, spanning above sidebar AND main area.
| # | Step | Files | Tests |
|---|---|---|---|
| 3.1 | `MainWindow` layout: TitleBar / **DocBar** / (sidebar \| stack) / StatusBar. `PageViewerPanel` hands `tabs` + doc-actions row up; keeps its controller role (add/close/change-tab logic stays). *Done — `QTabWidget` replaced with `QTabBar` + `QStackedWidget` mirrored through a `_TabHost` proxy. `panel.doc_row` (`QWidget` wrapping the QTabBar and the doc-actions) is added by `MainWindow` at `outer.insertWidget(1, ...)` so the row is at the window level. Body (page area + empty state) stays inside `panel`. Data API surface (`panel.tabs.{count,addTab,removeTab,widget,currentWidget,setCurrentIndex,...}`) unchanged, so `open_flow.py`, `find.py`, `_ViewerKeyFilter`, every test reading `vp.tabs.X` still works.* | `shell/window.py`, `panel.py` | `test_app`, `test_empty_state` |
| 3.2 | DocBar styling: tabs right-rounded, dirty dot, hover close; `Bearbeiten` becomes icon-only (menu unchanged); search already icon→expand (`FindBar`). *Done in earlier session — QTabBar QSS, hovers, button styling.* | `style.py`, `panel.py` | `test_app` |
| 3.3 | Empty state: with 0 tabs the DocBar stays (just `+` and actions), `EmptyStateWidget` fills the main area. *Done in 3.1 — `_bar.setVisible(False)` initial mirrors the empty-state contract; proxy.isVisible reflects bar.isVisible.* | `panel.py`, `empty_state.py` | `test_empty_state` |
| 3.4 | Verify `_switch`/`_sync_view_switch`: Layout/tools no longer hide tabs; Ctrl+Tab cycling intact | `shell/window.py` | `test_manage`, `test_merge_preview` |

### Phase 4 — View restructure
| # | Step | Files | Tests |
|---|---|---|---|
| 4.1 | Unify the sidebar slot into one `SidebarHost` API (`mount(view, widget)`) — today three protocols converge there (tool list / ManagePanel / Layout controls). Pure refactor first | `shell/window.py`, `panel.py` | `test_manage`, `test_app` |
| 4.2 | Tools toggle: `ToolsToggle` button in the sidebar (manage/layout only). Tools list APPENDS into the sidebar's single scroll area — no nested scroll | new widget + `window.py` | `test_hygiene`, `test_manage` |
| 4.3 | Manage view: ManagePanel restyled to concept (selection count in section header, icon rows, listbtn style with right-aligned kbd shortcuts); its bottom status label → center statusbar | `manage.py`, `style.py` | `test_manage` |
| 4.4 | Layout view: replace `PreviewPane` with a scrollable sheet column (ALL sheets, lazily rendered like `PageGrid`) + rail delegation via a new `_SheetRail` adapter (clone of `_GridRail`, tab.py:24–106). `PreviewPane` itself stays untouched (shared) | `layout_view.py`, maybe `rail.py` | `test_tools_crop`, `test_tools_nup` |
| 4.5 | Layout sidebar: stage switch-cards, opts column as cards, run row (Stop hidden-until-running — already matches) | `layout_view.py`, `style.py` | `test_tools_crop` |

### Phase 5 — Tool morphing (sidebar settings panels)
Picking a tool turns the whole sidebar into that tool's settings panel
(fields + one primary Apply + "‹ Werkzeuge" back). Main area keeps showing
the document; progress/results go to the center status message (decision 2).
N-Up/Crop/Broschüre are already Layout stages — the remaining 9 tools need
no interactive preview.
| # | Step | Files | Tests |
|---|---|---|---|
| 5.1 | `BasePanel`: split every panel into `controls_widget` (sidebar-bound: fields + Apply + back + Stop-while-running) and optional `preview_widget` (stack-bound). `run_async` busy-relabel + Stop surface in the sidebar panel; LogBox removed | `panels/base.py` | `test_tools_misc` |
| 5.2–5.10 | Migrate one panel per step: grayscale → colour_profile → page_numbers → forms → ocr → preflight → pdfx → compress → plugin manager | `panels/*.py` | per-tool test file |
| 5.11 | Tools toggle in manage/layout hosts the same list; picking a tool there behaves identically (same tools, same behaviour, every view) | `window.py` | `test_manage` |

### Phase 6 — Print dialog
| # | Step | Files | Tests |
|---|---|---|---|
| 6.1 | Radiopill widget (checkable QToolButton set in exclusive QButtonGroup, keeps group IDs so `_scale_index`/`_sync_preview` logic is untouched) + concept styling for the dialog shell | new widget, `dialog.py` | `test_printing` |
| 6.2 | Top row: printer combo (wide) + `Bitmap` checkbox + dpi combo (300/600/1200/2400, default 600) to its right | `dialog.py` | `test_printing` |
| 6.3 | Remove Farbkonvertierung (decision 1); Kopien near the top; range pill prefilled from the manage selection (`_selected_pages_text` exists); "Feste Größe" pill exposes % spinbox | `dialog.py`, `prefs.py` | `test_printing` |
| 6.4 | Duplex: checkbox + two Wendung pills (lange/kurze Seite); footer: stacked status messages (queue state, font warnings, margin note) INSIDE the action bar, summary + buttons right | `dialog.py` | `test_printing` |

### Phase 7 — Merge flow + file manager (decision 3)
| # | Step | Files | Tests |
|---|---|---|---|
| 7.1 | "+" opens `QFileDialog` multi-select (`file_dialog_filter()` exists); 1 file → open directly, >1 → merge view | `panel.py`, `open_flow.py` | `test_merge_preview` |
| 7.2 | `FileCard`: big first-page thumbnail + filename + page count beneath (new `paint_card` variant); concept card styling | `merge.py` | `test_merge_preview` |
| 7.3 | Merge pane mounts into the main sidebar via `SidebarHost`; rail beside the FileGrid via a `_GridRail`-style adapter (FileGrid is PageGrid's twin); temp-dir cleanup follows the new exit paths | `merge.py`, `open_flow.py` | `test_merge_preview` |

### Phase 8 — Icon sweep + polish
| # | Step | Files | Tests |
|---|---|---|---|
| 8.1 | Replace every glyph button (─ □ ✕ ⊞ ⟳ ▲ ▼ ◀) with `icons.py` icons; tooltips carry shortcuts | across viewer/shell/panels/printing | affected per-module tests |
| 8.2 | All new strings through `tr()` with English entries in `i18n.py` | `i18n.py` + touched files | grep for bare strings |
| 8.3 | Final: full suite, before/after screenshots both themes, update AGENTS.md test matrix + this file's status | — | full suite |

## Top cross-cutting risks

1. **`test_hygiene`** fires on nearly every step (moved widget code unbinds
   names). Run it after every file touch — cheapest test in the suite.
2. **Two repaint mechanisms** (QSS re-apply for shell widgets vs `_TV` live
   dict + `_register_themed` for painted widgets): every new/restyled widget
   must sit in exactly one, or it goes stale on theme switch. Verify with the
   theme toggle after each phase. Light theme is the forgotten child —
   screenshot both.
3. **Geometry tests**: `test_viewer_zoom` (sliver, Ctrl+0/Ctrl+1, drag) is
   sensitive to info-bar removal and the docbar move. Never "fix" by changing
   `GAP_PX` or strip padding — fix the emission/mount layer instead.
4. **The sidebar slot** is where three protocols already collide
   (`window.py:366–383`, `panel.py:612–637`, `panel.py:1101–1110`);
   Phase 4.1 unifies them BEFORE adding the tools toggle and the merge pane.
   Do not skip it.
5. **Background jobs**: the colour counter and lazy sheet rendering go
   through `tools/jobs` + the single `_render_queue` — never the GUI thread,
   never a new thread (pypdfium2 is process-wide serialised; the greyscale
   module's comments document the heap-corruption history in detail —
   respect the `_pdfium_lock` discipline).
6. **Rail delegation**: the rail is built by `SinglePageView`, reparented
   into `PdfTab` via `take_nav_rail()`, steered by `rail_delegate`
   (single_page.py:153, :2248; tab.py:24–106). Every new view that wants the
   rail (layout sheets, merge grid) implements the same adapter surface
   (`_GridRail` is the template).
7. **MainWindow ↔ PageViewerPanel are coupled by injected lambdas**
   (window.py:348–362), not signals. Phase 3 grows the bridge by one entry
   (`mount_docbar`); redesigning the bridge is out of scope.

## Concept behaviour reference ("concept-conform" means)

- One neutral palette everywhere; blue only = selection/active/primary.
- Full-width doc bar above sidebar AND main area; tabs rectangular, rounded
  right side only, flush left; dirty dot; "+" opens file manager.
- Search: icon-only until pressed (or Ctrl+F), Esc collapses.
- Window-level status bar: readings (format · colorspace · colour/greyscale
  counter · preflight) left, center transient message (progress in-place,
  results persist until the next message or tool exit — decision 2), ruler
  toggle + zoomer right. The center message is clickable and opens the
  Protokoll window (all app logs, scrollable) — the same window that
  Hilfe ▸ "Logs anzeigen" (renamed from "Fehlerprotokoll anzeigen") opens.
  Page count is NOT here — it sits at the rail's foot.
- Right-side nav rail in EVERY view (preview, manage, layout sheets, merge):
  track, thumb, current page, total at the bottom.
- Tools list always open in the preview sidebar; behind a toggle in
  manage/layout that APPENDS the list to the sidebar's scroll (one scroll
  surface). Picking a tool morphs the whole sidebar into that tool's panel
  ("‹ Werkzeuge" back, Apply + Stop-only-while-running).
- Manage view: no toolbar row over the grid; selection count in the sidebar
  section header.
- Print dialog: printer combo + Bitmap + dpi at top; radiopills; duplex
  sub-pills; stacked statuses inside the action bar; no Farbkonvertierung.
- Merge: fourth view, entered via "+" → file dialog; first-page file icons
  with name + page count beneath; Zusammenführen / Einzeln öffnen.

## Status

- [x] Concept: `docs/gui-concept.html` (includes the tool-run progress demo)
- [x] Analysis: `docs/gui-analysis.md`
- [x] Phase 0 — done (AGENTS.md corrected; baseline 302 tests green + 6 screenshots in `/tmp/opencode/baseline/`; `tools/shell/icons.py` with all 41 sprite glyphs, no callers yet)
- [x] Phase 1 — done (palette remapped 1.1, QSS rewritten 1.2, paper tokens for the empty-state glyph 1.3 — every other self-painting widget follows `_TV`/`theme_color()` and was retinted by 1.1 automatically; `test_app`'s light-theme test now asserts against `shell_colours()` instead of literals; full suite 302 green; screenshots in `/tmp/opencode/after_phase1/`, before in `/tmp/opencode/before_phase1/`)
- [x] Phase 2 — done. 2.4: single window-level StatusBar mounted below the body; per-tab #infoBar deleted; show_status re-routed through the centre message; `_resync_statusbar` re-publishes readings + resets the default message on tab/view switch. 2.5: Protokoll window (session log store + notify + "Logs anzeigen" rename) with two entry points (centre-message click + Hilfe menu); LogBox replaced by a LogAdapter (async final results held); dead _show_log_folder removed. Full suite 307/307 green.
- [x] Phase 3.1 — done. Full-width doc bar above sidebar+stack; `QTabBar + QStackedWidget` mirrored through `_TabHost` proxy; data API (`panel.tabs.{count,addTab,removeTab,widget,currentWidget,setCurrentIndex,setTabText,tabCloseRequested,...}`) unchanged, so callers (`open_flow.py`, `find.py`, `_ViewerKeyFilter`) and tests need no edits. `MainWindow._build` mounts `panel.doc_row` via `outer.insertWidget(1, ...)`. Tests GREEN: `test_hygiene`, `test_app`, `test_empty_state`, `test_manage`, `test_merge_preview`, `test_rulers` (60/60). KNOWN REGRESSION RESOLVED: the zoom provisional-image test (doc row ~5 px chrome nudging fit-zoom into a cached bucket) — root-caused to two bugs: a `_prerender_all` stale pre-render contaminating the shared cache bucket (fixed by skipping the on-screen page) and a viewport-bucket boundary drift (fixed by adding `_FullPageCache.get_any()` fallback in `_show_cached_page`).
- [x] Phase 3.2 — done. DocBar styling polish (tabs right-rounded, dirty dot, hover close; `Bearbeiten` icon-only; search icon→expand). quick 85/85.
- [x] Phase 3.3 — done. Empty state with 0 tabs (DocBar stays, EmptyStateWidget fills main area; Save/Print/Edit/Find disabled).
- [x] Phase 3.4 — done. `_switch`/`_sync_view_switch` verified; tabs survive every view; Ctrl+Tab cycling intact. No fix needed.
- [x] Phase 4.1 — done. One `SidebarHost` API (`mount(view, widget)`), tokens tool_list/manage/layout/merge; panel gets single `mount_sidebar`; merge test drives the new surface.
- [x] Phase 4.2 — done. `ToolsToggle` (grid+Werkzeuge+rotating chevron, manage/layout only); tools list APPENDS into the sidebar's single scroll; toggle resets per view switch. `tools/shell/tools_toggle.py`.
- [x] Phase 4.3 — done. ManagePanel restyled: AUSWAHL header with right-aligned "n von m Seiten" count, btnpair Alle/Keine, icon-only ANSICHT pill groups (⊖⊕⊡ zoom / ↺↻ rotate), OPERATIONEN+TRENNEN as `listbtn` (icon+label+right-aligned kbd shortcut, `_ListBtn` subclass), inner QScrollArea removed (sidebar's single scroll owns it), bottom status QLabel removed → all messages via `AppState.status_message`. Also fixed pre-existing missing `_apply_theme()` in `__init__` (panel was unstyled on first show). test_hygiene 4/4, test_manage 18/18. Visual audit PASS (notes: sidebar vertical overflow, "iten verwalt" view-switch clipping, ASCII umlaut digraphs — all deferred).
- [x] Phase 3.2–3.4 — done (see 3.1 entry for all of Phase 3)
- [ ] Phases 4.4–8 — pending
