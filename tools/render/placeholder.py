"""The page number shown wherever source pixels have not arrived yet."""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont
from tools.theme import _TV


def paint_page_number(painter, rect, page_index):
    """Draw a one-based source page number in an already painted page well.

    This is deliberately only ink: each preview owns its paper, margins and
    transforms, and the stand-in must keep that geometry while rendering runs.
    """
    if page_index is None:
        return
    painter.save()
    painter.setPen(QColor(_TV['dim']))
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(max(12, min(72, int(min(rect.width(), rect.height()) * 0.34))))
    painter.setFont(font)
    painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), str(page_index + 1))
    painter.restore()


def source_page_index(model, position, default_path):
    """Resolve a displayed position to its page index in the source file."""
    if model is not None and 0 <= position < len(model.order):
        uid = model.order[position]
        _, original = model.page_source(uid, default_path)
        return original
    return position
