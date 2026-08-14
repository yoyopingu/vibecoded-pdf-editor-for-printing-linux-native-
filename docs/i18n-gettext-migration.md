# Migrating `tools/i18n.py` to gettext

Plan and estimate. No code has been written for this.

## What is there today

`tools/i18n.py` is 1,028 lines, almost all of it one dict:

```python
_EN: dict[str, str] = { "Datei": "File", ... }

def tr(text):
    if _CURRENT_LANG == "de" or not text:
        return text
    return _EN.get(text, text)
```

German source strings are the keys. `tr()` returns its argument unchanged
for German, and for English looks the key up and **falls back to the key**
when it is missing. That fallback is the problem: a missing entry is not an
error, it is a German word on an English screen, and nothing anywhere
notices.

Measured against the current tree:

| | |
|---|---|
| `_EN` entries | 774 |
| `tr()` calls | 670, across 33 files |
| distinct literal arguments | 567 |
| calls whose argument is **not** a literal | 14 |
| literals with no `_EN` entry — **German today, in English mode** | 2 |
| `_EN` entries no literal asks for | 209 |
| entries where English == German (deliberate no-ops) | 34 |
| entries with `{p0}`-style placeholders | 125 |
| entries using the German `(n)` plural workaround | 33 |
| keys containing umlauts or ß | 109 |
| keys containing a newline | 25 |
| longest key | 300 characters |

The two already broken are `'Farbprofil: …'` and `'Thumbnails'`. They are
not hypothetical — an English user sees German there right now, and the
only reason we know is that this audit compared the two sets.

One piece of luck: changing language calls `os.execv` and restarts the
process (`tools/shell/window.py:270`). Nothing has to be re-translated
live, which removes the hardest part of a gettext migration.

## Do the cheap thing first

The safety net does not need gettext. Extract every literal `tr()`
argument with `ast`, compare against `_EN`, fail if any is missing —
roughly twenty lines in `tests/test_hygiene.py`, which already does this
kind of static check. That catches both live bugs and every future one,
and it is worth doing **whether or not** the rest of this plan happens.

**Estimate: 2 hours.** It is the only item here with a safety argument
behind it. Everything below is about tooling, plurals, and being able to
add a third language — real benefits, but not urgent ones.

## Target design

### Keep German as the msgid

gettext convention is English msgids, but nothing in gettext requires it,
and switching would mean rewriting all 670 call sites and inventing English
source strings for text that was written in German. Keep the German.

- `de` gets a null translation — msgid and msgstr identical, or simply no
  catalogue and gettext's fallback returns the msgid, which is what `tr()`
  already does for German.
- `en` gets the 774 translations that exist today.
- A future third language is translated from German. Worth stating in the
  translator notes; not a blocker.

Umlauts and ß in msgids are fine: `.po` files are UTF-8 and gettext is
byte-oriented. The 300-character key and the 25 multi-line keys are also
fine — `xgettext` writes those as adjacent quoted strings.

### Layout

```
locales/
  copyshop.pot            extracted, regenerated, never hand-edited
  de/LC_MESSAGES/copyshop.po
  en/LC_MESSAGES/copyshop.po
  */LC_MESSAGES/copyshop.mo     compiled at build time, not committed
```

### Loading

`tools/i18n.py` keeps its public surface — `tr`, `get_language`,
`load_language`, `set_language` — so no call site changes. `load_language()`
becomes roughly: read the language from QSettings as now, then
`gettext.translation("copyshop", localedir, languages=[lang], fallback=True)`
and bind its `gettext` method to the module. `tr` becomes that bound method.

`fallback=True` is what ships, so a missing `.mo` degrades to German rather
than crashing. But the **test suite should load with `fallback=False`**, so
a catalogue that failed to compile fails the build instead of quietly
un-translating the app. That asymmetry is the point of the whole exercise.

`localedir` resolves next to the package for a repo run and inside the
wheel for an installed one; `importlib.resources` handles both.

## Converting what exists

Mechanical, and worth writing as a throwaway script rather than by hand:

1. Parse `_EN` with `ast` — it is a plain literal dict, so
   `ast.literal_eval` is enough.
2. Emit `en/LC_MESSAGES/copyshop.po`: each key becomes `msgid`, each value
   `msgstr`, with `#, python-brace-format` on the 125 entries containing
   `{`. Escape newlines; `xgettext`'s own conventions for long strings are
   easiest to match by writing the file and then normalising it with
   `msgcat`.
3. Emit `de/LC_MESSAGES/copyshop.po` with `msgstr ""` throughout — the null
   translation.
4. Run `xgettext` over `tools/` to produce `copyshop.pot`, then `msgmerge`
   the generated `en.po` against it. This is where the audit becomes
   automatic: `msgmerge` marks the 209 unused entries obsolete (`#~`) and
   flags the 2 missing ones as untranslated.
5. Review the 209 by hand before deleting. Some are reached only through
   the 14 dynamic call sites below and are **not** dead — deleting them
   blind would un-translate working UI.

The 34 entries where English equals German stay as real entries. They are
deliberate ("OCR", "PDF:", "PLUGINS"), and an empty msgstr would mean
"untranslated" to every tool that reads the file.

## The 14 dynamic call sites

`xgettext` sees literals. These pass variables:

- class attributes — `tr(self.TITLE)`, `tr(self.SUBTITLE)`,
  `tr(self.RUN_LABEL)` in `tools/_base.py`, inherited by every tool panel
- loop and parameter values — `tr(tip)`, `tr(label)`, `tr(title)`,
  `tr(header)`, `tr(caption)`, `tr(busy_label)`
- a dict lookup — `tr(self._LANG_NAMES.get(code, code))` in `panels/ocr.py`

The strings themselves are German literals, just declared somewhere else.
The standard treatment is a no-op marker: define `N_(s) = s`, wrap the
declarations (`TITLE = N_("Bilder → PDF")`, the tooltip tuples, the
`_LANG_NAMES` values), and tell `xgettext` with `--keyword=N_`. The runtime
behaviour does not change at all — `N_` returns its argument — but the
extractor can now see them.

This is the fiddliest part of the migration, because it means finding every
declaration that feeds those 14 calls. The 209 apparently-unused entries
are the map: most of them will turn out to be exactly these.

## Plurals

33 entries use the German `(n)` workaround — `"{p0} Seite(n) in einem neuen
Tab oeffnen."` — which is what you write when the framework has no plural
support. gettext has `ngettext`, and English needs the distinction more than
German does ("1 page" / "2 pages" rather than "2 page(s)").

Converting them is a real translation task, not a mechanical one: each site
needs the count threaded through as a separate argument, and each `.po`
entry needs `msgid_plural` and `nplurals`. It is also entirely optional —
they work as-is. I would do it as a follow-up, not part of the migration,
so that the migration itself stays behaviour-preserving and reviewable.

## Build and packaging

- `.po` files are committed; `.mo` files are build artefacts and belong in
  `.gitignore`.
- Compilation is `msgfmt` per language. Simplest hook is a small step in the
  build backend, or a `make`-style script that the release process runs;
  `pyproject.toml` needs the `locales/**/*.mo` glob in
  `[tool.setuptools.package-data]` so the wheel carries them.
- The `msgfmt`/`xgettext` binaries come from `gettext`, which is a new build
  dependency. It is present on essentially every Linux dev machine and in
  CI images, but it is a dependency the project does not have today and
  should be written into the README next to the others.
- A test asserting `.po` and `.pot` agree (`msgcmp`) keeps the catalogue
  honest without needing anyone to remember to re-extract.

## Effort

Assumes one person who has done a gettext migration before. Roughly 3–4
working days end to end, but the phases are independently shippable and the
first one carries almost all of the safety value.

| Phase | Work | Estimate |
|---|---|---|
| 0 | Static test: every literal `tr()` argument has an entry. **Do this regardless.** | 2 h |
| 1 | Conversion script, `_EN` → `en.po` + `de.po`, `xgettext` → `.pot`, `msgmerge` | 4 h |
| 2 | `tools/i18n.py` swaps the dict for `gettext.translation`; public names unchanged | 3 h |
| 3 | `N_` markers for the 14 dynamic sites and the declarations feeding them | 4–6 h |
| 4 | Review the 209 obsolete entries, delete what is genuinely dead | 2 h |
| 5 | `.mo` compilation, package-data, README, `msgcmp` test in the suite | 4 h |
| 6 | Delete `_EN`, update `tools/i18n.py`'s docstring, full pass over both languages in the running app | 3 h |
| — | **Subtotal** | **22–24 h** |
| 7 | *Optional follow-up:* `ngettext` for the 33 `(n)` strings | 6–8 h |

Phase 6's manual pass is not padding. 774 strings across 33 files is more
than a test can judge — someone has to open the app in both languages and
look at it, and that is where a mangled escape or a lost trailing space
shows up. The `'  Ausführen'` entries in the current dict, with their
leading spaces, are a warning about how easily that detail is lost.

## Risks

- **Silent un-translation during the swap.** Phase 2 replaces the lookup
  wholesale; if `localedir` resolution is wrong the app falls back to German
  everywhere and still starts fine. The Phase 0 test does not catch this —
  it checks the source, not the runtime. Add a startup assertion in the test
  suite that `tr("Datei") == "File"` under `en`.
- **The 209.** Deleting them without tracing the dynamic call sites first
  would un-translate working UI, and the tests would not notice.
- **Whitespace and escapes.** Leading and trailing spaces are load-bearing
  in this catalogue and survive a `.po` round-trip only if the conversion
  script is careful. Diff the round-trip — convert to `.po`, convert back,
  compare against `_EN` — before trusting it.

## Recommendation

Do Phase 0 this week; it is two hours and it closes the actual hole. Treat
Phases 1–6 as a scheduled piece of work with a reason behind it — wanting a
third language, or wanting plurals to read properly. gettext is the right
destination, but the thing that is broken today is the absence of a check,
and that does not require gettext to fix.
