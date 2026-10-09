"""Text from outside (a web page, a model, a server's error message) made safe to show or to send on: one line, no control or
invisible characters (terminal escape sequences, text-direction tricks, zero-width characters), cut to size."""

from __future__ import annotations

import re
import unicodedata

_DROPPED = {"Cc", "Cf", "Cs", "Co", "Cn"}


def plain(value: object, limit: int = 300) -> str:
    """`value` as a single tidy line of at most `limit` characters ("" when it is not text). Line breaks and tabs become spaces
    (so words stay apart); other control, format, surrogate and unassigned characters are removed."""
    if not isinstance(value, str):
        return ""
    kept = [" " if c.isspace() else c for c in value if c.isspace() or unicodedata.category(c) not in _DROPPED]
    return re.sub(r" {2,}", " ", "".join(kept)).strip()[:limit]
