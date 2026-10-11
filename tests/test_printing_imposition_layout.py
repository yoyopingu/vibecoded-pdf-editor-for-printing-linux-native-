"""Independent expectations for imposition ordering and geometry."""
from tools.printing.handling import (
    nup_order, poster_grid, preview_sheets, rotation_to_fit, sheet_size)


def test_vertical_six_up_fills_columns_in_the_requested_direction():
    assert nup_order(6, 3, 2, 'v') == [0, 2, 4, 1, 3, 5]
    assert nup_order(6, 3, 2, 'vr') == [5, 3, 1, 4, 2, 0]
    assert nup_order(6, 3, 2, 'h') == [0, 1, 2, 3, 4, 5]
    assert nup_order(6, 3, 2, 'hr') == [5, 4, 3, 2, 1, 0]


def test_booklet_walks_every_face_including_blanks_and_sheet_ranges():
    sizes = [(200, 300)] * 5
    layouts = preview_sheets(sizes, (300, 400), 1, 'booklet', {})
    assert [[s.page for s in sheet.slots] for sheet in layouts] == [
        [0], [1], [2], [3, 4]]
    assert all(sheet.size == (400, 300) for sheet in layouts)
    layouts = preview_sheets([(200, 300)] * 8, (300, 400), 1, 'booklet',
                             {'side': 'back', 'bind': 'right', 'sheet_from': 2})
    assert [[s.page for s in sheet.slots] for sheet in layouts] == [[4, 3]]


def test_booklet_rotation_off_still_fits_the_whole_page():
    sizes = [(400, 200)] * 4
    off = preview_sheets(sizes, (200, 300), 1, 'booklet', {'booklet_rotate': False})
    on = preview_sheets(sizes, (200, 300), 1, 'booklet', {'booklet_rotate': True})
    slot = off[0].slots[0]
    assert not slot.rotate
    assert slot.rect == (0, 62.5, 150, 75)
    slot = on[0].slots[0]
    assert slot.rotate
    assert slot.rect == (25, 0, 100, 200)
    assert not rotation_to_fit((200, 200), (150, 200))


def test_poster_counts_real_source_sizes_and_every_selected_page():
    layouts = preview_sheets([(200, 300), (300, 200)], (200, 300), 1, 'poster',
                             {'tile_pct': 137, 'overlap_mm': 0})
    # Portrait source: 2 x 2. Landscape source: 3 x 1.
    assert len(layouts) == 7
    assert [sheet.slots[0].page for sheet in layouts] == [0] * 4 + [1] * 3
    assert [sheet.tile for sheet in layouts] == ['A1', 'A2', 'B1', 'B2', 'A1', 'A2', 'A3']
    assert all(abs(a - b) < 1e-6 for a, b in
               zip(layouts[1].slots[0].rect, (-200, 0, 274, 411)))


def test_sheet_geometry_honours_orientation_without_inventing_media():
    sizes = [(400, 200), (300, 600)]
    assert sheet_size((200, 300), 0, sizes, 'nup') == (300, 200)
    assert sheet_size((200, 300), 1, sizes, 'nup') == (200, 300)
    assert sheet_size(None, 1, sizes, 'nup') == (400, 200)
    assert sheet_size(None, 1, sizes, 'booklet') == (600, 600)


def test_poster_counts_actual_overlap_coverage_without_changing_scale():
    assert poster_grid(200, 300, 200, 300, 200) == (2, 2, 2)
    assert poster_grid(200, 300, 200, 300, 200, 3) == (3, 3, 2)
    # Overlap only adds tiles where existing coverage is insufficient.
    assert poster_grid(200, 300, 200, 300, 100, 3) == (1, 1, 1)
    assert poster_grid(200, 300, 200, 300, 150, 3) == (2, 2, 1.5)
    # A fraction of a point beyond the sheet is real content, not rounding.
    assert poster_grid(100.5, 100, 100, 100, 100) == (2, 1, 1)
    assert poster_grid(100.001, 100, 100, 100, 100) == (1, 1, 1)
