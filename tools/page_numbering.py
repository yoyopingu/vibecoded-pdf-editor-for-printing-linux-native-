"""Page-number text and visible-sheet placement shared by export and preview."""


def number_label(page_index, total, skip, start, prefix, suffix):
    if page_index < skip:
        return None
    number = start + page_index - skip
    return prefix + str(number) + suffix.replace("{gesamt}", str(total))


def number_position(page_w, page_h, font_size, margin, position):
    """Return (anchor x, PDF baseline y, alignment) in visible-page points."""
    bottom = position < 3
    horizontal = position % 3
    y = margin if bottom else page_h - margin - font_size
    if horizontal == 0:
        return page_w / 2, y, "center"
    if horizontal == 1:
        return margin, y, "left"
    return page_w - margin, y, "right"
