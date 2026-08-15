"""
Where the page sits on screen: how big it is, how far it is zoomed, and how far
it has been scrolled.

Six numbers, and the arithmetic built on them. They were fields of
SinglePageView, read and written by both of the things that draw a page — the
whole-page renderer and the window renderer used past MAX_RENDER_PX — and by
the wheel, the zoom, the scrollbar-less scrolling and the size readout. Shared
mutable state with no name is why single_page.py could not be split: every
attempt at a seam ran straight through it, and the window renderer alone
reached into fourteen attributes of the view.

Naming it does not make the sharing go away. It makes it something both
renderers can be handed instead of something they reach for.

Points against pixels
---------------------
`page_w_pt` and `page_h_pt` are the page *as displayed* — a quarter turn has
already swapped them — because everything computed from them is about what is
on screen. The scale that converts them to pixels is snapped so the page is a
whole number of pixels across, which is the only kind that can be rendered;
without that the scale laid out with and the scale rendered at differ in the
seventh decimal, and every pan reads as a new zoom.
"""
from tools.render.images import MAX_RENDER_PX
from tools.render.region import page_px_size, snap_scale


class PageLayout:
    """The page's size, zoom and scroll, and what follows from them."""

    def __init__(self):
        self.page_w_pt = 0.0     # the page as displayed, in points
        self.page_h_pt = 0.0
        self.zoom      = 1.0     # 1.0 = fit to window
        self.scroll_x  = 0.0     # how far in, in displayed pixels
        self.scroll_y  = 0.0
        # "put me at the bottom of this page once its height is known" — see
        # place_scroll. Never expressed as a coordinate: it used to be written
        # into scroll_y as 999999 and left there on any page whose height
        # nothing had measured, which stranded the view a thousand wheel
        # clicks from the top.
        self.want_bottom = False

    # ── what the numbers mean on screen ──────────────────────────────────────

    def display_scale(self, avail_w, avail_h, zoom=None):
        """Pixels per point the page is shown at, or None if it is unmeasured.

        No ceiling: past MAX_RENDER_PX the page is not rendered in one piece any
        more, it is rendered a window at a time, so the zoom is free to keep
        going. Snapped — see the module docstring on why.
        """
        zoom = self.zoom if zoom is None else zoom
        if self.page_w_pt <= 0 or self.page_h_pt <= 0:
            return None
        if avail_w < 16 or avail_h < 16:
            return None
        pad = 16
        fit = min((avail_w - pad) / self.page_w_pt,
                  (avail_h - pad) / self.page_h_pt)
        return snap_scale(self.page_w_pt, self.page_h_pt, fit * zoom)

    def page_px(self, scale):
        """The sheet's size on screen in whole pixels at `scale`.

        The same number the renderer works to, so the window it produces lands
        exactly where the view expects it. page_w_pt/page_h_pt already describe
        the page as displayed, hence rotation=0 here.
        """
        return page_px_size(self.page_w_pt, self.page_h_pt, scale, 0)

    def display_size(self, avail_w, avail_h, zoom=None):
        """(w, h) in pixels the page occupies at `zoom`, or (None, None).

        What the scroll limits and the zoom anchor are built on. It used to
        clamp to MAX_RENDER_PX, because that really was as large as the page
        could get when a render was one bitmap. With window rendering the page
        on screen keeps growing, and clamping here would have pinned the scroll
        range while the page kept getting bigger under it.
        """
        scale = self.display_scale(avail_w, avail_h, zoom)
        if scale is None:
            return None, None
        return self.page_px(scale)

    def needs_window(self, scale):
        """Is the page at this scale too large to render in one piece?"""
        if self.page_w_pt <= 0 or self.page_h_pt <= 0 or scale is None:
            return False
        return max(self.page_w_pt * scale,
                   self.page_h_pt * scale) > MAX_RENDER_PX

    def origin(self, avail_w, avail_h, page_px_w, page_px_h):
        """Where the sheet's top-left corner sits in widget coordinates."""
        return (max(0.0, (float(avail_w) - page_px_w) / 2.0) - self.scroll_x,
                max(0.0, (float(avail_h) - page_px_h) / 2.0) - self.scroll_y)

    # ── moving about ─────────────────────────────────────────────────────────

    def place_scroll(self, page_px_w, page_px_h, avail_w, avail_h):
        """Clamp the scroll to the page, honouring a pending "start at bottom".

        The only place that resolves want_bottom, and it can only do so once
        something knows how tall the page is — which is the whole reason the
        intent is a flag rather than a coordinate.
        """
        max_sx = max(0.0, page_px_w - avail_w)
        max_sy = max(0.0, page_px_h - avail_h)
        if self.want_bottom and page_px_h > 0:
            self.scroll_y = max_sy
            self.want_bottom = False
        self.scroll_x = max(0.0, min(self.scroll_x, max_sx))
        self.scroll_y = max(0.0, min(self.scroll_y, max_sy))

    def to_top(self):
        """A new page, shown from its top."""
        self.scroll_x = 0.0
        self.scroll_y = 0.0
        self.want_bottom = False

    def to_bottom(self):
        """A new page, shown from its bottom — once its height is known."""
        self.scroll_x = 0.0
        self.scroll_y = 0.0
        self.want_bottom = True

    def forget_page_size(self):
        """The page has changed; nothing measured describes it any more."""
        self.page_w_pt = 0.0
        self.page_h_pt = 0.0
