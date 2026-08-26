"""
The file-level grid, shown when several files are opened at once.

The same idea as the page grid one level up: a card per file, dragged into the
order they should be merged in, with a preview of each. Picking several files
opens this instead of guessing which one was meant.
"""
import os, logging
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame, QApplication, QScrollArea, QSizePolicy, QSplitter, QLineEdit
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QTimer, QSize
from PyQt6.QtGui import QPixmap, QImage, QColor, QDrag, QPainter, QFont
from tools.app_state import theme_color
from tools.i18n import tr
from tools.shell.icons import icon, rotated
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _ThumbSignals, _ThumbTask, _render_queue, _thumb_render_width
from tools.viewer.model import _parse_positions, _positions_to_str
from tools.viewer.shortcuts import ThumbGridShortcutFilter
from tools.viewer.page_grid import (CARD_H, CARD_W, GAP, MARGIN,
                                    card_size, paint_file_card)
from tools.theme import _DROP_THICKNESS, _TV, _paint_drop_marker, _register_themed


def _fade(colour, toward, k=0.62):
    """Dim `colour` part-way toward `toward`, so a disabled glyph/button sits
    clearly below its enabled sibling in BOTH themes (the light theme's `dim`/
    `vdim` pair is inverted, so using them raw made the disabled state look
    identical to the enabled one)."""
    c = QColor(colour); t = QColor(toward)
    def m(a, b): return int(a + (b - a) * k)
    return f"#{m(c.red(), t.red()):02x}{m(c.green(), t.green()):02x}{m(c.blue(), t.blue()):02x}"


class FileCard(QFrame):
    """Thumbnail card for one file.

    The merge view is the page-manager view for files, so a file card shares
    the page card's chrome — size, selection ring, zoom, Ctrl-click handling
    and multi-drag pixmap — but is drawn by its own painter (page_grid.
    paint_file_card) that shows the first-page thumbnail with the filename and
    page count beneath it, rather than a page number. It used to be a
    near-copy of PageCard kept in step by hand and by one test."""
    clicked = pyqtSignal(int)

    FILE_ICONS = {
        ".pdf":"📄",".jpg":"🖼",".jpeg":"🖼",".png":"🖼",
        ".tif":"🖼",".tiff":"🖼",".bmp":"🖼",".webp":"🖼",
        ".docx":"📝",".doc":"📝",".xlsx":"📊",".xls":"📊",
        ".pptx":"📊",".ppt":"📊",".odt":"📝",".ods":"📊",
        ".odp":"📊",".rtf":"📝",".pages":"📝"
    }

    def __init__(self, pos, path, pixmap=None, parent=None,
                 card_w=CARD_W, card_h=CARD_H):
        super().__init__(parent)
        self.pos         = pos
        self.display_pos = pos       # same attribute name PageCard uses
        self.path        = path
        self._card_w     = card_w
        self._card_h     = card_h
        self.setFixedSize(*card_size(card_w, card_h))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected           = False
        self._drag_pos           = None
        self._pending_ctrl_click = False

        self._pixmap = None
        self._icon = None
        self._caption = self._label_text()
        self._meta = None
        self._meta_loaded = False
        self.setToolTip(path)
        if pixmap is not None:
            self.set_pixmap(pixmap)
        else:
            self._load_local_preview()

    def _label_text(self):
        """The filename, elided to the card width. The order the file sits in is
        still readable from the merge position; the page count goes under the
        name, so the old "<n>  <name>" head would crowd both off the card."""
        from PyQt6.QtGui import QFontMetrics
        f = QFont()
        f.setPixelSize(max(9, min(12, self._card_w // 14)))
        return QFontMetrics(f).elidedText(
            os.path.basename(self.path),
            Qt.TextElideMode.ElideMiddle, self._card_w)

    def _page_count(self):
        """The page count shown under the filename, read once and cached.

        Read from the shared pdfium document registry, which keeps the open
        PDFs warm, so a card costs a cached `len(doc)` rather than a reparse.
        Non-PDF files have no count to show until they are converted — and a
        PDF that cannot be opened for any reason reads as unknown, so we never
        block the grid on one bad file."""
        if self._meta_loaded:
            return self._meta
        self._meta_loaded = True
        if os.path.splitext(self.path)[1].lower() != ".pdf":
            self._meta = None
            return self._meta
        try:
            from tools.render.document_cache import page_document
            with page_document(self.path) as doc:
                self._meta = tr('{p0} Seiten').format(p0=len(doc))
        except Exception:
            self._meta = None
        return self._meta

    def pixmap(self):
        return self._pixmap

    def set_pixmap(self, pm):
        if pm is None or pm.isNull(): return
        self._pixmap = pm.scaled(
            self._card_w, self._card_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.update()

    def set_image(self, image: QImage):
        """Called from the GUI thread with a freshly rendered QImage (same entry
        point as PageCard, so the shared render queue can drive both)."""
        self.set_pixmap(QPixmap.fromImage(image))

    def paintEvent(self, _e):
        paint_file_card(self, self._pixmap, self._caption, self._page_count(),
                        self._card_w, self._card_h, self._selected,
                        placeholder=self._icon)

    def _load_local_preview(self):
        """Non-PDF files: images render from disk, everything else gets its icon.
        PDF thumbnails come from the shared render queue via FileGrid."""
        ext = os.path.splitext(self.path)[1].lower()
        if ext == ".pdf":
            return
        try:
            if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"):
                self.set_pixmap(QPixmap(self.path))
            else:
                self._set_preview_icon(ext)
        except Exception:
            self._set_preview_icon(ext)

    def _set_preview_icon(self, ext):
        self._icon = self.FILE_ICONS.get(ext, "📄")
        self.update()

    def set_selected(self, sel):
        sel = bool(sel)
        if sel != self._selected:
            self._selected = sel
            self.update()

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_pos = e.position().toPoint()
        ctrl = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier)
        # Ctrl+click on a selected card: defer, so a following drag keeps the
        # whole selection (exactly what PageCard does).
        if ctrl and self._selected:
            self._pending_ctrl_click = True
        else:
            self._pending_ctrl_click = False
            self.clicked.emit(self.pos)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pending_ctrl_click:
            self._pending_ctrl_click = False
            self.clicked.emit(self.pos)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.MouseButton.LeftButton): return
        if self._drag_pos is None: return
        if (e.position().toPoint()-self._drag_pos).manhattanLength() < 12: return
        self._pending_ctrl_click = False

        grid = self.parent()
        while grid and not isinstance(grid, FileGrid):
            grid = grid.parent()
        if not self._selected:
            self.clicked.emit(self.pos)
        is_multi = bool(grid and self._selected and len(grid._selected) > 1)

        drag = QDrag(self)
        mime = QMimeData()
        mime.setText(f"multi:{self.pos}" if is_multi else str(self.pos))
        drag.setMimeData(mime)
        if is_multi and grid:
            pm = QPixmap(self.size()); pm.fill(QColor(_TV['border']))
            p = QPainter(pm); p.setPen(QColor(_TV['text']))
            f = QFont(); f.setPointSize(11); f.setBold(True); p.setFont(f)
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter,
                       tr('{p0} Dateien').format(p0=len(grid._selected)))
            p.end()
        else:
            pm = QPixmap(self.size()); self.render(pm)
        drag.setPixmap(pm); drag.setHotSpot(e.position().toPoint())
        drag.exec(Qt.DropAction.MoveAction)


class FileGrid(QWidget):
    """Grid of FileCards — the file-level twin of PageGrid: same zoom, same
    Ctrl/Shift selection, same drag & drop, and PDF thumbnails come off the same
    shared render queue instead of a thread per card."""
    order_changed          = pyqtSignal()
    order_about_to_change  = pyqtSignal()  # fired before a drag reorders the list
    selection_changed      = pyqtSignal(int)   # pos of the card that was clicked

    def __init__(self, paths, parent=None):
        super().__init__(parent)
        self._paths          = list(paths)
        self._cards          = []
        self._selected       = set()
        self._last_selected  = -1
        self._last_click_pos = None       # for Shift+click ranges
        self._drop_indicator = -1
        self._card_w         = CARD_W
        self._card_h         = CARD_H
        self._thumb_gen      = 0
        self._thumb_tasks    = []
        self._thumb_signals  = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        self._scroll_connected = False
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rebuild()
        _register_themed(self)

    def _apply_theme(self):
        self._rebuild()

    # ── zoom (same steps and limits as PageGrid) ──────────────────────────────
    def zoom_in(self):
        step = 20 if self._card_w < 300 else 40 if self._card_w < 600 else 80
        self._card_w = min(1400, self._card_w + step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_out(self):
        step = 20 if self._card_w <= 300 else 40 if self._card_w <= 600 else 80
        self._card_w = max(60, self._card_w - step)
        self._card_h = int(self._card_w * (CARD_H / CARD_W))
        self._rebuild()

    def zoom_reset(self):
        self._card_w = CARD_W; self._card_h = CARD_H
        self._rebuild()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if e.angleDelta().y() > 0 else self.zoom_out()
            e.accept()
        else:
            e.ignore()

    def _render_w(self):
        return _thumb_render_width(max(self._card_w * 2, 200))

    def _rebuild(self):
        if getattr(self, "_rebuilding", False):
            return
        self._rebuilding = True
        try:
            for t in self._thumb_tasks: t.cancel()
            self._thumb_tasks.clear()
            self._thumb_gen += 1
            old_cards = self._cards[:]
            old_pm = {c.path: c.pixmap() for c in old_cards
                      if c.pixmap() and not c.pixmap().isNull()}
            self._cards = []
            render_w = self._render_w()
            for i, path in enumerate(self._paths):
                pm = None
                if os.path.splitext(path)[1].lower() == ".pdf":
                    cached = (_ThumbnailCache.get((path, 0, 0, render_w))
                              or _ThumbnailCache.get_any(path, 0, 0))
                    if cached is not None:
                        pm = QPixmap.fromImage(cached)
                    else:
                        pm = old_pm.get(path)
                card = FileCard(i, path, pm, self, self._card_w, self._card_h)
                card.clicked.connect(self._on_click)
                card.set_selected(i in self._selected)
                self._cards.append(card)
            for c in old_cards:
                c.hide(); c.deleteLater()
            self._relayout()
            QTimer.singleShot(0, self._connect_scroll)
            QTimer.singleShot(0, self._schedule_visible)
        finally:
            self._rebuilding = False

    # ── lazy PDF thumbnails on the shared queue ───────────────────────────────
    def _get_scroll_area(self):
        p = self.parent()
        if p is None: return None
        p = p.parent()
        return p if isinstance(p, QScrollArea) else None

    def _connect_scroll(self):
        if self._scroll_connected: return
        sa = self._get_scroll_area()
        if sa is None: return
        sa.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self._scroll_connected = True

    def _schedule_visible(self, _=None):
        if not self._cards: return
        sa = self._get_scroll_area()
        if sa is not None:
            scroll_y   = sa.verticalScrollBar().value()
            viewport_h = sa.viewport().height() or 600
            y_min = max(0, scroll_y - viewport_h); y_max = scroll_y + 2*viewport_h
        else:
            y_min, y_max = 0, 9_999_999
        per_row = self._per_row()
        cell_h  = self._card_h + 28 + GAP
        row_min = max(0, int((y_min - MARGIN) // cell_h))
        row_max = int((y_max - MARGIN) // cell_h) + 1
        gen = self._thumb_gen
        for i, card in enumerate(self._cards):
            if i // per_row < row_min or i // per_row > row_max: continue
            self._maybe_schedule(i, gen)

    def _maybe_schedule(self, cidx, gen):
        if cidx >= len(self._cards): return
        card = self._cards[cidx]
        if os.path.splitext(card.path)[1].lower() != ".pdf": return
        render_w = self._render_w()
        if _ThumbnailCache.get((card.path, 0, 0, render_w)) is not None:
            return
        self._thumb_tasks = [t for t in self._thumb_tasks if t._active]
        for t in self._thumb_tasks:
            if t._cidx == cidx: return
        task = _ThumbTask(gen, cidx, card.path, 0, 0, render_w, self._thumb_signals)
        self._thumb_tasks.append(task)
        _render_queue.submit(task, 1)

    def _on_thumb_ready(self, gen, cidx, image):
        if gen != self._thumb_gen: return
        if 0 <= cidx < len(self._cards):
            self._cards[cidx].set_image(image)

    def cancel_render_work(self):
        """Stop any background thumbnail renders still queued or in flight.

        Called when the merge view is torn down, so a render task cannot emit
        into a grid that no longer exists."""
        for t in self._thumb_tasks:
            t.cancel()
        self._thumb_tasks.clear()

    # ── layout ────────────────────────────────────────────────────────────────
    def _per_row(self):
        w = self.width() or 800
        return max(1, (w - 2*MARGIN + GAP) // (self._card_w+16+GAP))

    def _relayout(self):
        if not self._cards: self.setMinimumHeight(200); return
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        rows   = (len(self._cards)+pr-1)//pr
        for i, card in enumerate(self._cards):
            card.move(MARGIN+i%pr*cell_w, MARGIN+i//pr*cell_h)
            card.show()
        self.setMinimumHeight(MARGIN+rows*cell_h+MARGIN)
        self.update()

    def resizeEvent(self, e):
        self._relayout(); self._schedule_visible()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_indicator < 0 or not self._cards: return
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        pos    = min(self._drop_indicator, len(self._cards))
        p = QPainter(self)
        if pr == 1:
            y = MARGIN + pos*cell_h - GAP//2
            _paint_drop_marker(p, MARGIN, y - _DROP_THICKNESS/2.0,
                               self._cards[0].width(), horizontal=True)
        else:
            col = pos%pr; row = pos//pr
            x   = MARGIN+col*cell_w-GAP//2
            y   = MARGIN+row*cell_h
            _paint_drop_marker(p, x - _DROP_THICKNESS/2.0, y, self._card_h)
        p.end()

    def _pos_from_point(self, pt):
        # Same rule as PageGrid._pos_from_point, including the past-the-end
        # guard. Clamping the cell index to n-1 first and only then applying the
        # half-cell test made the marker flip between "before" and "after" the
        # last card as the cursor swept across the empty space beyond it.
        if not self._cards: return 0
        n      = len(self._cards)
        pr     = self._per_row()
        cell_w = self._card_w+16+GAP; cell_h = self._card_h+28+GAP
        rel_x  = pt.x()-MARGIN; rel_y = pt.y()-MARGIN
        if pr == 1:
            for i in range(n):
                top    = MARGIN + i*cell_h
                bottom = top + self._card_h
                if pt.y() < (top + bottom) // 2:
                    return i
            return n
        col    = max(0, min(rel_x//cell_w, pr-1))
        row    = max(0, rel_y//cell_h)
        pos    = row*pr + col
        if pos >= n:
            return n
        if rel_x - col*cell_w > cell_w//2:
            pos += 1
        return min(pos, n)

    # ── selection (Ctrl toggles, Shift selects a range — as in PageGrid) ──────
    def mousePressEvent(self, e):
        # Click on empty background clears the selection, as in PageGrid. The
        # file grid simply had no handler, so a picked thumbnail could not be
        # unpicked by clicking beside it.
        if e.button() == Qt.MouseButton.LeftButton:
            self.deselect_all()
        super().mousePressEvent(e)

    def _on_click(self, pos):
        mods  = QApplication.keyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if shift and self._last_click_pos is not None:
            lo, hi = sorted((self._last_click_pos, pos))
            self._selected |= set(range(lo, hi+1))
        elif ctrl:
            self._selected ^= {pos}
            self._last_click_pos = pos
        else:
            self._selected = {pos}
            self._last_click_pos = pos
        self._last_selected = pos
        self._update_selection()
        self.selection_changed.emit(pos)

    def _update_selection(self):
        for i, c in enumerate(self._cards):
            c.set_selected(i in self._selected)

    def select_all(self):
        self._selected = set(range(len(self._paths)))
        self._update_selection(); self.selection_changed.emit(self._last_selected)

    def deselect_all(self):
        self._selected.clear(); self._last_selected = -1; self._last_click_pos = None
        self._update_selection(); self.selection_changed.emit(-1)

    def current_path(self):
        if 0 <= self._last_selected < len(self._paths):
            return self._paths[self._last_selected]
        return None

    # ── drag & drop ───────────────────────────────────────────────────────────
    def handle_drop(self, from_pos, to_pos, multi=False):
        self._drop_indicator = -1; self.update()
        # Let the owner snapshot before the list changes, so a drag is undoable
        # like every other reorder.
        self.order_about_to_change.emit()
        if multi:
            picked = [self._paths[i] for i in sorted(self._selected)
                      if 0 <= i < len(self._paths)]
            if not picked: return
            before = sum(1 for i in self._selected if i < to_pos)
            rest   = [p for i, p in enumerate(self._paths) if i not in self._selected]
            ins    = max(0, min(to_pos - before, len(rest)))
            self._paths = rest[:ins] + picked + rest[ins:]
            self._selected = set(range(ins, ins+len(picked)))
            self._last_selected = ins
        else:
            if from_pos == to_pos: return
            p   = self._paths.pop(from_pos)
            ins = to_pos-1 if from_pos < to_pos else to_pos
            ins = max(0, min(ins, len(self._paths)))
            self._paths.insert(ins, p)
            self._selected = {ins}; self._last_selected = ins
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def dragEnterEvent(self, e):
        if e.mimeData().hasText(): e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasText(): return
        e.acceptProposedAction()
        self._drop_indicator = self._pos_from_point(e.position().toPoint())
        self.update()

    def dragLeaveEvent(self, e):
        self._drop_indicator = -1; self.update()

    def dropEvent(self, e):
        if not e.mimeData().hasText(): return
        to = self._drop_indicator
        if to < 0: to = self._pos_from_point(e.position().toPoint())
        self._drop_indicator = -1; self.update()
        text = e.mimeData().text()
        e.acceptProposedAction()
        try:
            if text.startswith("multi:"):
                self.handle_drop(int(text.split(":")[1]), to, multi=True)
            else:
                self.handle_drop(int(text), to)
        except (ValueError, IndexError):
            pass

    # ── operations ────────────────────────────────────────────────────────────
    def remove_selected(self):
        for i in sorted(self._selected, reverse=True):
            if 0<=i<len(self._paths): self._paths.pop(i)
        self._selected.clear(); self._last_selected = -1; self._last_click_pos = None
        self._rebuild(); self.order_changed.emit(); self.selection_changed.emit(-1)

    def move_up(self):
        sel = sorted(self._selected)
        if not sel or sel[0]==0: return
        for i in sel:
            self._paths[i-1], self._paths[i] = self._paths[i], self._paths[i-1]
        self._selected = {i-1 for i in sel}
        self._last_selected = min(self._selected)
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def move_down(self):
        sel = sorted(self._selected, reverse=True)
        if not sel or sel[0]>=len(self._paths)-1: return
        for i in sel:
            self._paths[i], self._paths[i+1] = self._paths[i+1], self._paths[i]
        self._selected = {i+1 for i in sel}
        self._last_selected = max(self._selected)
        self._last_click_pos = self._last_selected
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def get_paths(self): return list(self._paths)

    def insert_paths(self, at, paths):
        """Insert files at a position and leave them selected — the file-level
        twin of pasting pages into the page manager."""
        paths = [p for p in paths if p]
        if not paths: return
        at = max(0, min(at, len(self._paths)))
        self._paths[at:at] = list(paths)
        self._selected = set(range(at, at + len(paths)))
        self._last_selected  = at
        self._last_click_pos = at
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def set_state(self, paths, selected):
        """Restore a previous list and selection wholesale (undo / redo)."""
        self._paths    = list(paths)
        self._selected = {i for i in selected if 0 <= i < len(self._paths)}
        self._last_selected  = min(self._selected) if self._selected else -1
        self._last_click_pos = self._last_selected if self._selected else None
        self._rebuild(); self.order_changed.emit()
        self.selection_changed.emit(self._last_selected)

    def get_selected_info(self):
        if not self._selected: return tr("Keine Auswahl")
        sel = sorted(self._selected)
        if len(sel)==1: return tr('Datei {p0}').format(p0=sel[0] + 1)
        return tr('{p0} Dateien ausgewählt').format(p0=len(sel))


class _FileGridRail:
    """Drives the shared navigation rail while the merge grid is showing.

    The clone of `_GridRail` (tools/viewer/tab.py): the thumb maps onto the
    file grid's scroll range, the arrows step it, and every scroll pushes the
    position back to the rail. The merge view is "Seiten verwalten" for files,
    so the file grid is the page grid's twin and maps the rail onto *cards*
    exactly the way the page manager does — `single` is the active tab's
    SinglePageView, set by the host when the rail is attached.
    """
    def __init__(self, widget):
        self.widget = widget   # the MergeOrderWidget that owns the grid
        self.single = None     # the active tab's SinglePageView, set at mount

    def _bar(self):
        return self.widget._scroll.verticalScrollBar()

    def _cards(self):
        return self.widget._grid._cards

    def rail_prev(self):
        self._nudge(-1)

    def rail_next(self):
        self._nudge(1)

    def _nudge(self, direction):
        bar = self._bar()
        step = max(60, int(self.widget._scroll.viewport().height() * 0.85))
        bar.setValue(bar.value() + direction * step)

    def rail_wheel(self, dy_px):
        bar = self._bar()
        bar.setValue(bar.value() + int(dy_px))

    def rail_go_to(self, page):
        cards = self._cards()
        if not cards:
            return
        i = max(0, min(int(page) - 1, len(cards) - 1))
        self._bar().setValue(max(0, int(cards[i].y()) - 8))

    def rail_drag_to(self, frac):
        bar = self._bar()
        bar.setValue(round(float(frac) * bar.maximum()))

    def rail_prompt_goto(self):
        n = len(self._cards())
        if n <= 0:
            return
        from PyQt6.QtWidgets import QInputDialog
        page, ok = QInputDialog.getInt(
            self.widget, tr("Gehe zu Datei"),
            tr('Datei (1 – {p0}):').format(p0=n),
            self.page(), 1, n)
        if ok:
            self.rail_go_to(page)

    def page(self):
        cards = self._cards()
        n = len(cards)
        if n <= 1:
            return max(1, n)
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        return max(1, min(n, int(round(frac * (n - 1))) + 1))

    def sync(self):
        """Scrollbar position → rail thumb and file number."""
        if self.single is None:
            return
        bar = self._bar()
        vmax = bar.maximum()
        frac = (bar.value() / vmax) if vmax > 0 else 0.0
        self.single.nav_set_document(len(self._cards()), self.page())
        self.single.nav_set_fraction(frac)


class MergeOrderWidget(QWidget):
    merge_confirmed = pyqtSignal(list)
    open_separately = pyqtSignal(list)
    cancelled       = pyqtSignal()

    # Shared between merge previews, like the page manager's clipboard
    _shared_clipboard: list = []

    def __init__(self, file_paths, parent=None):
        super().__init__(parent)
        self._busy        = False
        self.source_paths = list(file_paths)   # what the batch was opened with
        self.tmp_dir      = None               # set by PageViewerPanel
        self._history     = []
        self._redo_stack  = []
        self._key_filter  = None
        self._setup(file_paths)
        # The rail adapter that drives the shared nav rail over the file grid.
        self._file_rail = _FileGridRail(self)
        self.destroyed.connect(self._cleanup_filter)

    # ── keyboard ─────────────────────────────────────────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        self._ensure_filter()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._cleanup_filter()

    def _ensure_filter(self):
        """Install the page-manager shortcut filter. Called from showEvent and
        again by the panel when the merge view is entered without `self` being
        visible (its controls + grid are mounted out into the sidebar / main
        area, so the merge object itself never gets a showEvent)."""
        if self._key_filter is None:
            self._key_filter = ThumbGridShortcutFilter(
                lambda: (self.isVisible()
                         or getattr(self, "controls_widget", self).isVisible())
                and not self._busy, self._grid,
                self._remove, self._copy, self._cut, self._paste,
                self._undo, self._redo)
            QApplication.instance().installEventFilter(self._key_filter)

    def _cleanup_filter(self):
        if getattr(self, "_key_filter", None) is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self._key_filter)
            self._key_filter = None

    # ── clipboard / history, mirroring the page manager ──────────────────────
    def _save_history(self):
        self._history.append((self._grid.get_paths(), set(self._grid._selected)))
        del self._history[:-40]
        self._redo_stack.clear()

    def _copy(self):
        picked = [self._grid.get_paths()[i] for i in sorted(self._grid._selected)
                  if 0 <= i < len(self._grid.get_paths())]
        if not picked:
            self.status.setText(tr("Zuerst Dateien auswählen.")); return
        MergeOrderWidget._shared_clipboard = picked
        n = len(picked)
        self.status.setText(tr("{p0} Datei kopiert.").format(p0=n) if n == 1
                            else tr("{p0} Dateien kopiert.").format(p0=n))

    def _cut(self):
        if not self._grid._selected:
            self.status.setText(tr("Zuerst Dateien auswählen.")); return
        self._copy()
        self._save_history()
        self._grid.remove_selected()
        self._on_order_changed()

    def _paste(self):
        clip = MergeOrderWidget._shared_clipboard
        if not clip:
            self.status.setText(tr("Zwischenablage ist leer.")); return
        at = (max(self._grid._selected) + 1) if self._grid._selected \
             else len(self._grid.get_paths())
        self._save_history()
        self._grid.insert_paths(at, clip)
        self._on_order_changed()
        n = len(clip)
        self.status.setText(tr("{p0} Datei eingefügt.").format(p0=n) if n == 1
                            else tr("{p0} Dateien eingefügt.").format(p0=n))

    def _undo(self):
        if not self._history:
            self.status.setText(tr("Nichts rückgängig zu machen.")); return
        self._redo_stack.append((self._grid.get_paths(), set(self._grid._selected)))
        paths, sel = self._history.pop()
        self._grid.set_state(paths, sel)
        self._on_order_changed()
        self.status.setText(tr("Rückgängig."))

    def _redo(self):
        if not self._redo_stack:
            self.status.setText(tr("Nichts zu wiederholen.")); return
        self._history.append((self._grid.get_paths(), set(self._grid._selected)))
        paths, sel = self._redo_stack.pop()
        self._grid.set_state(paths, sel)
        self._on_order_changed()
        self.status.setText(tr("Wiederhergestellt."))

    def _sep(self):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color:{_TV['border']};margin:3px 0;")
        return f

    def _section(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        layout.addWidget(lbl)

    def _btn(self, text, fn, _icon=None):
        b = QPushButton(text)
        b.setObjectName("secondaryBtn")
        if _icon is not None:
            b.setIcon(_icon)
            b.setIconSize(QSize(16, 16))
        b.clicked.connect(fn)
        b.setMinimumHeight(28)
        return b

    def _chevron(self, up, colour):
        """The reorder arrow glyph in a colour — the up variant is the down
        chevron turned round."""
        ic = icon("chev", colour=colour)
        return rotated(ic, 180) if up else ic

    def _setup(self, file_paths):
        # The layout below mirrors ManagePanel exactly (fixed title bar, scrollable
        # sidebar with the same margins/sections/helpers, grid on the right) — this
        # view is "Seiten verwalten" for files, so it should not look like a
        # different program.
        #
        # Phase 7.3 (decision 3): the merge view is the fourth view of the main
        # chrome, not a doc-bar tab. The left controls become a sidebar tenant —
        # `controls_widget`, mounted into the app's 224px SidebarHost slot the way
        # Layout's staging sections are — and the FileGrid becomes the main-area
        # content — `preview_widget`, with a narrow rail host beside it for the
        # shared navigation rail. `self` keeps owning both (and the whole
        # selection/history/reorder logic) so tests and callers can keep talking
        # to one object, and a standalone `MergeOrderWidget(paths).show()` still
        # lays both out side by side through a splitter.
        root = QVBoxLayout(self)
        root.setContentsMargins(0,0,0,0); root.setSpacing(0)

        # ── Steuerung: wird der Sidebar-Slot (controls_widget) ────────────────
        self.controls_widget = QWidget()
        self.controls_widget.setObjectName("mergeControlsW")
        cw = QVBoxLayout(self.controls_widget)
        cw.setContentsMargins(0,0,0,0); cw.setSpacing(0)

        self._left_scroll = QScrollArea(); self._left_scroll.setWidgetResizable(True)
        self._left_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._left_content = QWidget(); self._left_content.setObjectName("mergeLeftContent")
        ll = QVBoxLayout(self._left_content); ll.setContentsMargins(10, 8, 22, 10); ll.setSpacing(5)
        self._left_scroll.setWidget(self._left_content)
        cw.addWidget(self._left_scroll, 1)

        sel_lbl = QLabel(tr("Auswahl  (z.B. 1, 3, 5-8)"))
        sel_lbl.setObjectName("sectionLabel")
        ll.addWidget(sel_lbl)
        self.sel_edit = QLineEdit()
        self.sel_edit.setPlaceholderText(tr("z.B. 1, 3, 5-8, 12  →  Enter"))
        self.sel_edit.returnPressed.connect(self._apply_sel_edit)
        ll.addWidget(self.sel_edit)
        self._info = QLabel(tr("Keine Auswahl"))
        self._info.setWordWrap(True)
        self._info.setObjectName("dimLabel")
        ll.addWidget(self._info); ll.addWidget(self._sep())

        # Zoom only. The reset button used to be labelled "↺", which is the page
        # manager's rotate-left icon — so it read as "turn this thumbnail", an
        # action that means nothing for a whole file. Same fix as was already
        # made one view over: call it what it is.
        self._section(ll, tr("ANSICHT"))
        zoom_row = QHBoxLayout(); zoom_row.setSpacing(4)
        self._zoom_btns = []
        for text, tip, fn in [
                ("−",   "Thumbnails verkleinern",  lambda: self._grid.zoom_out()),
                ("+",   "Thumbnails vergrößern", lambda: self._grid.zoom_in()),
                ("1:1", "Zoom zurücksetzen",      lambda: self._grid.zoom_reset())]:
            b = QPushButton(text); b.setFixedSize(32, 26)
            b.setToolTip(tr(tip))
            b.clicked.connect(fn)
            zoom_row.addWidget(b); self._zoom_btns.append(b)
        self._zoom_hint_lbl = QLabel(tr("Thumbnails"))
        zoom_row.addWidget(self._zoom_hint_lbl); zoom_row.addStretch()
        ll.addLayout(zoom_row)
        ll.addWidget(self._sep())

        self._section(ll, tr("AUSWAHL"))
        ll.addWidget(self._btn(tr("Alle auswählen  (Strg+A)"),  lambda: self._grid.select_all()))
        ll.addWidget(self._btn(tr("Auswahl aufheben  (Strg+D)"), lambda: self._grid.deselect_all()))
        ll.addWidget(self._sep())

        self._section(ll, tr("REIHENFOLGE"))
        self._btn_up   = self._btn(tr("Hoch"),   self._move_up,
                                   _icon=rotated(icon("chev", colour=theme_color("DIM")), 180))
        self._btn_down = self._btn(tr("Runter"), self._move_down,
                                   _icon=icon("chev", colour=theme_color("DIM")))
        self._btn_up.setObjectName("btnUp")
        self._btn_down.setObjectName("btnDown")
        ll.addWidget(self._btn_up); ll.addWidget(self._btn_down)
        ll.addWidget(self._sep())

        self._section(ll, tr("OPERATIONEN"))
        ll.addWidget(self._btn(tr("Entfernen  (Entf)"),       self._remove))
        ll.addWidget(self._btn(tr("Kopieren  (Strg+C)"),      self._copy))
        ll.addWidget(self._btn(tr("Ausschneiden  (Strg+X)"),  self._cut))
        ll.addWidget(self._btn(tr("Einfügen  (Strg+V)"),     self._paste))
        ll.addWidget(self._btn(tr("Rückgängig  (Strg+Z)"),  self._undo))
        ll.addWidget(self._sep())

        self._section(ll, tr("DATEI-INFO"))
        self._inf_name = QLabel("—"); self._inf_name.setWordWrap(True)
        self._inf_name.setObjectName("currentFileLabel")
        ll.addWidget(self._inf_name)
        self._inf_type = QLabel(""); self._inf_pages = QLabel(""); self._inf_size = QLabel("")
        for w in [self._inf_type, self._inf_pages, self._inf_size]:
            w.setObjectName("dimLabel"); ll.addWidget(w)
        ll.addStretch()

        # ── The two ways out, pinned below the scroll area ───────────────
        # These are what the view exists for, so they must never be scrolled
        # out of reach: at the bottom of the scrolling column they sat below
        # the fold on a standard window and "Zusammenführen" was invisible
        # until the sidebar was scrolled.
        self._actions_w = QWidget(); self._actions_w.setObjectName("mergeActionsW")
        al = QVBoxLayout(self._actions_w)
        # No 22px right inset here: that one exists in the scroll area above to
        # clear its scrollbar, and copying it made "Zusammenführen (n)" wider
        # than its button. The extra bottom inset keeps the drag-hint status
        # line clear of the scroll edge.
        al.setContentsMargins(10, 8, 10, 20); al.setSpacing(5)

        self._section(al, tr("ÖFFNEN"))
        self._total = QLabel("")
        self._total.setWordWrap(True)
        self._total.setObjectName("dimLabel")
        al.addWidget(self._total)
        self._btn_go = QPushButton(tr("  Zusammenführen"))
        self._btn_go.setObjectName("actionBtn")
        self._btn_go.setMinimumHeight(28)
        self._btn_go.clicked.connect(self._confirm)
        al.addWidget(self._btn_go)
        self._btn_single = self._btn(tr("  Einzeln öffnen"), self._do_open_separately)
        al.addWidget(self._btn_single)
        self._btn_cancel = self._btn(tr("✗  Abbrechen"), self._do_cancel)
        al.addWidget(self._btn_cancel)

        self.status = QLabel(tr("Drag & Drop sortiert · Strg/Shift mehrfach"))
        self.status.setWordWrap(True)
        # One line on purpose: the full hint ("Drag & Drop zum Umsortieren ·
        # Strg/Shift zum Mehrfachauswählen") wrapped to two lines, and as the
        # last row of a scrolling sidebar the second line's lower half sat under
        # the viewport edge (and the BETA chip). A single line that fits the
        # ~210px column cannot be cut the same way.
        self.status.setStyleSheet("font-size:9px;min-height:20px;background:transparent;")
        al.addWidget(self.status)
        cw.addWidget(self._actions_w)

        # ── → der Hauptbereich: FileGrid + Rail (preview_widget) ─────────
        self.preview_widget = QWidget()
        self.preview_widget.setObjectName("mergePreviewW")
        pl = QHBoxLayout(self.preview_widget); pl.setContentsMargins(0,0,0,0); pl.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid = FileGrid(file_paths)
        self._grid.order_changed.connect(self._on_order_changed)
        self._grid.order_about_to_change.connect(self._save_history)
        self._grid.selection_changed.connect(self._on_select)
        self._scroll.setWidget(self._grid)
        pl.addWidget(self._scroll, 1)

        # The narrow host the shared nav rail is parked in while the merge view
        # shows (decision 3 / concept: rail in every view). Hidden until the
        # host reparents a tab's rail into it — see PageViewerPanel._enter_merge.
        self.rail_host = QWidget()
        rh = QVBoxLayout(self.rail_host)
        rh.setContentsMargins(0, 0, 0, 0); rh.setSpacing(0)
        self.rail_host.setFixedWidth(40)
        self.rail_host.setVisible(False)
        pl.addWidget(self.rail_host)

        # ── Beide in einem Splitter, damit ein Standalone-Widget alles zeigt ──
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setStyleSheet(
            f"QSplitter::handle{{background:{_TV['splitter']};width:2px;}}")
        self._splitter.addWidget(self.controls_widget)
        self._splitter.addWidget(self.preview_widget)
        self._splitter.setSizes([236, 500])
        self._splitter.setStretchFactor(0,0); self._splitter.setStretchFactor(1,1)
        root.addWidget(self._splitter, 1)

        # Keys go through the same app-level filter class the page manager uses
        # (ThumbGridShortcutFilter, installed in showEvent / on enter), so this
        # view answers to the same set. It used to register three lone
        # QShortcuts, which is why
        # Ctrl+C / Ctrl+X / Ctrl+V / Ctrl+Z did nothing here while working one
        # view over.
        self._on_order_changed()

        # Enter merges, the way Enter runs a tool in every panel.
        from PyQt6.QtGui import QKeySequence, QShortcut
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            QShortcut(QKeySequence(key), self).activated.connect(
                self._enter_pressed)

        _register_themed(self)
        self._apply_theme()

    def _enter_pressed(self):
        """Enter merges — except in the selection box, which asked for it first.

        That field's own placeholder reads "z.B. 1, 3, 5-8, 12  →  Enter", so
        Enter there is a documented instruction to apply what was typed. A
        window-wide shortcut is seen before the focused widget's own
        returnPressed, so merging on Enter unconditionally would have quietly
        broken the one thing the field tells the user to do.
        """
        if self.sel_edit.hasFocus():
            self._apply_sel_edit()
            return
        self._confirm()

    def _apply_theme(self):
        t = _TV
        self.controls_widget.setStyleSheet(
            f"QWidget#mergeControlsW{{background:{t['sidebar_bg']};}}")
        self._left_scroll.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._left_content.setStyleSheet(
            f"QWidget#mergeLeftContent{{background:{t['sidebar_bg']};}}")
        self._actions_w.setStyleSheet(
            f"QWidget#mergeActionsW{{background:{t['sidebar_bg']};"
            f"border-top:1px solid {t['border']};}}")
        self.preview_widget.setStyleSheet(
            f"QWidget#mergePreviewW{{background:{t['viewer_bg']};}}")
        self._scroll.setStyleSheet(
            f"QScrollArea{{background:{t['viewer_bg']};border:none;}}")
        _zs = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:4px;font-size:13px;}}"
               f"QPushButton:hover{{background:{t['hover']};}}")
        for b in getattr(self, "_zoom_btns", []):
            b.setStyleSheet(_zs)
        if hasattr(self, "_zoom_hint_lbl"):
            self._zoom_hint_lbl.setStyleSheet(
                f"color:{t['vdim']};font-size:9px;background:transparent;")
        # Hoch/Runter must LOOK disabled: a flat ground, dimmed text and a
        # quiet border that match the sidebar, and an icon dimmed to match. The
        # shared #secondaryBtn QSS has no :disabled rule, so without this a
        # disabled reorder button rendered identically to an enabled one.
        if hasattr(self, "_btn_up"):
            # Muted grey plate for the disabled button: the fill is pulled
            # part-way toward the theme's dim tone so it is a visibly different
            # shade from the enabled fill in BOTH themes, and the glyph is
            # dimmed toward the sidebar so it sits clearly below enabled.
            dis_bg = _fade(t['btn_bg'], t['dim'], k=0.5)
            dis_fg = _fade(t['text'], t['sidebar_bg'], k=0.55)
            for b in (self._btn_up, self._btn_down):
                oid = b.objectName()
                b.setStyleSheet(
                    f"QPushButton#{oid}{{background:{t['btn_bg']};color:{t['text']};"
                    f"border:1px solid {t['btn_brd']};border-radius:7px;"
                    f"padding:6px 14px;}}"
                    f"QPushButton#{oid}:hover{{background:{t['hover']};}}"
                    f"QPushButton#{oid}:disabled{{background:{dis_bg};"
                    f"color:{dis_fg};border:1px solid {t['border']};border-radius:7px;}}")
                b.setIcon(self._chevron(b is self._btn_up,
                                        t['text'] if b.isEnabled() else dis_fg))
        if hasattr(self, "status"):
            self.status.setStyleSheet(
                f"color:{t['dim']};font-size:9px;min-height:20px;background:transparent;")

    FILE_KINDS = {
        ".pdf":"PDF",".jpg":"JPEG",".jpeg":"JPEG",".png":"PNG",
        ".tif":"TIFF",".tiff":"TIFF",".bmp":"BMP",".webp":"WebP",
        ".docx":"Word",".doc":"Word",".xlsx":"Excel",".xls":"Excel",
        ".pptx":"PowerPoint",".ppt":"PowerPoint",
        ".odt":"Writer",".ods":"Calc",".odp":"Impress",
        ".rtf":"RTF",".pages":"Pages"
    }

    def _on_order_changed(self):
        n = len(self._grid.get_paths())
        n_conv = sum(1 for p in self._grid.get_paths()
                     if os.path.splitext(p)[1].lower() != ".pdf")
        txt = (tr("{p0} Datei").format(p0=n) if n == 1
               else tr("{p0} Dateien").format(p0=n))
        if n_conv: txt += tr("  —  {p0} zu konvertieren").format(p0=n_conv)
        self._total.setText(txt)
        self._btn_go.setText(tr('  Zusammenführen  ({p0})').format(p0=n))
        self._btn_single.setText(tr('  Einzeln öffnen  ({p0})').format(p0=n))
        self._update_move_buttons()

    def _update_move_buttons(self):
        """Hoch/Runter only make sense when something is selected and it is not
        already pinned against that edge of the list."""
        sel = self._grid._selected
        n = len(self._grid.get_paths())
        if not sel or n < 2:
            self._btn_up.setEnabled(False); self._btn_down.setEnabled(False)
            return
        self._btn_up.setEnabled(min(sel) > 0)
        self._btn_down.setEnabled(max(sel) < n - 1)
        dis_fg = _fade(_TV['text'], _TV['sidebar_bg'], k=0.55)
        for b, up in ((self._btn_up, True), (self._btn_down, False)):
            b.setIcon(self._chevron(up, _TV['text'] if b.isEnabled() else dis_fg))

    def _apply_sel_edit(self):
        positions = _parse_positions(self.sel_edit.text(), len(self._grid.get_paths()))
        if positions:
            self._grid._selected = set(positions)
            self._grid._last_selected = min(positions)
            self._grid._last_click_pos = self._grid._last_selected
            self._grid._update_selection()
            self._on_select(self._grid._last_selected)
        else:
            self.update_info()

    def update_info(self):
        """Keep the selection field showing the current selection in compact
        form — the page manager does the same after every selection change."""
        self.sel_edit.blockSignals(True)
        self.sel_edit.setText(_positions_to_str(sorted(i+1 for i in self._grid._selected)))
        self.sel_edit.blockSignals(False)

    def _on_select(self, pos):
        self.update_info()
        self._update_move_buttons()
        path = self._grid.current_path()
        if not path:
            self._inf_name.setText("—"); self._inf_type.setText("")
            self._inf_pages.setText(""); self._inf_size.setText("")
            self._info.setText(tr("Keine Auswahl")); return
        # A multi-selection advertises how many files are picked; a single pick
        # says where in the list the file sits. get_selected_info() already
        # branched on this, but the position line below used to clobber it.
        if len(self._grid._selected) == 1:
            self._info.setText(tr('Datei {p0} von {p1}').format(
                p0=pos + 1, p1=len(self._grid.get_paths())))
        else:
            self._info.setText(self._grid.get_selected_info())
        ext = os.path.splitext(path)[1].lower()
        self._inf_name.setText(os.path.basename(path))
        self._inf_type.setText(f"Typ: {self.FILE_KINDS.get(ext, ext.upper().lstrip('.'))}")
        try: self._inf_size.setText(tr('Größe: {p0:.0f} KB').format(p0=os.path.getsize(path) / 1024))
        except Exception: self._inf_size.setText("")
        if ext == ".pdf":
            try:
                from pypdf import PdfReader
                self._inf_pages.setText(tr('Seiten: {p0}').format(p0=len(PdfReader(path, strict=False).pages)))
            except Exception: self._inf_pages.setText(tr("Seiten: ?"))
        else:
            self._inf_pages.setText(tr("Seiten: nach Konvertierung"))

    def _move_up(self):
        if self._grid._selected: self._save_history()
        self._grid.move_up()
        self._on_order_changed()

    def _move_down(self):
        if self._grid._selected: self._save_history()
        self._grid.move_down()
        self._on_order_changed()

    def _remove(self):
        if not self._grid._selected:
            self.status.setText(tr("Zuerst Dateien auswählen.")); return
        self._save_history()
        self._grid.remove_selected()
        self._on_order_changed()

    def set_busy(self, busy):
        """Latch the view while a conversion runs. Every button that starts or
        aborts work goes dead, so a double click — or a click on the second
        button while the first one's work is in flight — cannot start a second
        run behind the first one."""
        self._busy = bool(busy)
        for b in (self._btn_go, self._btn_single, self._btn_cancel):
            b.setEnabled(not self._busy)

    def _confirm(self):
        if self._busy:
            return
        paths = self._grid.get_paths()
        logging.debug(f"MergeOrderWidget._confirm: {paths}")
        if not paths:
            return
        self.set_busy(True)
        self.merge_confirmed.emit(paths)

    def _do_open_separately(self):
        if self._busy:
            return
        paths = self._grid.get_paths()
        if not paths:
            return
        self.set_busy(True)
        self.open_separately.emit(paths)

    def _do_cancel(self):
        if self._busy:
            return
        self.set_busy(True)      # one cancel only — the tab is about to go
        self.cancelled.emit()
