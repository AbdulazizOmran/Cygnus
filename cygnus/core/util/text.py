"""Text that came from somewhere else (a catalogue, a package list, a web answer) reduced to something safe to show."""

from __future__ import annotations


def plain(text: str, limit: int) -> str:
    """One line of printable text no longer than `limit`: control, format and bidi characters become spaces, runs of white space become one space, and a
    cut ends in an ellipsis."""
    text = " ".join(("".join(c if c.isprintable() else " " for c in text)).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
