"""Identify installable inputs by content, never by file extension alone."""

from cygnus.core.detect.dispatch import detect_file, sniff_format

__all__ = ["detect_file", "sniff_format"]
