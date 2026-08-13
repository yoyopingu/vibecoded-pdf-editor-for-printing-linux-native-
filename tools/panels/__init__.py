"""
One module per tool panel, plus the helpers they share.

All of this was a single 3,502-line module holding thirteen unrelated panels.
It was split by pure code movement — every body byte-identical to what it
replaced. Panels are imported from their own modules now; there is no
aggregating module left.

Shared helpers live in the underscore modules, grouped by what they are for:

  _shared.py      rows, PreviewPane, paper sizes, page geometry
  _verify.py      did a colour conversion black the page out
  _colour.py      how far a page is from neutral grey
  _cropmarks.py   crop / cut marks
  _imposition.py  placing a page into a slot on a sheet
"""
