"""
How far a page is from neutral grey, measured over its pixels.

Shared by Grayscale and Preflight. Which colour spaces a page *declares* is a
different question, and that is tools/colorspace.py.
"""


def _colour_histogram(pil_rgb):
    """256-bin histogram of how far each pixel is from neutral grey, where the
    distance is max(|r-g|, |r-b|, |g-b|) — 0 is exactly grey, 255 fully saturated.

    Measured over EVERY pixel of the render. The scan used to squash the page to
    128×128 first and loop over that in Python, which averaged small colour marks
    away: a 1 pt red dot on A4 came out as a distance of 19 instead of 255, so at
    the default threshold of 20 it was invisible and the page was silently
    converted to grey. Losing a red stamp or a coloured logo that way is exactly
    the mistake a copy shop pays for. Pillow does the work in C, so reading every
    pixel is also faster than the old Python loop over the thumbnail."""
    from PIL import ImageChops
    r, g, b = pil_rgb.split()
    d = ImageChops.lighter(
        ImageChops.lighter(ImageChops.difference(r, g), ImageChops.difference(r, b)),
        ImageChops.difference(g, b))
    return d.histogram()


def _hist_stats(hist, thr):
    """(max distance from grey, fraction of pixels above `thr`) for a histogram.

    Derived on demand rather than frozen at scan time: the colour fraction used
    to be computed with whatever the threshold slider happened to be when the
    document was scanned, so moving that slider afterwards changed nothing at all
    in "Nach Anteil farbiger Pixel" mode — the control looked broken because it
    was."""
    total = sum(hist)
    if not total:
        return 0, 0.0
    max_diff = max((b for b, n in enumerate(hist) if n), default=0)
    return max_diff, sum(hist[thr + 1:]) / total
