"""
Persisted preferences, and the four dialogs over them — appearance,
performance, prepress and general.
"""
import json
import os
from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
                             QDialog, QRadioButton, QCheckBox, QComboBox,
                             QSpinBox, QFormLayout, QLineEdit, QListWidget,
                             QListWidgetItem)
from PyQt6.QtCore import QSettings, Qt, pyqtSignal
from tools.i18n import tr


def _total_ram_kb() -> int:
    """Read total physical RAM in KB from /proc/meminfo, fallback 4 GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass          # no procfs, or a line that does not parse: use the default
    return 4 * 1024 * 1024


def _ram_cache_bytes(percent: int) -> int:
    """The memory this app may keep rendered pages in, from a share of RAM."""
    return max(64 * 1024 * 1024, _total_ram_kb() * 1024 * percent // 100)


def _thumb_cache_bytes(percent: int) -> int:
    """Of that, the thumbnails' share.

    Capped: a thumbnail is a couple of hundred kilobytes and there are only
    ever a few hundred worth keeping, so past a point more of them buys
    nothing and the rest belongs to whole pages — which is what "how many
    pages stay rendered" means.
    """
    return min(256 * 1024 * 1024, _ram_cache_bytes(percent) // 4)


def _full_page_cache_bytes(percent: int) -> int:
    """And the rest, for whole rendered pages."""
    return _ram_cache_bytes(percent) - _thumb_cache_bytes(percent)


def _ram_percent_to_gb(percent: int) -> float:
    """Convert a RAM-% to the equivalent GB."""
    return _total_ram_kb() * percent / 100 / (1024 * 1024)


class AppSettings:
    """Singleton wrapper around QSettings — persists across restarts."""
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self._qs = QSettings("CopyShop", "PDFSuite")

    # Appearance
    def theme(self) -> str:
        return self._qs.value("appearance/theme", "dark")

    def set_theme(self, val: str):
        self._qs.setValue("appearance/theme", val)

    # Performance
    # Rendering speed preset: "balanced" | "fast" | "max"
    def prerender(self) -> bool:
        return self._qs.value("performance/prerender", True, type=bool)

    def set_prerender(self, val: bool):
        self._qs.setValue("performance/prerender", bool(val))

    def ram_percent(self) -> int:
        return int(self._qs.value("performance/ram_percent", 25))

    def set_ram_percent(self, val: int):
        self._qs.setValue("performance/ram_percent", int(val))

    # Prepress — the shop's press, set once rather than per job
    def pdfx_condition(self) -> str:
        """The output condition the PDF/X export separates against.

        Stored as the profile's label. An empty value means "whatever the
        table lists first", which is the generic entry.
        """
        return self._qs.value("prepress/pdfx_condition", "")

    def set_pdfx_condition(self, val: str):
        self._qs.setValue("prepress/pdfx_condition", val)

    def pdfx_standard(self) -> str:
        """Which PDF/X profile to write: "x4" or "x3".

        X-4 by default. X-3 has to flatten transparency, which rasterises the
        affected pages — slower, larger, and vector artwork stops being vector.
        X-3 is here for a RIP too old to accept X-4, not as a preference.
        """
        return self._qs.value("prepress/pdfx_standard", "x4")

    def set_pdfx_standard(self, val: str):
        self._qs.setValue("prepress/pdfx_standard", val)

    def pdfx_image_dpi(self) -> int:
        """Images above this are downsampled on export, and any page that has
        to be flattened is rendered at it.

        600 rather than the 300 a press images at, because this number does
        double duty: it is also the resolution transparency is flattened to,
        and flattening is the one step that turns vector artwork into pixels.
        300 is enough for a photograph and visibly soft for a flattened logo
        or a hairline. The cost of the extra detail is size and time, both of
        which the operator can trade back here.
        """
        return int(self._qs.value("prepress/pdfx_image_dpi", 600))

    def set_pdfx_image_dpi(self, val: int):
        self._qs.setValue("prepress/pdfx_image_dpi", int(val))

    # How the pages are laid out in the viewer. Off by default: turning one
    # page at a time is what this application has always done, and a document
    # that suddenly scrolls under the reader is not a setting anybody asked for
    # by opening the program.
    def continuous_scroll(self) -> bool:
        return self._qs.value("appearance/continuous_scroll", False, type=bool)

    def set_continuous_scroll(self, val: bool):
        self._qs.setValue("appearance/continuous_scroll", bool(val))

    # General
    def reopen_last(self) -> bool:
        return self._qs.value("general/reopen_last", False, type=bool)

    def set_reopen_last(self, val: bool):
        self._qs.setValue("general/reopen_last", bool(val))

    def last_file(self) -> str:
        return self._qs.value("general/last_file", "")

    # Recent files — the empty window's "Zuletzt geöffnet" row. A JSON list
    # rather than QSettings' own array support: that stores each entry under
    # an indexed key (recent_files/1/path, /2/path, …), which needs its own
    # size-tracking and leaves stale trailing keys behind when the list
    # shrinks. One string, one write, nothing to go stale.
    def recent_files(self) -> list:
        """Up to four most-recently-opened paths, newest first, filtered to
        files that still exist — a removed USB stick should not leave a dead
        card in the empty window."""
        raw = self._qs.value("general/recent_files", "")
        try:
            paths = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            paths = []
        return [p for p in paths if isinstance(p, str) and os.path.isfile(p)][:4]

    def add_recent_file(self, path: str):
        if not path:
            return
        raw = self._qs.value("general/recent_files", "")
        try:
            paths = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            paths = []
        paths = [p for p in paths if p != path]
        paths.insert(0, path)
        self._qs.setValue("general/recent_files", json.dumps(paths[:4]))


def _dlg_section(layout, title):
    lbl = QLabel(tr(title))
    lbl.setObjectName("sectionLabel")
    lbl.setContentsMargins(0, 4, 0, 2)
    layout.addWidget(lbl)


def _dlg_note(text):
    """The dim explanatory line these dialogs put under a section heading."""
    lbl = QLabel(text)
    lbl.setObjectName("dimLabel")
    lbl.setWordWrap(True)
    return lbl


def _dlg_buttons(layout, save_slot):
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    cancel = QPushButton(tr("Abbrechen"))
    cancel.setObjectName("secondaryBtn")
    save = QPushButton(tr("Speichern"))
    save.setObjectName("actionBtn")
    save.clicked.connect(save_slot)
    btn_row.addWidget(cancel)
    btn_row.addWidget(save)
    layout.addLayout(btn_row)

    # Enter saves. Every QPushButton in a QDialog is autoDefault, so Qt made
    # the first one in tab order the default — Cancel, which is added to this
    # row before Save, and in the prepress dialog the "Profil installieren…"
    # button further up the page. Enter threw away the settings just typed, or
    # opened a file picker. Cleared across the whole dialog and granted back to
    # these two, so a control added above cannot silently take it over again.
    dlg = layout.parentWidget()
    if dlg is not None:
        for btn in dlg.findChildren(QPushButton):
            btn.setAutoDefault(False)
            btn.setDefault(False)
    for btn in (cancel, save):
        btn.setAutoDefault(True)
    save.setDefault(True)
    return cancel  # caller connects cancel.clicked.connect(self.reject)


class AppearanceDialog(QDialog):
    theme_changed = pyqtSignal(str)
    scroll_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Darstellung"))
        self.setMinimumWidth(380)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "DARSTELLUNG")

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        lbl = QLabel(tr("Farbschema:"))
        lbl.setMinimumWidth(140)
        theme_row.addWidget(lbl)
        # Two tiny rectangles, one per theme, so the choice is visible before
        # it is saved — a dark and a light preview beside the radios they stand
        # for.
        def _swatch(bg, border):
            w = QLabel()
            w.setFixedSize(20, 20)
            w.setStyleSheet(
                f"background:{bg}; border:1px solid {border}; border-radius:4px;")
            return w
        dark_sw = _swatch("#14181f", "#3a4356")
        light_sw = _swatch("#f7f8fa", "#b8c0ce")
        self._dark_rb  = QRadioButton(tr("Dunkel"))
        self._light_rb = QRadioButton(tr("Hell"))
        if self._s.theme() == "light":
            self._light_rb.setChecked(True)
        else:
            self._dark_rb.setChecked(True)
        theme_row.addWidget(dark_sw)
        theme_row.addWidget(self._dark_rb)
        theme_row.addWidget(light_sw)
        theme_row.addWidget(self._light_rb)
        theme_row.addStretch()
        outer.addLayout(theme_row)

        _dlg_section(outer, "SEITENDARSTELLUNG")
        self._cont_cb = QCheckBox(tr("Seiten fortlaufend scrollen"))
        self._cont_cb.setChecked(self._s.continuous_scroll())
        self._cont_cb.setToolTip(tr(
            "Aus: eine Seite auf einmal, wie bisher.\n"
            "Ein: die Seiten laufen untereinander durch, mit Abstand dazwischen."))
        outer.addWidget(self._cont_cb)

        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _save(self):
        s = self._s
        new_theme = "light" if self._light_rb.isChecked() else "dark"
        changed = (new_theme != s.theme())
        s.set_theme(new_theme)
        if changed:
            self.theme_changed.emit(new_theme)

        new_cont = self._cont_cb.isChecked()
        if new_cont != s.continuous_scroll():
            s.set_continuous_scroll(new_cont)
            self.scroll_changed.emit(new_cont)
        self.accept()


class PerformanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Leistung"))
        self.setMinimumWidth(500)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "LEISTUNG")

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)

        self._prerender_cb = QCheckBox(tr("Seiten im Hintergrund vorab rendern"))
        self._prerender_cb.setChecked(self._s.prerender())
        prerender_lbl = QLabel(tr("Vorab-Rendering:"))
        prerender_lbl.setMinimumWidth(140)
        form.addRow(prerender_lbl, self._prerender_cb)

        self._ram_spin = QSpinBox()
        self._ram_spin.setRange(5, 80)
        self._ram_spin.setSingleStep(5)
        self._ram_spin.setValue(self._s.ram_percent())
        self._ram_spin.setSuffix("  % RAM")
        self._ram_spin.setMaximumWidth(140)
        self._ram_hint = QLabel()
        self._ram_hint.setObjectName("dimLabel")
        self._ram_spin.valueChanged.connect(self._update_ram_hint)
        self._update_ram_hint(self._ram_spin.value())
        cache_col = QVBoxLayout()
        cache_col.setSpacing(2)
        cache_col.addWidget(self._ram_spin)
        cache_col.addWidget(self._ram_hint)
        ram_lbl = QLabel(tr("Seitenspeicher:"))
        ram_lbl.setMinimumWidth(140)
        form.addRow(ram_lbl, cache_col)

        outer.addLayout(form)
        note = QLabel(tr(
            "Gerendert wird in einem Thread: pdfium ist nicht threadsicher, und\n"
            "alle Aufrufe teilen sich eine Sperre. Mehr Threads wären hier ohne\n"
            "Wirkung."))
        note.setObjectName("dimLabel")
        outer.addWidget(note)
        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _update_ram_hint(self, pct: int):
        """What that share of RAM buys, in the terms the user is choosing in.

        The number of pages is not a setting because it is not a constant: it
        is the budget divided by what a page of *this* document costs, and a
        poster costs twenty times a paperback. The example is a page rendered
        to a normal window, which is what most of them are."""
        typical = 2.5 * 1024 * 1024
        pages = int(_full_page_cache_bytes(pct) / typical)
        self._ram_hint.setText(tr(
            "≈ {p0:.1f} GB — etwa {p1} Seiten üblicher Größe").format(
                p0=_ram_percent_to_gb(pct), p1=pages))

    def _save(self):
        s = self._s
        s.set_prerender(self._prerender_cb.isChecked())
        s.set_ram_percent(self._ram_spin.value())
        from tools.render.queue import apply_performance_settings
        apply_performance_settings(
            prerender       = s.prerender(),
            thumb_bytes     = _thumb_cache_bytes(s.ram_percent()),
            full_page_bytes = _full_page_cache_bytes(s.ram_percent()),
        )
        self.accept()


class PrepressDialog(QDialog):
    """The press the shop prints on, and how much resolution it can use.

    These live here rather than on the PDF/X panel because they are a property
    of the press, not of the job: set once, then every export is one button.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Druckvorstufe"))
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()
        # An explicit width floor must be read AFTER _build: a hardcoded
        # number below the layout's own minimum lets rows squeeze below their
        # content — the Standard combo clipped its PDF/X-4 label at 560px.
        self.setMinimumWidth(self.minimumSizeHint().width())

    def _build(self):
        from tools.panels._icc import CMYK_PROFILES, resolve_icc

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "DRUCKVORSTUFE")

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)

        from tools.panels._prepress import PDFX_STANDARDS, standard_of
        self._std_combo = QComboBox()
        current_std = standard_of(self._s.pdfx_standard())[0]
        for key in ("x4", "x3"):
            self._std_combo.addItem(tr(PDFX_STANDARDS[key][1]), key)
            if key == current_std:
                self._std_combo.setCurrentIndex(self._std_combo.count() - 1)
        self._std_combo.setMaximumWidth(400)
        std_lbl = QLabel(tr("Standard:")); std_lbl.setMinimumWidth(140)
        form.addRow(std_lbl, self._std_combo)

        self._cond_combo = QComboBox()
        # Capped: the profile labels carry their paper and use-case, and an
        # uncapped combo takes the whole row width and clips the field label.
        self._cond_combo.setMaximumWidth(400)
        current = self._s.pdfx_condition()
        for label, candidates, _oci, _condition in CMYK_PROFILES:
            self._cond_combo.addItem(tr(label), label)
            if label == current:
                self._cond_combo.setCurrentIndex(self._cond_combo.count() - 1)
        self._cond_combo.currentIndexChanged.connect(self._update_profile_hint)
        cond_lbl = QLabel(tr("Ausgabebedingung:")); cond_lbl.setMinimumWidth(140)
        form.addRow(cond_lbl, self._cond_combo)

        self._dpi_spin = QSpinBox()
        self._dpi_spin.setRange(72, 2400)
        self._dpi_spin.setSingleStep(50)
        self._dpi_spin.setValue(self._s.pdfx_image_dpi())
        self._dpi_spin.setSuffix("  dpi")
        self._dpi_spin.setMaximumWidth(140)
        dpi_lbl = QLabel(tr("Bildauflösung:")); dpi_lbl.setMinimumWidth(140)
        form.addRow(dpi_lbl, self._dpi_spin)
        outer.addLayout(form)

        self._prof_hint = QLabel()
        self._prof_hint.setObjectName("dimLabel")
        self._prof_hint.setWordWrap(True)
        outer.addWidget(self._prof_hint)

        install_row = QHBoxLayout()
        install_btn = QPushButton(tr("Profil installieren…"))
        install_btn.setObjectName("secondaryBtn")
        install_btn.clicked.connect(self._install)
        install_row.addWidget(install_btn)
        install_row.addStretch()
        outer.addLayout(install_row)

        note = QLabel(tr(
            "Gilt für zwei Dinge: Bilder über diesem Wert werden reduziert\n"
            "(darunterliegende bleiben unverändert), und Seiten mit Transparenz\n"
            "werden mit dieser Auflösung in Pixel umgewandelt. Vektoren und\n"
            "Schrift bleiben davon unberührt und bleiben in jeder Größe scharf.\n"
            "Höher heißt größere Dateien und längere Exportzeiten."))
        note.setObjectName("dimLabel")
        outer.addWidget(note)

        _dlg_section(outer, "PAPIERFORMATE")
        outer.addWidget(_dlg_note(tr(
            "Welche Formate in allen Werkzeugen und im Druckdialog zur Auswahl "
            "stehen. Abgewählte werden nirgends mehr angeboten; eigene "
            "Formate erscheinen überall, wo ein Format gewählt wird.")))
        outer.addWidget(self._build_paper_list())

        # The "add a paper" form, on two lines so the fields keep their width
        # even at the dialog's minimum width: the name + dimensions on the
        # first, the two actions on the second.
        add_fields = QHBoxLayout()
        add_fields.setSpacing(6)
        self._paper_name = QLineEdit()
        self._paper_name.setPlaceholderText(tr("Name"))
        self._paper_name.setMaximumWidth(150)
        self._paper_w = QSpinBox(); self._paper_w.setRange(1, 5000)
        self._paper_w.setValue(320); self._paper_w.setSuffix(" mm")
        self._paper_h = QSpinBox(); self._paper_h.setRange(1, 5000)
        self._paper_h.setValue(450); self._paper_h.setSuffix(" mm")
        add_fields.addWidget(self._paper_name)
        add_fields.addWidget(self._paper_w)
        add_fields.addWidget(QLabel("×"))
        add_fields.addWidget(self._paper_h)
        add_fields.addStretch()
        outer.addLayout(add_fields)

        add_actions = QHBoxLayout()
        add_actions.setSpacing(6)
        add_btn = QPushButton(tr("Hinzufügen"))
        add_btn.setObjectName("secondaryBtn")
        add_btn.clicked.connect(self._add_paper)
        del_btn = QPushButton(tr("Eigenes entfernen"))
        del_btn.setObjectName("secondaryBtn")
        del_btn.clicked.connect(self._remove_paper)
        del_btn.setEnabled(False)   # greyed until a custom size is selected
        self._paper_del_btn = del_btn
        add_actions.addWidget(add_btn)
        add_actions.addWidget(del_btn)
        add_actions.addStretch()
        outer.addLayout(add_actions)
        self._paper_msg = QLabel("")
        self._paper_msg.setObjectName("dimLabel")
        self._paper_msg.setWordWrap(True)
        outer.addWidget(self._paper_msg)

        outer.addStretch()
        self._resolve_icc = resolve_icc
        self._update_profile_hint()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _build_paper_list(self):
        """One row per size, ticked when it is offered.

        Built-ins are hidden rather than deleted — a size that ships is always
        recoverable, and a job or a queue that still names one must keep
        resolving even while it is off the dropdowns.
        """
        import tools.paper as paper
        self._paper_list = QListWidget()
        self._paper_list.setMaximumHeight(190)
        hidden = paper.hidden_names()
        custom = paper.custom_sizes()
        for name in paper.builtin_names() + sorted(custom):
            item = QListWidgetItem(paper.label(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if name in hidden
                               else Qt.CheckState.Checked)
            if name in custom:
                item.setToolTip(tr("Eigenes Format"))
            self._paper_list.addItem(item)
        self._paper_list.currentItemChanged.connect(self._update_remove_state)
        self._update_remove_state()
        return self._paper_list

    def _reload_paper_list(self):
        row = self._paper_list.currentRow()
        self._paper_list.clear()
        import tools.paper as paper
        hidden = paper.hidden_names()
        custom = paper.custom_sizes()
        for name in paper.builtin_names() + sorted(custom):
            item = QListWidgetItem(paper.label(name))
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if name in hidden
                               else Qt.CheckState.Checked)
            if name in custom:
                item.setToolTip(tr("Eigenes Format"))
            self._paper_list.addItem(item)
        self._paper_list.setCurrentRow(min(row, self._paper_list.count() - 1))
        self._update_remove_state()

    def _update_remove_state(self):
        """`Eigenes entfernen` only works on the shop's own sizes.

        Enabled exactly when a custom (user-added) size is selected; a built-in
        is hidden with its tick, never deleted, so picking one greys the button.
        """
        import tools.paper as paper
        item = self._paper_list.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        btn = getattr(self, "_paper_del_btn", None)
        if btn is not None:
            btn.setEnabled(name is not None and name in paper.custom_sizes())

    def _add_paper(self):
        import tools.paper as paper
        try:
            name = paper.add_custom(self._paper_name.text(),
                                    self._paper_w.value(),
                                    self._paper_h.value())
        except ValueError as e:
            self._paper_msg.setText(str(e))
            return
        self._paper_name.clear()
        self._reload_paper_list()
        self._paper_msg.setText(
            tr("{p0} hinzugefügt.").format(p0=paper.label(name)))

    def _remove_paper(self):
        """Only the shop's own sizes can be removed; a built-in is unticked."""
        import tools.paper as paper
        item = self._paper_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if name not in paper.custom_sizes():
            self._paper_msg.setText(tr(
                "Mitgelieferte Formate lassen sich nicht löschen — "
                "das Häkchen entfernen blendet sie überall aus."))
            return
        paper.remove_custom(name)
        self._reload_paper_list()
        self._paper_msg.setText(tr("{p0} entfernt.").format(p0=name))

    def _save_paper_visibility(self):
        import tools.paper as paper
        for i in range(self._paper_list.count()):
            item = self._paper_list.item(i)
            paper.set_hidden(item.data(Qt.ItemDataRole.UserRole),
                             item.checkState() == Qt.CheckState.Unchecked)

    def _selected_row(self):
        from tools.panels._icc import profile_by_key
        return profile_by_key(self._cond_combo.currentData())

    def _update_profile_hint(self):
        """Say whether the chosen condition has its profile, because the
        export quietly falls back to generic when it does not — and a file
        separated generically under a named condition is a false claim."""
        _label, candidates, _oci, _cond = self._selected_row()
        if not candidates:
            self._prof_hint.setText(tr(
                "Generisches CMYK — kein ICC-Profil nötig."))
            return
        found = self._resolve_icc(candidates)
        if found:
            self._prof_hint.setText(tr("✓  Profil installiert: {p0}").format(
                p0=os.path.basename(found)))
        else:
            self._prof_hint.setText(tr(
                "✗  Profil fehlt ({p0}). Der Export nutzt so lange generisches "
                "CMYK und vermerkt das im Bericht. Mit „Profil installieren…“ "
                "eine heruntergeladene .icc-Datei hinzufügen.").format(
                    p0=candidates[0]))

    def _install(self):
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from tools.panels._icc import (install_profile, icc_colour_space,
                                       profile_description)
        label, candidates, _oci, _cond = self._selected_row()
        if not candidates:
            QMessageBox.information(
                self, tr("Kein Profil nötig"),
                tr("Die generische Ausgabebedingung braucht keine ICC-Datei."))
            return
        path, _ = QFileDialog.getOpenFileName(
            self, tr("ICC-Profil auswählen"), os.path.expanduser("~"),
            "ICC (*.icc *.icm)")
        if not path:
            return
        space = icc_colour_space(path)
        if space != "CMYK":
            QMessageBox.warning(
                self, tr("Kein CMYK-Profil"),
                tr("Diese Datei ist kein CMYK-Profil ({p0}). Ein Profil in "
                   "einem anderen Farbraum würde jeden Export falsch "
                   "separieren.").format(p0=space or tr("unlesbar")))
            return
        described = profile_description(path) or os.path.basename(path)
        if QMessageBox.question(
                self, tr("Profil installieren"),
                tr('„{p0}“ als Profil für {p1} installieren?').format(
                    p0=described, p1=label.split(" — ")[0]),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes) != QMessageBox.StandardButton.Yes:
            return
        try:
            dest = install_profile(path, candidates[0])
        except (ValueError, OSError) as e:
            QMessageBox.warning(self, tr("Installation fehlgeschlagen"), str(e))
            return
        self._update_profile_hint()
        QMessageBox.information(
            self, tr("Profil installiert"),
            tr("Installiert nach {p0}").format(p0=dest))

    def _save(self):
        self._s.set_pdfx_standard(self._std_combo.currentData())
        self._s.set_pdfx_condition(self._cond_combo.currentData())
        self._s.set_pdfx_image_dpi(self._dpi_spin.value())
        # Adding and removing a size take effect at once — they are their own
        # buttons. Which sizes are offered is a form field like the rest, and
        # is written when Speichern is pressed.
        self._save_paper_visibility()
        self.accept()


class GeneralDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Allgemein"))
        self.setMinimumWidth(380)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "ALLGEMEIN")

        self._reopen_cb = QCheckBox(tr("Letzte Datei beim Programmstart automatisch öffnen"))
        self._reopen_cb.setChecked(self._s.reopen_last())
        outer.addWidget(self._reopen_cb)

        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _save(self):
        self._s.set_reopen_last(self._reopen_cb.isChecked())
        self.accept()
