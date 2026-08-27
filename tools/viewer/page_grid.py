"""
The thumbnails of "Seiten verwalten".

A card per page, laid out in a grid, with the drag and drop that reorders them
and the marker showing where a dragged page will land. Thumbnails are rendered
lazily for what is on screen — a 500-page document would otherwise queue 500
renders to show twelve of them.
"""
from PyQt6.QtWidgets import (QWidget, QFrame, QApplication, QScrollArea,
                             QSizePolicy)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QTimer
from PyQt6.QtGui import (QPixmap, QImage, QColor, QDrag, QPainter, QPen,
                         QTransform, QFont, QFontMetrics)
from tools.i18n import tr
from tools.render.caches import _FullPageCache, _ThumbnailCache
from tools.render.queue import _ThumbSignals, _ThumbTask, _render_queue, _thumb_render_width
from tools.render.region import cached_page_size_pt as _cached_page_size_pt
from tools.theme import (STATUS, _DROP_THICKNESS, _TV, _paint_drop_marker,
                         _register_themed)


CARD_W = 187
CARD_H = 264
GAP    = 10
MARGIN = 12


# A card is one widget that paints itself, not a QFrame holding two QLabels
# with a stylesheet each.
#
# The page manager builds a card per page, so on a 1000-page document the old
# shape meant 3000 widgets and 3000 stylesheet parses before the grid could be
# shown — which is what made the first switch into it take over a second.
# Measured on that switch, first entry into the grid:
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
# One *widget* per card is kept, rather than going all the way to a delegate:
# the drag-and-drop reordering, the per-card cursor and the drag pixmap are all
# widget behaviour, and a delegate would mean rewriting those as well. The
# saving is in what a card contains, not in how many there are.
CARD_MARGIN   = 4     # around the thumbnail
CARD_CAPTION  = 20    # the strip under it that holds the number
CARD_SPACING  = 2


def card_size(card_w, card_h):
    """The whole card, thumbnail plus its margins and caption."""
    return card_w + 2 * CARD_MARGIN + 8, card_h + 2 * CARD_MARGIN + CARD_CAPTION + 4


def _mix(a, b, k=0.6):
    """Blend token colour `a` toward token colour `b` — a derived tone from
    the live _TV palette, never a hardcoded hex, so it follows theme switches."""
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * k),
                  int(ca.green() + (cb.green() - ca.green()) * k),
                  int(ca.blue() + (cb.blue() - ca.blue()) * k))


def paint_card(widget, pixmap, caption, card_w, card_h, selected, hover=False,
               placeholder=None):
    """Draw one card: selection frame, thumbnail well, thumbnail, caption.

    Shared by both grids so the merge view and the page manager cannot drift
    apart — they are the same card showing different things. `placeholder` is
    drawn large in the well when there is no thumbnail, which is how the merge
    view shows a file type it cannot render a preview of. `hover` paints the
    pointer-over state (a wash of the theme's hover tone plus an outer border
    tinted toward line_strong); it changes paint only, never geometry.
    """
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    t = _TV
    rect = widget.rect().adjusted(1, 1, -1, -1)
    if selected:
        p.setPen(QPen(QColor(t['acc']), 2))
        p.setBrush(QColor(t['sel_bg']))
        p.drawRoundedRect(rect, 5, 5)
    elif hover:
        # Subtle hover feedback: a wash of the theme's hover tone across the
        # card and an outer border pulled toward line_strong, so the card
        # reads as grabbable without competing with the selection ring.
        p.fillRect(widget.rect(), QColor(t['hover']))
        p.setPen(QPen(_mix(t['border'], t['line_strong']), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 5, 5)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    # The well the thumbnail sits in, so a page that has not been rendered yet
    # still reads as a page rather than as a hole. The stronger line separates
    # each page from the canvas background (concept .card .well
    # border:1px solid var(--line-strong)).
    x = CARD_MARGIN + 2
    y = CARD_MARGIN + 2
    p.setPen(QPen(QColor(t['line_strong']), 1))
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

    # Selection tint over the thumbnail well: the sel_bg fill above only shows
    # on the margins/caption, so the page itself would read unselected. A
    # translucent accent wash over the well makes the pick unmistakable at a
    # glance while the 2px frame above keeps the card's outline sharp. A
    # rotated/flipped page still carries the tint because it rides the well,
    # not the thumbnails orientation.
    if selected:
        tint = QColor(t['acc'])
        tint.setAlpha(40)
        p.fillRect(x, y, card_w - 1, card_h - 1, tint)

    if caption:
        # The selected card's caption sits over the sel_bg fill + accent tint,
        # so it is drawn bold in the `text` token to hold its contrast; the
        # unselected caption keeps its regular weight.
        p.setPen(QColor(t['text']))
        f = p.font()
        f.setPixelSize(11)
        f.setBold(selected)
        p.setFont(f)
        p.drawText(x, y + card_h + CARD_SPACING, card_w, CARD_CAPTION,
                   int(Qt.AlignmentFlag.AlignCenter), caption)
    p.end()


def paint_file_card(widget, pixmap, name, meta, card_w, card_h, selected,
                    badge=None, placeholder=None):
    """Draw one file card: the same frame, well and selection ring as
    paint_card, but the caption is the filename (bold) over the page count
    (faint) rather than a page number, and the well holds the file's first page.
    A sibling of paint_card — the merge view is "Seiten verwalten" for files, so
    it shares the page grid's chrome while reading as a file list, not a page
    list. `meta` is the page-count line, drawn only when it is known. `badge`
    is the 1-based merge-order number drawn as the concept's .ord badge in the
    card's top-left corner (merge file cards only — page-manager cards pass
    nothing and stay unbudgeted for it)."""
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    t = _TV
    rect = widget.rect().adjusted(1, 1, -1, -1)
    if selected:
        p.setPen(QPen(QColor(t['acc']), 2))
        p.setBrush(QColor(t['sel_bg']))
        p.drawRoundedRect(rect, 5, 5)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    x = CARD_MARGIN + 2
    y = CARD_MARGIN + 2
    p.setPen(QPen(QColor(t['line_strong']), 1))
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

    # Selection tint over the file well — same treatment as paint_card so the
    # merge view's selection reads the same way as the page manager's.
    if selected:
        tint = QColor(t['acc'])
        tint.setAlpha(40)
        p.fillRect(x, y, card_w - 1, card_h - 1, tint)

    # Filename over page count — the file-list twin of the page-number caption.
    # The block starts CARD_SPACING higher than before and the count line is a
    # 10px row ending at card_h+29: drawn flush to the widget bottom its ink
    # reached the last rows, where the selected card's 2px accent border cuts
    # across it (the page card's single centred caption clears the border by
    # ~16px and needs no such care).
    cy = y + card_h
    p.setPen(QColor(t['text']))
    f = p.font(); f.setBold(True)
    f.setPixelSize(max(9, min(12, card_w // 14)))
    p.setFont(f)
    name = QFontMetrics(f).elidedText(name, Qt.TextElideMode.ElideMiddle, card_w)
    p.drawText(x, cy, card_w, 13,
               int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), name)
    if meta:
        p.setPen(QColor(t['dim']))
        f.setBold(False)
        f.setPixelSize(max(8, min(10, card_w // 16)))
        p.setFont(f)
        meta = QFontMetrics(f).elidedText(meta, Qt.TextElideMode.ElideMiddle, card_w)
        p.drawText(x, cy + 13, card_w, 10,
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), meta)

    # The concept's .ord ordering badge (docs/gui-concept.html :481-484): a
    # 19px rounded square on the card's top-left with the 1-based merge
    # position — surface_3 on line_strong with a dim bold number, inverted to
    # the accent with white text when the card is picked. The white is fixed
    # like PAPER/INK: the contrast tone for the accent, which both themes
    # share, not part of either palette.
    if badge is not None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if selected:
            p.setPen(QPen(QColor(t['acc']), 1))
            p.setBrush(QColor(t['acc']))
            num = QColor(Qt.GlobalColor.white)
        else:
            p.setPen(QPen(QColor(t['line_strong']), 1))
            p.setBrush(QColor(t['surface_3']))
            num = QColor(t['dim'])
        p.drawRoundedRect(6, 6, 19, 19, 6, 6)
        f = p.font(); f.setBold(True); f.setPixelSize(10)
        p.setFont(f); p.setPen(num)
        p.drawText(6, 6, 19, 19, int(Qt.AlignmentFlag.AlignCenter), str(badge))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
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
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._selected            = False
        self._hovered             = False
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

    def set_zoom(self, card_w, card_h):
        """Resize an existing card for a new zoom — no widget recreation.

        The stored pixmap is re-scaled as a stand-in; a fresh render at the
        new width is queued separately by _schedule_visible."""
        self._card_w = card_w
        self._card_h = card_h
        self.setFixedSize(*card_size(card_w, card_h))
        if self._pixmap is not None and not self._pixmap.isNull():
            self._pixmap = self._pixmap.scaled(
                card_w, card_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
        self.update()

    def set_image(self, image: QImage):
        """Called from the GUI thread with a freshly rendered QImage."""
        self.set_pixmap(QPixmap.fromImage(image))

    def set_selected(self, sel):
        sel = bool(sel)
        if sel != self._selected:
            self._selected = sel
            self.update()

    # Hover is per-card: enter/leave repaint only this card (paint_card reads
    # the flag), never the grid, and there is no mouse tracking — tracking
    # would fire mouseMoveEvents across every card a drag sweeps past.
    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def hideEvent(self, e):
        # A leave event can be lost when the cursor leaves while the card is
        # being repainted or the grid rebuilt under it — clearing here keeps a
        # stale hover wash from sticking to a card nobody points at.
        self._hovered = False
        super().hideEvent(e)

    def _ghost(self, pm):
        """A semi-transparent drag ghost: the drop slot the pointer is over
        stays visible beneath the card being carried, so reordering reads as
        "put it here" rather than "a card is gone"."""
        ghost = QPixmap(pm.size())
        ghost.fill(Qt.GlobalColor.transparent)
        p = QPainter(ghost)
        p.setOpacity(0.55)
        p.drawPixmap(0, 0, pm)
        p.end()
        return ghost

    def paintEvent(self, _e):
        paint_card(self, self._pixmap, self._caption,
                   self._card_w, self._card_h, self._selected,
                   hover=self._hovered)

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
            drag.setPixmap(self._ghost(pm))
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
            drag.setPixmap(self._ghost(pm))

        drag.setHotSpot(e.position().toPoint())
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        drag.exec(actions)


class PageGrid(QWidget):
    order_changed     = pyqtSignal()
    selection_changed = pyqtSignal()

    def __init__(self, model, pdf_path, parent=None):
        super().__init__(parent)
        self.model    = model
        self.pdf_path = pdf_path
        self._cards   = []
        self._card_render_widths = []   # render_w per card, parallel to _cards
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
        # Debounce timer for zoom: re-laying out and re-queuing renders is only
        # worth doing once a wheel-zoom gesture stops, not once per notch.
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._apply_zoom)
        self._zooming = False  # re-entrancy guard for _apply_zoom
        self._scroll_connected = False  # connect scrollbar only once
        self.setAcceptDrops(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._rebuild()
        _register_themed(self)

    def _apply_theme(self):
        # The cards read _TV at paint time, so a theme switch only needs a
        # repaint. Rebuilding here used to destroy and recreate every card and
        # re-queue all thumbnail renders on every toggle — the "super duper
        # slow" dark/light switch.
        for card in self._cards:
            card.update()
        self.update()

    def _set_zoom(self, card_w, card_h):
        self._card_w = card_w
        self._card_h = card_h
        # Resize the existing cards in place synchronously — cheap (~0.1 ms) and
        # what callers and tests expect to have happened by the time zoom_in
        # returns. Only the render pass is deferred to the debounce timer, so a
        # wheel-zoom gesture doesn't re-queue every thumbnail once per notch.
        for card in self._cards:
            card.set_zoom(card_w, card_h)
        self._relayout()
        self._zoom_timer.start(80)

    def zoom_in(self):
        step = 20 if self._card_w < 300 else 40 if self._card_w < 600 else 80
        new_w = min(1400, self._card_w + step)
        self._set_zoom(new_w, int(new_w * (CARD_H / CARD_W)))

    def zoom_out(self):
        step = 20 if self._card_w <= 300 else 40 if self._card_w <= 600 else 80
        new_w = max(60, self._card_w - step)
        self._set_zoom(new_w, int(new_w * (CARD_H / CARD_W)))

    def zoom_reset(self):
        self._set_zoom(CARD_W, CARD_H)

    def _apply_zoom(self):
        """Render pass for a zoom: refresh thumbnails at the new card width.

        The cards were already resized synchronously by _set_zoom; this only
        re-queues renders for the visible ones once the gesture pauses, so a
        wheel-zoom doesn't submit every thumbnail's render once per notch."""
        if getattr(self, "_zooming", False):
            return
        self._zooming = True
        try:
            if not self._cards:
                self._rebuild()
                return
            for t in self._thumb_tasks:
                t.cancel()
            self._thumb_tasks.clear()
            self._thumb_gen += 1

            # Recompute the render width per card (aspect-correct for the new
            # zoom, and the single-page mode's card width tracks the widget).
            self._card_render_widths.clear()
            per_row   = self._per_row()
            is_single = (per_row == 1)
            grid_w    = max(100, self.width() or 800)
            for i, card in enumerate(self._cards):
                if is_single:
                    uid = card.orig_idx
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    rot            = self.model.get_rotation(uid)
                    c_w = max(60, grid_w - 2*MARGIN - 16)
                    pw, ph = _FullPageCache.get_dims(src_path, orig, rot)
                    if pw <= 0 or ph <= 0:
                        pw_pt, ph_pt = _cached_page_size_pt(src_path, orig)
                        if pw_pt and ph_pt:
                            pw, ph = ((ph_pt, pw_pt) if rot % 180 == 90
                                      else (pw_pt, ph_pt))
                    if pw > 0 and ph > 0:
                        c_h = int(c_w * ph / pw)
                    else:
                        c_h = int(c_w * CARD_H / CARD_W)
                    render_w = _thumb_render_width(c_w * 1.5)
                    card.set_zoom(c_w, c_h)
                else:
                    render_w = _thumb_render_width(max(self._card_w * 2, 200))
                self._card_render_widths.append(render_w)

            self._relayout()
            # Kick off thumbnail loading for currently visible cards only.
            QTimer.singleShot(0, self._schedule_visible)
        finally:
            self._zooming = False

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
            old_cards = self._cards[:]
            old_pm_by_uid = {}
            for c in old_cards:
                pm = c.pixmap()
                if pm and not pm.isNull():
                    old_pm_by_uid[c.orig_idx] = pm
            self._cards.clear()

            per_row   = self._per_row()
            is_single = (per_row == 1)
            grid_w    = max(100, self.width() or 800)
            self._card_render_widths.clear()

            for pos, uid in enumerate(self.model.order):
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                rot            = self.model.get_rotation(uid)

                if is_single:
                    c_w = max(60, grid_w - 2*MARGIN - 16)
                    pw, ph = _FullPageCache.get_dims(src_path, orig, rot)
                    # get_dims already returns the page's oriented dimensions
                    # (rotation applied, like the render the cache keys on), so
                    # the thumbnail box simply follows that aspect. Swapping
                    # here again for rot in (90, 270) was a second, opposite
                    # swap: a rotated landscape page ended up in a portrait
                    # box, with the thumbnail floated in ~half of it.
                    if pw <= 0 or ph <= 0:
                        # Full-page cache is cold (manage renders through the
                        # thumbnail cache) — fall back to the page's measured
                        # size, still oriented.
                        pw_pt, ph_pt = _cached_page_size_pt(src_path, orig)
                        if pw_pt and ph_pt:
                            pw, ph = ((ph_pt, pw_pt) if rot % 180 == 90
                                      else (pw_pt, ph_pt))
                    if pw > 0 and ph > 0:
                        c_h = int(c_w * ph / pw)
                    else:
                        c_h = int(c_w * CARD_H / CARD_W)
                    render_w = _thumb_render_width(c_w * 1.5)
                else:
                    c_w      = self._card_w
                    c_h      = self._card_h
                    render_w = _thumb_render_width(max(self._card_w * 2, 200))

                self._card_render_widths.append(render_w)

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
                self._cards.append(card)

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
        self._cards[cidx].set_image(image)

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
        if not self._cards:
            return
        sa = self._get_scroll_area()
        if sa is not None:
            scroll_y    = sa.verticalScrollBar().value()
            viewport_h  = sa.viewport().height() or 600
            y_min = max(0, scroll_y - viewport_h)          # 1 vp above
            y_max = scroll_y + 2 * viewport_h              # 2 vp below (scroll direction)
        else:
            y_min, y_max = 0, 9_999_999   # no scroll area — show all

        gen      = self._thumb_gen
        per_row  = self._per_row()
        is_single = (per_row == 1)

        if is_single:
            # Cards stacked vertically; heights vary
            y = MARGIN
            for i, card in enumerate(self._cards):
                card_h = card.height() or self._card_h
                y_top, y_bot = y, y + card_h
                y += card_h + GAP
                if y_bot < y_min or y_top > y_max:
                    continue
                self._maybe_schedule(i, gen)
        else:
            # Uniform grid
            cell_h  = self._card_h + 28 + GAP
            row_min = max(0, int((y_min - MARGIN) // cell_h))
            row_max = int((y_max - MARGIN) // cell_h) + 1
            for i, card in enumerate(self._cards):
                if i // per_row < row_min or i // per_row > row_max:
                    continue
                self._maybe_schedule(i, gen)

    def _maybe_schedule(self, cidx, gen):
        """Submit a thumb task for card[cidx] if its thumbnail isn't cached yet."""
        if cidx >= len(self._cards) or cidx >= len(self._card_render_widths):
            return
        card     = self._cards[cidx]
        render_w = self._card_render_widths[cidx]
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

    def _card_tops(self):
        """Return a list of y-offsets for each card (single-page mode only)."""
        tops = []
        y = MARGIN
        for card in self._cards:
            tops.append(y)
            y += card.height() + GAP
        return tops

    def cards(self):
        """The card widgets, in display order — for callers that map a page
        number onto the grid's geometry (the shared navigation rail)."""
        return list(self._cards)

    def _relayout(self):
        if not self._cards:
            self.setMinimumHeight(200); return
        pr = self._per_row()
        if pr == 1:
            # Single-page mode: stack cards vertically, each filling full width
            y = MARGIN
            for card in self._cards:
                card.move(MARGIN, y)
                card.show()
                y += card.height() + GAP
            self.setMinimumHeight(y + MARGIN)
        else:
            cell_w = self._card_w+16+GAP
            cell_h = self._card_h+28+GAP
            rows   = (len(self._cards)+pr-1)//pr
            for i, card in enumerate(self._cards):
                card.move(MARGIN + i%pr*cell_w, MARGIN + i//pr*cell_h)
                card.show()
            self.setMinimumHeight(MARGIN + rows*cell_h + MARGIN)
        self.update()

    def resizeEvent(self, e):
        # In single-page mode the card width must track the widget width.
        # Debounce to avoid triggering _rebuild on every pixel of a drag-resize.
        if self._per_row() == 1:
            self._resize_timer.start(120)
        else:
            self._relayout()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_indicator < 0 or not self._cards: return
        pr = self._per_row()
        p  = QPainter(self)
        if pr == 1:
            tops = self._card_tops()
            idx  = min(self._drop_indicator, len(tops))
            if idx < len(tops):
                y = tops[idx] - GAP//2
            else:
                y = tops[-1] + self._cards[-1].height() + GAP//2
            _paint_drop_marker(p, MARGIN, y - _DROP_THICKNESS/2.0,
                               self._cards[0].width(), horizontal=True)
        else:
            cell_w = self._card_w+16+GAP
            cell_h = self._card_h+28+GAP
            pos    = min(self._drop_indicator, len(self._cards))
            col    = pos % pr; row = pos // pr
            x      = MARGIN + col*cell_w - GAP//2
            y      = MARGIN + row*cell_h
            _paint_drop_marker(p, x - _DROP_THICKNESS/2.0, y, self._card_h)
        p.end()

    def _pos_from_point(self, pt):
        if not self._cards: return 0
        n  = len(self._cards)
        pr = self._per_row()
        if pr == 1:
            # Single-page mode: find which card the y coordinate falls in
            tops = self._card_tops()
            rel_y = pt.y()
            for i, top in enumerate(tops):
                bottom = top + self._cards[i].height()
                if rel_y < (top + bottom) // 2:
                    return i
            return n
        cell_w = self._card_w + 16 + GAP
        cell_h = self._card_h + 28 + GAP
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
        for i, card in enumerate(self._cards):
            card.set_selected(self.model.is_selected(i))

    def handle_drop(self, from_pos, to_pos, multi=False, copy=False):
        self._drop_indicator = -1; self.update()
        self._record()
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
    def _record(self):
        """Note the document's shape before changing it.

        Whoever mutates, records — and the grid mutates in three places. They
        used to record in none of them: every button in the page manager's
        sidebar called _save_history and then asked the grid to do the work, so
        the edits reached by dragging a card or pressing the rotate buttons
        wrote no history entry at all. Strg+Z after either of those undid
        whatever had been done *before* it, and neither marked the document as
        having unsaved changes."""
        from tools.viewer.tab_base import owning_tab
        tab = owning_tab(self)
        if tab is not None:
            tab.push_history()

    def rotate_selected(self, deg):
        self._record()
        self.model.rotate_selected(deg); self._rebuild(); self.order_changed.emit()

    def delete_selected(self):
        self._record()
        self.model.delete_selected(); self._rebuild()
        self.order_changed.emit(); self.selection_changed.emit()

    def select_all(self):
        self.model.select_all(); self._update_selection(); self.selection_changed.emit()

    def deselect_all(self):
        self.model.deselect_all(); self._update_selection(); self.selection_changed.emit()
