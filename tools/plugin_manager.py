"""Plugin Manager v3"""
import os, sys, importlib.util
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QListWidget, QListWidgetItem, QTextEdit, QFileDialog, QGroupBox
)
from PyQt6.QtCore import Qt
from tools._base import BasePanel, make_label

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
        ib = QGroupBox("WIE PLUGINS FUNKTIONIEREN")
        il = QVBoxLayout(ib)
        il.addWidget(make_label(f"Plugin-Ordner:  {PLUGIN_DIR}", dim=True))
        il.addWidget(make_label(
            "Lege eine .py-Datei mit einer BasePanel-Unterklasse und PLUGIN_NAME "
            "in den Plugin-Ordner. Nach einem Neustart erscheint das Plugin in der Seitenleiste.", dim=True))
        ob = QPushButton("Plugin-Ordner öffnen")
        ob.setObjectName("secondaryBtn"); ob.clicked.connect(self._open_folder); il.addWidget(ob)
        layout.addWidget(ib)
        layout.addWidget(make_label("Installierte Plugins:", dim=True))
        self.plugin_list = QListWidget(); self.plugin_list.setMinimumHeight(120)
        self.plugin_list.currentItemChanged.connect(self._show_details)
        layout.addWidget(self.plugin_list)
        self.detail = QTextEdit(); self.detail.setReadOnly(True); self.detail.setMaximumHeight(90)
        self.detail.setPlaceholderText("Plugin auswählen für Details...")
        layout.addWidget(self.detail)
        self._refresh()

    def build_action_row(self, row):
        ib = QPushButton("Plugin installieren (.py)...")
        ib.setObjectName("secondaryBtn"); ib.clicked.connect(self._install); row.addWidget(ib)
        row.addStretch()
        rb = QPushButton("Liste aktualisieren")
        rb.setObjectName("actionBtn"); rb.clicked.connect(self._refresh); row.addWidget(rb)

    def _refresh(self):
        self.plugin_list.clear()
        plugins = discover_plugins()
        if not plugins:
            self.plugin_list.addItem("  (keine Plugins installiert)"); return
        for label, cls in plugins:
            item = QListWidgetItem("  " + label.strip())
            item.setData(Qt.ItemDataRole.UserRole, cls)
            self.plugin_list.addItem(item)

    def _show_details(self, current, _):
        if not current: return
        cls = current.data(Qt.ItemDataRole.UserRole)
        if not cls: return
        self.detail.setPlainText(
            f"Name:    {getattr(cls,'PLUGIN_NAME','—')}\n"
            f"Titel:   {getattr(cls,'TITLE','—')}\n"
            f"Info:    {getattr(cls,'SUBTITLE','—')}")

    def _install(self):
        src, _ = QFileDialog.getOpenFileName(self,"Plugin auswählen","","Python-Dateien (*.py)")
        if not src: return
        import shutil; os.makedirs(PLUGIN_DIR, exist_ok=True)
        shutil.copy2(src, os.path.join(PLUGIN_DIR, os.path.basename(src)))
        self.log.log(f"Installiert. Nach Neustart aktiv.")
        self._refresh()

    def _open_folder(self):
        import subprocess; os.makedirs(PLUGIN_DIR, exist_ok=True)
        subprocess.Popen(["xdg-open", PLUGIN_DIR])

    def _run_action(self): return "Buttons oben verwenden."
