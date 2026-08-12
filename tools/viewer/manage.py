"""
Manage, moved verbatim out of tools/page_viewer.py.
"""
import os
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame, QFileDialog, QApplication, QScrollArea, QLineEdit
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QEvent
from tools.app_state import AppState
from tools.i18n import tr
from tools.viewer.model import _parse_positions, _positions_to_str
from tools.viewer.tab import PdfTab
from tools.viewer.theme import _TV, _register_themed


class ManageShortcutFilter(QObject):
    """
    Fängt Tastendruecke auf App-Ebene ab.
    Nur aktiv wenn manage_panel sichtbar ist.
    """
    def __init__(self, manage_panel, parent=None):
        super().__init__(parent)
        self.panel = manage_panel

    def eventFilter(self, obj, event):
        # Never intercept while a modal dialog is open — its widgets own the keys.
        if QApplication.activeModalWidget() is not None:
            return False
        t = event.type()
        # Claim ShortcutOverride so widgets don't eat our Ctrl combos.
        # MUST use accept()+return False (not return True) so Qt still dispatches
        # the subsequent KeyPress — return True eats the event entirely.
        if t == QEvent.Type.ShortcutOverride:
            if not self.panel.isVisible():
                return False
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit):
                return False
            k    = event.key()
            ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if ctrl and k in (Qt.Key.Key_A, Qt.Key.Key_C, Qt.Key.Key_V,
                              Qt.Key.Key_X, Qt.Key.Key_Z, Qt.Key.Key_Y,
                              Qt.Key.Key_D):
                event.accept()
            # Key_P is handled by the global QShortcut at PdfViewerWidget level;
            # don't claim it here so the global shortcut can fire.
            return False

        if t != QEvent.Type.KeyPress:
            return False
        if not self.panel.isVisible():
            return False

        # Shortcuts nicht abfangen wenn ein Textfeld fokussiert ist
        focused = QApplication.focusWidget()
        if isinstance(focused, QLineEdit):
            return False

        k     = event.key()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and not ctrl:
            self.panel._delete(); return True
        if ctrl:
            if k == Qt.Key.Key_A: self.panel.grid.select_all();   return True
            if k == Qt.Key.Key_D: self.panel.grid.deselect_all(); return True
            if k == Qt.Key.Key_C: self.panel._copy();              return True
            if k == Qt.Key.Key_X: self.panel._cut();               return True
            if k == Qt.Key.Key_V: self.panel._paste();             return True
            if k == Qt.Key.Key_Z and not shift: self.panel._undo(); return True
            if (k == Qt.Key.Key_Z and shift) or k == Qt.Key.Key_Y:
                self.panel._redo(); return True
        return False


class ManagePanel(QWidget):
    closed = pyqtSignal()

    # Shared cross-tab clipboard: list of (pdf_path, orig_page_idx, rotation)
    _shared_clipboard: list = []

    def __init__(self, model, pdf_path, grid, parent=None, tab=None):
        super().__init__(parent)
        self.setObjectName("managePanel")
        self.model        = model
        self.pdf_path     = pdf_path
        self.grid         = grid
        # The owning PdfTab, kept explicitly. Manage mode reparents this panel
        # into a splitter owned by the viewer, so the parent chain no longer
        # leads back to the tab — see _swap_source.
        self.tab          = tab if tab is not None else parent
        self._history     = []
        self._redo_stack  = []
        self._filter    = None
        self.setMinimumWidth(220)
        self._setup()
        _register_themed(self)
        self.destroyed.connect(self._cleanup_filter)

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Titel oben (fixiert)
        self._title_w = QWidget()
        self._title_w.setObjectName("manageTitleW")
        self._title_w.setFixedHeight(36)
        tl = QHBoxLayout(self._title_w)
        tl.setContentsMargins(10, 0, 10, 0)
        self._title_lbl = QLabel(tr("Seiten verwalten"))
        self._title_lbl.setStyleSheet(
            "font-size:13px;font-weight:bold;background:transparent;")
        tl.addWidget(self._title_lbl)
        outer.addWidget(self._title_w)

        # Scrollbarer Bereich
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content_w = QWidget()
        self._content_w.setObjectName("manageContentW")
        layout = QVBoxLayout(self._content_w)
        layout.setContentsMargins(10, 8, 22, 10)
        layout.setSpacing(5)
        self._scroll_area.setWidget(self._content_w)
        outer.addWidget(self._scroll_area, 1)

        sel_lbl = QLabel(tr("Auswahl  (z.B. 1, 3, 5-8)"))
        sel_lbl.setObjectName("sectionLabel")
        layout.addWidget(sel_lbl)

        self.sel_edit = QLineEdit()
        self.sel_edit.setPlaceholderText(tr("z.B. 1, 3, 5-8, 12  →  Enter"))
        self.sel_edit.returnPressed.connect(self._apply_sel_edit)
        layout.addWidget(self.sel_edit)
        layout.addWidget(self._sep())

        # Zoom and rotation are both one-click view actions, so they share a
        # single row of icon buttons. Rotation used to be its own section with
        # two full-width buttons — three rows of sidebar for two clicks — while
        # this row carried a "↺" that looked like rotate but reset the zoom.
        self._section(layout, tr("ANSICHT"))
        view_row = QHBoxLayout()
        view_row.setSpacing(4)
        self._zoom_btns_manage = []
        def _icon_btn(text, tip, slot):
            b = QPushButton(text)
            b.setFixedSize(32, 26)
            b.setToolTip(tr(tip))
            b.clicked.connect(slot)
            view_row.addWidget(b)
            self._zoom_btns_manage.append(b)
            return b
        _icon_btn("−",   "Thumbnails verkleinern", lambda: self._zoom_grid(-1))
        _icon_btn("+",   "Thumbnails vergroessern", lambda: self._zoom_grid(+1))
        _icon_btn("1:1", "Zoom zuruecksetzen",      lambda: self._zoom_grid(0))
        view_row.addSpacing(10)
        _icon_btn("↺", "Auswahl 90° gegen den Uhrzeigersinn drehen",
                  lambda: self.grid.rotate_selected(270))
        _icon_btn("↻", "Auswahl 90° im Uhrzeigersinn drehen",
                  lambda: self.grid.rotate_selected(90))
        view_row.addStretch()
        layout.addLayout(view_row)
        layout.addWidget(self._sep())

        self._section(layout, tr("AUSWAHL"))
        layout.addWidget(self._btn(tr("Alle auswaehlen  (Strg+A)"),  self.grid.select_all))
        layout.addWidget(self._btn(tr("Auswahl aufheben  (Strg+D)"), self.grid.deselect_all))
        layout.addWidget(self._sep())

        self._section(layout, tr("OPERATIONEN"))
        layout.addWidget(self._btn(tr("Loeschen  (Entf)"),         self._delete))
        layout.addWidget(self._btn(tr("Kopieren  (Strg+C)"),       self._copy))
        layout.addWidget(self._btn(tr("Einfuegen  (Strg+V)"),      self._paste))
        layout.addWidget(self._btn(tr("Rueckgaengig  (Strg+Z)"),   self._undo))
        layout.addWidget(self._btn(tr("Extrahieren..."),            self._extract))
        layout.addWidget(self._btn(tr("Als neuen Tab oeffnen"),    self._open_as_tab))
        layout.addWidget(self._btn(tr("Leere Seite einfuegen"),    self._insert_blank))
        # Takes several files at once, which is what the separate
        # "ZUSAMMENFUEHREN ▸ PDFs zusammenfuehren..." section did — same dialog,
        # same insert position, one button.
        layout.addWidget(self._btn(tr("Aus Datei(en) einfuegen..."), self._insert_from_file))
        layout.addWidget(self._sep())

        # Speichern / Speichern unter live in Datei ▸ at the top of the window
        # (Strg+S / Strg+Umschalt+S) — they are document-wide, not page-manager
        # actions, and having them in both places invited saving twice.

        self._section(layout, tr("TRENNEN"))
        layout.addWidget(self._btn(tr("Auswahl als Datei speichern"), self._split_selection))
        layout.addWidget(self._btn(tr("Jede Seite als Datei"),        self._split_each))
        layout.addWidget(self._btn(tr("Alle N Seiten..."),            self._split_n))

        layout.addStretch()

        back_btn = QPushButton(tr("◀  Einzelansicht  [Tab / Esc]"))
        back_btn.setObjectName("secondaryBtn")
        back_btn.clicked.connect(self.closed.emit)
        layout.addWidget(back_btn)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:10px;min-height:32px;background:transparent;")
        layout.addWidget(self.status)

    def showEvent(self, e):
        """Installiere Event-Filter wenn Panel sichtbar wird."""
        super().showEvent(e)
        if self._filter is None:
            self._filter = ManageShortcutFilter(self)
            QApplication.instance().installEventFilter(self._filter)

    def hideEvent(self, e):
        """Entferne Event-Filter wenn Panel unsichtbar wird."""
        super().hideEvent(e)
        self._cleanup_filter()

    def _cleanup_filter(self):
        if self._filter:
            QApplication.instance().removeEventFilter(self._filter)
            self._filter = None

    def _apply_theme(self):
        t = _TV
        self.setStyleSheet(
            f"QWidget#managePanel{{background:{t['sidebar_bg']};border-right:1px solid {t['border']};}}")
        self._title_w.setStyleSheet(
            f"QWidget#manageTitleW{{background:{t['sidebar_bg']};}}")
        self._title_lbl.setStyleSheet(
            f"color:{t['text']};font-size:13px;font-weight:bold;background:transparent;")
        self._scroll_area.setStyleSheet(
            f"QScrollArea{{background:{t['sidebar_bg']};border:none;}}")
        self._content_w.setStyleSheet(
            f"QWidget#manageContentW{{background:{t['sidebar_bg']};}}")
        _zs = (f"QPushButton{{background:{t['btn_bg']};color:{t['text']};"
               f"border:1px solid {t['btn_brd']};border-radius:4px;font-size:13px;}}"
               f"QPushButton:hover{{background:{t['hover']};}}")
        for b in getattr(self, '_zoom_btns_manage', []):
            b.setStyleSheet(_zs)
        if hasattr(self, '_zoom_hint_lbl'):
            self._zoom_hint_lbl.setStyleSheet(
                f"color:{t['vdim']};font-size:9px;background:transparent;")
        if hasattr(self, 'sel_edit'):
            self.sel_edit.setStyleSheet(
                f"QLineEdit{{background:{t['input_bg']};color:{t['text']};"
                f"border:1px solid {t['input_brd']};border-radius:3px;"
                f"padding:3px 6px;font-size:12px;}}"
                f"QLineEdit:focus{{border:1px solid {t['acc']};}}")

    def _section(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        layout.addWidget(lbl)

    def _btn(self, text, fn):
        b = QPushButton(text)
        b.setObjectName("secondaryBtn")
        b.clicked.connect(fn)
        b.setMinimumHeight(28)
        return b

    def _sep(self):
        f = QFrame()
        f.setFrameShape(QFrame.Shape.HLine)
        f.setStyleSheet(f"color:{_TV['border']};margin:3px 0;")
        return f

    def update_info(self):
        positions = sorted(i+1 for i, u in enumerate(self.model.order)
                           if u in self.model.selected)
        self.sel_edit.blockSignals(True)
        self.sel_edit.setText(self._positions_to_str(positions))
        self.sel_edit.blockSignals(False)

    _positions_to_str = staticmethod(_positions_to_str)

    def _apply_sel_edit(self):
        """Parst den Eingabetext und setzt Auswahl — nur bei Enter."""
        positions = _parse_positions(self.sel_edit.text(), len(self.model.order))
        if positions:
            self.model.selected = {self.model.order[i] for i in positions}
            self.grid._update_selection()
            self.grid.selection_changed.emit()
        # Feld immer auf die kompakte Darstellung normalisieren (auch bei
        # ungültiger Eingabe — die Auswahl bleibt dann unverändert).
        self.update_info()

    def _snapshot(self):
        return (
            list(self.model.order),
            dict(self.model.rotations),
            dict(self.model.src),
            self.model._next_uid,
            dict(self.model.foreign_src),
            self.pdf_path,
        )

    def _restore_snapshot(self, snap):
        order, rotations, model_src, next_uid, foreign_src, pdf_path = snap
        self.model.order       = order
        self.model.rotations   = rotations
        self.model.src         = model_src
        self.model._next_uid   = next_uid
        self.model.foreign_src = foreign_src
        self.model.selected.clear()
        if pdf_path and pdf_path != self.pdf_path:
            self.pdf_path      = pdf_path
            self.grid.pdf_path = pdf_path

    def _save_history(self):
        self._history.append(self._snapshot())
        self._redo_stack.clear()   # new action clears redo branch
        if len(self._history) > 50: self._history.pop(0)

    def _cap_redo(self):
        if len(self._redo_stack) > 50: self._redo_stack.pop(0)

    def _delete(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        self._save_history()
        n = len(self.model.selected)
        self.grid.delete_selected()
        self.status.setText(tr('{p0} Seite(n) geloescht.  Strg+Z = Rueckgaengig.').format(p0=n))

    def _copy(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        # Store (pdf_path, orig_page_idx, rotation) in shared cross-tab clipboard
        ManagePanel._shared_clipboard = []
        for u in self.model.order:
            if u in self.model.selected:
                path, orig = self.model.page_source(u, self.pdf_path)
                rot = self.model.rotations.get(u, 0)
                ManagePanel._shared_clipboard.append((path, orig, rot))
        n = len(ManagePanel._shared_clipboard)
        self.status.setText(
            tr('{p0} Seite(n) kopiert.  Strg+V = Einfuegen (auch in anderen Tabs).').format(p0=n))

    def _cut(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        self._copy()
        self._save_history()
        n = len(self.model.selected)
        self.grid.delete_selected()
        self.status.setText(
            tr('{p0} Seite(n) ausgeschnitten.  Strg+V = Einfuegen.').format(p0=n))

    def _paste(self):
        if not ManagePanel._shared_clipboard:
            self.status.setText(tr("Nichts zum Einfuegen.  Zuerst Strg+C.")); return
        self._save_history()
        if self.model.selected:
            positions = [i for i, u in enumerate(self.model.order)
                         if u in self.model.selected]
            insert_at = max(positions) + 1
        else:
            insert_at = len(self.model.order)
        # Paste from shared clipboard — source may be a different pdf_path
        for i, (src_path, orig_idx, rot) in enumerate(ManagePanel._shared_clipboard):
            new_uid = self.model._new_uid()
            if src_path == self.pdf_path:
                self.model.src[new_uid] = orig_idx
            else:
                # Foreign page: store dummy in src, real source in foreign_src
                self.model.src[new_uid] = orig_idx
                self.model.foreign_src[new_uid] = (src_path, orig_idx)
            if rot:
                self.model.rotations[new_uid] = rot
            self.model.order.insert(insert_at + i, new_uid)
        self.grid._rebuild(); self.grid.order_changed.emit()
        n = len(ManagePanel._shared_clipboard)
        self.status.setText(tr('{p0} Seite(n) eingefuegt.').format(p0=n))

    def _undo(self):
        if not self._history:
            self.status.setText(tr("Nichts zum Rueckgaengig.")); return
        self._redo_stack.append(self._snapshot())
        self._cap_redo()
        self._restore_snapshot(self._history.pop())
        self.grid._rebuild(); self.grid.order_changed.emit()
        self.status.setText(tr("Rueckgaengig.  Strg+Y = Wiederholen."))

    def _redo(self):
        if not self._redo_stack:
            self.status.setText(tr("Nichts zum Wiederholen.")); return
        self._history.append(self._snapshot())
        if len(self._history) > 50: self._history.pop(0)
        self._restore_snapshot(self._redo_stack.pop())
        self.grid._rebuild(); self.grid.order_changed.emit()
        self.status.setText(tr("Wiederholt."))

    def _zoom_grid(self, direction):
        """direction: +1=rein, -1=raus, 0=reset"""
        if direction > 0:   self.grid.zoom_in()
        elif direction < 0: self.grid.zoom_out()
        else:               self.grid.zoom_reset()

    def _extract(self):
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Extrahieren als"), "", tr("PDF (*.pdf)"))
        if not path: return
        try:
            from pypdf import PdfReader, PdfWriter
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter()
            for uid in self.model.order:
                if uid in self.model.selected:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot  = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    writer.add_page(page)
            with open(path, "wb") as f: writer.write(f)
            AppState.get().open_result(path, "Extrahiert")
            self.status.setText(tr('{p0} Seite(n) extrahiert.').format(p0=len(self.model.selected)))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _open_as_tab(self):
        """Ausgewählte Seiten als neue temporäre PDF in neuem Tab öffnen.

        Asks whether to copy or move them. Moving is the case the separate
        "Nach Bereichen..." split button used to cover, and it is easier to do
        by picking the pages you want than by typing ranges into a prompt."""
        from PyQt6.QtWidgets import QMessageBox
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        box = QMessageBox(self)
        box.setWindowTitle(tr("Als neuen Tab oeffnen"))
        box.setText(tr('{p0} Seite(n) in einem neuen Tab oeffnen.').format(
            p0=len(self.model.selected)))
        box.setInformativeText(tr("Sollen die Seiten hier ebenfalls bleiben?"))
        keep = box.addButton(tr("Kopieren  (hier behalten)"),
                             QMessageBox.ButtonRole.AcceptRole)
        move = box.addButton(tr("Verschieben  (hier entfernen)"),
                             QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() not in (keep, move):
            return
        try:
            import tempfile
            from pypdf import PdfReader, PdfWriter
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter(); n = 0
            for uid in self.model.order:
                if uid in self.model.selected:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot  = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    writer.add_page(page); n += 1
            # Temporäre Datei — bleibt solange der Tab offen ist
            tmp = tempfile.NamedTemporaryFile(
                suffix=".pdf", delete=False,
                prefix="copyshop_sel_")
            with open(tmp.name, "wb") as f: writer.write(f)
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            # Only remove them here once the new file is safely written.
            if box.clickedButton() is move:
                self._save_history()
                self.model.delete_selected()
                self.grid._rebuild(); self.grid.order_changed.emit()
            AppState.get().open_result(tmp.name, f"{stem} [{n}S]")
            self.status.setText(
                (tr('{p0} Seite(n) in neuen Tab verschoben.')
                 if box.clickedButton() is move
                 else tr('{p0} Seite(n) als neuer Tab geoeffnet.')).format(p0=n))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _swap_source(self, new_path):
        """Point the page manager — and everything else that resolves the model's
        page indexes — at a rebuilt file.

        The grid, the owning tab and AppState each cached the old path, and only
        the panel's own copy was updated. Anything reading the document therefore
        resolved the model against the *previous* file: the tools kept processing
        the pre-edit PDF (the booklet tool imposed the old page order, so an
        inserted blank surfaced as the back of the cover), and switching tabs
        snapped the viewer back to it too."""
        self.pdf_path = new_path
        self.grid.pdf_path = new_path
        # Use the recorded tab, not a walk up the parent chain: in manage mode
        # this panel lives in the viewer's splitter, so the walk found no PdfTab
        # and silently skipped the update. The single view then resolved the new
        # page indexes against the old, shorter file — an inserted blank page is
        # past its last index, so the render threw and the preview showed the
        # blue "could not render" fallback at a bogus size.
        tab = self.tab if isinstance(self.tab, PdfTab) else None
        if tab is None:
            tab = self.parent()
            while tab is not None and not isinstance(tab, PdfTab):
                tab = tab.parent()
        if tab is not None:
            tab.pdf_path = new_path
            tab.single.pdf_path = new_path
        if AppState.get().page_model is self.model:
            AppState.get().current_pdf = new_path

    def _insert_blank(self):
        try:
            from pypdf import PdfReader, PdfWriter
            import tempfile
            self._save_history()
            reader = PdfReader(self.pdf_path, strict=False)
            if self.model.selected:
                positions = [i for i, u in enumerate(self.model.order)
                             if u in self.model.selected]
                insert_at = max(positions) + 1
            else:
                insert_at = len(self.model.order)
            # Size the blank like the page it follows, as that page is displayed
            # (page 0's raw MediaBox gave a mixed-size document an A4 sheet in
            # the middle of its A5 pages, and a portrait one next to landscape).
            if self.model.order:
                ref_uid = self.model.order[min(max(0, insert_at - 1),
                                               len(self.model.order) - 1)]
                ref_path, ref_orig = self.model.page_source(ref_uid, self.pdf_path)
                ref = PdfReader(ref_path, strict=False).pages[ref_orig]
                rot = (int(ref.get("/Rotate", 0) or 0)
                       + self.model.get_rotation(ref_uid)) % 360
            else:
                ref, rot = reader.pages[0], 0
            pw = float(ref.mediabox.width)
            ph = float(ref.mediabox.height)
            if rot in (90, 270): pw, ph = ph, pw
            # Leerseite ans Ende der Datei anhängen
            new_orig = len(reader.pages)
            writer = PdfWriter()
            for page in reader.pages: writer.add_page(page)
            writer.add_blank_page(pw, ph)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
            with open(tmp, "wb") as f: writer.write(f)
            # Neue UID für die Leerseite
            new_uid = self.model._new_uid()
            self.model.src[new_uid] = new_orig
            self.model.order.insert(insert_at, new_uid)
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            self.status.setText(tr("Leere Seite eingefuegt."))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _insert_from_file(self):
        """Insert the pages of one or more PDFs after the selection.

        Takes several files because this replaced the separate "PDFs
        zusammenfuehren..." button, which asked the same question and inserted at
        the same place — the only difference was that it opened the result in a
        new tab instead of editing this one, which is not what a page manager is
        for."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("PDF(s) einfuegen"), "", tr("PDF Dateien (*.pdf)"))
        if not paths: return
        try:
            from pypdf import PdfReader, PdfWriter
            self._save_history()
            import tempfile
            ins_pages = []
            for p in paths:
                ins_pages.extend(PdfReader(p, strict=False).pages)
            n_ins = len(ins_pages)
            if self.model.selected:
                positions = [i for i, u in enumerate(self.model.order)
                             if u in self.model.selected]
                insert_at = max(positions) + 1
            else:
                insert_at = len(self.model.order)
            # Neue UIDs für die eingefügten Seiten
            # pdf_path bleibt — wir hängen neue Seiten an die bestehende Datei
            # Einfachster Weg: Neue PDF bauen, Model neu initialisieren
            writer = PdfWriter()
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            for i, uid in enumerate(self.model.order):
                if i == insert_at:
                    for p in ins_pages: writer.add_page(p)
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot  = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                writer.add_page(page)
            if insert_at >= len(self.model.order):
                for p in ins_pages: writer.add_page(p)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
            with open(tmp, "wb") as f: writer.write(f)
            from pypdf import PdfReader as PR
            n_new = len(PR(tmp).pages)
            # Model neu aufbauen mit frischen UIDs
            self.model.__init__(n_new)
            self.model.selected.clear()
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            self.status.setText(tr('{p0} Seite(n) eingefuegt.').format(p0=n_ins))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    # ── Trennen ──────────────────────────────────────────────────────────────
    def _split_selection(self):
        """Save selected pages as a new PDF file and open in new tab."""
        from PyQt6.QtWidgets import QFileDialog
        from pypdf import PdfReader, PdfWriter
        if not self.model.selected:
            self.status.setText(tr("Zuerst Seiten auswaehlen.")); return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Auswahl speichern als"), "", tr("PDF (*.pdf)"))
        if not path: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            writer = PdfWriter()
            for uid in self.model.order:
                if uid not in self.model.selected: continue
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                writer.add_page(page)
            with open(path, "wb") as f: writer.write(f)
            n = len(self.model.selected)
            self.status.setText(f"OK: {n} {tr('Seite(n) gespeichert.')}")
            AppState.get().open_result(path, os.path.basename(path))
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _split_each(self):
        from PyQt6.QtWidgets import QFileDialog
        from pypdf import PdfReader, PdfWriter
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner waehlen"))
        if not out_dir: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            # If pages are selected only split those, otherwise split all in model order
            uids = ([uid for uid in self.model.order if uid in self.model.selected]
                    if self.model.selected else list(self.model.order))
            for i, uid in enumerate(uids):
                src_path, orig = self.model.page_source(uid, self.pdf_path)
                page = _rdr(src_path).pages[orig]
                rot = self.model.get_rotation(uid)
                if rot: page.rotate(rot)
                w = PdfWriter(); w.add_page(page)
                p = os.path.join(out_dir, f"{stem}_seite{i+1:03d}.pdf")
                with open(p, "wb") as f: w.write(f)
            self.status.setText(f"OK: {len(uids)} {tr('Dateien erstellt')}")
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))

    def _split_n(self):
        from PyQt6.QtWidgets import QFileDialog, QInputDialog
        from pypdf import PdfReader, PdfWriter
        n, ok = QInputDialog.getInt(self, tr("Seiten pro Teil"),
                                    tr("Wie viele Seiten pro Datei?"), 1, 1, 9999)
        if not ok: return
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner waehlen"))
        if not out_dir: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            uids = ([uid for uid in self.model.order if uid in self.model.selected]
                    if self.model.selected else list(self.model.order))
            chunk = 0
            for start in range(0, len(uids), n):
                chunk += 1; w = PdfWriter()
                for uid in uids[start:start+n]:
                    src_path, orig = self.model.page_source(uid, self.pdf_path)
                    page = _rdr(src_path).pages[orig]
                    rot = self.model.get_rotation(uid)
                    if rot: page.rotate(rot)
                    w.add_page(page)
                p = os.path.join(out_dir, f"{stem}_teil{chunk:03d}.pdf")
                with open(p, "wb") as f: w.write(f)
            self.status.setText(f"OK: {chunk} {tr('Dateien erstellt')}")
        except Exception as e:
            self.status.setText(tr('Fehler: {p0}').format(p0=e))
