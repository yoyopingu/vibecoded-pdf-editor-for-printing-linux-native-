"""Compatibility path for the scrollbar shared with tool previews."""

from tools.scrollbar import (SLIM_W, SlimScrollBar, slim_qss,  # noqa: F401
                             use_slim_scrollbars)             # noqa: F401

__all__ = ("SLIM_W", "SlimScrollBar", "slim_qss", "use_slim_scrollbars")
