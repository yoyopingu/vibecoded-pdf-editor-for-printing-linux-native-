"""
Printing — its own subsystem, not part of the viewer.

    dialog    what to print, on what, and how
    preview   the sheet as the printer will produce it
    handling  poster / n-up / booklet PDFs the spooler then sends at 100 %
    content   Acrobat Comments & Forms: form fields, comments, stamps
    spool     sending it: Ghostscript and lp, or Qt

A tab opens the dialog; nothing else here is reachable from the rest of the app.
"""
