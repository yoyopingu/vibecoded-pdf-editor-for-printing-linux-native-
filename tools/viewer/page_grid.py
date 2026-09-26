"""
The thumbnails of "Seiten verwalten".

Visible page cards in a grid, with drag and drop to reorder them and a marker
showing where a dragged page will land. Both cards and thumbnails are loaded
near the viewport — a 500-page document needs only a few to show its first row.
"""
from PyQt6.QtWidgets import (QWidget, QFrame, QApplication, QScrollArea,
                             QSizePolicy)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QTimer
from PyQt6.QtGui import (QPixmap, QImage, QColor, QDrag, QPainter, QPen,
                         QTransform, QFont)
from tools.i18n import tr
from tools.render.caches import _ThumbnailCache
from tools.render.queue import _ThumbSignals, _ThumbTask, _render_queue, _thumb_render_width
from tools.theme import (STATUS, _DROP_THICKNESS, _TV, _paint_drop_marker,
                         _register_themed)


CARD_W = 187
CARD_H = 264
GAP    = 10
MARGIN = 12


# A card is one widget that paints itself, not a QFrame holding two QLabels
# with a stylesheet each.
#
# A prior pass reduced each page from three widgets to one. That helped, but
# making even one widget per page before switching still scales with document
# length. Only viewport cards are now materialized; empty slots are geometry,
# not widgets. The earlier measurements explain why the first pass existed:
#
#     200 pages     360 ms -> 120 ms
#     500 pages     680 ms -> 147 ms
#    1000 pages    1237 ms -> 215 ms
#
# It is also what the viewers that do this well do. Okular's thumbnail list
# (part/thumbnaillist.cpp) has no QWidget per page: its ThumbnailWidget is a
# lightweight item with its own geometry and paint, drawn into one scroll
# area. Qt's own answer to the same question is QListView in IconMode with a
# delegate and uniformItemSizes — the same trade, one widget and many painted
# items.
#
# Visible cards remain widgets for drag-and-drop and cursor behavior. Offscreen
# cards are discarded, so startup and scrolling cost follow viewport size.
CARD_MARGIN   = 4     # around the thumbnail
CARD_CAPTION  = 20    # the strip under it that holds the number
CARD_SPACING  = 2


def card_size(card_w, card_h):
    """The whole card, thumbnail plus its margins and caption."""
    return card_w + 2 * CARD_MARGIN + 8, card_h + 2 * CARD_MARGIN + CARD_CAPTION + 4


def paint_card(widget, pixmap, caption, card_w, card_h, selected,
               placeholder=None):
    """Draw one card: selection frame, thumbnail well, thumbnail, caption.

    Shared by both grids so the merge view and the page manager cannot drift
    apart — they are the same card showing different things. `placeholder` is
    drawn large in the well when there is no thumbnail, which is how the merge
    view shows a file type it cannot render a preview of.
    """
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    t = _TV
    rect = widget.rect().adjusted(1, 1, -1, -1)
    if selected:
        p.setPen(QPen(QColor(t['acc']), 2))
        p.setBrush(QColor(t['sel_bg']))
        p.drawRoundedRect(rect, 5, 5)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    # The well the thumbnail sits in, so a page that has not been rendered yet
    # still reads as a page rather than as a hole.
    x = CARD_MARGIN + 2
    y = CARD_MARGIN + 2
    p.setPen(QPen(QColor(t['border']), 1))
    p.setBrush(QColor(t['card_bg']))
    p.drawRect(x, y, card_w - 1, card_h - 1)

    if pixmap is not None and not pixmap.isNull():
        p.drawPixmap(x + (card_w - pixmap.width()) // 2,
                     y + (card_h - pixmap.height()) // 2, pixmap)
    elif placeholder:
        p.setPen(QColor(t['dim']))
        f = p.font()
        f.setPixelSize(max(18, card_w // 3))
        p.setFont(f)
        p.drawText(x, y, card_w, card_h,
                   int(Qt.AlignmentFlag.AlignCenter), placeholder)

    if caption:
        p.setPen(QColor(t['dim']))
        f = p.font()
        f.setPixelSize(max(9, min(13, card_w // 10)))
        p.setFont(f)
        p.drawText(x, y + card_h + CARD_SPACING, card_w, CARD_CAPTION,
                   int(Qt.AlignmentFlag.AlignCenter), caption)
    p.end()


class PageCard(QFrame):
    clicked = pyqtSignal(int)

    def __init__(self, display_pos, orig_idx, pixmap, rotation=0, parent=None,
                 card_w=CARD_W, card_h=CARD_H):
        super().__init__(parent)
        self.display_pos = display_pos
        self.orig_idx    = orig_idx
        self._card_w     = card_w
        self._card_h     = card_h
        self.setFixedSize(*card_size(card_w, card_h))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._selected            = False
        self._drag_pos            = None
        self._pending_ctrl_click  = False

        self._pixmap = None
        self._caption = str(display_pos + 1)
        if pixmap is not None:
            self.set_pixmap(pixmap, rotation)

    # ── the look ─────────────────────────────────────────────────────────────

    def pixmap(self):
        """The thumbnail as it is being shown, or None."""
        return self._pixmap

    def set_pixmap(self, pm, rotation=0):
        if pm is None or pm.isNull():
            return
        if rotation:
            pm = pm.transformed(QTransform().rotate(rotation))
        self._pixmap = pm.scaled(self._card_w, self._card_h,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        self.update()

    def set_image(self, image: QImage):
        """Called from the GUI thread with a freshly rendered QImage."""
        self.set_pixmap(QPixmap.fromImage(image))

    def set_selected(self, sel):
        sel = bool(sel)
        if sel != self._selected:
            self._selected = sel
            self.update()

    def paintEvent(self, _e):
        paint_card(self, self._pixmap, self._caption,
                   self._card_w, self._card_h, self._selected)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag_pos = e.position().toPoint()
        mods  = QApplication.keyboardModifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        # Ctrl+click on an already-selected card: don't deselect yet — wait to
        # see if the user drags (multi-drag) or just releases (then deselect).
        if ctrl and self._selected:
            self._pending_ctrl_click = True
        else:
            self._pending_ctrl_click = False
            self.clicked.emit(self.display_pos)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and getattr(self, '_pending_ctrl_click', False):
            # No drag happened — process the deferred Ctrl+click now
            self._pending_ctrl_click = False
            self.clicked.emit(self.display_pos)

    def mouseMoveEvent(self, e):
        if not (e.buttons() & Qt.MouseButton.LeftButton): return
        if self._drag_pos is None: return
        if (e.position().toPoint() - self._drag_pos).manhattanLength() < 12: return

        mods  = QApplication.keyboardModifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        # Deferred Ctrl+click: card stays selected for multi-drag, no deselect
        self._pending_ctrl_click = False

        # Find parent grid
        grid = self.parent()
        while grid and not isinstance(grid, PageGrid):
            grid = grid.parent()

        # If card is not selected yet, select it now as single
        if not self._selected:
            self.clicked.emit(self.display_pos)

        is_multi = (grid and self._selected and len(grid.model.selected) > 1)

        drag = QDrag(self)
        mime = QMimeData()
        # Ctrl+drag = copy; plain drag = move
        # Format: "copy_multi:<pos>", "copy:<pos>", "multi:<pos>", "<pos>"
        if ctrl:
            prefix = "copy_multi" if is_multi else "copy"
        else:
            prefix = "multi" if is_multi else ""
        mime.setText(f"{prefix}:{self.display_pos}" if prefix else str(self.display_pos))
        drag.setMimeData(mime)

        if is_multi and grid:
            n_sel = len(grid.model.selected)
            pm = QPixmap(self.size())
            pm.fill(QColor(_TV['border']))
            p = QPainter(pm); p.setPen(QColor(_TV['text']))
            f = QFont(); f.setPointSize(11); f.setBold(True); p.setFont(f)
            label = tr('+{p0} Seiten').format(p0=n_sel) if ctrl else tr('{p0} Seiten').format(p0=n_sel)
            p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, label)
            p.end()
            drag.setPixmap(pm)
        else:
            pm = QPixmap(self.size())
            self.render(pm)
            if ctrl:
                # Overlay a "+" to signal copy
                p = QPainter(pm); p.setPen(QColor(STATUS['copy']))
                f = QFont(); f.setPointSize(18); f.setBold(True); p.setFont(f)
                p.drawText(pm.rect().adjusted(0, 0, -4, -4),
                           Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight, "+")
                p.end()
            drag.setPixmap(pm)

        drag.setHotSpot(e.position().toPoint())
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        # QDrag.exec runs a nested event loop. Auto-scroll can evict cards
        # while it is active, but deleting the source widget mid-drag is unsafe.
        if grid:
            grid._dragging_card = self
        try:
            drag.exec(actions)
        finally:
            if grid:
                grid._dragging_card = None
                grid._schedule_visible()


class PageGrid(QWidget):
    order_changed     = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, model, pdf_path, parent=None):
        super().__init__(parent)
        self.model    = model
        self.pdf_path = pdf_path
        self._cards   = []
        self._live_cards = set()
        self._dragging_card = None
        self._drop_indicator = -1
        self._last_click_pos   = None  # für Shift+Click Bereichsauswahl
        self._card_w  = CARD_W   # zoombarer Thumbnail-Breite
        self._card_h  = CARD_H   # zoombarer Thumbnail-Höhe
        # Background thumbnail rendering
        self._thumb_gen     = 0
        self._thumb_tasks   = []        # active _ThumbTask objects
        self._thumb_signals = _ThumbSignals()
        self._thumb_signals.ready.connect(self._on_thumb_ready)
        # Debounce timer for resize-triggered rebuilds in single-page mode
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._rebuild)
        self._scroll_connected = False  # connect scrollbar only once
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rebuild()
        _register_themed(self)

    def _apply_theme(self):
        self._rebuild()

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
        self._card_w = CARD_W
        self._card_h = CARD_H
        self._rebuild()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if e.angleDelta().y() > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            e.accept()
        else:
            e.ignore()  # Scroll an ScrollArea weitergeben

    def _per_row(self):
        # Before layout, width() returns 0; using a default prevents zero-column layouts
        # but we also clamp to at least 1 column.
        w = self.width() or 800
        return max(1, (w - 2*MARGIN + GAP) // (self._card_w+16+GAP))

    def _rebuild(self):
        # Crash-Guard: verhindert doppelten Aufruf
        if getattr(self, '_rebuilding', False):
            return
        self._rebuilding = True
        try:
            # Cancel all pending thumbnail tasks
            for t in self._thumb_tasks:
                t.cancel()
            self._thumb_tasks.clear()
            _render_queue.cancel_queued(1)
            self._thumb_gen += 1
            gen = self._thumb_gen

            # Build a uid→pixmap map from the existing cards so we can reuse
            # them as placeholders instead of going blank during re-renders.
            old_cards = [self._cards[i] for i in self._live_cards]
            old_pm_by_uid = {}
            for c in old_cards:
                pm = c.pixmap()
                if pm and not pm.isNull():
                    old_pm_by_uid[c.orig_idx] = pm
            self._cards = [None] * len(self.model.order)
            self._live_cards.clear()

            per_row   = self._per_row()
            is_single = (per_row == 1)
            self._layout_single = is_single
            grid_w    = max(100, self.width() or 800)

            # Narrow single-column mode uses a full-width, uniform page well.
            # Reading every page's exact dimensions here used to freeze the
            # switch on large documents. The thumbnail keeps its aspect ratio
            # inside the well when it arrives; geometry needs no PDF reads.
            self._layout_w = max(60, grid_w - 2*MARGIN - 16) if is_single else self._card_w
            self._layout_h = (int(self._layout_w * CARD_H / CARD_W)
                              if is_single else self._card_h)
            self._render_width = _thumb_render_width(
                self._layout_w * 1.5 if is_single else max(self._card_w * 2, 200))

            positions = range(min(24, len(self._cards)))
            for pos in positions:
                uid = self.model.order[pos]
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                rot            = self.model.get_rotation(uid)
                c_w, c_h, render_w = self._layout_w, self._layout_h, self._render_width

                # Show cached image if available; otherwise use old placeholder.
                # Tasks are NOT submitted here — _schedule_visible() does that
                # lazily based on the scroll position (avoids flooding for big PDFs).
                cached = _ThumbnailCache.get((src_path, orig, rot, render_w))
                if cached is None:
                    cached = _ThumbnailCache.get_any(src_path, orig, rot)

                if cached is not None:
                    pm = QPixmap.fromImage(cached).scaled(
                        c_w, c_h,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                else:
                    pm = old_pm_by_uid.get(uid)
                    if pm is not None:
                        pm = pm.scaled(c_w, c_h,
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.FastTransformation)

                card = PageCard(pos, uid, pm, 0, self, c_w, c_h)
                card.set_selected(self.model.is_selected(pos))
                card.clicked.connect(self._on_click)
                self._cards[pos] = card
                self._live_cards.add(pos)

            # Destroy old cards after new ones are ready
            for c in old_cards:
                c.hide()
                c.deleteLater()
            self._relayout()
            # Kick off thumbnail loading for currently visible cards only
            QTimer.singleShot(0, self._connect_scroll)
            QTimer.singleShot(0, self._schedule_visible)
        finally:
            self._rebuilding = False

    def _on_thumb_ready(self, gen, cidx, image):
        """GUI thread — receive rendered thumbnail from background worker."""
        if gen != self._thumb_gen:
            return   # stale
        if cidx < 0 or cidx >= len(self._cards):
            return
        card = self._cards[cidx]
        if card is not None:
            card.set_image(image)

    def _materialize(self, pos):
        """Create one nearby card only when its viewport needs it."""
        if self._cards[pos] is not None:
            return self._cards[pos]
        uid = self.model.order[pos]
        src_path, orig = self.model.page_source(uid, self.pdf_path)
        rot = self.model.get_rotation(uid)
        render_w = self._render_width
        cached = (_ThumbnailCache.get((src_path, orig, rot, render_w))
                  or _ThumbnailCache.get_any(src_path, orig, rot))
        pm = QPixmap.fromImage(cached) if cached is not None else None
        card = PageCard(pos, uid, pm, 0, self, self._layout_w, self._layout_h)
        card.set_selected(self.model.is_selected(pos))
        card.clicked.connect(self._on_click)
        self._cards[pos] = card
        self._live_cards.add(pos)
        return card

    # ── Lazy thumbnail loading ────────────────────────────────────────────────

    def _get_scroll_area(self):
        """Return the QScrollArea this grid lives in, or None."""
        p = self.parent()           # viewport
        if p is None: return None
        p = p.parent()              # QScrollArea
        return p if isinstance(p, QScrollArea) else None

    def _connect_scroll(self):
        """Connect the parent scroll-bar to _schedule_visible (once only)."""
        if self._scroll_connected:
            return
        sa = self._get_scroll_area()
        if sa is None:
            return
        sa.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self._scroll_connected = True

    def _schedule_visible(self, _=None):
        """Submit thumb tasks only for cards visible in the scroll viewport.
        A one-viewport buffer above and below is included so scrolling feels
        instant.  Already-cached and already-scheduled cards are skipped."""
        if not self._cards or not self.isVisible():
            return
        sa = self._get_scroll_area()
        if sa is not None:
            scroll_y    = sa.verticalScrollBar().value()
            viewport_h  = sa.viewport().height() or 600
            y_min = max(0, scroll_y - viewport_h)          # 1 vp above
            y_max = scroll_y + 2 * viewport_h              # 2 vp below (scroll direction)
        else:
            # A detached grid (tests or a transient reparent) still must not
            # materialize an entire long document before it gets a viewport.
            y_min, y_max = 0, 2 * (self.height() or 600)

        gen      = self._thumb_gen
        per_row  = self._per_row()

        cell_h = self._layout_h + 28 + GAP
        row_min = max(0, int((y_min - MARGIN) // cell_h))
        row_max = int((y_max - MARGIN) // cell_h) + 1
        first = min(len(self._cards), row_min * per_row)
        last = min(len(self._cards), (row_max + 1) * per_row)
        # A bounded widget set keeps the switch and later scrolls fast even
        # for PDFs with thousands of pages. Cancel stale offscreen renders.
        for i in tuple(self._live_cards):
            if not first <= i < last and self._cards[i] is not self._dragging_card:
                card = self._cards[i]
                card.hide()
                card.deleteLater()
                self._cards[i] = None
                self._live_cards.remove(i)
        for task in self._thumb_tasks:
            if not first <= task._cidx < last:
                task.cancel()
        cell_w = self._layout_w + 16 + GAP
        for i in range(first, last):
            card = self._materialize(i)
            card.move(MARGIN + i % per_row * cell_w,
                      MARGIN + i // per_row * cell_h)
            card.show()
            self._maybe_schedule(i, gen)

    def _maybe_schedule(self, cidx, gen):
        """Submit a thumb task for card[cidx] if its thumbnail isn't cached yet."""
        if cidx >= len(self._cards):
            return
        card     = self._cards[cidx]
        if card is None:
            return
        render_w = self._render_width
        uid      = card.orig_idx
        src_path, orig = self.model.page_source(uid, self.pdf_path)
        rot      = self.model.get_rotation(uid)
        key      = (src_path, orig, rot, render_w)
        if _ThumbnailCache.get(key) is not None:
            return   # already cached
        # Prune finished tasks, then check for an in-flight task for this card
        self._thumb_tasks = [t for t in self._thumb_tasks if t._active]
        for t in self._thumb_tasks:
            if t._cidx == cidx:
                return  # already in flight
        task = _ThumbTask(gen, cidx, src_path, orig, rot,
                          render_w, self._thumb_signals)
        self._thumb_tasks.append(task)
        _render_queue.submit(task, 1)   # P1: visible thumbnails

    def _relayout(self):
        if not self._cards:
            self.setMinimumHeight(200); return
        pr = self._per_row()
        cell_w = self._layout_w + 16 + GAP
        cell_h = self._layout_h + 28 + GAP
        rows = (len(self._cards) + pr - 1) // pr
        for i in self._live_cards:
            card = self._cards[i]
            card.move(MARGIN + i % pr * cell_w, MARGIN + i // pr * cell_h)
            card.show()
        self.setMinimumHeight(MARGIN + rows * cell_h + MARGIN)
        self._schedule_visible()
        self.update()

    def resizeEvent(self, e):
        # In single-page mode the card width must track the widget width.
        # Debounce to avoid triggering _rebuild on every pixel of a drag-resize.
        if self._per_row() == 1 or (self._per_row() == 1) != self._layout_single:
            self._resize_timer.start(120)
        else:
            self._relayout()
            QTimer.singleShot(0, self._schedule_visible)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_indicator < 0 or not self._cards: return
        pr = self._per_row()
        p  = QPainter(self)
        if pr == 1:
            idx = min(self._drop_indicator, len(self._cards))
            y = MARGIN + idx * (self._layout_h + 28 + GAP) - GAP // 2
            _paint_drop_marker(p, MARGIN, y - _DROP_THICKNESS/2.0,
                               self._layout_w + 16, horizontal=True)
        else:
            cell_w = self._layout_w+16+GAP
            cell_h = self._layout_h+28+GAP
            pos    = min(self._drop_indicator, len(self._cards))
            col    = pos % pr; row = pos // pr
            x      = MARGIN + col*cell_w - GAP//2
            y      = MARGIN + row*cell_h
            _paint_drop_marker(p, x - _DROP_THICKNESS/2.0, y, self._layout_h)
        p.end()

    def _pos_from_point(self, pt):
        if not self._cards: return 0
        n  = len(self._cards)
        pr = self._per_row()
        if pr == 1:
            cell_h = self._layout_h + 28 + GAP
            return max(0, min(n, (pt.y() - MARGIN + cell_h // 2) // cell_h))
        cell_w = self._layout_w + 16 + GAP
        cell_h = self._layout_h + 28 + GAP
        rel_x  = pt.x() - MARGIN
        rel_y  = pt.y() - MARGIN
        col    = max(0, min(rel_x // cell_w, pr - 1))
        row    = max(0, rel_y // cell_h)
        pos    = row * pr + col
        if pos >= n:
            return n
        if rel_x - col * cell_w > cell_w // 2:
            pos += 1
        return min(pos, n)

    def mousePressEvent(self, e):
        # Klick auf leeren Hintergrund → Auswahl aufheben
        if e.button() == Qt.MouseButton.LeftButton:
            self.model.deselect_all()
            self._update_selection()
            self.selection_changed.emit()
        super().mousePressEvent(e)

    def _on_click(self, pos):
        mods  = QApplication.keyboardModifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if shift and self._last_click_pos is not None:
            # Bereichsauswahl: alle Seiten zwischen letztem Klick und jetzt
            lo = min(self._last_click_pos, pos)
            hi = max(self._last_click_pos, pos)
            for i in range(lo, hi + 1):
                uid = self.model.order[i]
                self.model.selected.add(uid)
        else:
            self.model.select(pos, multi=ctrl)
            self._last_click_pos = pos

        self._update_selection()
        self.selection_changed.emit()

    def _update_selection(self):
        for i in self._live_cards:
            self._cards[i].set_selected(self.model.is_selected(i))

    def handle_drop(self, from_pos, to_pos, multi=False, copy=False):
        self._drop_indicator = -1; self.update()
        if copy:
            # Ctrl+drag: duplicate pages at destination, leave originals in place
            if multi:
                sel_uids = [u for u in self.model.order if u in self.model.selected]
            else:
                if 0 <= from_pos < len(self.model.order):
                    sel_uids = [self.model.order[from_pos]]
                else:
                    sel_uids = []
            insert_at = min(to_pos, len(self.model.order))
            for i, uid in enumerate(sel_uids):
                new_uid = self.model._new_uid()
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                if src_path == self.pdf_path:
                    self.model.src[new_uid] = orig
                else:
                    self.model.src[new_uid] = orig
                    self.model.foreign_src[new_uid] = (src_path, orig)
                rot = self.model.rotations.get(uid, 0)
                if rot:
                    self.model.rotations[new_uid] = rot
                self.model.order.insert(insert_at + i, new_uid)
        elif multi:
            self.model.move_selection(to_pos)
        else:
            self.model.move(from_pos, to_pos)
        self._rebuild(); self.order_changed.emit()

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
        to_pos = self._drop_indicator
        if to_pos < 0:
            to_pos = self._pos_from_point(e.position().toPoint())
        self._drop_indicator = -1; self.update()
        text = e.mimeData().text()
        e.acceptProposedAction()

        if text.startswith("copy_multi:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, multi=True, copy=True)
            except (ValueError, IndexError): return
        elif text.startswith("copy:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, copy=True)
            except (ValueError, IndexError): return
        elif text.startswith("multi:"):
            try: self.handle_drop(int(text.split(":")[1]), to_pos, multi=True)
            except (ValueError, IndexError): return
        else:
            try: from_pos = int(text)
            except Exception: return
            self.handle_drop(from_pos, to_pos)

    # Public
    def rotate_selected(self, deg):
        self.model.rotate_selected(deg); self._rebuild(); self.order_changed.emit()

    def delete_selected(self):
        self.model.delete_selected(); self._rebuild()
        self.order_changed.emit(); self.selection_changed.emit()

    def select_all(self):
        self.model.select_all(); self._update_selection(); self.selection_changed.emit()

    def deselect_all(self):
        self.model.deselect_all(); self._update_selection(); self.selection_changed.emit()
