"""Plugin Manager v3"""
import os, sys, importlib.util
from PyQt6.QtWidgets import (
    QVBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QGroupBox, QLabel
)
from PyQt6.QtCore import Qt
from tools.panels.base import BasePanel, make_label, section_header
from tools.i18n import tr

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugins")


def discover_plugins(plugin_dir=PLUGIN_DIR):
    os.makedirs(plugin_dir, exist_ok=True)
    results = []
    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py") or fname.startswith("_"): continue
        fpath = os.path.join(plugin_dir, fname)
        try:
            spec   = importlib.util.spec_from_file_location(f"plugins.{fname[:-3]}", fpath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for attr in dir(module):
                obj = getattr(module, attr)
                if (isinstance(obj, type) and issubclass(obj, BasePanel)
                        and obj is not BasePanel and hasattr(obj, "PLUGIN_NAME")):
                    icon  = getattr(obj, "PLUGIN_ICON", "Plugin")
                    results.append((f"{icon}  {obj.PLUGIN_NAME}", obj))
                    break
        except Exception as exc:
            print(f"[Plugin] {fname}: {exc}", file=sys.stderr)
    return results


class PluginManagerPanel(BasePanel):
    TITLE    = "Plugin-Manager"
    SUBTITLE = "Eigene Werkzeuge installieren und verwalten."

    def build_ui(self, layout):
        ib = QGroupBox(tr("WIE PLUGINS FUNKTIONIEREN"))
        il = QVBoxLayout(ib)
        # The folder path is long enough to wrap and clip inside the box, so it
        # gets its own word-wrapped label in a smaller monospace face that fits.
        path_lbl = QLabel(tr("Plugin-Ordner:"))
        path_lbl.setObjectName("dimLabel")
        path_lbl.setWordWrap(True)
        path_lbl.setTextFormat(Qt.TextFormat.PlainText)
        path_lbl.setStyleSheet(
            "font-family:'JetBrains Mono','DejaVu Sans Mono',monospace;"
            "font-size:10px;padding:2px 0;")
        il.addWidget(path_lbl)
        path_val = QLabel(PLUGIN_DIR)
        path_val.setObjectName("dimLabel")
        path_val.setWordWrap(True)
        path_val.setStyleSheet(
            "font-family:'JetBrains Mono','DejaVu Sans Mono',monospace;"
            "font-size:10px;padding:0 0 4px 0;")
        il.addWidget(path_val)
        il.addWidget(make_label(tr(
            "Lege eine .py-Datei mit einer BasePanel-Unterklasse und PLUGIN_NAME "
            "in den Plugin-Ordner. Nach einem Neustart erscheint das Plugin in der Seitenleiste."), dim=True))
        ob = QPushButton(tr("Plugin-Ordner öffnen"))
        ob.setObjectName("secondaryBtn"); ob.clicked.connect(self._open_folder); il.addWidget(ob)
        layout.addWidget(ib)
        layout.addWidget(section_header(tr("INSTALLIERTE PLUGINS")))
        self.plugin_list = QListWidget(); self.plugin_list.setMinimumHeight(120)
        self.plugin_list.currentItemChanged.connect(self._show_details)
        layout.addWidget(self.plugin_list)
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setMaximumHeight(90)
        self.detail.setPlaceholderText(tr("Plugin auswählen für Details..."))
        layout.addWidget(self.detail)
        self._refresh()

    def build_action_row(self, row):
        ib = QPushButton(tr("Plugin installieren (.py)..."))
        ib.setObjectName("secondaryBtn"); ib.clicked.connect(self._install); row.addWidget(ib)
        row.addStretch()
        rb = QPushButton(tr("Liste aktualisieren"))
        rb.setObjectName("actionBtn"); rb.clicked.connect(self._refresh); row.addWidget(rb)

    def _refresh(self):
        self.plugin_list.clear()
        plugins = discover_plugins()
        if not plugins:
            self.plugin_list.addItem(tr("  (keine Plugins installiert)")); return
        for label, cls in plugins:
            item = QListWidgetItem("  " + label.strip())
            item.setData(Qt.ItemDataRole.UserRole, cls)
            self.plugin_list.addItem(item)

    def _show_details(self, current, _):
        if not current: return
        cls = current.data(Qt.ItemDataRole.UserRole)
        if not cls: return
        self.detail.setPlainText(
            tr("Name:    {p0}\nTitel:   {p1}\nInfo:    {p2}").format(
                p0=getattr(cls,'PLUGIN_NAME','—'),
                p1=getattr(cls,'TITLE','—'),
                p2=getattr(cls,'SUBTITLE','—')))

    def _install(self):
        from PyQt6.QtWidgets import QMessageBox
        src, _ = QFileDialog.getOpenFileName(self,tr("Plugin auswählen"),"",tr("Python-Dateien (*.py)"))
        if not src: return
        import shutil
        dest = os.path.join(PLUGIN_DIR, os.path.basename(src))
        # Every step here can fail on a real machine — an unreadable source, a
        # read-only plugin folder — and an exception in a slot takes the app
        # down rather than reporting a failed install.
        if os.path.abspath(src) == os.path.abspath(dest):
            self.log.log(tr("Plugin ist bereits installiert."))
            return
        if os.path.exists(dest):
            if QMessageBox.question(
                    self, tr("Plugin ersetzen?"),
                    tr('"{p0}" ist bereits installiert. Ersetzen?').format(
                        p0=os.path.basename(dest))) != QMessageBox.StandardButton.Yes:
                return
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            shutil.copy2(src, dest)
        except Exception as e:
            self.log.log(tr('Installation fehlgeschlagen: {p0}').format(p0=e), error=True)
            QMessageBox.warning(self, tr("Installation fehlgeschlagen"), str(e))
            return
        self.log.log(tr("Installiert. Nach Neustart aktiv."))
        self._refresh()

    def _open_folder(self):
        import subprocess
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            subprocess.Popen(["xdg-open", PLUGIN_DIR])
        except Exception as e:
            # No xdg-open on a bare desktop, or the folder cannot be created.
            self.log.log(tr('Ordner konnte nicht geöffnet werden: {p0}\n{p1}').format(
                p0=PLUGIN_DIR, p1=e), error=True)

    def _run_action(self): return tr("Buttons oben verwenden.")
