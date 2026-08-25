"""
The toolbar over the page grid.

Rotate, delete, insert, extract, select by range — the operations of "Seiten
verwalten", and the shortcut filter that makes them reachable from the keyboard
whatever has focus. The edits themselves go to the PageModel; this is the
control surface over it.
"""
import logging
import os
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QApplication, QLineEdit
from PyQt6.QtCore import Qt, pyqtSignal, QSize
from tools.app_state import AppState, theme_color
from tools.i18n import tr
from tools.shell.icons import icon
from tools.viewer.model import _parse_positions, _positions_to_str
from tools.viewer.shortcuts import ThumbGridShortcutFilter
from tools.viewer.tab_base import PdfTabBase, owning_tab
from tools.theme import _TV, _register_themed


class _ListBtn(QPushButton):
    def __init__(self, label, icon_name, kbd, parent=None):
        super().__init__(parent)
        self.setObjectName("listbtn")
        self.setFixedHeight(31)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_name = icon_name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(6)
        self._icon = QLabel()
        self._icon.setObjectName("listbtnIcon")
        self._icon.setFixedSize(18, 18)
        lay.addWidget(self._icon)
        self._label = QLabel(label)
        lay.addWidget(self._label, 1)
        self._kbd_lbl = None
        if kbd:
            self._kbd_lbl = QLabel(kbd)
            self._kbd_lbl.setObjectName("listbtnKbd")
            lay.addWidget(self._kbd_lbl)
        self._paint_icon(False)

    def _paint_icon(self, hover):
        """Draw the monochrome icon in the live theme's colour — dim normally,
        accent under the pointer. The emoji glyphs this replaced rendered in
        full colour and broke the theme (concept: a drawn, monochrome set)."""
        t = _TV
        colour = t['acc'] if hover else t['vdim']
        self._icon.setPixmap(icon(self._icon_name, colour=colour, size=16)
                             .pixmap(16, 16))
        if self._kbd_lbl:
            self._kbd_lbl.setStyleSheet(
                f"color:{t['vdim']};background:transparent;"
                f"font-family:monospace;font-size:10.5px;")

    def enterEvent(self, e):
        super().enterEvent(e)
        self._paint_icon(True)

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._paint_icon(False)


class ManagePanel(QWidget):
    closed = pyqtSignal()

    _shared_clipboard: list = []

    def __init__(self, model, pdf_path, grid, parent=None, tab=None):
        super().__init__(parent)
        self.setObjectName("managePanel")
        self.model        = model
        self.pdf_path     = pdf_path
        self.grid         = grid
        self.tab          = tab if tab is not None else parent
        self._filter    = None
        self._op_btns   = []
        self._pair_btns = []
        self.setMinimumWidth(220)
        self._setup()
        self._apply_theme()
        self.update_info()
        _register_themed(self)
        self.destroyed.connect(self._cleanup_filter)

    def _setup(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._content_w = QWidget()
        self._content_w.setObjectName("manageContentW")
        layout = QVBoxLayout(self._content_w)
        layout.setContentsMargins(10, 8, 12, 10)
        layout.setSpacing(4)
        outer.addWidget(self._content_w, 1)

        auswahl_row = QHBoxLayout()
        auswahl_row.setContentsMargins(0, 0, 0, 0)
        sel_lbl = QLabel(tr("Auswahl"))
        sel_lbl.setObjectName("navGroup")
        sel_lbl.setContentsMargins(0, 2, 0, 3)
        auswahl_row.addWidget(sel_lbl, 1)
        self._sel_count = QLabel("")
        self._sel_count.setObjectName("manageCount")
        auswahl_row.addWidget(self._sel_count)
        layout.addLayout(auswahl_row)

        self.sel_edit = QLineEdit()
        self.sel_edit.setObjectName("selEdit")
        self.sel_edit.setPlaceholderText(tr("z.B. 1, 3, 5-8, 12  →  Enter"))
        self.sel_edit.returnPressed.connect(self._apply_sel_edit)
        layout.addWidget(self.sel_edit)

        sel_row = QHBoxLayout()
        sel_row.setSpacing(0)
        btnpair = QWidget()
        btnpair.setObjectName("btnpair")
        bp_layout = QHBoxLayout(btnpair)
        bp_layout.setContentsMargins(0, 0, 0, 0)
        bp_layout.setSpacing(0)
        for text, tip, fn in (
                (tr("Alle"),  tr("Alle auswählen") + "  (Strg+A)",  self.grid.select_all),
                (tr("Keine"), tr("Auswahl aufheben") + "  (Strg+D)", self.grid.deselect_all)):
            b = QPushButton(text)
            b.setObjectName("btnpairBtn")
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(fn)
            bp_layout.addWidget(b)
            self._pair_btns.append(b)
        sel_row.addWidget(btnpair, 1)
        layout.addLayout(sel_row)

        self._section(layout, tr("ANSICHT"))
        view_row = QHBoxLayout()
        view_row.setSpacing(6)

        zoom_pill = QWidget()
        zoom_pill.setObjectName("pillGroup")
        zp_lay = QHBoxLayout(zoom_pill)
        zp_lay.setContentsMargins(2, 2, 2, 2)
        zp_lay.setSpacing(0)
        self._zoom_btns = []
        for ico, tip, slot in (
                ("minus", "Thumbnails verkleinern", lambda: self._zoom_grid(-1)),
                ("plus", "Thumbnails vergrößern", lambda: self._zoom_grid(+1)),
                ("fit", "Zoom zurücksetzen",      lambda: self._zoom_grid(0))):
            b = QPushButton()
            b.setFixedSize(28, 26)
            b.setToolTip(tr(tip))
            b.setObjectName("pillBtn")
            b.clicked.connect(slot)
            zp_lay.addWidget(b)
            self._zoom_btns.append((b, ico))
        view_row.addWidget(zoom_pill)

        rot_pill = QWidget()
        rot_pill.setObjectName("pillGroup")
        rp_lay = QHBoxLayout(rot_pill)
        rp_lay.setContentsMargins(2, 2, 2, 2)
        rp_lay.setSpacing(0)
        self._rot_btns = []
        for ico, tip, slot in (
                ("rotl", "Auswahl 90° gegen den Uhrzeigersinn drehen",
                 lambda: self.grid.rotate_selected(270)),
                ("rotr", "Auswahl 90° im Uhrzeigersinn drehen",
                 lambda: self.grid.rotate_selected(90))):
            b = QPushButton()
            b.setFixedSize(28, 26)
            b.setToolTip(tr(tip))
            b.setObjectName("pillBtn")
            b.clicked.connect(slot)
            rp_lay.addWidget(b)
            self._rot_btns.append((b, ico))
        view_row.addWidget(rot_pill)
        view_row.addStretch()
        layout.addLayout(view_row)

        self._section(layout, tr("OPERATIONEN"))
        layout.addWidget(self._listbtn(tr("Löschen"),     self._delete,          "trash",    "Entf"))
        layout.addWidget(self._listbtn(tr("Kopieren"),     self._copy,            "copy",     "Strg+C"))
        layout.addWidget(self._listbtn(tr("Einfügen"),    self._paste,           "paste",    "Strg+V"))
        layout.addWidget(self._listbtn(tr("Rückgängig"), self._undo,            "undo",     "Strg+Z"))
        layout.addWidget(self._listbtn(tr("Extrahieren..."),           self._extract,          "scissors"))
        layout.addWidget(self._listbtn(tr("Als neuen Tab öffnen"),    self._open_as_tab,      "doc"))
        layout.addWidget(self._listbtn(tr("Leere Seite einfügen"),    self._insert_blank,     "fileplus"))
        layout.addWidget(self._listbtn(tr("Aus Dateien einfügen..."), self._insert_from_file, "files"))

        self._section(layout, tr("TRENNEN"))
        layout.addWidget(self._listbtn(tr("Auswahl als Datei speichern"), self._split_selection, "save"))
        layout.addWidget(self._listbtn(tr("Jede Seite als Datei"),        self._split_each,      "split"))
        layout.addWidget(self._listbtn(tr("Alle N Seiten..."),            self._split_n,         "split"))

        layout.addStretch()

        back_btn = QPushButton(tr("Einzelansicht  [Tab / Esc]"))
        back_btn.setIcon(icon("prev", colour=theme_color("DIM")))
        back_btn.setIconSize(QSize(16, 16))
        back_btn.setObjectName("secondaryBtn")
        back_btn.clicked.connect(self.closed.emit)
        self._back_btn = back_btn
        layout.addWidget(back_btn)

    def _listbtn(self, label, fn, icon_name, kbd=""):
        b = _ListBtn(label, icon_name, kbd)
        b.clicked.connect(fn)
        self._op_btns.append(b)
        return b

    def showEvent(self, e):
        super().showEvent(e)
        if self._filter is None:
            self._filter = ThumbGridShortcutFilter(
                self.isVisible, self.grid, self._delete, self._copy,
                self._cut, self._paste, self._undo, self._redo)
            QApplication.instance().installEventFilter(self._filter)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._cleanup_filter()

    def _cleanup_filter(self):
        if self._filter:
            QApplication.instance().removeEventFilter(self._filter)
            self._filter = None

    def _apply_theme(self):
        t = _TV
        self.setStyleSheet(f"QWidget#managePanel{{background:{t['sidebar_bg']};}}")
        self._content_w.setStyleSheet(
            f"QWidget#manageContentW{{background:{t['sidebar_bg']};}}")

        if hasattr(self, '_sel_count'):
            self._sel_count.setStyleSheet(
                f"color:{t['vdim']};font-size:11px;background:transparent;")

        # The "Einzelansicht" button's arrow is a QIcon painted once at build
        # time with theme_color("DIM") — it must be rebuilt so it follows a
        # theme switch instead of keeping the theme it was born in.
        if hasattr(self, '_back_btn'):
            self._back_btn.setIcon(
                icon("prev", colour=theme_color("DIM")))
            self._back_btn.setIconSize(QSize(16, 16))

        _list = (f"QPushButton#listbtn{{background:transparent;color:{t['text']};"
                 f"border:none;border-radius:0;padding:0;text-align:left;}}"
                 f"QPushButton#listbtn:hover{{background:{t['surface_3']};"
                 f"border:1px solid {t['border']};}}")
        for b in self._op_btns:
            b.setStyleSheet(_list)
            if hasattr(b, '_paint_icon'):
                b._paint_icon(False)
            if hasattr(b, '_label'):
                b._label.setStyleSheet(
                    f"color:{t['text']};background:transparent;font-size:12px;")
            if hasattr(b, '_kbd_lbl') and b._kbd_lbl is not None:
                b._kbd_lbl.setStyleSheet(
                    f"color:{t['vdim']};background:transparent;"
                    f"font-family:monospace;font-size:10.5px;")

        _pill_group = (f"QWidget#pillGroup{{background:{t['surface_2']};"
                       f"border:1px solid {t['border']};border-radius:8px;}}")
        for w in self.findChildren(QWidget):
            if w.objectName() == "pillGroup":
                w.setStyleSheet(_pill_group)

        _pill_btn = (f"QPushButton#pillBtn{{background:transparent;color:{t['vdim']};"
                     f"border:none;border-radius:6px;font-size:13px;}}"
                     f"QPushButton#pillBtn:hover{{background:{t['surface_3']};}}")
        for b in self.findChildren(QPushButton):
            if b.objectName() == "pillBtn":
                b.setStyleSheet(_pill_btn)
                # The zoom/rotate pills draw their marks as icons (minus/plus/
                # fit/rotl/rotr), not as text glyphs — rebuild in the live theme.
                ico = None
                for grp in (self._zoom_btns, self._rot_btns):
                    for bb, name in grp:
                        if bb is b:
                            ico = name
                if ico:
                    b.setIcon(icon(ico, colour=t['vdim'], size=14))
                    b.setIconSize(QSize(14, 14))

        _pair = (f"QWidget#btnpair{{border:1px solid {t['border']};"
                 f"border-radius:8px;}}")
        for w in self.findChildren(QWidget):
            if w.objectName() == "btnpair":
                w.setStyleSheet(_pair)

        for i, b in enumerate(self._pair_btns):
            left = f"border-left:1px solid {t['border']};" if i > 0 else ""
            b.setStyleSheet(
                f"QPushButton#btnpairBtn{{background:{t['surface_2']};"
                f"color:{t['vdim']};border:none;{left}"
                f"font-size:12px;min-height:29px;}}"
                f"QPushButton#btnpairBtn:hover{{background:{t['surface_3']};"
                f"color:{t['text']};}}")

        if hasattr(self, 'sel_edit'):
            # Scoped to the object name so the global QLineEdit rule cannot
            # override it — the field must always read as a bordered input.
            self.sel_edit.setStyleSheet(
                f"QLineEdit#selEdit{{background:{t['input_bg']};color:{t['text']};"
                f"border:1px solid {t['input_brd']};border-radius:3px;"
                f"padding:3px 6px;font-size:12px;}}"
                f"QLineEdit#selEdit:focus{{border:1px solid {t['acc']};}}")

    def _section(self, layout, text):
        lbl = QLabel(text)
        lbl.setObjectName("navGroup")
        lbl.setContentsMargins(0, 8, 0, 3)
        layout.addWidget(lbl)

    def update_info(self):
        positions = sorted(i+1 for i, u in enumerate(self.model.order)
                           if u in self.model.selected)
        self.sel_edit.blockSignals(True)
        self.sel_edit.setText(self._positions_to_str(positions))
        self.sel_edit.blockSignals(False)
        selected = len(self.model.selected)
        total = len(self.model.order)
        self._sel_count.setText(
            tr("{n0} von {n1} Seiten").format(n0=selected, n1=total))

    _positions_to_str = staticmethod(_positions_to_str)

    def _apply_sel_edit(self):
        text = self.sel_edit.text()
        positions = _parse_positions(text, len(self.model.order))
        if positions:
            self.model.selected = {self.model.order[i] for i in positions}
            self.grid._update_selection()
            self.grid.selection_changed.emit()
        elif text.strip():
            # Nothing parsed from a real input — the user typed something the
            # field cannot make sense of (abc, 0, 99 on a 4-page file…).
            # Leaving the selection alone silently reads as the input being
            # ignored, so say what happened.
            AppState.get().status_message.emit(
                tr("Keine gültige Seitenangabe."))
        self.update_info()

    def _owning_tab(self):
        tab = self.tab if isinstance(self.tab, PdfTabBase) else None
        return tab if tab is not None else owning_tab(self.parent())

    def _save_history(self):
        tab = self._owning_tab()
        if tab is not None:
            tab.push_history()

    def _delete(self):
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
        n = len(self.model.selected)
        self.grid.delete_selected()
        AppState.get().status_message.emit(
            tr('{p0} Seite(n) gelöscht.  Strg+Z = Rückgängig.').format(p0=n))

    def _copy(self):
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
        ManagePanel._shared_clipboard = []
        for u in self.model.order:
            if u in self.model.selected:
                path, orig = self.model.page_source(u, self.pdf_path)
                rot = self.model.rotations.get(u, 0)
                ManagePanel._shared_clipboard.append((path, orig, rot))
        n = len(ManagePanel._shared_clipboard)
        AppState.get().status_message.emit(
            tr('{p0} Seite(n) kopiert.  Strg+V = Einfügen (auch in anderen Tabs).').format(p0=n))

    def _cut(self):
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
        self._copy()
        n = len(self.model.selected)
        self.grid.delete_selected()
        AppState.get().status_message.emit(
            tr('{p0} Seite(n) ausgeschnitten.  Strg+V = Einfügen.').format(p0=n))

    def _paste(self):
        if not ManagePanel._shared_clipboard:
            AppState.get().status_message.emit(
                tr("Nichts zum Einfügen.  Zuerst Strg+C."))
            return
        self._save_history()
        if self.model.selected:
            positions = [i for i, u in enumerate(self.model.order)
                         if u in self.model.selected]
            insert_at = max(positions) + 1
        else:
            insert_at = len(self.model.order)
        for i, (src_path, orig_idx, rot) in enumerate(ManagePanel._shared_clipboard):
            new_uid = self.model._new_uid()
            if src_path == self.pdf_path:
                self.model.src[new_uid] = orig_idx
            else:
                self.model.src[new_uid] = orig_idx
                self.model.foreign_src[new_uid] = (src_path, orig_idx)
            if rot:
                self.model.rotations[new_uid] = rot
            self.model.order.insert(insert_at + i, new_uid)
        self.grid._rebuild(); self.grid.order_changed.emit()
        n = len(ManagePanel._shared_clipboard)
        AppState.get().status_message.emit(
            tr('{p0} Seite(n) eingefügt.').format(p0=n))

    def _undo(self):
        tab = self._owning_tab()
        if tab is None or not tab.undo():
            AppState.get().status_message.emit(tr("Nichts zum Rückgängig."))
            return
        self.pdf_path = tab.pdf_path
        AppState.get().status_message.emit(
            tr("Rückgängig.  Strg+Y = Wiederholen."))

    def _redo(self):
        tab = self._owning_tab()
        if tab is None or not tab.redo():
            AppState.get().status_message.emit(tr("Nichts zum Wiederholen."))
            return
        self.pdf_path = tab.pdf_path
        AppState.get().status_message.emit(tr("Wiederholt."))

    def _zoom_grid(self, direction):
        if direction > 0:   self.grid.zoom_in()
        elif direction < 0: self.grid.zoom_out()
        else:               self.grid.zoom_reset()

    def _extract(self):
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
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
            AppState.get().status_message.emit(
                tr('{p0} Seite(n) extrahiert.').format(p0=len(self.model.selected)))
        except Exception as e:
            logging.exception("manage: _extract failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _open_as_tab(self):
        from PyQt6.QtWidgets import QMessageBox
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("Als neuen Tab öffnen"))
        box.setText(tr('{p0} Seite(n) in einem neuen Tab öffnen.').format(
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
            tmp = tempfile.NamedTemporaryFile(
                suffix=".pdf", delete=False,
                prefix="copyshop_sel_")
            with open(tmp.name, "wb") as f: writer.write(f)
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
            if box.clickedButton() is move:
                self._save_history()
                self.model.delete_selected()
                self.grid._rebuild(); self.grid.order_changed.emit()
            AppState.get().open_result(tmp.name, f"{stem} [{n}S]")
            AppState.get().status_message.emit(
                (tr('{p0} Seite(n) in neuen Tab verschoben.')
                 if box.clickedButton() is move
                 else tr('{p0} Seite(n) als neuer Tab geöffnet.')).format(p0=n))
        except Exception as e:
            logging.exception("manage: _open_as_tab failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _swap_source(self, new_path):
        self.pdf_path = new_path
        self.grid.pdf_path = new_path
        tab = self._owning_tab()
        if tab is not None:
            tab.retarget(new_path)
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
            new_orig = len(reader.pages)
            writer = PdfWriter()
            for page in reader.pages: writer.add_page(page)
            writer.add_blank_page(pw, ph)
            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
            with open(tmp, "wb") as f: writer.write(f)
            new_uid = self.model._new_uid()
            self.model.src[new_uid] = new_orig
            self.model.order.insert(insert_at, new_uid)
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            AppState.get().status_message.emit(tr("Leere Seite eingefügt."))
        except Exception as e:
            logging.exception("manage: _insert_blank failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _insert_from_file(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("PDF(s) einfügen"), "", tr("PDF Dateien (*.pdf)"))
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
            self.model.__init__(n_new)
            self.model.selected.clear()
            self._swap_source(tmp)
            self.grid._rebuild(); self.grid.order_changed.emit()
            AppState.get().status_message.emit(
                tr('{p0} Seite(n) eingefügt.').format(p0=n_ins))
        except Exception as e:
            logging.exception("manage: _insert_from_file failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _split_selection(self):
        from pypdf import PdfReader, PdfWriter
        if not self.model.selected:
            AppState.get().status_message.emit(tr("Zuerst Seiten auswählen."))
            return
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
            AppState.get().status_message.emit(f"OK: {n} {tr('Seite(n) gespeichert.')}")
            AppState.get().open_result(path, os.path.basename(path))
        except Exception as e:
            logging.exception("manage: _split_selection failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _split_each(self):
        from pypdf import PdfReader, PdfWriter
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner wählen"))
        if not out_dir: return
        try:
            readers = {}
            def _rdr(p):
                if p not in readers: readers[p] = PdfReader(p, strict=False)
                return readers[p]
            stem = os.path.splitext(os.path.basename(self.pdf_path))[0]
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
            AppState.get().status_message.emit(f"OK: {len(uids)} {tr('Dateien erstellt')}")
        except Exception as e:
            logging.exception("manage: _split_each failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))

    def _split_n(self):
        from PyQt6.QtWidgets import QInputDialog
        from pypdf import PdfReader, PdfWriter
        n, ok = QInputDialog.getInt(self, tr("Seiten pro Teil"),
                                    tr("Wie viele Seiten pro Datei?"), 1, 1, 9999)
        if not ok: return
        out_dir = QFileDialog.getExistingDirectory(self, tr("Zielordner wählen"))
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
            AppState.get().status_message.emit(f"OK: {chunk} {tr('Dateien erstellt')}")
        except Exception as e:
            logging.exception("manage: _split_n failed")
            AppState.get().status_message.emit(tr('Fehler: {p0}').format(p0=e))