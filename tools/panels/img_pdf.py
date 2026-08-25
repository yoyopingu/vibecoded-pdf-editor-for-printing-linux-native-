"""
Bild zu/von PDF — images into a PDF, and PDF pages back out as images.
"""
import os
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QSpinBox, QComboBox, QGroupBox
from tools.panels.base import BasePanel, FileDropList, make_label
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
        tl.addWidget(make_label(tr("Bilder hinzufügen. Reihenfolge per Drag & Drop."), dim=True))
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

        fb = QGroupBox(tr("PDF  nach  BILDER  (aktuell geöffnete PDF)")); fl = QVBoxLayout(fb)
        self.fmt = QComboBox(); self.fmt.addItems(["PNG","JPEG","TIFF"])
        fl.addLayout(row(tr("Ausgabeformat:"), self.fmt))
        self.dpi = QSpinBox(); self.dpi.setRange(72,600); self.dpi.setValue(300); self.dpi.setSuffix(" dpi")
        fl.addLayout(row(tr("Auflösung:"), self.dpi))
        eb = QPushButton(tr("  Seiten als Bilder exportieren...")); eb.setObjectName("actionBtn")
        eb.clicked.connect(self._to_img); fl.addWidget(eb)
        layout.addWidget(fb)

    def build_action_row(self, r):
        # No general "run" here: this panel's two actions live in their own
        # group boxes. The image export renders every page, though, which is
        # minutes on a long document — so the way out still belongs on screen.
        r.addStretch()
        self.add_stop_button(r)

    def _add_imgs(self):
        paths = self.pick_images()
        if paths: self.img_list.add_files(paths)

    def _to_pdf(self):
        # img2pdf.convert() re-encodes every image, which used to run right
        # here on the GUI thread — a window that could not repaint for as long
        # as the largest batch of scans took. run_async moves that off the GUI
        # thread and gives it the same busy state (button disabled, Stop shown)
        # every other tool's action has; img2pdf itself has no checkpoint to
        # respond mid-convert, so Stop can only catch it before or after, not
        # during — no worse than before, where there was no Stop at all.
        paths = self.img_list.get_paths()
        if not paths: self.log.log(tr("Bilder hinzufügen."), error=True); return
        out = self.save_pdf("Als PDF speichern")
        if not out: return
        self.log.clear_log()
        self.run_async(
            lambda report: _images_to_pdf(paths, out, report),
            on_done=self._to_pdf_done,
            busy_label="PDF wird erstellt …",
        )

    def _to_pdf_done(self, result):
        out, n = result
        self.log.log(tr('PDF aus {p0} Bildern erstellt').format(p0=n))
        self.open_result(out, tr("Aus Bildern"))

    def _to_img(self):
        # Rasterising every page at the chosen dpi is the slowest thing this
        # panel does, and it used to run right here with processEvents() pumped
        # between batches — which repaints but leaves the window unusable and
        # gives no way to stop.
        try: src = self.require_pdf()
        except ValueError as e: self.log.log(str(e), error=True); return
        out_dir = self.save_dir()
        if not out_dir: return
        self.log.clear_log()
        fmt = self.fmt.currentText().lower()
        dpi = self.dpi.value()

        self.run_async(
            lambda report: _pdf_to_images(src, out_dir, fmt, dpi, report),
            on_done=lambda n: self.log.log(tr('{p0} Bilder exportiert').format(p0=n)),
            busy_label="Bilder werden exportiert …",
        )

    def _run_action(self): pass


def _images_to_pdf(paths, out, report):
    """Encode `paths` into one PDF at `out`, on a worker thread. Returns
    (out, n_images)."""
    import img2pdf
    report(tr("Bilder werden zusammengeführt …"))
    with open(out, "wb") as f:
        f.write(img2pdf.convert(paths))
    return out, len(paths)


def _pdf_to_images(src, out_dir, fmt, dpi, report):
    """Render every page of `src` into `out_dir`. Returns how many."""
    from pdf2image import convert_from_path
    from pypdf import PdfReader
    ext = {"jpeg": "jpg"}.get(fmt, fmt)
    n_pages = len(PdfReader(src, strict=False).pages)
    stem = os.path.splitext(os.path.basename(src))[0]
    # In batches of ten: convert_from_path holds every page it produced in
    # memory at once, and a long document at 300 dpi does not fit.
    for i in range(0, n_pages, 10):
        report.check()          # between batches, before the next ten renders
        end = min(i + 10, n_pages)
        report(tr('Seite {i} / {total}…').format(i=end, total=n_pages))
        pages = convert_from_path(src, dpi=dpi, first_page=i + 1, last_page=end)
        for j, img in enumerate(pages):
            img.save(os.path.join(out_dir, f"{stem}_s{i+j+1:03d}.{ext}"), fmt.upper())
    return n_pages
