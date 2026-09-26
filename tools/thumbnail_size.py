"""Default thumbnail geometry shared by the page and tool grids."""

CARD_W = 260
CARD_H = 367  # A-series sheet ratio, matching the original 187 x 264 cards.


def default_card_width(widget):
    """Start with larger cards on larger monitors; zoom remains user controlled."""
    screen = widget.screen()
    if screen is None:
        return CARD_W
    return max(CARD_W, min(380, round(screen.availableGeometry().width() * 0.16)))


def card_height(width):
    return round(width * CARD_H / CARD_W)
