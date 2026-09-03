"""Pure coordinate helpers for the Windows danmaku overlay."""

from __future__ import annotations


def initial_message_x(
    screen_left: int,
    screen_right: int,
    text_width: int,
    margin: int = 1,
) -> float:
    """Place a new message just outside the right edge for marquee entry.

    The text starts outside the visible desktop and moves left through the
    existing animation loop.  ``margin`` controls the initial gap outside the
    right edge; the text width and left edge are retained in the signature for
    compatibility with the overlay caller.
    """

    del screen_left, text_width
    return float(int(screen_right) + max(1, int(margin)))
