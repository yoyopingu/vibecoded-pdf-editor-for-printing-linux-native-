"""
Persisted preferences, and the three dialogs over them — appearance,
performance and general.
"""
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QDialog, QRadioButton, QCheckBox, QSpinBox, QFormLayout
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


def _ram_percent_to_pages(percent: int) -> int:
    """Convert a RAM-% to a thumbnail cache page count (~200 KB per thumb)."""
    allowed_kb = _total_ram_kb() * percent // 100
    return max(50, allowed_kb // 200)


def _ram_percent_to_full_pages(percent: int) -> int:
    """Convert a RAM-% to a full-page render cache count.
    Each full-page render is roughly 70 MB (4000 x 5660 px RGB).
    We reserve 1/4 of the allowed RAM for this cache and cap at 12
    so the app never monopolises memory on large files.
    """
    allowed_kb = _total_ram_kb() * percent // 100
    quarter_kb = allowed_kb // 4
    return max(2, min(12, quarter_kb // (70 * 1024)))


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
        form.addRow(tr("Thumbnail-Cache:"), cache_col)

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
        self._ram_hint.setText(f"≈ {_ram_percent_to_gb(pct):.1f} GB")

    def _save(self):
        s = self._s
        s.set_prerender(self._prerender_cb.isChecked())
        s.set_ram_percent(self._ram_spin.value())
        from tools.render.queue import apply_performance_settings
        apply_performance_settings(
            prerender       = s.prerender(),
            cache_size      = _ram_percent_to_pages(s.ram_percent()),
            full_page_cache = _ram_percent_to_full_pages(s.ram_percent()),
        )
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
