"""
Persisted preferences, and the four dialogs over them — appearance,
performance, prepress and general.
"""
import os
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QDialog, QRadioButton, QCheckBox, QComboBox, QSpinBox, QFormLayout
from PyQt6.QtCore import QSettings, pyqtSignal
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

    # General
    def reopen_last(self) -> bool:
        return self._qs.value("general/reopen_last", False, type=bool)

    def set_reopen_last(self, val: bool):
        self._qs.setValue("general/reopen_last", bool(val))

    def last_file(self) -> str:
        return self._qs.value("general/last_file", "")


def _dlg_section(layout, title):
    lbl = QLabel(tr(title))
    lbl.setObjectName("sectionLabel")
    lbl.setContentsMargins(0, 4, 0, 2)
    layout.addWidget(lbl)


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
    return cancel  # caller connects cancel.clicked.connect(self.reject)


class AppearanceDialog(QDialog):
    theme_changed = pyqtSignal(str)

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
        theme_row.setSpacing(20)
        lbl = QLabel(tr("Farbschema:"))
        lbl.setMinimumWidth(160)
        theme_row.addWidget(lbl)
        self._dark_rb  = QRadioButton(tr("Dunkel"))
        self._light_rb = QRadioButton(tr("Hell"))
        if self._s.theme() == "light":
            self._light_rb.setChecked(True)
        else:
            self._dark_rb.setChecked(True)
        theme_row.addWidget(self._dark_rb)
        theme_row.addWidget(self._light_rb)
        theme_row.addStretch()
        outer.addLayout(theme_row)

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
        form.addRow(tr("Vorab-Rendering:"), self._prerender_cb)

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
        form.addRow(tr("Seitenspeicher:"), cache_col)

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
        self.setMinimumWidth(560)
        self.setModal(True)
        self._s = AppSettings.get()
        self._build()

    def _build(self):
        from tools.panels._icc import CMYK_PROFILES, resolve_icc

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(10)

        _dlg_section(outer, "DRUCKVORSTUFE")

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(10)

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
        form.addRow(tr("Ausgabebedingung:"), self._cond_combo)

        self._dpi_spin = QSpinBox()
        self._dpi_spin.setRange(72, 2400)
        self._dpi_spin.setSingleStep(50)
        self._dpi_spin.setValue(self._s.pdfx_image_dpi())
        self._dpi_spin.setSuffix("  dpi")
        self._dpi_spin.setMaximumWidth(140)
        form.addRow(tr("Bildauflösung:"), self._dpi_spin)
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
        outer.addStretch()
        self._resolve_icc = resolve_icc
        self._update_profile_hint()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

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
        self._s.set_pdfx_condition(self._cond_combo.currentData())
        self._s.set_pdfx_image_dpi(self._dpi_spin.value())
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

        self._reopen_cb = QCheckBox(tr("Letzte Datei beim Programmstart automatisch oeffnen"))
        self._reopen_cb.setChecked(self._s.reopen_last())
        outer.addWidget(self._reopen_cb)

        outer.addStretch()
        cancel = _dlg_buttons(outer, self._save)
        cancel.clicked.connect(self.reject)

    def _save(self):
        self._s.set_reopen_last(self._reopen_cb.isChecked())
        self.accept()
