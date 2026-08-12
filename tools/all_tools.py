"""
Alle Tool-Panels v3.3 — re-export shim.

The panels themselves live in tools/panels/, one module each; this module
imports them back so that ``from tools.all_tools import ...`` and
``import tools.all_tools as T`` keep answering for every name they did when
this file held all 3,502 lines of them.

The wholesale Qt and stdlib imports below are no longer used by anything in
this file. They stay because removing them would narrow what the module
exports, and this module exists to export exactly what it used to.
"""
import os, io, math, subprocess, shutil, logging
from tools.page_viewer import (_TV, _register_themed, _pdfium_lock,
                               _ThumbnailCache, _render_queue,
                               _ThumbTask, _ThumbSignals, pil_to_qpixmap)
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QSpinBox, QDoubleSpinBox, QComboBox, QGroupBox, QCheckBox,
    QListWidget, QListWidgetItem, QRadioButton, QTextEdit,
    QScrollArea, QWidget, QSlider, QApplication, QFileDialog, QFrame,
    QSizePolicy, QSplitter, QGridLayout
)
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import (QPixmap, QImage, QKeySequence, QShortcut,
                         QPainter, QPen, QColor, QBrush)
from tools.app_state import AppState, theme_color
from tools._base     import BasePanel, FileDropList, make_label, make_separator, LogBox, CurrentFileBar
from tools.i18n      import tr

# ── Shared helpers, now in tools/panels/ ─────────────────────────────────────
# Imported rather than defined here, and re-exported: this module is a shim.
from tools.panels._shared     import (MM_TO_PT, PAPER_SIZES_PT, LABEL_W,
                                      _normalized_page, _inherited_rotate,
                                      _visible_box, _visible_size, _mat_mul,
                                      _display_matrix, row, row2, PreviewPane)
from tools.panels._colour     import _colour_histogram, _hist_stats
from tools.panels._verify     import (_VERIFY_SCALE, _BLACKOUT_LIMIT, _page_luma,
                                      _conversion_damage, _verify_pages_intact)
from tools.panels._cropmarks  import (_crop_mark_segments,
                                      _crop_marks_content_stream)
from tools.panels._imposition import (_ROT_MATRIX, _slot_placement,
                                      _flatten_annots)
from tools.panels.merge_split    import MergeSplitPanel
from tools.panels.compress       import CompressPanel, _fmt
from tools.panels.crop_resize    import CropResizePanel
from tools.panels.page_numbers   import PageNumbersPanel
from tools.panels.img_pdf        import ImgPdfPanel
from tools.panels.grayscale      import GrayscalePanel, _grey_retry_page, _grey_vector
from tools.panels.impose         import (ImposePanel, _booklet_sides,
                                         _impose_page_size, _build_impose)
from tools.panels.forms          import FormsPanel, _plain_ink, _flatten_form
from tools.panels.ocr            import (OcrPanel, _run_ocr, tesseract_langs,
                                         _page_has_text, _ocr_with_tesseract)
from tools.panels.preflight      import PreflightPanel
from tools.panels.layers         import LayersPanel
from tools.panels.colour_profile import ColourProfilePanel
from tools.panels.nup            import NUpPanel, _nup_slot_rects, _build_nup
