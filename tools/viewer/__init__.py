"""
The viewer, one module per part.

    model        page order, rotation, selection
    canvas       draws a page, selects text on it
    single_page  the one-page view and its zoom
    page_grid    the thumbnails of "Seiten verwalten"
    manage       the toolbar over that grid
    merge        the file-level grid, for several files at once
    tab          one open document
    panel        the tab host

This was one file of 7,600 lines.

Everything here draws; nothing here rasterises. Turning a PDF into pixels is
tools/render/, which this imports and which imports nothing from here. Printing
is tools/printing/ — a tab opens its dialog, and that is the only edge between
them. The palette is tools/theme.py, shared with the tool panels, the print
dialog and the window, so it belongs to none of them.
"""
