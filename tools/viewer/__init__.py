"""
The viewer, one module per part.

    theme        colours, and the widgets that follow them
    model        page order, rotation, selection
    canvas       draws a page, selects text on it
    single_page  the one-page view and its zoom
    page_grid    the thumbnails of "Seiten verwalten"
    manage       the toolbar over that grid
    merge        the file-level grid, for several files at once
    printing     the print dialog and its preview
    tab          one open document
    panel        the tab host

This was tools/page_viewer.py, one file of 7,600 lines. tools/page_viewer.py is
now a shim that re-exports these, so existing imports keep working; it also
carries the map of where everything went.

Everything here draws; nothing here rasterises. Turning a PDF into pixels is
tools/render/, which this imports and which imports nothing from here.
"""
