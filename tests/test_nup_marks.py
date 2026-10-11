"""N-Up trim marks follow placed page edges, not unused cell padding."""
import os
from unittest.mock import patch

import pikepdf
from reportlab.pdfgen import canvas

from tests.support import _TMP, _app, _open, _wait_for
from tools.jobs import null_progress
from tools.panels.nup import NUpPanel, _build_nup
from tools.panels._cropmarks import _crop_mark_segments
from tools.render.images import _render_image


def _source():
    path = os.path.join(_TMP, 'mixed-nup-mark-source.pdf')
    c = canvas.Canvas(path)
    for size, colour in (((200, 100), (1, 0, 0)),
                         ((300, 300), (0, 1, 0)),
                         ((120, 120), (0, 0, 1))):
        c.setPageSize(size)
        c.setFillColorRGB(*colour)
        c.rect(0, 0, *size, fill=1, stroke=0)
        c.showPage()
    c.save()
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        # A nonzero CropBox plus /Rotate: visible page is 100 x 200 points.
        pdf.pages[1].CropBox = [50, 40, 250, 140]
        pdf.pages[1].Rotate = 90
        pdf.save(path)
    return path


def test_nup_trim_marks_match_real_page_edges_in_preview_and_pdf():
    source = _source()
    cases = ((None, 500, 190, [(40, 315, 280, 435),
                              (372.5, 280, 467.5, 470),
                              (65, 50, 255, 240)]),
             (1.0, 560, 220, [(60, 370, 260, 470),
                              (370, 320, 470, 520),
                              (100, 100, 220, 220)]))
    images = {i: _render_image(source, i, 400) for i in range(3)}
    for fixed, height, slot_height, expected in cases:
        # Asymmetric margins, two rows/columns, nonzero gutters, one blank slot.
        params = (600, height, 30, 50, 40, 60, 20, 40, 240, slot_height, 2, 2)
        out = os.path.join(_TMP, f'nup-marks-{height}.pdf')
        _build_nup(source, out, [0, 1, 2, None], params, 4, null_progress(),
                   crop_marks=True, fixed_scale=fixed)
        segments = []
        with pikepdf.open(out) as pdf:
            start = None
            for operands, operator in pikepdf.parse_content_stream(pdf.pages[0]):
                if str(operator) == 'm':
                    start = tuple(float(v) for v in operands)
                elif str(operator) == 'l':
                    segments.append(start + tuple(float(v) for v in operands))
        expected_segments = _crop_mark_segments(expected)
        assert len(segments) == len(expected_segments), 'blank cells must not get trim marks'
        for actual, wanted in zip(segments, expected_segments):
            assert all(abs(a - b) <= .006 for a, b in zip(actual, wanted)), (actual, wanted)

        _open(source)
        panel = NUpPanel()
        try:
            panel.cols.setValue(2); panel.rows.setValue(2)
            panel.crop_marks.setChecked(True)
            panel.full_scale.setChecked(fixed is not None)
            panel._get_layout_params = lambda *_, params=params: params
            panel._page_image = lambda _path, page, _width: images[page]
            shown = []
            def marks(rects):
                shown.extend(rects)
                return _crop_mark_segments(rects)
            with patch('tools.panels.nup._crop_mark_segments', side_effect=marks):
                preview, _ = panel._render_preview(1200, height * 2, 1.0)
            assert len(shown) == len(expected)
            for actual, wanted in zip(shown, expected):
                assert all(abs(a - b) < .001 for a, b in zip(actual, wanted)), (actual, wanted)
            preview = preview.toImage()
            exported = _render_image(out, 0, 1200)
            for rect, colour in zip(expected, ('#ff0000', '#00ff00', '#0000ff')):
                x0, y0, x1, y1 = rect
                for image in (preview, exported):
                    assert image.pixelColor(round(x0 + x1), round((height - (y0 + y1)/2)*2)).name() == colour
                    # The horizontal bottom-left crop tick must line up with
                    # the actual page edge, even when the cell has padding.
                    x, y = round((x0 - 5.5) * 2), round((height - y0) * 2)
                    assert image.pixelColor(x, y).lightness() < 150
        finally:
            panel.close(); panel.deleteLater(); _app.processEvents()


def test_nup_reads_visible_sheet_geometry_in_one_background_job():
    source = _source()
    _open(source)
    panel = NUpPanel()
    try:
        panel.cols.setValue(2); panel.rows.setValue(2)
        panel.crop_marks.setChecked(True)
        # Warm only the representative page, as on initial preview entry.
        panel._page_dims(source)
        panel._page_image = lambda *a: None
        with patch('tools.panels.nup._read_page_dims', side_effect=AssertionError('GUI metadata read')):
            panel._render_preview(600, 500, 1.0, async_dims=True)
            assert _wait_for(lambda: all((source, i) in panel._dims_cache for i in range(3)))
            assert panel._dims_cache[(source, 1)][1:3] == (100, 200)
            panel._render_preview(600, 500, 1.0, async_dims=True)
    finally:
        panel.close(); panel.deleteLater(); _app.processEvents()
