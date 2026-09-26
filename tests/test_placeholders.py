"""Pending source pages stay identifiable while their pixels are in flight."""
from threading import Event

from PyQt6.QtGui import QColor, QPainter, QPixmap

from tools.render.placeholder import paint_page_number, source_page_index
from tools.viewer.canvas import PdfPageCanvas
from tools.viewer.model import PageModel
from tools.viewer.page_grid import PageCard
from tools.panels.crop_resize import CropResizePanel
from tools.panels.nup import NUpPanel
import tools.panels.crop_resize as crop_resize
import tools.panels.nup as nup
import tools.render.images as render_images
from tests.support import FX, _open, _wait_for


def _pixels(pixmap):
    image = pixmap.toImage()
    bits = image.bits()
    bits.setsize(image.sizeInBytes())
    return bytes(bits)


def test_pending_pages_show_their_original_source_number():
    model = PageModel(3)
    model.order = [2, 0, 1]
    assert source_page_index(model, 0, FX['normal']) == 2

    # The same ink is used by the page canvas and both kinds of preview card.
    first = QPixmap(120, 160); first.fill(QColor('white'))
    third = QPixmap(120, 160); third.fill(QColor('white'))
    for pm, index in ((first, 0), (third, 2)):
        painter = QPainter(pm)
        paint_page_number(painter, pm.rect(), index)
        painter.end()
    assert _pixels(first) != _pixels(third)

    canvas = PdfPageCanvas()
    canvas.resize(120, 160)
    canvas.show_placeholder(0, 0, 120, 160)
    blank_canvas = QPixmap(canvas.size())
    canvas.render(blank_canvas)
    canvas.show_placeholder(0, 0, 120, 160, 2)
    assert canvas._placeholder_page == 2
    numbered_canvas = QPixmap(canvas.size())
    canvas.render(numbered_canvas)
    assert _pixels(blank_canvas) != _pixels(numbered_canvas)

    blank_card = PageCard(0, 2, None)
    card = PageCard(0, 2, None, page_index=2)
    assert card._page_index == 2 and card.pixmap() is None
    blank_pm = QPixmap(blank_card.size())
    numbered_pm = QPixmap(card.size())
    blank_card.render(blank_pm)
    card.render(numbered_pm)
    assert _pixels(blank_pm) != _pixels(numbered_pm)


def test_crop_pane_does_not_wait_for_a_complex_source_page():
    _open(FX['normal'])
    panel = CropResizePanel()
    started = Event()
    release = Event()
    original = crop_resize._render_base_page

    def slow_render(path, index):
        started.set()
        assert release.wait(5), 'test render was never released'
        return original(path, index)

    crop_resize._render_base_page = slow_render
    try:
        pm, info = panel._render_preview(300, 420, 1.0, async_load=True)
        assert pm is not None and 'rendert' in info
        assert panel._base_pending_key is not None
        assert _wait_for(started.is_set)
        # Refreshes and option changes remain usable while the worker waits.
        again, _ = panel._render_preview(240, 340, 1.25, async_load=True)
        assert again is not None and getattr(panel, '_base_cache', None) is None
    finally:
        release.set()
        crop_resize._render_base_page = original
    assert _wait_for(lambda: panel._base_pending_key is None)
    pm, info = panel._render_preview(300, 420, 1.0, async_load=True)
    assert pm is not None and 'rendert' not in info


def test_nup_pane_does_not_wait_for_page_geometry():
    _open(FX['normal'])
    panel = NUpPanel()
    started = Event()
    release = Event()
    original = nup._read_page_dims

    def slow_dimensions(path, index):
        started.set()
        assert release.wait(5), 'test dimension read was never released'
        return original(path, index)

    nup._read_page_dims = slow_dimensions
    try:
        pm, info = panel._render_preview(300, 420, 1.0, async_dims=True)
        assert pm is not None and 'rendert' in info
        assert _wait_for(started.is_set)
        again, _ = panel._render_preview(240, 340, 1.25, async_dims=True)
        assert again is not None and not panel._dims_cache
    finally:
        release.set()
        nup._read_page_dims = original
    assert _wait_for(lambda: panel._dims_pending is None)
    pm, info = panel._render_preview(300, 420, 1.0, async_dims=True)
    assert pm is not None


def test_failed_thumbnail_still_identifies_its_page():
    original = render_images.render_window
    render_images.render_window = lambda *args, **kwargs: None
    try:
        first = render_images._render_image(FX['normal'], 0, 120)
        second = render_images._render_image(FX['normal'], 1, 120)
    finally:
        render_images.render_window = original
    assert _pixels(QPixmap.fromImage(first)) != _pixels(QPixmap.fromImage(second))
