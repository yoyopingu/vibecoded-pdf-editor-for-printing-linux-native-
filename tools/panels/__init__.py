"""
One module per tool panel, plus the helpers they share.

All of this was tools/all_tools.py, a single 3,502-line file holding thirteen
unrelated panels. It is split here by pure code movement — every body is
byte-identical to what it replaced. tools/all_tools.py remains as a re-export
shim so existing imports keep working.

Shared helpers live in the underscore modules, grouped by what they are for:

  _shared.py      rows, PreviewPane, paper sizes, page geometry
  _verify.py      did a colour conversion black the page out
  _colour.py      how far a page is from neutral grey
  _cropmarks.py   crop / cut marks
  _imposition.py  placing a page into a slot on a sheet
"""
