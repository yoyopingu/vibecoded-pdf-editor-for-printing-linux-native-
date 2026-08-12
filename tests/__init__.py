"""
The regression suite, grouped by subject.

    support.py            the QApplication, the fixture PDFs, shared helpers
    run.py                runs everything without pytest

    test_tools_crop       crop and its cut marks
    test_tools_nup        N-Up and the booklet imposition
    test_tools_colour     greyscale, CMYK, preflight, and the blackout guard
    test_tools_misc       the remaining panels, and that each produces a PDF
    test_merge_preview    opening several files at once
    test_manage           "Seiten verwalten", and rotation reaching everything
    test_printing         what actually gets spooled
    test_viewer_zoom      zoom, deep zoom, and the window renderer
    test_render           caches, the document cache, and background jobs
    test_app              the window, single instance, theming

Every test body here is the one it had in tests/test_copyshop.py, which was one
file of 3,296 lines; only where they live changed.
"""
