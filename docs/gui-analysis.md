# GUI-Analyse — Folio (CopyShop PDF Suite)

Stand: dunkles Theme (Standard), 1440×900. Grundlage sind der Code in
`tools/theme.py`, `tools/shell/style.py`, die Viewer-Module und die drei
Ansichten (Vorschau / Seiten verwalten / Layout) sowie Offscreen-Screenshots,
die mit `QT_QPA_PLATFORM=offscreen` aufgenommen und per Pixelanalyse
ausgewertet wurden. Farbangaben in Hex sind die *gemessenen* Werte aus den
Screenshots, nicht die aus dem Quellcode — wo sie abweichen, ist das selbst
ein Befund.

---

## 1. Der Kernbefund: zwei Paletten, die voneinander nichts wissen

Die Anwendung malt mit **zwei getrennten Farbwelten**, die dasselbe visuelle
Vokabular (Sidebar, Panel, Karte, Eingabe) zweimal und *unterschiedlich*
definieren:

| Rolle          | Shell (`tools/theme.py` „shell“) | Viewer (`_TV`)          |
|----------------|----------------------------------|-------------------------|
| Sidebar        | `SIDE  #0e2d58`                  | `sidebar_bg  #0f3460`   |
| Panel          | `PANEL #1c2340`                  | `panel_bg    #16213e`   |
| Fenster/Hintergrund | `BG  #151520`               | `viewer_bg   #111827`   |
| Karte          | —                                | `card_bg     #1a2a40`   |

Beides sind „dunkles Blau auf noch dunklerem Blau“ — die Werte liegen
pro Kanal nur wenige Punkte auseinander, sind aber **nicht gleich**. Das
Ergebnis ist kein klarer Kontrast, sondern ein flackerndes „fast gleich“:
 dieselbe Bildschirmregion wechselt die Farbe, je nachdem welche Ansicht sie
gerade malt (gemessen):

| Region                                  | Vorschau      | Seiten verwalten (themed) | Seiten verwalten (1. Eintritt) | Layout        | Tool-Panel |
|-----------------------------------------|---------------|---------------------------|--------------------------------|---------------|------------|
| Linke 224-px-Spalte                     | `#0e2d58`     | `#0f3460`                 | **`#151520` (unstyled, Bug)**   | `#0f3460`     | `#0e2d58`  |
| Haupt-Canvas                            | `#111827`     | `#151520`                 | `#151520`                       | `#111827` + Options-Spalte `#16213e` | `#151520` |
| Info-/Statusleiste                      | `#0f3460`     | —                         | —                               | —             | —          |

Allein in der **Vorschau** stehen drei Blautöne nebeneinander: Sidebar
`#0e2d58`, Tab-Leisten-Hintergrund `#151520`, Tab-Fläche `#1c2340`,
Info-Leiste `#0f3460`. In der **Layout-Ansicht** kommt als vierte Fläche die
Options-Spalte `#16213e` dazu, zwischen Sidebar und Options-Spalte klafft eine
ca. 4 px breite Naht aus `#111827` (gemessen bei x≈226–230) — das liest sich
als Renderingfehler, nicht als gestalterische Entscheidung.

**Folge:** Bereiche, die funktional verschieden sind (Navigation vs. Canvas vs.
Werkzeug), sind farblich nicht unterscheidbar; Bereiche, die zusammengehören
(dieselbe Spalte in drei Ansichten), wechseln die Farbe. Das ist die Ursache
für den Eindruck „Panels sind nicht voneinander zu unterscheiden“.

---

## 2. Echte Bugs

### 2.1 ManagePanel ist beim ersten Eintritt ungestylt
`ManagePanel.__init__` (`tools/viewer/manage.py:41`) registriert sich nur bei
`_register_themed()` — **ruft `_apply_theme()` aber nie selbst auf**. Der Theme-
Schalter läuft aber *vor* dem Fensterbau (`tools/app.py:95`). Beim ersten
Eintritt in „Seiten verwalten“ einer Sitzung zeigt die Spalte therefore den
nackten Fensterhintergrund `#151520` mit ungeklärten Standard-Widgets
(gemessen: Spalte `#20222f` statt `#0f3460`; nach einem manuellen
Theme-Wechsel korrekt `#183054`). Jede andere selbst-malende Ansicht
(`SinglePageView`, `LayoutPanel`, `MergeOrderWidget`, `EmptyStateWidget`) ruft
`_apply_theme()` am Ende ihres Setups auf — nur das ManagePanel nicht.

### 2.2 Das Thumbnail-Raster liegt auf der Fensterfarbe
`PdfTab._build_manage_once` (`tools/viewer/tab.py:211`) gibt dem
`grid_scroll` kein Stylesheet. Das Raster liegt damit auf `#151520`
(Fenster-BG), während die Vorschau auf `#111827` (viewer_bg) liegt — zwei
„Canvas“-Dunkelheiten, je nach Ansicht.

### 2.3 Vorschau-Tints sind nicht im Theme
`layout_view.py:_render_sheet` malt Slot-Platzhalter und Blattrand mit
hartkodierten Farben (`(200,210,230,60)`, `(120,140,180,120)`,
`(120,160,255,180)`) — im hellen Theme sind das Dunkeltheme-Tints auf
weißem Papier.

---

## 3. Fehlende Grenzen — „wo hört was auf“

Gemessene Übergänge (dunkles Theme):

- **Sidebar → Canvas (Vorschau):** `#0e2d58` geht direkt in `#111827` über.
  Die 1-px-Linie `LINE #1e3354` ist gegen beide Nachbarn praktisch unsichtbar
  (Helligkeitsdifferenz < 3 %). Keine messbare Grenze.
- **Sidebar → Grid (Seiten verwalten):** `#0f3460`/`#151520` direkt
  nebeneinander, keine Trennung.
- **Tool-Panel:** Die Inhaltsfläche `#1a2038` (= INPUT_BG!) liegt als
  rahmenloser Block auf `#151520`; die „CurrentFileBar“ `#1c2340` ist vom
  Umfeld kaum zu unterscheiden (ΔL ≈ 4).
- **Layout-Ansicht:** die besagte 4-px-`#111827`-Naht zwischen Sidebar und
  Options-Spalte.

Der einzige Ort mit einer *sichtbaren* Grenze ist die Tab-Leiste
(`border-bottom #1e3354` auf `#151520`↔`#1c2340` — auch das nur knapp).

Karten im Seiten-Raster sind zudem die **einzigen scharfkantigen Rechtecke**
der App (`page_grid.paint_card` zeichnet `drawRect` ohne Radius), während
Buttons 4/5/6/7/8-px-Radii haben — das Thumbnail-Words ellbt aus allen
Rundungen heraus.

---

## 4. Inkonsistenzen zwischen Vorschau / Seiten verwalten / Layout

### 4.1 Die Dokumentleiste verschwindet
Tabs + „Öffnen/Suchen/Speichern/Drucken“ existieren nur im Viewer-Stack
(Index 0). In **Layout** und in **allen Tool-Panels** ist die Tab-Leiste weg:
man sieht nicht, welche Datei offen ist (nur die kleine CurrentFileBar in der
224-px-Spalte), kann nicht zwischen Dokumenten wechseln, nicht speichern.

### 4.2 Drei Zoom-UIs
- Vorschau: `− / fit / +` **unten rechts** in der Info-Leiste (`_PREV_BTN`
  34×26, Glyphen „−“, „fit“, „+“)
- Seiten verwalten: `− / + / 1:1` **oben in der linken Spalte** (30×24,
  daneben `↺ ↻` drehen)
- Layout/Vorschau-Panes der Tools: `− / 100 % / + / ⟳` **oben rechts über der
  Vorschau** (24×24, `iconBtn`)

Drei Positionen, drei Größen, drei Glyphen-Sets für dieselbe Operation.

### 4.3 Drei Status-Zielorte
- Vorschau: Statuszeile der Info-Leiste (unten, 12 px)
- Seiten verwalten: Mini-Label **10 px** am unteren Rand der 224-px-Spalte
  (`manage.py:148` — „font-size:10px“)
- Layout/Tools: monospace LogBox

Wo Meldungen auftauchen, hängt von der Ansicht ab — „Gespeichert: …“ landet
je nach Modus an dreierlei Orten.

### 4.4 Fünf Button-Sprachen
`actionBtn` (Akzent, 32 px) · `secondaryBtn` (28 px) · `iconBtn` (22 px) ·
`opRow` (25 px, linksbündig, *nur* im ManagePanel) · `docBtn`/`docIconBtn`
(24 px, transparent). Gleiche semantische Handlung, verschiedener Look je
nach Ort — „Kopieren“ ist im Raster ein opRow, im Tool ein secondaryBtn.

### 4.5 Typografie-Wildwuchs
Schriften in 9 / 10 / 10.5 / 11 / 12 / 13 / 15 px; Überschriften-Stile:
`sectionLabel` **9 px** + 3 px Letterspacing (fast unlesbar), `navGroup` 11 px
+ 1 px, `optGroup` 11 px + 1 px + Border unten, `stageHead` 13 px fett,
GroupBox-Titel 9 px + 2 px. Die 224-px-Spalte benutzt `navGroup`-Überschriften,
Tool-Panels `sectionLabel`, Layout `stageHead` — drei Grammatiken für
„Abschnitt“.

### 4.6 Rechte Kante
Vorschau: Nav-Rail (40 px, `#0f3460`, Track + Seitenzahl). Seiten verwalten:
dieselbe Rail (treibt das Grid). Layout: **keine Rail, kein Scroll-Indikator**.
Tool-Panels: normale QScrollArea-Scrollbar. Vier verschiedene Antworten auf
„wo bin ich im Dokument“.

### 4.7 Shortcuts in Labels
Nur das ManagePanel schreibt Tastenkürzel in Button-Texte
(„Löschen  (Entf)“, „Kopieren  (Strg+C)“); überall sonst sind Kürzel
Tooltips. Sieben Buttons tragen ihren Shortcut im Label, drei im Tooltip.

---

## 5. Was „alt“ aussieht (ästhetische Befunde)

1. **Blau auf Blau auf Blau.** `#0e2d58`/`#1e4d82`/`#0f3460` mit
   Akzent `#4d8df5` wirkt wie ein Admin-Template von 2013. Moderne Werkzeuge
   (Acrobat, Affinity, Figma, VS Code) halten das Chrome neutral und heben
   nur *Status* farblich hervor.
2. **Glyphen-Icons** statt gezeichneter Icons: „▲▼“ (Rail), „─ □ ✕“
   (Fenster), „⟳“, „⊞“, „◀“, „＋“, „▾“. Unterschiedliches Gewicht,
   unterschiedlicheoptische Größe, je nach Font der Maschine des Nutzers.
3. **Manuelle Leerzeichen-Padding in Tab-Titeln**
   (`panel.py:_tab_label` → `f"  {dot}{disp}  "`), „●“ als Dirty-Punkt.
4. **Selected-Thumbnail:** 2-px-Akzentrahmen **plus** `#1a4a80`-Füllung —
   das markierte Thumbnail wird fast zugeschüttet; moderne Auswahl ist ein
   Ring um die Karte.
5. **Die Options-Spalte der Layout-Ansicht** (300 px fest) neben der
   224-px-Spalte, Tools mit 340–480 px Sidebar — vier Steuerflächen-Breiten
   für vergleichbaren Inhalt.
6. **Drei Chrome-Balken übereinander** in der Vorschau: Titlebar 42 px +
   Tab-Leiste ~40 px + (in Tools) Panel-Header — bevor Inhalt kommt.
7. **`QComboBox` min-height 38 px** vs `QLineEdit` 28 px — Eingaben gleicher
   Rolle haben unterschiedliche Höhen in derselben Form.

---

## 6. Was die Vorschau richtig macht (und daher als Maßstab dient)

- **Eine** untere Info-Leiste, die Lesewerte links, Meldungen mittig und
  Zoom rechts trägt — genau richtig; sie existiert nur hier.
- Die Nav-Rail mit Track + Seitenzahl ist ein gutes, eigenständiges Element.
- Der Tab-Streifen mit Aktionen in der rechten Ecke ist kompakt und korrekt.
- Der Preflight-Punkt (grün/amber, klickbar) ist vorbildliche Status-Sprache.

Das Konzept (`gui-concept.html`) übernimmt genau diese vier Elemente als
durchgängiges Chrome für **alle** Ansichten und ersetzt die zweite
(„viewer“-)Palette durch eine einzige neutrale Oberflächen-Skala.

---

## 7. Empfehlungen (Kurzform)

1. **Eine Palette.** Shell- und Viewer-Vokabular zusammenführen: neutrales
   dunkles Chrome (`#0f1115` / `#161a21` / `#1d232c`), Grenzen sichtbar
   (`#262d38`), Papier bleibt weiß. Blau nur noch für Auswahl/Aktiv/Primär.
2. **ManagePanel-Bug fixen:** `_apply_theme()` am Ende von `__init__` aufrufen
   (manage.py), wie es jede andere registrierte Ansicht tut.
3. **Grid auf Canvas-Farbe** (`viewer_bg`) statt Fensterfarbe legen — und
   Karten mit 8-px-Radius + Grenze zeichnen.
4. **Dokumentleiste immer sichtbar** — auch in Layout und Tool-Panels.
5. **Eine Zoom-Kontrolle, ein Ort** (unten rechts in der Statusleiste),
   ein Glyphen-Set.
6. **Eine Statusleiste für alles** — Lesewerte + Meldungen + Zoom; das 10-px-
   Label und die LogBox (letztere behält das Protokoll, nicht die Meldung).
7. **Buttons auf zwei Sorten reduzieren:** primär (Akzent) und sekundär
   (Surface + Grenze); Icon-Buttons 32×32 einheitlich; Shortcuts in Tooltips.
8. **Typografie-Skala festlegen:** 11 (caps, dim) / 12 (Labels) / 13 (Body) /
   15 (Titel). `sectionLabel` mit 9 px abschaffen.
9. **Icon-Sprite** statt Glyphen — die App zeichnet ihre Icons bereits selbst
   (Suche, Tab-Cross, App-Icon); derselbe Ansatz für Rail-, Fenster- und
   Werkzeug-Icons.
10. **Layout-Vorschau-Tints** aus der Palette ableiten, nicht hartkodieren.
