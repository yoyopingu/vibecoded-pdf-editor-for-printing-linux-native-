"""
The print dialog: what to print, on what, and how.

Paper size, orientation, duplex, colour mode, page ranges, copies and
collation, resolved against what the printer says it can do. The sending
itself is tools/printing/spool.py; this decides what to ask it for.
"""
import os, logging
from PyQt6.QtWidgets import (QButtonGroup, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton,
                             QLabel, QFrame, QApplication, QScrollArea, QDialog,
                             QSpinBox, QLineEdit, QCheckBox)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QPageLayout
from tools.i18n import tr
from tools.printing.preview import _PrintPreview
from tools.render.images import pil_to_qpixmap
from tools.printing import prefs
from tools.printing.spool import (PAPER_PRINTER_DEFAULT,
                                  _run_capturing, print_via_gs,
                                  prerender_for_qt, paper_sources, queue_defaults)
from tools.viewer.model import _positions_to_str
from tools.viewer.tab_base import owning_tab
from tools.theme import _TV
from tools.printing.radiopill import RadioPill


def _qt_page_sizes():
    """The paper names this dialog uses, as Qt's page-size ids.

    One table, read in both directions — the reverse of it is what turns the
    queue's default back into a name for the combo. It used to be written out
    twice, once each way, and the lp->Qt copy spelled the ids `QPrinter.PageSize`,
    which is PyQt5: PyQt6 moved them to QPageSize.PageSizeId. That copy was only
    reached when Ghostscript had already failed, so it sat there raising
    AttributeError for anyone who got that far.
    """
    from PyQt6.QtGui import QPageSize
    ids = QPageSize.PageSizeId
    return {"A4": ids.A4, "A3": ids.A3, "A5": ids.A5, "Letter": ids.Letter,
            "Legal": ids.Legal, "B4": ids.B4, "B5": ids.B5,
            "Executive": ids.Executive, "Folio": ids.Folio}


def _is_gone(obj):
    """Has Qt already destroyed this widget's C++ half?

    deleteLater() frees that half while the Python object it was wrapped in
    lives on, so a weakref still hands back something that looks like a
    dialog and is not one. Touching it — emitting one of its signals from a
    worker that has just finished — does not raise RuntimeError, it takes the
    process down. This is the check that has to happen first, and it is why
    the font check crashed the print tests seven runs in eight: the only tests
    that waited long enough for Ghostscript to finish were also the ones that
    then closed the dialog.
    """
    try:
        from PyQt6 import sip
    except ImportError:      # not available in this build; the weakref is all
        return False         # there is, and RuntimeError has to do the rest
    try:
        return sip.isdeleted(obj)
    except Exception:
        return False


_QUEUE_INFO_CACHE = {}       # printer -> {"sources": ..., "defaults": ...}
_PRINTER_LIST_CACHE = None   # (names:list[str], default:str) — last enumeration seen.
                             # Only ever a head start: every dialog open
                             # re-enumerates and replaces it if it has changed.


class PrintDialog(QDialog):
    """
    Vollstaendiger Druckdialog mit allen gaengigen Optionen.
    Verwendet Qt QPrinter wenn verfuegbar, sonst Ghostscript/lp als Fallback.
    """

    def done(self, result_code):
        """Close down the background work this dialog started.

        Printer enumeration and the print job itself run on the pool. Closing
        the dialog used to leave them running against a widget on its way out;
        they now stop at their next checkpoint and their results are dropped.
        """
        try:
            from tools.jobs import cancel_owner
            cancel_owner(self)
        except Exception:
            logging.debug("print dialog: cancelling background jobs failed",
                          exc_info=True)
        super().done(result_code)

    # Delivers the async-enumerated printer list to the GUI thread.
    _printers_loaded = pyqtSignal(list, str)

    # (unembedded font names, whether the print path redraws the page
    # differently) — both worked out off the GUI thread.
    _fonts_checked  = pyqtSignal(list, bool)

    # Print-job results delivered from the worker thread to the GUI thread.
    # (A background thread has no event loop, so QTimer.singleShot never fires
    # there — signals are auto-queued to the GUI thread instead.)
    _print_status   = pyqtSignal(str)
    _print_finished = pyqtSignal(object, int, object)   # pages, copies, skipped
    _print_failed   = pyqtSignal(str)
    _print_qt_send  = pyqtSignal(object)                # packed args tuple

    # Exact paper dimensions in points (portrait baseline, ISO 216)

    def __init__(self, pdf_path, model, parent=None):
        super().__init__(parent, Qt.WindowType.Dialog)
        self.pdf_path = pdf_path
        self.model    = model
        self._progress = None       # transfer-progress popup while a job spools
        self.setWindowTitle(tr("Drucken"))
        # The minimum must fit the settings pane's natural height above the
        # pinned action bar. Set too short, the bottom rows (Kopien, Farbe)
        # land below the scroll viewport and sit hidden behind the footer —
        # which is exactly what happened at the old 820x540.
        self.setMinimumSize(820, 660)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._setup()

    def _scale_buttons(self):
        """The three scaling modes, in the order the rest of the code numbers
        them: 0 fit, 1 fixed size, 2 shrink only."""
        return (self.scale_fit, self.scale_fixed, self.scale_shrink)

    def _scale_index(self):
        """Which mode is chosen, as the index spool.py expects."""
        checked = self._scale_group.checkedId()
        return checked if checked >= 0 else 0

    def _set_scale_index(self, index):
        if isinstance(index, int) and 0 <= index < 3:
            self._scale_buttons()[index].setChecked(True)

    def _sync_scale_pct(self):
        """The percentage only means anything beside "Feste Größe"."""
        on = self.scale_fixed.isChecked()
        self.scale_pct.setEnabled(on)

    def _selected_pages_text(self):
        """The pages picked in the page manager, as "1-3, 5" — or "" if none."""
        try:
            order = self.model.order
            positions = sorted(i + 1 for i, uid in enumerate(order)
                               if uid in self.model.selected)
            return _positions_to_str(positions)
        except Exception:
            logging.debug("print dialog: could not read the page selection",
                          exc_info=True)
            return ""

    def _setup(self):
        from PyQt6.QtWidgets import QGridLayout, QComboBox

        # Solid themed background. Without this the dialog inherits the system
        # palette; on a light desktop the transparent settings pane then shows
        # light, and the (light) theme text/labels become invisible — the
        # "invisible hitboxes". Painting the panel colour keeps text readable
        # regardless of the OS theme.
        self.setStyleSheet(f"QDialog{{background:{_TV['panel_bg']};}}")

        # ── Layout helpers ────────────────────────────────────────────────────
        def _sep():
            f = QFrame()
            f.setFrameShape(QFrame.Shape.HLine)
            f.setFixedHeight(1)
            f.setStyleSheet(
                f"background:{_TV['border']};border:none;margin:3px 0;")
            return f

        def _sec(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"font-size:10px;font-weight:bold;letter-spacing:1px;"
                f"color:{_TV['dim']};background:transparent;")
            lbl.setContentsMargins(0, 4, 0, 0)
            return lbl

        def _lbl(text):
            """Row label in a grid: right-aligned, theme colour, transparent bg."""
            l = QLabel(text)
            l.setStyleSheet(
                f"color:{_TV['text']};background:transparent;")
            l.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return l

        # ── Dialog layout: [ preview | divider | settings ] on top, a pinned
        #    action bar at the bottom that is ALWAYS visible (outside the scroll
        #    area, so the Print button never gets clipped on short screens).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addLayout(root, 1)

        # Left: preview panel
        self._preview = _PrintPreview(self.pdf_path, self.model, self)
        root.addWidget(self._preview)

        # 1-px vertical divider — must use background, not color, for QFrame
        div = QFrame()
        div.setFrameShape(QFrame.Shape.VLine)
        div.setFixedWidth(1)
        div.setStyleSheet(
            f"QFrame{{background:{_TV['border']};border:none;}}")
        root.addWidget(div)

        # Right: scrollable settings
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Scope to QScrollArea — a bare "border:none;" here cascades to every
        # child widget and strips the comboboxes'/buttons' own borders, making
        # them look like plain text ("invisible" clickable controls).
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        root.addWidget(scroll, 1)

        right = QWidget()
        right.setObjectName("printSettingsPane")
        # Scope to this widget only, so the transparent background does not
        # cascade onto the child controls.
        right.setStyleSheet(
            "QWidget#printSettingsPane{background:transparent;}")
        scroll.setWidget(right)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(18, 14, 18, 14)
        rl.setSpacing(4)

        n = len(self.model.order)

        # Title — filename, truncated with ellipsis if too long
        title = QLabel(os.path.basename(self.pdf_path))
        title.setStyleSheet(
            f"font-size:13px;font-weight:bold;color:{_TV['text']};"
            f"background:transparent;")
        title.setWordWrap(False)
        rl.addWidget(title)
        rl.addWidget(_sep())

        # ── Printer row: printer combo + Bitmap + dpi ────────────────────────
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(8)
        self.printer_combo = QComboBox()
        self._hw_margin_mm = 3.0
        top_row.addWidget(self.printer_combo, 1)

        self.bitmap_check = QCheckBox(tr("Bitmap"))
        self.bitmap_check.setToolTip(tr(
            "Druckt die Seiten so, wie sie in der Vorschau aussehen.\n\n"
            "Normalerweise wird die PDF an den Drucker geschickt und dort "
            "erneut interpretiert — mit einer anderen Schrift-Ersetzung als "
            "in der Vorschau, wenn die Datei ihre Schriften nicht mitbringt.\n"
            "Als Bitmap wird stattdessen genau das gedruckt, was die Vorschau "
            "zeigt.\n\n"
            "Dafuer ist der Text im Druckauftrag nicht mehr markierbar und "
            "die Datei wird groesser."))
        top_row.addWidget(self.bitmap_check)

        self.bitmap_dpi = QComboBox()
        for dpi in (300, 600, 1200, 2400):
            self.bitmap_dpi.addItem(f"{dpi} dpi", dpi)
        self.bitmap_dpi.setCurrentIndex(1)          # 600 dpi
        self.bitmap_dpi.setFixedWidth(96)
        self.bitmap_dpi.setToolTip(tr(
            "Aufloesung der Rasterung. 600 dpi ist fuer Text und normale "
            "Grafiken ueblich, 1200 dpi fuer feine Linien und kleine Schrift."))
        self.bitmap_dpi.setEnabled(False)
        self.bitmap_dpi.setVisible(False)
        self.bitmap_check.toggled.connect(self.bitmap_dpi.setEnabled)
        self.bitmap_check.toggled.connect(self.bitmap_dpi.setVisible)
        top_row.addWidget(self.bitmap_dpi)
        rl.addLayout(top_row)
        rl.addWidget(_sep())

        # ── SEITEN ───────────────────────────────────────────────────────────
        rl.addWidget(_sec(tr("SEITEN")))
        self.radio_all     = RadioPill(tr("Alle (1 – {n})").format(n=n))
        self.radio_current = RadioPill(tr("Aktuelle Seite"))
        self.radio_range   = RadioPill(tr("Bereich"))
        # Radiopills are QToolButtons, which are not auto-exclusive — the
        # group is what makes them behave like radios. No IDs are needed here;
        # the code reads isChecked(), not checkedId().
        self._page_group = QButtonGroup(self)
        for _r in (self.radio_all, self.radio_current, self.radio_range):
            self._page_group.addButton(_r)
        self.radio_all.setChecked(True)

        pages_row = QHBoxLayout()
        pages_row.setContentsMargins(0, 0, 0, 0)
        pages_row.setSpacing(8)
        pages_row.addWidget(self.radio_all)
        pages_row.addWidget(self.radio_current)

        self.range_edit = QLineEdit()
        self.range_edit.setPlaceholderText(tr("z.B.  1-3, 5, 7-9"))
        self.range_edit.setFixedWidth(160)
        self.range_edit.setEnabled(False)
        # Start from whatever is picked in "Seiten verwalten", written the same
        # way its own selection field writes it — _positions_to_str is shared,
        # so the two read identically rather than merely similarly. Nothing
        # selected there means nothing here, and the radio is left alone: the
        # pages are ready if you want them, not chosen on your behalf.
        self.range_edit.setText(self._selected_pages_text())
        self.radio_range.toggled.connect(self.range_edit.setEnabled)
        range_row = QHBoxLayout()
        range_row.setContentsMargins(0, 0, 0, 0)
        range_row.setSpacing(6)
        range_row.addWidget(self.radio_range)
        range_row.addWidget(self.range_edit)
        range_row.addStretch()
        pages_row.addLayout(range_row)
        pages_row.addStretch()
        rl.addLayout(pages_row)
        rl.addWidget(_sep())

        # ── SEITENHANDHABUNG ─────────────────────────────────────────────────
        rl.addWidget(_sec(tr("SEITENHANDHABUNG")))
        pg = QGridLayout()
        pg.setHorizontalSpacing(8)
        pg.setVerticalSpacing(4)
        pg.setColumnMinimumWidth(0, 145)
        pg.setColumnStretch(1, 1)

        pg.addWidget(_lbl(tr("Skalierung:")), 0, 0)

        # On show, not behind a dropdown: three settings that decide how big the
        # job prints, one click each instead of two. Radiopills rather than tick
        # boxes because exactly one of them applies — two ticked would be a state
        # the printer cannot be in.
        self.scale_fit    = RadioPill(tr("Anpassen"))
        self.scale_fixed  = RadioPill(tr("Feste Größe"))
        self.scale_shrink = RadioPill(tr("Verkleinern"))
        self.scale_fit.setToolTip(tr(
            "Skaliert hoch und runter — Seite füllt den Druckbereich vollständig (Acrobat: Fit Page)"))
        self.scale_fixed.setToolTip(tr(
            "Druckt in der Größe daneben — 100 % ist 1:1, Inhalt kann am Rand beschnitten werden"))
        self.scale_shrink.setToolTip(tr(
            "Verkleinert nur wenn nötig, vergrössert nie (Acrobat: Shrink to Printable Area)"))
        self._scale_group = QButtonGroup(self)
        for i, btn in enumerate(self._scale_buttons()):
            self._scale_group.addButton(btn, i)
        self.scale_fit.setChecked(True)

        # The size that "Feste Größe" is a size *of*. 100 is the page at its own
        # size, which is all that option could once mean.
        self.scale_pct = QSpinBox()
        self.scale_pct.setRange(10, 400)
        self.scale_pct.setValue(100)
        self.scale_pct.setSuffix(" %")
        self.scale_pct.setFixedWidth(78)
        self.scale_pct.setToolTip(tr(
            "Größe relativ zum Original. 100 % druckt 1:1."))

        scale_col = QVBoxLayout()
        scale_col.setContentsMargins(0, 0, 0, 0)
        scale_col.setSpacing(2)
        scale_col.addWidget(self.scale_fit)
        fixed_row = QHBoxLayout()
        fixed_row.setContentsMargins(0, 0, 0, 0)
        fixed_row.setSpacing(6)
        fixed_row.addWidget(self.scale_fixed)
        fixed_row.addWidget(self.scale_pct)
        fixed_row.addStretch()
        scale_col.addLayout(fixed_row)
        scale_col.addWidget(self.scale_shrink)
        pg.addLayout(scale_col, 0, 1)
        self._scale_group.idToggled.connect(lambda _i, _on: self._sync_scale_pct())
        self._sync_scale_pct()

        self._margin_lbl = QLabel("")
        self._margin_lbl.setStyleSheet(
            f"font-size:10px;color:{_TV['dim']};background:transparent;")
        self._margin_lbl.setWordWrap(True)
        self._margin_lbl.setMinimumHeight(28)
        pg.addWidget(self._margin_lbl, 1, 0, 1, 2)

        pg.addWidget(_lbl(tr("Ausrichtung:")), 2, 0)
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(
            [tr("Automatisch"), tr("Hochformat"), tr("Querformat")])
        pg.addWidget(self.orient_combo, 2, 1)

        pg.addWidget(_lbl(tr("Papier:")), 3, 0)
        paper_row = QHBoxLayout()
        paper_row.setContentsMargins(0, 0, 0, 0)
        paper_row.setSpacing(8)
        self.paper_combo = QComboBox()
        self.paper_combo.setToolTip(tr(
            "Drucker-Standard laesst das Papier so, wie die Warteschlange es "
            "eingestellt hat — richtig, solange im Drucker liegt, worauf "
            "gedruckt werden soll.\n\n"
            "Eine Groesse hier zu waehlen ueberschreibt das. Wird eine "
            "kleinere genannt als eingelegt ist, wird nur diese Flaeche "
            "bedruckt; wird eine groessere genannt, verkleinert der Drucker."))
        paper_row.addWidget(self.paper_combo, 1)
        # Two boxes for a size nobody's list has. Hidden until "Benutzer-
        # definiert" is chosen, because that is the only time they mean
        # anything and the row is tight enough already.
        self.paper_w_mm = QSpinBox()
        self.paper_w_mm.setRange(10, 2000); self.paper_w_mm.setValue(320)
        self.paper_w_mm.setSuffix(" mm"); self.paper_w_mm.setFixedWidth(80)
        self.paper_h_mm = QSpinBox()
        self.paper_h_mm.setRange(10, 2000); self.paper_h_mm.setValue(450)
        self.paper_h_mm.setSuffix(" mm"); self.paper_h_mm.setFixedWidth(80)
        self._paper_x = QLabel("×")
        for w in (self.paper_w_mm, self._paper_x, self.paper_h_mm):
            w.setVisible(False)
            paper_row.addWidget(w)
        self.paper_combo.currentIndexChanged.connect(self._sync_custom_paper)
        for box in (self.paper_w_mm, self.paper_h_mm):
            box.valueChanged.connect(self._sync_preview)
        pg.addLayout(paper_row, 3, 1)

        # Which tray to draw from. The choices come from the queue itself, and
        # a printer that offers only one is not asked about — see
        # _apply_paper_sources. "Drucker-Standard" sends no tray at all, the
        # same convention the colour selector uses and the one every native
        # dialog follows.
        pg.addWidget(_lbl(tr("Papierfach:")), 4, 0)
        self.source_combo = QComboBox()
        self.source_combo.addItem(tr("Drucker-Standard"), None)
        self.source_combo.setToolTip(tr(
            "Aus welchem Schacht das Papier gezogen wird.\n"
            "Drucker-Standard: keine Vorgabe senden — die Warteschlange "
            "entscheidet."))
        self.source_combo.setEnabled(False)
        self._source_keyword = None      # set once the queue has been asked
        pg.addWidget(self.source_combo, 4, 1)

        # Asking CUPS what the queue defaults to happens in the background, and
        # the user may well have changed something by the time it answers. This
        # says whether they have, so the answer fills in blanks rather than
        # overwriting a deliberate choice. _applying suppresses it while the
        # dialog is setting the same widgets itself.
        self._settings_touched = False
        self._applying = False

        rl.addLayout(pg)

        # Kopien + Farbe: a two-column row (the concept's Seitenhandhabung
        # 2-col) — Kopien moved up out of AUSGABE to sit near the top.
        kf = QGridLayout()
        kf.setHorizontalSpacing(8)
        kf.setVerticalSpacing(4)
        kf.setColumnMinimumWidth(0, 145)
        kf.setColumnStretch(1, 1)

        kf.addWidget(_lbl(tr("Kopien:")), 0, 0)
        copies_row = QHBoxLayout()
        copies_row.setContentsMargins(0, 0, 0, 0)
        copies_row.setSpacing(8)
        self.copies_spin = QSpinBox()
        self.copies_spin.setRange(1, 999)
        self.copies_spin.setValue(1)
        self.copies_spin.setFixedWidth(60)
        copies_row.addWidget(self.copies_spin)
        self.collate_check = QCheckBox(tr("Sortieren  (1,2,3 / 1,2,3)"))
        self.collate_check.setChecked(True)
        copies_row.addWidget(self.collate_check)
        copies_row.addStretch()
        kf.addLayout(copies_row, 0, 1)

        kf.addWidget(_lbl(tr("Farbe:")), 1, 0)
        self.color_combo = QComboBox()
        # "Drucker-Standard" sends no colour option at all, so the queue's own
        # setting decides — that is what lets a job be re-routed or configured
        # from somewhere else. Every other PDF viewer on Linux behaves this way.
        self.color_combo.addItem(tr("Drucker-Standard"), "auto")
        self.color_combo.addItem(tr("Farbe"),            "color")
        self.color_combo.addItem(tr("Graustufen"),       "mono")
        self.color_combo.setToolTip(tr(
            "Drucker-Standard: keine Vorgabe senden — der Drucker bzw. die "
            "Warteschlange entscheidet.\n"
            "Die Farbinformation bleibt in jedem Fall in der Datei erhalten."))
        kf.addWidget(self.color_combo, 1, 1)

        rl.addLayout(kf)
        rl.addWidget(_sep())

        # ── AUSGABE (Duplex) ─────────────────────────────────────────────────
        rl.addWidget(_sec(tr("AUSGABE")))
        duplex_row = QHBoxLayout()
        duplex_row.setContentsMargins(0, 0, 0, 0)
        duplex_row.setSpacing(8)
        self.duplex_check = QCheckBox(tr("Beidseitig drucken  (Duplex)"))
        duplex_row.addWidget(self.duplex_check)
        # Binding edge: long edge (book, back upright) vs short edge (notepad,
        # back rotated 180°). Only meaningful when duplex is on.
        self.duplex_edge_combo = QComboBox()
        self.duplex_edge_combo.addItem(tr("Lange Seite (Buch)"),       "long")
        self.duplex_edge_combo.addItem(tr("Kurze Seite (Notizblock)"), "short")
        self.duplex_edge_combo.setToolTip(
            tr("Lange Seite: Rückseite steht gleich herum wie die Vorderseite "
               "(Bindung an der langen Kante, wie ein Buch).\n"
               "Kurze Seite: Rückseite ist um 180° gedreht "
               "(Bindung an der kurzen Kante, wie ein Notizblock)."))
        self.duplex_edge_combo.setEnabled(False)
        # Edge selection only applies when duplex is enabled.
        self.duplex_check.toggled.connect(self.duplex_edge_combo.setEnabled)
        duplex_row.addWidget(self.duplex_edge_combo)
        duplex_row.addStretch()
        rl.addLayout(duplex_row)

        rl.addStretch(1)

        # ── Pinned action bar (status + buttons), always visible ────────────
        # Lives in the dialog's vertical layout, OUTSIDE the scroll area, so the
        # Print button is never clipped when the settings don't fit the height.
        bottom = QWidget()
        bottom.setObjectName("printActionBar")
        bottom.setStyleSheet(
            f"QWidget#printActionBar{{background:{_TV['panel_bg']};"
            f"border-top:1px solid {_TV['border']};}}")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(18, 8, 18, 10); bl.setSpacing(6)

        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("dimLabel")
        self.status_lbl.setWordWrap(True)
        # A wrapped QLabel reports the height of a single line as its sizeHint,
        # so the layout reserves one line and the second is cut off by the
        # window edge — which is what happened to the unembedded-fonts notice,
        # the longest thing that appears here. Reserve the two lines it can
        # actually need; the bar is this tall either way, empty or not.
        self.status_lbl.setMinimumHeight(30)
        bl.addWidget(self.status_lbl)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)
        # Summary note on the left of the button row: "6 Seiten · 1 Kopie ·
        # Farbe · beidseitig" — the concept's right-aligned block.
        self.summary_lbl = QLabel("")
        self.summary_lbl.setStyleSheet(
            f"color:{_TV['dim']};background:transparent;font-size:12px;")
        btn_row.addWidget(self.summary_lbl)
        btn_row.addStretch()
        cancel_btn = QPushButton(tr("Abbrechen"))
        cancel_btn.setObjectName("secondaryBtn")
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)
        print_btn = QPushButton(tr("  Drucken  "))
        print_btn.setObjectName("actionBtn")
        print_btn.setMinimumWidth(110)
        print_btn.clicked.connect(self._do_print)
        btn_row.addWidget(print_btn)
        bl.addLayout(btn_row)

        outer.addWidget(bottom)

        # Deliver print-job results from the worker thread to the GUI thread.
        self._print_status.connect(self._on_print_status)
        self._print_finished.connect(self._finish)
        self._print_failed.connect(self._on_print_failed)
        self._print_qt_send.connect(lambda a: self._qt_send_to_printer(*a))

        for widget, signal in (
                (self.paper_combo, "currentIndexChanged"),
                (self.orient_combo, "currentIndexChanged"),
                (self.color_combo, "currentIndexChanged"),
                (self._scale_group, "idToggled"),
                (self.scale_pct, "valueChanged"),
                (self.source_combo, "currentIndexChanged"),
                (self.duplex_edge_combo, "currentIndexChanged"),
                (self.collate_check, "toggled"),
                (self.duplex_check, "toggled")):
            getattr(widget, signal).connect(self._note_user_change)

        # All widgets created — populate printers (triggers _on_printer_changed)
        self._load_printers()
        self._unembedded = []
        self._print_differs = False
        self._fonts_check_started = True
        self._check_fonts()

        # Live preview: update whenever any print-affecting setting changes
        self._scale_group.idToggled.connect(lambda *_: self._sync_preview())
        self.scale_pct.valueChanged.connect(self._sync_preview)
        self.paper_combo.currentIndexChanged.connect(self._sync_preview)
        self.orient_combo.currentIndexChanged.connect(self._sync_preview)

        # Preview follows the page selection (all / current page / range)
        self.radio_all.toggled.connect(self._sync_preview_pages)
        self.radio_current.toggled.connect(self._sync_preview_pages)
        self.radio_range.toggled.connect(self._sync_preview_pages)
        self.range_edit.textChanged.connect(self._sync_preview_pages)
        self._sync_preview_pages()

        # The summary line follows what would be sent: pages, copies, colour,
        # sides.
        for widget, signal in (
                (self.radio_all, "toggled"),
                (self.radio_current, "toggled"),
                (self.radio_range, "toggled"),
                (self.range_edit, "textChanged"),
                (self.copies_spin, "valueChanged"),
                (self.color_combo, "currentIndexChanged"),
                (self.duplex_check, "toggled")):
            getattr(widget, signal).connect(self._update_summary)
        self._update_summary()

        # Wide rows: printers and paper/tray/colour/combo rows must compress
        # inside their column rather than stretch the pane past the viewport.
        for name in ("printer_combo", "orient_combo", "paper_combo",
                     "source_combo", "color_combo", "duplex_edge_combo"):
            self._shrinkable(getattr(self, name))

        self._make_enter_print(print_btn, cancel_btn)

    def _shrinkable(self, combo):
        """Let a combo compress to its column instead of forcing the pane wide.

        A QComboBox's minimumSizeHint is the widest item it holds, so a long
        paper or tray name used to stretch the settings pane beyond the scroll
        viewport and push the dropdown's arrow out of the window. The label
        column it sits in keeps the row readable; Ignored lets the combo give
        up characters (elided with an ellipsis) before it gives up pixels."""
        from PyQt6.QtWidgets import QSizePolicy
        combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                            QSizePolicy.Policy.Fixed)

    def _make_enter_print(self, print_btn, cancel_btn):
        """Enter prints, the way Enter runs a tool in every panel.

        Every QPushButton in a QDialog is autoDefault, so Qt made the first one
        in tab order the default — and the first one here belongs to the
        embedded preview, whose ◀ ▶ arrows are built in printing/preview.py.
        Enter turned the preview page instead of printing: not a missing
        feature so much as the key already being spoken for by the wrong
        control.

        Cleared on every button and granted back to these two, rather than
        named button by button, so a control added to the sidebar or the
        preview later cannot quietly take Enter over again. Cancel keeps it
        because a focused Cancel that prints on Enter is how a copyshop
        discovers it has printed a hundred sheets it did not want.
        """
        for btn in self.findChildren(QPushButton):
            btn.setAutoDefault(False)
            btn.setDefault(False)
        for btn in (print_btn, cancel_btn):
            btn.setAutoDefault(True)
        print_btn.setDefault(True)

    def _current_page_pos(self):
        """Position of the page the viewer is showing, or None if unknown.

        Shared by the preview and the print job so they can never disagree
        about which page "Aktuelle Seite" means."""
        parent = owning_tab(self.parent())
        if parent is not None and getattr(parent, "single", None) is not None:
            pos = parent.single.current_page
            if 0 <= pos < len(self.model.order):
                return pos
        return None

    def _preview_pages(self):
        """Page positions the preview should show for the current selection.

        Quiet counterpart to _get_pages() — never touches the status label.
        Returns a list of 0-based positions, or None if the range is currently
        incomplete/invalid (caller then leaves the preview unchanged).
        """
        n = len(self.model.order)
        if self.radio_current.isChecked():
            cur = self._current_page_pos()
            return None if cur is None else [cur]
        if self.radio_range.isChecked():
            text = self.range_edit.text().strip()
            if not text:
                return list(range(n))
            # Same rules as _get_pages(), deliberately: this used to silently
            # clamp, so "5-99" on a ten-page file previewed pages 5–10 and then
            # printing rejected it. The preview must not show a job that will
            # not run.
            pages = []
            try:
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        lo, hi = int(a.strip()), int(b.strip())
                        if lo < 1 or hi > n or lo > hi:
                            return None
                        pages.extend(range(lo - 1, hi))
                    else:
                        p = int(part)
                        if p < 1 or p > n:
                            return None
                        pages.append(p - 1)
            except ValueError:
                return None
            pages = [p for p in sorted(set(pages)) if 0 <= p < n]
            return pages or None
        return list(range(n))   # "Alle Seiten"

    def _sync_preview_pages(self):
        pages = self._preview_pages()
        if pages:   # None/empty → leave the current preview in place
            self._preview.set_pages(pages)

    def _update_summary(self):
        """The action-bar note: how many pages, copies, colour and sides.

        Mirrors the concept's "6 Seiten · 1 Kopie · Farbe · beidseitig". Reads
        the same controls _do_print reads, so the summary cannot promise a job
        the dialog will not send.
        """
        n = len(self.model.order)
        if self.radio_all.isChecked():
            count = n
        elif self.radio_current.isChecked():
            count = 1
        else:
            text = self.range_edit.text().strip()
            count = 0
            try:
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        lo, hi = int(a.strip()), int(b.strip())
                        if lo < 1 or hi > n or lo > hi:
                            count = n; break
                        count += hi - lo + 1
                    else:
                        p = int(part)
                        count += 1 if 1 <= p <= n else 0
            except ValueError:
                count = n
            if count == 0:
                count = n
        copies = self.copies_spin.value()
        color = self.color_combo.currentText()
        sides = (tr("beidseitig") if self.duplex_check.isChecked()
                 else tr("einseitig"))
        self.summary_lbl.setText(tr(
            "{pages} Seiten · {copies} Kopie(n) · {color} · {sides}").format(
                pages=count, copies=copies, color=color, sides=sides))

    def _load_printers(self):
        """Populate the printer combo.

        Enumerating printers via CUPS (QPrinterInfo.availablePrinters() or
        `lpstat -e`) takes ~1–2 s — doing it during construction froze the whole
        dialog before it appeared. Instead the dialog opens instantly with a
        placeholder and the list is fetched in a background thread (subprocess —
        safe off the GUI thread, unlike Qt's print classes).

        Later opens show the previous result immediately and enumerate again
        behind it, so the list is both instant and current. The combo is only
        rebuilt if the answer differs.
        """
        self.printer_combo.clear()

        # The cache is a head start, not the answer: show last time's list at
        # once so the dialog is usable immediately, then enumerate anyway. A
        # printer plugged in while the app was running used to be invisible
        # until a restart, because this returned here and never asked again.
        if _PRINTER_LIST_CACHE is not None:
            names, default = _PRINTER_LIST_CACHE
            self._apply_printer_list(names, default)
        else:
            self.printer_combo.addItem(tr("Drucker werden geladen…"), "none")
            self.printer_combo.setEnabled(False)
        self._printers_loaded.connect(self._on_printers_enumerated)

        import weakref
        self_ref = weakref.ref(self)

        def _bg(job):
            names, default = [], ""
            try:
                # 15s: enumerating queues, which is local and near-instant —
                # generous headroom for a CUPS server having a slow moment.
                r = _run_capturing(["lpstat", "-e"], timeout=15)
                names = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            except Exception:
                logging.debug("lpstat -e failed; trying Qt", exc_info=True)
            if not names:
                # Fallback to Qt enumeration if lpstat is unavailable
                try:
                    from PyQt6.QtPrintSupport import QPrinterInfo
                    names = list(QPrinterInfo.availablePrinterNames())
                except Exception:
                    logging.debug("Qt found no printers either", exc_info=True)
            try:
                # 10s: the same query as above, one line instead of a list.
                r = _run_capturing(["lpstat", "-d"], timeout=10)
                out = r.stdout.strip()
                if ":" in out:
                    cand = out.split(":", 1)[1].strip()
                    if cand in names:
                        default = cand
            except Exception:
                logging.debug("lpstat -d failed; no system default", exc_info=True)
            obj = self_ref()
            if obj is not None and not job.cancelled:
                try:
                    obj._printers_loaded.emit(names, default)
                except RuntimeError:
                    pass   # dialog closed
        from tools.jobs import submit
        submit(_bg, owner=self, name="printer-list")

    PAPER_CUSTOM = "__custom__"      # combo entry, not a media name

    def _sync_custom_paper(self):
        """Show the two millimetre boxes only for "Benutzerdefiniert"."""
        on = self.paper_combo.currentData() == self.PAPER_CUSTOM
        for w in (self.paper_w_mm, self._paper_x, self.paper_h_mm):
            w.setVisible(on)

    def selected_paper(self):
        """The media name for the job, or "" to leave the paper alone.

        "" is not a size and is never sent: the queue keeps whatever it is set
        to. That is the right answer far more often than naming one — the
        operator has already loaded the stock and picked the tray, and a size
        named here can only agree with that or override it wrongly.
        """
        data = self.paper_combo.currentData()
        if data == self.PAPER_CUSTOM:
            from tools.printing.spool import custom_paper_key
            return custom_paper_key(self.paper_w_mm.value(),
                                    self.paper_h_mm.value())
        return data or ""

    def prints_as_bitmap(self):
        """Whether this job is rasterised here rather than sent as a PDF.

        Sent as a PDF, the file is interpreted a second time — by Ghostscript
        and then the printer — and a font the file only references is resolved
        again there, to something other than what pdfium chose for the preview.
        Rasterising is what makes the paper match the screen, because it is the
        preview's own renderer that draws it.
        """
        return self.bitmap_check.isChecked()

    def _raster_dpi(self, printer_dpi):
        """The resolution to rasterise at: the operator's choice when they have
        asked for a bitmap, the printer's own resolution otherwise."""
        if self.prints_as_bitmap():
            return self.bitmap_dpi.currentData() or 300
        return printer_dpi

    def _check_fonts(self):
        """Warn when the print path will not reproduce the preview.

        The first version of this asked whether the fonts were embedded, which
        is one reason among several and not the one that turned up first in
        practice: a file whose fonts *were* embedded still printed wrong, and
        got no warning at all. Every other reason — a broken font program, a
        Type 3, a bad CID map — would have slipped past the same way, because
        each is a separate thing to think of.

        So the question asked is the operator's own: put a page through the
        print path and draw the result with the preview's renderer. If it
        still looks the same, there is nothing to say. The font list is kept
        alongside it, as the usual explanation for a page that does not.

        Off the GUI thread. It runs Ghostscript over a single page and renders
        twice — about a quarter of a second, and the same on a 500-page
        document as on a one-page one — which is still far too long to spend
        in a dialog that is supposed to appear at once.
        """
        self._fonts_checked.connect(self._on_fonts_checked)
        import weakref
        self_ref = weakref.ref(self)

        def _bg(job):
            names, differs = [], False
            try:
                from tools.panels._prepress import unembedded_fonts
                names = sorted(unembedded_fonts(self.pdf_path))
            except Exception:
                logging.debug("could not check the fonts of %s",
                              self.pdf_path, exc_info=True)
            if job.cancelled:
                return
            try:
                from tools.printing.spool import print_path_redraws_the_page
                # None means it could not be checked — no Ghostscript, an
                # unreadable page. Not knowing is not the same as a problem.
                differs = bool(print_path_redraws_the_page(self.pdf_path))
            except Exception:
                logging.debug("could not compare the print path with the "
                              "preview for %s", self.pdf_path, exc_info=True)
            obj = self_ref()
            if obj is None or job.cancelled or _is_gone(obj):
                return
            try:
                obj._fonts_checked.emit(names, differs)
            except RuntimeError:
                pass   # dialog closed between the check above and here
        from tools.jobs import submit
        submit(_bg, owner=self, name="font-check")

    def _on_fonts_checked(self, names, differs):
        self._unembedded = list(names)
        self._print_differs = bool(differs)
        if not differs:
            # Unembedded fonts that Ghostscript replaces with the same metrics
            # print correctly, and most documents have some — warning about
            # every one of them is how a warning stops being read.
            return
        if names:
            shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
            self.status_lbl.setText(tr(
                "Achtung: Der Ausdruck wird anders aussehen als die Vorschau "
                "— die Schriften {p1} sind nicht eingebettet. „Als Bitmap "
                "drucken“ druckt genau das, was die Vorschau zeigt."
            ).format(p1=shown))
        else:
            self.status_lbl.setText(tr(
                "Achtung: Der Ausdruck wird anders aussehen als die Vorschau. "
                "Die Schriften der Datei ueberstehen den Druckweg nicht. "
                "„Als Bitmap drucken“ druckt genau das, was die Vorschau "
                "zeigt."))

    def _note_user_change(self, *_):
        if not self._applying:
            self._settings_touched = True

    def _load_queue_info(self, printer_name):
        """Ask CUPS what this queue offers and defaults to, off the GUI thread.

        One lpoptions call answers all of it: the trays, the default paper and
        the default sides. It shells out and can take a second or two on a
        network queue — the same reason the printer list is fetched in the
        background.
        """
        self.source_combo.setEnabled(False)
        while self.source_combo.count() > 1:
            self.source_combo.removeItem(1)
        if not printer_name or printer_name in ("lp", "none"):
            return
        cached = _QUEUE_INFO_CACHE.get(printer_name)
        if cached is not None:
            self._apply_queue_info(printer_name, cached)
            return
        from tools.jobs import submit

        def _ask(job):
            return {"sources": paper_sources(printer_name),
                    "defaults": queue_defaults(printer_name)}

        submit(_ask, owner=self, name="queue-info",
               on_done=lambda info, p=printer_name: self._apply_queue_info(p, info))

    def _apply_queue_info(self, printer_name, info):
        _QUEUE_INFO_CACHE[printer_name] = info
        if printer_name != self.printer_combo.currentData():
            return                       # the user picked another printer
        self._applying = True
        try:
            self._apply_queue_info_now(printer_name, info)
        finally:
            self._applying = False

    def _apply_queue_info_now(self, printer_name, info):

        # ── The trays ────────────────────────────────────────────────────────
        while self.source_combo.count() > 1:
            self.source_combo.removeItem(1)
        found = info.get("sources")
        if found:
            keyword, choices, default = found
            # The keyword is a property of the queue, not of the entry, so it
            # is held here and only the choice goes in the combo. Item data has
            # to be a plain string: QComboBox.findData compares Python objects
            # by identity, so a tuple rebuilt from parsed text never matches the
            # one that was stored — which is exactly what a remembered tray is.
            self._source_keyword = keyword
            for choice in choices:
                label = choice + (tr("  (Standard)") if choice == default else "")
                self.source_combo.addItem(label, choice)
            self.source_combo.setEnabled(True)
        else:
            # A queue with one tray, or none it will talk about. A combo with a
            # single entry would only be something else to read.
            self._source_keyword = None
            self.source_combo.setEnabled(False)

        # ── What the queue is set to ─────────────────────────────────────────
        # Only where the user has not already said otherwise for this printer:
        # what they used last outranks the queue's default, and the queue's
        # default outranks whatever Qt guessed.
        saved = prefs.for_printer(printer_name)
        defaults = info.get("defaults") or {}
        if self._settings_touched:
            return                       # they have already said what they want
        # The queue's default paper is deliberately not selected here. Doing so
        # turned "whatever this queue is set to" into a size named on the job —
        # the same value, said out loud, and therefore an override. It read as
        # harmless while the queue said A4 and the tray held A4; the moment
        # SRA3 went in, the job still carried A4 and printed an A4 area on it.
        # Leaving the combo on "Drucker-Standard" sends nothing and follows the
        # queue wherever it goes.
        if "duplex" in defaults and "duplex" not in saved:
            self.duplex_check.setChecked(bool(defaults["duplex"]))
            ei = self.duplex_edge_combo.findData(defaults.get("duplex_edge", "long"))
            if ei >= 0:
                self.duplex_edge_combo.setCurrentIndex(ei)
            self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())

        self._restore_saved(printer_name)
        self._update_margin_label()
        self._sync_preview()

    # ── Remembering what was used last ───────────────────────────────────────

    def _current_settings(self):
        """The dialog's settings, in the shape prefs stores them."""
        return {
            "paper":        self.paper_combo.currentData(),
            "orientation":  self.orient_combo.currentIndex(),
            "color":        self.color_combo.currentData(),
            "scale":        self._scale_index(),
            "scale_pct":    self.scale_pct.value(),
            "collate":      self.collate_check.isChecked(),
            "duplex":       self.duplex_check.isChecked(),
            "duplex_edge":  self.duplex_edge_combo.currentData(),
            "paper_source": ([self._source_keyword, self.source_combo.currentData()]
                             if self._source_keyword and self.source_combo.currentData()
                             else None),
        }

    def _restore_saved(self, printer_name):
        """Re-apply what this printer was last used with.

        Every field is optional and every one is checked against what the combo
        actually offers now — a saved paper size or tray may simply not exist on
        this queue any more, and a stale setting must not silently change what
        gets printed.
        """
        saved = prefs.for_printer(printer_name)
        if not saved:
            return

        def _combo_by_data(combo, value):
            if value is None:
                return
            idx = combo.findData(value)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        def _combo_by_index(combo, value):
            if isinstance(value, int) and 0 <= value < combo.count():
                combo.setCurrentIndex(value)

        self.paper_combo.blockSignals(True)
        _combo_by_data(self.paper_combo, saved.get("paper"))
        self.paper_combo.blockSignals(False)
        _combo_by_index(self.orient_combo, saved.get("orientation"))
        _combo_by_data(self.color_combo, saved.get("color"))
        self._set_scale_index(saved.get("scale"))
        if isinstance(saved.get("scale_pct"), int):
            self.scale_pct.setValue(saved["scale_pct"])
        if isinstance(saved.get("collate"), bool):
            self.collate_check.setChecked(saved["collate"])
        if isinstance(saved.get("duplex"), bool):
            self.duplex_check.setChecked(saved["duplex"])
        _combo_by_data(self.duplex_edge_combo, saved.get("duplex_edge"))
        self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())
        source = saved.get("paper_source")
        # Only if this queue still uses the same keyword: a tray remembered from
        # a driver queue means nothing on a driverless one.
        if source and len(source) == 2 and source[0] == self._source_keyword:
            _combo_by_data(self.source_combo, source[1])

    def _on_printers_enumerated(self, names, default):
        """A fresh enumeration has come back from the background thread.

        Usually it says exactly what the cache already said, and then there is
        nothing to do — rebuilding the combo under someone who is using it is
        worse than not refreshing at all. Only a real change redraws, and it
        keeps whatever printer is selected if that printer still exists."""
        if _PRINTER_LIST_CACHE == (list(names), default):
            return
        current = self.printer_combo.currentData()
        self._apply_printer_list(names, default,
                                 keep=current if current not in (None, "none") else None)

    def _apply_printer_list(self, names, default, keep=None):
        """Populate the combo from an enumerated printer list (GUI thread)."""
        global _PRINTER_LIST_CACHE
        _PRINTER_LIST_CACHE = (list(names), default)
        was_selected = self.printer_combo.currentData()

        try:
            self.printer_combo.currentIndexChanged.disconnect(self._on_printer_changed)
        except TypeError:
            pass   # not connected yet

        self.printer_combo.blockSignals(True)
        self.printer_combo.clear()
        self.printer_combo.setEnabled(True)
        for nm in names:
            self.printer_combo.addItem(nm, nm)
        if self.printer_combo.count() == 0:
            self.printer_combo.addItem(tr("Kein Drucker gefunden"), "none")
        # The one used last, if it is still there; otherwise the system default.
        # Reopening on the printer you actually print to is the whole point of
        # remembering it.
        for wanted in (keep, prefs.last_printer(), default):
            if not wanted:
                continue
            idx = self.printer_combo.findData(wanted)
            if idx >= 0:
                self.printer_combo.setCurrentIndex(idx)
                break
        self.printer_combo.blockSignals(False)

        self.printer_combo.currentIndexChanged.connect(self._on_printer_changed)
        # Only when the selection actually moved, and only if the user has not
        # already made a choice. A refresh that finds the same printer selected
        # has nothing to apply; one that finds the selected printer gone would
        # otherwise undo the paper size and sides the operator just picked.
        # _apply_queue_info has honoured _settings_touched all along — this is
        # the Qt half of the same rule.
        if (self.printer_combo.currentData() != was_selected
                and not self._settings_touched):
            self._on_printer_changed()

    # Fallback paper list used when printer reports no supported sizes
    @property
    def _FALLBACK_PAPERS(self):
        """Shown when the queue reports nothing to enumerate — which is every
        driverless press, including the one this came up on.

        The operator's own list, so a sheet added in Einstellungen can be named
        here too. It used to be five entries ending at A3, so the stock
        actually in the machine could not be picked at all.
        """
        from tools.paper import label, sizes
        return [(label(name), name) for name in sizes()]

    def _on_printer_changed(self):
        """Aktualisiert Papierformat, Duplex und Farbe basierend auf dem gewaehlten Drucker."""
        try:
            from PyQt6.QtPrintSupport import QPrinterInfo, QPrinter
            from PyQt6.QtGui import QPageSize

            printer_name = self.printer_combo.currentData()
            have_info = printer_name and printer_name not in ("lp", "none")
            info = QPrinterInfo.printerInfo(printer_name) if have_info else None
            valid = info is not None and not info.isNull()

            _qt_to_lp = {v: k for k, v in _qt_page_sizes().items()}

            # ── Paper sizes ───────────────────────────────────────────────────
            prev_paper = self.paper_combo.currentData()
            self.paper_combo.blockSignals(True)
            self.paper_combo.clear()

            # First, and selected by default: leave the paper alone. A press
            # with SRA3 loaded was being told "media=A4" on every job, because
            # this list had no way to say "the one that is already in there" —
            # and, for a driverless queue that reports no sizes at all, no way
            # to name SRA3 either. The colour control has worked this way all
            # along; the paper had no equivalent.
            # Not "Drucker-Standard": that reads as "look up the printer's
            # default size and send it", which is the thing this exists to
            # stop — and was, until recently, exactly what happened.
            self.paper_combo.addItem(tr("Wie im Drucker eingestellt"),
                                     PAPER_PRINTER_DEFAULT)

            populated = False
            if valid:
                try:
                    supported = info.supportedPageSizes()
                    if supported:
                        seen = set()
                        for ps in supported:
                            key = _qt_to_lp.get(ps.id(), ps.name())
                            if key in seen:
                                continue
                            seen.add(key)
                            mm = ps.size(QPageSize.Unit.Millimeter)
                            label = f"{ps.name()}  ({mm.width():.0f} × {mm.height():.0f} mm)"
                            self.paper_combo.addItem(label, key)
                        populated = True
                except Exception:
                    logging.debug("printer reported no usable page sizes; "
                                  "falling back to the built-in list", exc_info=True)

            if not populated:
                for label, key in self._FALLBACK_PAPERS:
                    self.paper_combo.addItem(label, key)

            # Last: a size no list here has. A driverless press reports nothing
            # to enumerate, so without this there is no way to name the sheet
            # it is actually running.
            self.paper_combo.addItem(tr("Benutzerdefiniert…"), self.PAPER_CUSTOM)

            # Stay on whatever was chosen before; otherwise leave the paper to
            # the printer, which is the first entry. Qt's defaultPageSize() is
            # deliberately not consulted any more — it answered A4 for a queue
            # that reports no page sizes whatsoever, which is a guess wearing
            # the printer's authority.
            target_paper = prev_paper
            if target_paper:
                idx = self.paper_combo.findData(target_paper)
                if idx >= 0:
                    self.paper_combo.setCurrentIndex(idx)
            self.paper_combo.blockSignals(False)

            # ── Duplex ────────────────────────────────────────────────────────
            # Qt's supportedDuplexModes()/defaultDuplexMode() is UNRELIABLE for
            # driverless/IPP printers on Linux/CUPS — it reports DuplexNone even
            # for printers that clearly duplex (verified: Brother HL-L5210DN,
            # EPSON ET-8500). The real job is spooled via lp/CUPS (not QPrinter),
            # and CUPS applies the sides= option regardless, so gating the
            # checkbox on this query wrongly greyed it out (the "cosmetic" bug).
            # Keep the control ALWAYS enabled; use the query only for a default.
            self.duplex_check.setEnabled(True)
            self.duplex_check.setToolTip("")
            default_duplex, edge = False, "long"
            if valid:
                try:
                    dm = info.defaultDuplexMode()
                    default_duplex = (dm != QPrinter.DuplexMode.DuplexNone)
                    if dm == QPrinter.DuplexMode.DuplexShortSide:
                        edge = "short"
                except Exception:
                    logging.debug("Qt has no default duplex mode for this queue",
                                  exc_info=True)
            self.duplex_check.setChecked(default_duplex)
            ei = self.duplex_edge_combo.findData(edge)
            if ei >= 0:
                self.duplex_edge_combo.setCurrentIndex(ei)
            # Explicitly sync the edge combo to the checkbox — setChecked() only
            # emits toggled() when the state actually changes, so an unchanged
            # (still-unchecked) checkbox would otherwise leave the combo stale.
            self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())

            # ── Color ─────────────────────────────────────────────────────────
            # Same lesson as duplex above: Qt's colour query is not to be trusted
            # on driverless/IPP queues. It reports defaultColorMode() ==
            # GrayScale for an EPSON ET-8500 and for both Xerox colour presses
            # here, and the old code turned that into "open in Graustufen" *and*
            # disabled the control — so a colour press printed monochrome and the
            # user could not switch it back.
            #
            # The control is therefore always enabled and always opens on
            # "Drucker-Standard", which sends no colour option at all. Nothing is
            # forced, nothing is destroyed, and the queue (or whoever picks the
            # settings downstream) decides. A mono-only printer is worth a hint,
            # never a lock.
            self.color_combo.setEnabled(True)
            self.color_combo.blockSignals(True)
            self.color_combo.setCurrentIndex(0)          # "Drucker-Standard"
            self.color_combo.blockSignals(False)
            mono_only = False
            if valid:
                try:
                    modes = info.supportedColorModes()
                    mono_only = bool(modes) and all(
                        m == QPrinter.ColorMode.GrayScale for m in modes)
                except Exception:
                    mono_only = False
            self.color_combo.setToolTip(
                tr("Dieser Drucker meldet nur Graustufen — die Farbe bleibt in "
                   "der Datei erhalten und kann anderswo gedruckt werden.")
                if mono_only else tr(
                    "Drucker-Standard: keine Vorgabe senden — der Drucker bzw. "
                    "die Warteschlange entscheidet.\n"
                    "Die Farbinformation bleibt in jedem Fall in der Datei erhalten."))

            # ── Hardware margins (determines "Fit Page" / "Shrink" behaviour) ──
            self._hw_margin_mm = 3.0  # safe default
            if valid:
                try:
                    tmp = QPrinter(info, QPrinter.PrinterMode.ScreenResolution)
                    layout    = tmp.pageLayout()
                    paper_r   = layout.fullRect(QPageLayout.Unit.Millimeter)
                    page_r    = layout.paintRect(QPageLayout.Unit.Millimeter)
                    margin = min(
                        abs(page_r.left()    - paper_r.left()),
                        abs(page_r.top()     - paper_r.top()),
                        abs(paper_r.right()  - page_r.right()),
                        abs(paper_r.bottom() - page_r.bottom()),
                    )
                    self._hw_margin_mm = max(0.0, margin)
                except Exception:
                    logging.warning("Hardware margin detection failed", exc_info=True)

            self._update_margin_label()
            self._sync_preview()
            # Everything above was the dialog setting itself up, not the user.
            self._settings_touched = False

            # Last, not first. Everything above is what Qt believes, and Qt is
            # the weakest source — it is consulted only because it answers
            # instantly. Asking CUPS from the top of this method looked harmless
            # while the answer arrived later, but the answer is cached for the
            # session and a cached one is applied on the spot, after which every
            # line above overwrote it. That is why the second and every later
            # open came back to Letter and to two-sided, and why a remembered
            # setting did not survive: the restore happens in the same step.
            self._load_queue_info(printer_name)

        except Exception:
            logging.warning("Printer capability query failed", exc_info=True)

    def _update_margin_label(self):
        """Updates the info label below the scale combo to reflect hardware margins."""
        m = self._hw_margin_mm
        if m < 0.5:
            text = tr("Randloser Druck — bei gleichem Seitenformat keine Skalierung")
            tip  = tr("Dieser Drucker unterstützt randlosen Druck (full-bleed). "
                      "'An Seite anpassen' ändert eine A4-Seite auf A4-Papier nicht.")
        else:
            text = tr("Druckrand: ca. {m:.1f} mm  (roter Rahmen in Vorschau)").format(m=m)
            tip  = tr("Ca. {m:.1f} mm Hardware-Rand kann nicht bedruckt werden. "
                      "'An Seite anpassen' verkleinert den Inhalt auf den bedruckbaren Bereich.").format(m=m)
        self._margin_lbl.setText(text)
        self._margin_lbl.setToolTip(tip)
        self.scale_shrink.setToolTip(tip)

    def _sync_preview(self):
        """Push current dialog settings into the preview widget."""
        self._preview.update_settings(
            scale_idx  = self._scale_index(),
            scale_pct  = self.scale_pct.value(),
            paper_key  = self.selected_paper(),
            orient_idx = self.orient_combo.currentIndex(),
            margin_mm  = self._hw_margin_mm,
        )

    def _detect_pdf_paper(self):
        """Auto-select the paper size that best matches the PDF's first page."""
        try:
            from pypdf import PdfReader
            reader = PdfReader(self.pdf_path, strict=False)
            if not reader.pages:
                return
            box = reader.pages[0].mediabox
            pw = float(box.width);  ph = float(box.height)
            if pw > ph:             # normalise to portrait for comparison
                pw, ph = ph, pw
            best_key, best_diff = None, float("inf")
            from tools.paper import sizes as _paper_sizes
            for key, (w, h) in _paper_sizes().items():
                diff = abs(pw - w) + abs(ph - h)
                if diff < best_diff and diff < 15:   # 15 pt ≈ 5 mm tolerance
                    best_diff, best_key = diff, key
            if best_key:
                idx = self.paper_combo.findData(best_key)
                if idx >= 0:
                    self.paper_combo.setCurrentIndex(idx)
                    self._sync_preview()
        except Exception:
            logging.debug("could not guess the paper size from the PDF",
                          exc_info=True)

    def _get_pages(self):
        """Returns list of page indices to print, or None on validation error."""
        n = len(self.model.order)
        if self.radio_all.isChecked():
            return list(range(n))
        elif self.radio_current.isChecked():
            cur = self._current_page_pos()
            if cur is None:
                # Never quietly substitute page 1 for the page the user is
                # looking at — that prints the wrong sheet and says nothing.
                self.status_lbl.setText(tr(
                    "Aktuelle Seite kann nicht ermittelt werden — bitte "
                    "»Alle Seiten« oder einen Bereich waehlen."))
                return None
            return [cur]
        else:
            text = self.range_edit.text().strip()
            if not text:
                return list(range(n))
            pages = []
            try:
                for part in text.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        lo, hi = int(a.strip()), int(b.strip())
                        if lo < 1 or hi > n or lo > hi:
                            raise ValueError()
                        pages.extend(range(lo - 1, hi))
                    else:
                        p = int(part)
                        if p < 1 or p > n:
                            raise ValueError()
                        pages.append(p - 1)
            except ValueError:
                self.status_lbl.setText(
                    tr("Ungültiger Seitenbereich — bitte Zahlen zwischen 1 und {n} eingeben.").format(n=n))
                return None
            pages = [p for p in sorted(set(pages)) if 0 <= p < n]
            if not pages:
                self.status_lbl.setText(
                    tr("Kein gültiger Seitenbereich für {n}-seitige Datei.").format(n=n))
                return None
            return pages


    def _set_printing(self, busy):
        """Disable/re-enable controls while a print job is in progress."""
        for w in [self.printer_combo, self.copies_spin,
                  self.paper_combo, self.orient_combo,
                  self.color_combo,
                  self.collate_check, self.duplex_check, self.duplex_edge_combo,
                  self.source_combo,
                  self.radio_all, self.radio_current, self.radio_range,
                  self.range_edit]:
            w.setEnabled(not busy)
        for btn in self.findChildren(QPushButton):
            btn.setEnabled(not busy)
        # The edge selector is only usable while duplex is on — re-sync it to
        # the checkbox after a job so it doesn't stay enabled when duplex is off.
        if not busy:
            self.duplex_edge_combo.setEnabled(self.duplex_check.isChecked())
            # Likewise the tray: a queue that offers no choice of one must not
            # come back from a job with an empty combo suddenly enabled.
            self.source_combo.setEnabled(self.source_combo.count() > 1)

    def _do_print(self):
        pages_to_print = self._get_pages()
        if pages_to_print is None:
            return
        if not pages_to_print:
            self.status_lbl.setText(tr("Keine Seiten ausgewählt."))
            return

        printer_name = self.printer_combo.currentData()
        if printer_name == "none":
            self.status_lbl.setText(tr("Kein Drucker verfügbar."))
            return

        copies    = self.copies_spin.value()
        color_mode = self.color_combo.currentData() or "auto"
        # Farbkonvertierung was removed from the dialog (settled decision 1);
        # the spooler still takes the parameter, so hand it the fixed value.
        colorconv = 0
        collate   = self.collate_check.isChecked()
        duplex    = self.duplex_check.isChecked()
        duplex_edge = self.duplex_edge_combo.currentData() or "long"
        choice = self.source_combo.currentData()         # None = printer default
        paper_source = (self._source_keyword, choice) if choice else None
        scale_idx  = self._scale_index()
        scale_pct  = self.scale_pct.value()
        paper_key  = self.selected_paper()
        orient_idx = self.orient_combo.currentIndex()

        try:
            from tools.pdf_access import is_locked
            if is_locked(self.pdf_path):
                self.status_lbl.setText(
                    tr("Fehler: PDF ist passwortgeschützt — "
                       "bitte zuerst entsperren."))
                return
        except Exception:
            logging.debug("could not check whether the PDF is encrypted; "
                          "letting the print attempt report it", exc_info=True)

        n = len(pages_to_print)
        self.status_lbl.setText(
            tr("Sende {n} Seite(n) an »{name}«…").format(
                n=n, name=self.printer_combo.currentText()))
        self._set_printing(True)

        # Progress popup — shows the transfer stages as they happen.
        from PyQt6.QtWidgets import QProgressDialog
        self._progress = QProgressDialog(
            tr("Druckauftrag wird vorbereitet…"), "", 0, 100, self)
        self._progress.setWindowTitle(tr("Drucken"))
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setCancelButton(None)       # a spooling job can't be cancelled
        self._progress.setMinimumDuration(0)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.setValue(8)
        self._progress.show()
        QApplication.processEvents()

        # ── Query printer DPI on GUI thread — QPrinter must not be used from ──
        # ── background threads (Qt constraint).                               ──
        try:
            from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
            if printer_name and printer_name not in ("lp", "none"):
                _pi = QPrinterInfo.printerInfo(printer_name)
                _qp = (QPrinter(_pi, QPrinter.PrinterMode.HighResolution)
                       if not _pi.isNull()
                       else QPrinter(QPrinter.PrinterMode.HighResolution))
            else:
                _qp = QPrinter(QPrinter.PrinterMode.HighResolution)
            qt_dpi = _qp.resolution()
            del _qp
        except Exception:
            qt_dpi = 600   # laser-printer safe default
        as_bitmap = self.prints_as_bitmap()
        qt_dpi = self._raster_dpi(qt_dpi)
        hw_margin_mm = self._hw_margin_mm   # capture now; _on_printer_changed won't run in bg

        import shutil, weakref
        self_ref = weakref.ref(self)

        def _report(msg):
            obj = self_ref()
            if obj is not None:
                try:
                    obj._print_status.emit(msg)   # queued to the GUI thread
                except RuntimeError:
                    pass

        def _bg(job):
            errors = []

            # ── Primary: Ghostscript + lp/CUPS ───────────────────────────────
            # Skipped entirely for "Als Bitmap": Ghostscript re-interprets the
            # PDF and resolves its fonts a second time, which is the one thing
            # this option exists to avoid. Going through it and rasterising
            # afterwards would print the substitution, not the preview.
            if shutil.which("lp") and not as_bitmap:
                try:
                    skipped = print_via_gs(self.pdf_path, self.model,
                        pages_to_print, copies, color_mode, collate, duplex,
                        duplex_edge, colorconv, printer_name, scale_idx,
                        paper_key, orient_idx, _report,
                        paper_source=paper_source, scale_pct=scale_pct)
                    obj = self_ref()
                    if obj is not None:
                        obj._print_finished.emit(pages_to_print, copies, skipped)
                    return
                except Exception as e:
                    errors.append(f"GS/lp: {e}")
                    _report(tr("GS-Pfad fehlgeschlagen — Versuche Qt-Fallback…"))

            # ── Fallback: Qt rasteriser ───────────────────────────────────────
            # Pre-render pages in background (pdfium, no QPrinter); draw on GUI thread.
            try:
                rendered, skipped = prerender_for_qt(self.pdf_path, self.model,
                    pages_to_print, color_mode, scale_idx, orient_idx,
                    paper_key, qt_dpi, hw_margin_mm, _report,
                    scale_pct=scale_pct)
            except Exception as e:
                errors.append(f"Qt render: {e}")
                msg = tr("Druckfehler:") + "\n" + "\n".join(errors)
                obj = self_ref()
                if obj is not None and not job.cancelled:
                    obj._print_failed.emit(msg)
                return

            obj = self_ref()
            if obj is not None and not job.cancelled:
                obj._print_qt_send.emit((
                    rendered, skipped, pages_to_print, copies, color_mode,
                    collate, duplex, duplex_edge, printer_name, paper_key,
                    orient_idx, qt_dpi))

        from tools.jobs import submit
        self._print_job = submit(_bg, owner=self, name="print-job")

    def _progress_pct(self, msg):
        """Map a status message to an approximate transfer-progress percentage.

        Matches on language-independent tokens (the ``x / y`` fraction) and on
        both the German and English keyword stems, so translated status text
        still advances the progress bar correctly.
        """
        import re
        m = re.search(r'(\d+)\s*/\s*(\d+)', msg)   # "Seite x / y" / "page x / y"
        if m:   # per-page rendering (Qt fallback) gives a real fraction
            x, tot = int(m.group(1)), max(1, int(m.group(2)))
            return 15 + int(60 * x / tot)
        low = msg.lower()
        if "zusammenstellen" in low or "collecting" in low:  return 20
        if "ghostscript" in low or "normalis" in low:        return 50
        if "sende an drucker" in low or "sending to printer" in low: return 85
        if "fallback" in low or "render" in low:             return 30
        return None

    def _on_print_status(self, msg):
        self.status_lbl.setText(msg)
        if self._progress is not None:
            self._progress.setLabelText(msg)
            pct = self._progress_pct(msg)
            if pct is not None:
                # Only ever advance the bar, never jump backwards.
                self._progress.setValue(max(self._progress.value(), pct))

    def _close_progress(self):
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _on_print_failed(self, msg):
        self._close_progress()
        self.status_lbl.setText(msg)
        self._set_printing(False)   # keep the dialog open so the user can retry

    def _finish(self, pages, copies, skipped):
        # Remember how this was printed, so the next document opens on it.
        # Here rather than at the moment of sending: settings that ended in an
        # error are not the ones to come back to.
        try:
            prefs.remember(self.printer_combo.currentData(),
                           self._current_settings())
        except Exception:
            logging.debug("print: could not remember the settings", exc_info=True)

        # Count what was actually sent, not what was asked for: a page that
        # could not be read was already dropped from the job, so reporting the
        # requested figure told the operator more sheets were coming than the
        # printer had been given — while listing the skipped pages in the same
        # breath.
        sent  = max(0, len(pages) - len(skipped or []))
        total = sent * copies
        msg = tr("Druckauftrag gesendet: "
                 "{pages} Seite(n) × {copies} Kopie(n) = {total} Blatt.").format(
                     pages=sent, copies=copies, total=total)
        if skipped:
            msg += tr("  (Übersprungen: S. {skipped})").format(skipped=skipped)
        self.status_lbl.setText(msg)
        if self._progress is not None:
            self._progress.setLabelText(tr("Fertig — an Drucker gesendet."))
            self._progress.setValue(100)
        # Briefly show 100 %, then close the popup AND the print dialog.
        QTimer.singleShot(600, self._after_print_close)

    def _after_print_close(self):
        self._close_progress()
        self.accept()




    def _qt_send_to_printer(self, rendered, skipped, pages, copies, color_mode,
                             collate, duplex, duplex_edge, printer_name,
                             paper_key, orient_idx, render_dpi=None):
        """Draw pre-rendered images to QPrinter.  MUST run on the GUI thread."""
        from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
        from PyQt6.QtGui import QPageSize, QPainter

        try:
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            if printer_name and printer_name not in ("lp", "none"):
                info = QPrinterInfo.printerInfo(printer_name)
                if not info.isNull():
                    printer = QPrinter(info, QPrinter.PrinterMode.HighResolution)

            # The pages were rasterised for a device of this many dots per
            # inch, and are about to be drawn a dot to a pixel — so the device
            # has to agree, or they land at the wrong size.
            #
            # It used to agree by accident: the resolution was read off a
            # HighResolution QPrinter and handed to the rasteriser, so both
            # ends were the printer's own number. "Als Bitmap" is the first
            # thing that chooses a different one, and 300 dpi of pixels drawn
            # on a 1200 dpi device came out at a quarter size in a corner of
            # the sheet.
            if render_dpi:
                printer.setResolution(int(render_dpi))

            printer.setCopyCount(copies)
            printer.setCollateCopies(collate)
            # Set it either way: leaving it alone means the printer's own
            # default decides, so a queue defaulted to duplex ignored the box
            # being unticked. Same bug as the lp path, same fix.
            printer.setDuplex(
                (QPrinter.DuplexMode.DuplexShortSide if duplex_edge == "short"
                 else QPrinter.DuplexMode.DuplexLongSide) if duplex
                else QPrinter.DuplexMode.DuplexNone)
            if color_mode == "mono":
                printer.setColorMode(QPrinter.ColorMode.GrayScale)
            elif color_mode == "color":
                printer.setColorMode(QPrinter.ColorMode.Color)

            printer.setPageSize(QPageSize(_qt_page_sizes().get(
                paper_key, QPageSize.PageSizeId.A4)))
            if orient_idx == 1:
                printer.setPageOrientation(QPageLayout.Orientation.Portrait)
            elif orient_idx == 2:
                printer.setPageOrientation(QPageLayout.Orientation.Landscape)

            painter = QPainter()
            if not painter.begin(printer):
                raise RuntimeError(tr("QPainter konnte nicht gestartet werden."))
            try:
                first = True
                for pil, page_orient, target_w, target_h in rendered:
                    if orient_idx == 0:
                        printer.setPageOrientation(page_orient)
                    if not first:
                        printer.newPage()
                    first = False

                    pm = pil_to_qpixmap(pil)
                    cx = max(0, int((target_w - pm.width())  / 2))
                    cy = max(0, int((target_h - pm.height()) / 2))
                    painter.drawPixmap(cx, cy, pm)
            finally:
                painter.end()

            self._finish(pages, copies, skipped)

        except Exception as e:
            self._on_print_failed(tr("Qt-Druckfehler: {e}").format(e=e))
