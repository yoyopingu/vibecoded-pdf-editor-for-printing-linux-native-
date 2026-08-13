"""
Bild zu/von PDF — images into a PDF, and PDF pages back out as images.
"""
import os
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QComboBox, QGroupBox, QApplication
from tools._base import BasePanel, FileDropList, make_label
from tools.i18n import tr
from tools.panels._shared import row


# ══════════════════════════════════════════════════════════════════════════════
# IMAGE <-> PDF
# ══════════════════════════════════════════════════════════════════════════════
class ImgPdfPanel(BasePanel):
    TITLE    = "Bild zu/von PDF"
    SUBTITLE = "Bilder in PDF umwandeln oder PDF-Seiten als Bilder exportieren."

    def build_ui(self, layout):
        tb = QGroupBox(tr("BILDER  nach  PDF")); tl = QVBoxLayout(tb)
        tl.addWidget(make_label(tr("Bilder hinzufuegen. Reihenfolge per Drag & Drop."), dim=True))
        self.img_list = FileDropList(extensions=("*.png","*.jpg","*.jpeg","*.tiff","*.tif","*.bmp","*.webp"))
        tl.addWidget(self.img_list)
        br = QHBoxLayout()
        for txt, fn in [(tr("+ Bilder"), self._add_imgs), (tr("- Entfernen"), self.img_list.remove_selected),
                        (tr("Hoch"), self.img_list.move_up), (tr("Runter"), self.img_list.move_down)]:
            b = QPushButton(txt); b.setObjectName("secondaryBtn"); b.clicked.connect(fn); br.addWidget(b)
        br.addStretch(); tl.addLayout(br)
        cb = QPushButton(tr("  In PDF umwandeln...")); cb.setObjectName("actionBtn")
        cb.clicked.connect(self._to_pdf); tl.addWidget(cb)
        layout.addWidget(tb)

        fb = QGroupBox(tr("PDF  nach  BILDER  (aktuell geoeffnete PDF)")); fl = QVBoxLayout(fb)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG","JPEG","TIFF"])
        fl.addLayout(row(tr("Ausgabeformat:"), self.fmt))
        self.dpi = QSpinBox(); self.dpi.setRange(72,600); self.dpi.setValue(300); self.dpi.setSuffix(" dpi")
        fl.addLayout(row(tr("Aufloesung:"), self.dpi))
        eb = QPushButton(tr("  Seiten als Bilder exportieren...")); eb.setObjectName("actionBtn")
        eb.clicked.connect(self._to_img); fl.addWidget(eb)
        layout.addWidget(fb)

    def build_action_row(self, r): pass

    def _add_imgs(self):
        paths = self.pick_images()
        if paths: self.img_list.add_files(paths)

    def _to_pdf(self):
        import img2pdf
        paths = self.img_list.get_paths()
        if not paths: self.log.log(tr("Bilder hinzufuegen."), error=True); return
        out = self.save_pdf("Als PDF speichern")
        if not out: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            with open(out, "wb") as f: f.write(img2pdf.convert(paths))
            self.log.log(tr('PDF aus {p0} Bildern erstellt').format(p0=len(paths)))
            self.open_result(out, tr("Aus Bildern"))
        except Exception as e: self.log.log(str(e), error=True)

    def _to_img(self):
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log(); QApplication.processEvents()
        try:
            from pdf2image import convert_from_path
            from pypdf import PdfReader
            fmt=self.fmt.currentText().lower(); ext={"jpeg":"jpg"}.get(fmt,fmt)
            n_pages = len(PdfReader(src, strict=False).pages)
            stem=os.path.splitext(os.path.basename(src))[0]
            for i in range(0, n_pages, 10):
                QApplication.processEvents()
                end = min(i+10, n_pages)
                pages=convert_from_path(src, dpi=self.dpi.value(), first_page=i+1, last_page=end)
                for j, img in enumerate(pages):
                    img.save(os.path.join(out_dir, f"{stem}_s{i+j+1:03d}.{ext}"), fmt.upper())
            self.log.log(tr('{p0} Bilder exportiert').format(p0=n_pages))
        except Exception as e: self.log.log(str(e), error=True)

    def _run_action(self): pass
