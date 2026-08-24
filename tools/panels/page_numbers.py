"""
Seitenzahlen — stamp page numbers onto the pages.
"""
import io
from PyQt6.QtWidgets import QVBoxLayout, QLineEdit, QSpinBox, QComboBox, QGroupBox
from tools.panels.base import BasePanel
from tools.i18n import tr
from tools.panels._shared import row


# ══════════════════════════════════════════════════════════════════════════════
# PAGE NUMBERS
# ══════════════════════════════════════════════════════════════════════════════
class PageNumbersPanel(BasePanel):
    TITLE         = "Seitenzahlen"
    SUBTITLE      = "Seitenzahlen auf jede Seite stempeln."

    def build_ui(self, layout):
        ob = QGroupBox(tr("EINSTELLUNGEN")); ol = QVBoxLayout(ob)

        self.pos = QComboBox()
        self.pos.addItems([tr("Unten Mitte"), tr("Unten Links"), tr("Unten Rechts"),
                           tr("Oben Mitte"),  tr("Oben Links"),  tr("Oben Rechts")])
        ol.addLayout(row(tr("Position:"), self.pos))

        self.prefix = QLineEdit(); self.prefix.setPlaceholderText(tr("z.B. Seite "))
        ol.addLayout(row(tr("Praefix:"), self.prefix))

        self.suffix = QLineEdit(); self.suffix.setPlaceholderText(tr("z.B.  / {gesamt}"))
        ol.addLayout(row(tr("Suffix:"), self.suffix))

        self.start_spin = QSpinBox(); self.start_spin.setRange(0,9999); self.start_spin.setValue(1)
        ol.addLayout(row(tr("Startnummer:"), self.start_spin))

        self.skip_spin = QSpinBox(); self.skip_spin.setRange(0,999); self.skip_spin.setValue(0)
        ol.addLayout(row(tr("Erste N Seiten ueberspringen:"), self.skip_spin))

        self.font_spin = QSpinBox(); self.font_spin.setRange(5,48); self.font_spin.setValue(10)
        ol.addLayout(row(tr("Schriftgroesse (pt):"), self.font_spin))

        self.margin_spin = QSpinBox(); self.margin_spin.setRange(5,100); self.margin_spin.setValue(20)
        ol.addLayout(row(tr("Abstand vom Rand (pt):"), self.margin_spin))

        layout.addWidget(ob)

    def _run_action(self):
        src = self.require_pdf()
        out = self.save_pdf("PDF mit Seitenzahlen speichern als")
        if not out: raise ValueError(tr("Kein Ausgabepfad."))
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        reader = PdfReader(src, strict=False); writer = PdfWriter()
        n=len(reader.pages); skip=self.skip_spin.value()
        start=self.start_spin.value(); fs=self.font_spin.value()
        margin=self.margin_spin.value(); pos_idx=self.pos.currentIndex()
        prefix=self.prefix.text(); suffix=self.suffix.text()
        for i, page in enumerate(reader.pages):
            if i < skip: writer.add_page(page); continue
            num   = start + (i-skip)
            label = prefix + str(num) + suffix.replace("{gesamt}", str(n))
            pw=float(page.mediabox.width); ph=float(page.mediabox.height)
            packet = io.BytesIO()
            c = rl_canvas.Canvas(packet, pagesize=(pw,ph))
            c.setFont("Helvetica", fs)
            # position by index: 0=Mitte/Unten, 1=Links/Unten, 2=Rechts/Unten,
            #                    3=Mitte/Oben,  4=Links/Oben,  5=Rechts/Oben
            bottom = pos_idx < 3
            h_mode = pos_idx % 3   # 0=center, 1=left, 2=right
            y = margin if bottom else ph - margin - fs
            if h_mode == 0:
                x = pw / 2; c.drawCentredString(x, y, label)
            elif h_mode == 1:
                x = margin; c.drawString(x, y, label)
            else:
                x = pw - margin; c.drawRightString(x, y, label)
            c.save(); packet.seek(0)
            from pypdf import PdfReader as PR
            page.merge_page(PR(packet).pages[0]); writer.add_page(page)
        with open(out, "wb") as f: writer.write(f)
        self.open_result(out, tr("Mit Seitenzahlen"))
        return tr('Seitenzahlen auf {p0} Seiten hinzugefuegt').format(p0=n - skip)
