"""Minimal /etc/fstab reader (used for canonical mountpoints and mount policy hints)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cygnus.core.storage.mountinfo import unescape


@dataclass(frozen=True, slots=True)
class FstabEntry:
    spec: str  # UUID=..., LABEL=..., /dev/...
    mountpoint: str
    fstype: str
    options: frozenset[str]

    @property
    def uuid(self) -> str | None:
        if self.spec.startswith("UUID="):
            return self.spec[5:].strip('"')
        return None

    @property
    def automount(self) -> bool:
        return "x-systemd.automount" in self.options

    @property
    def nofail(self) -> bool:
        return "nofail" in self.options


def parse(text: str) -> list[FstabEntry]:
    entries: list[FstabEntry] = []
    for raw in text.split("\n"):
        line = raw.strip(" \t")
        if not line or line.startswith("#"):  # a comment starts a line; a "#" inside a field is part of the field
            continue
        parts = re.split(r"[ \t]+", line)  # fields are separated by blanks only, as libmount reads them
        if len(parts) < 3:
            continue
        options = frozenset(parts[3].split(",")) if len(parts) > 3 else frozenset()
        entries.append(FstabEntry(unescape(parts[0]), unescape(parts[1]), parts[2], options))
    return entries


def read(path: Path = Path("/etc/fstab")) -> list[FstabEntry]:
    try:
        return parse(path.read_text())
    except OSError:
        return []
