"""
Crop / cut marks. Shared by the Crop tool and N-Up.
"""


def _crop_mark_segments(rects, length=7.0, gap=2.0):
    """L-shaped crop/cut marks at each rectangle corner: two short line segments
    per corner, offset outward from the trim by `gap` and `length` long (PDF
    points). Returns a flat list of (x0, y0, x1, y1) line segments. Shared by the
    N-Up grid marks and the Crop tool's size marks."""
    segs = []
    for (x0, y0, x1, y1) in rects:
        # Corner marks (L-shaped: a horizontal + a vertical tick at each corner).
        for cx, cy, dx, dy in ((x0, y0, -1, -1), (x1, y0, 1, -1),
                               (x0, y1, -1, 1), (x1, y1, 1, 1)):
            segs.append((cx + dx * gap, cy, cx + dx * (gap + length), cy))  # horizontal
            segs.append((cx, cy + dy * gap, cx, cy + dy * (gap + length)))  # vertical
        # Centre marks: on each side, TWO short lines that run ALONG the edge
        # (in the outer margin). Each sits ~2/3 of the way from the page centre
        # toward its corner — so the two marks are closer to the corner marks
        # than to each other, with a wide clear gap in the middle.
        mx = (x0 + x1) / 2; my = (y0 + y1) / 2
        off_x = (x1 - x0) / 2 * (2.0 / 3.0)   # mark centre offset from the side midpoint
        off_y = (y1 - y0) / 2 * (2.0 / 3.0)
        hl = length / 2.0                     # half the mark length
        # Top & bottom edges → horizontal segments near each corner.
        for yy, dy in ((y1, 1), (y0, -1)):
            oy = yy + dy * gap
            for sx in (mx - off_x, mx + off_x):
                segs.append((sx - hl, oy, sx + hl, oy))
        # Left & right edges → vertical segments near each corner.
        for xx, dx in ((x0, -1), (x1, 1)):
            ox = xx + dx * gap
            for sy in (my - off_y, my + off_y):
                segs.append((ox, sy - hl, ox, sy + hl))
    return segs


def _crop_marks_content_stream(rects, length=7.0, gap=2.0):
    """Build a PDF content stream (bytes) that strokes crop marks for `rects`
    as thin black lines. Appended to a page via `page.contents_add(Stream(...))`.
    Used by both N-Up and the Crop tool so the marks are identical everywhere."""
    ops = ["q", "0 0 0 RG", "0.5 w"]
    for (a, b, c, d) in _crop_mark_segments(rects, length, gap):
        ops.append(f"{a:.2f} {b:.2f} m {c:.2f} {d:.2f} l S")
    ops.append("Q")
    return ("\n".join(ops)).encode("latin-1")
