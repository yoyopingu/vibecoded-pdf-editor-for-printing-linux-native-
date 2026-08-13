"""
The application shell — everything around the documents.

    style       the palette, the stylesheets, the window icon
    settings    persisted preferences and the three dialogs over them
    titlebar    the frameless window's own chrome
    window      MainWindow: the sidebar, and what it switches between
    instance    one app, one window — a second launch hands its files over

This was all of main.py. What is left of the entry point is tools/app.py:
configure logging, build the QApplication, hand off to an already-running
instance if there is one, otherwise open the window.
"""
