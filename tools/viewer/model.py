"""
What the document looks like right now.

The order pages are in, which way each is turned, and which are selected —
everything the page manager edits and the viewer, the tools and the print path
read. It holds no widgets and no pdfium handles: reordering a document is a list
operation here, and only becomes a file when something asks it to be saved.
"""
from tools.i18n import tr


def _positions_to_str(positions):
    """[1,2,3,5,6,9] → '1-3, 5-6, 9'. Shared by the page manager and the merge
    view so their selection fields read identically."""
    if not positions: return ""
    ranges = []; start = end = positions[0]
    for p in positions[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = p
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ", ".join(ranges)


def _parse_positions(text, n):
    """'1, 3, 5-8' → {0, 2, 4, 5, 6, 7} clamped to n items. Empty set when the
    text holds nothing usable (the caller then leaves the selection alone)."""
    out = set()
    for part in (text or "").split(","):
        part = part.strip()
        if not part: continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                for i in range(int(a.strip())-1, int(b.strip())):
                    if 0 <= i < n: out.add(i)
            else:
                i = int(part) - 1
                if 0 <= i < n: out.add(i)
        except ValueError:
            pass
    return out


class PageModel:
    """
    Jede Position in 'order' ist eine eindeutige Instanz-ID (uid).
    'src' bildet uid → originaler PDF-Seitenindex.
    Kopien bekommen eigene UIDs und sind damit vollständig unabhängig.
    """
    def __init__(self, n):
        self._next_uid = n
        # order: Liste von UIDs (Anzeigereihenfolge)
        self.order       = list(range(n))          # uid 0..n-1
        # src: uid → originaler PDF-Seitenindex (int, always for default pdf_path)
        self.src         = {i: i for i in range(n)}
        self.rotations   = {}   # uid → Rotationsgrad
        self.selected    = set()  # Menge von UIDs
        # foreign_src: uid → (pdf_path, orig_idx) for pages from other tabs
        self.foreign_src = {}

    def page_source(self, uid, default_path):
        """Returns (pdf_path, orig_page_idx) for rendering uid."""
        if uid in self.foreign_src:
            return self.foreign_src[uid]
        return (default_path, self.src[uid])

    def _new_uid(self):
        uid = self._next_uid
        self._next_uid += 1
        return uid

    def orig(self, uid):
        """Gibt den originalen PDF-Seitenindex für eine UID zurück."""
        return self.src[uid]

    def move(self, from_pos, to_pos):
        n = len(self.order)
        if from_pos == to_pos: return
        if not (0 <= from_pos < n): return
        to_pos = max(0, min(to_pos, n))
        page = self.order.pop(from_pos)
        insert_at = to_pos - 1 if from_pos < to_pos else to_pos
        self.order.insert(max(0, min(insert_at, len(self.order))), page)

    def move_selection(self, to_pos):
        """Bewegt alle selektierten Seiten als Block an to_pos."""
        if not self.selected: return
        # Selektierte UIDs in aktueller Reihenfolge
        sel_uids  = [u for u in self.order if u in self.selected]
        rest_uids = [u for u in self.order if u not in self.selected]
        # Einfügeposition im rest_uids-Array berechnen
        # to_pos ist Position im alten order-Array
        # Wir zählen wieviele nicht-selektierte Seiten vor to_pos liegen
        insert_at = sum(1 for i, u in enumerate(self.order)
                        if i < to_pos and u not in self.selected)
        insert_at = max(0, min(insert_at, len(rest_uids)))
        new_order = rest_uids[:insert_at] + sel_uids + rest_uids[insert_at:]
        self.order = new_order

    def select(self, pos, multi=False):
        if not (0 <= pos < len(self.order)): return
        uid = self.order[pos]
        if multi:
            if uid in self.selected: self.selected.discard(uid)
            else: self.selected.add(uid)
        else:
            self.selected = {uid}

    def select_all(self):   self.selected = set(self.order)
    def deselect_all(self): self.selected.clear()

    def delete_selected(self):
        removed = self.selected.copy()
        self.order = [u for u in self.order if u not in removed]
        for u in removed:
            self.src.pop(u, None)
            self.rotations.pop(u, None)
            self.foreign_src.pop(u, None)
        self.selected.clear()

    def copy_selected(self):
        """Gibt Liste von (neue_uid, orig_src) zurück für alle selektierten UIDs."""
        copies = []
        for uid in [u for u in self.order if u in self.selected]:
            new_uid = self._new_uid()
            self.src[new_uid] = self.src[uid]
            if uid in self.foreign_src:
                self.foreign_src[new_uid] = self.foreign_src[uid]
            if uid in self.rotations:
                self.rotations[new_uid] = self.rotations[uid]
            copies.append(new_uid)
        return copies

    def rotate_selected(self, deg):
        targets = self.selected if self.selected else set(self.order)
        for uid in targets:
            self.rotations[uid] = (self.rotations.get(uid, 0) + deg) % 360

    def get_rotation(self, uid): return self.rotations.get(uid, 0)

    def is_selected(self, pos):
        if not (0 <= pos < len(self.order)): return False
        return self.order[pos] in self.selected

    def selected_info(self):
        positions = [i+1 for i, u in enumerate(self.order) if u in self.selected]
        if not positions: return tr("Keine Seiten ausgewählt")
        if len(positions) == 1: return tr('Seite {p0}').format(p0=positions[0])
        if len(positions) <= 6:
            return tr('{p0} Seiten: {p1}').format(p0=len(positions), p1=', '.join((str(p) for p in positions)))
        return tr('{p0} Seiten ausgewählt').format(p0=len(positions))
