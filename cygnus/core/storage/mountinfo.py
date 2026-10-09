"""Parser for /proc/self/mountinfo (see proc(5))."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_OCTAL = re.compile(r"\\([0-7]{3})")


def unescape(value: str) -> str:
    """Decode the octal escapes the kernel uses for space, tab, newline and backslash."""
    return _OCTAL.sub(lambda m: chr(int(m.group(1), 8)), value)


@dataclass(frozen=True, slots=True)
class Mount:
    mount_id: int
    parent_id: int
    major: int
    minor: int
    root: str
    mountpoint: str
    options: frozenset[str]  # per-mount options (ro, nosuid, noexec, ...)
    optional: tuple[str, ...] = field(default=())
    fstype: str = ""
    source: str = ""
    super_options: dict[str, str] = field(default_factory=dict)

    @property
    def dev(self) -> tuple[int, int]:
        return (self.major, self.minor)

    @property
    def read_only(self) -> bool:
        return "ro" in self.options or "ro" in self.super_options

    def has(self, flag: str) -> bool:
        return flag in self.options


def _parse_super_options(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in text.split(","):
        if not item:
            continue
        key, sep, value = item.partition("=")
        result[key] = value if sep else ""
    return result


def parse_line(line: str) -> Mount:
    try:
        return _parse_line(line)
    except (ValueError, IndexError) as exc:
        raise ValueError(f"malformed mountinfo line: {line!r}") from exc


def _parse_line(line: str) -> Mount:
    parts = line.rstrip("\n").split(" ")
    sep = parts.index("-", 6)
    major, minor = parts[2].split(":")
    tail = parts[sep + 1 :]
    if len(tail) < 2:
        raise ValueError("too few fields after the separator")
    return Mount(
        mount_id=int(parts[0]),
        parent_id=int(parts[1]),
        major=int(major),
        minor=int(minor),
        root=unescape(parts[3]),
        mountpoint=unescape(parts[4]),
        options=frozenset(parts[5].split(",")),
        optional=tuple(parts[6:sep]),
        fstype=tail[0],
        source=unescape(tail[1]),
        super_options=_parse_super_options(tail[2] if len(tail) > 2 else ""),
    )


def parse(text: str) -> list[Mount]:
    return [parse_line(line) for line in text.split("\n") if line.strip()]


def read(path: Path = Path("/proc/self/mountinfo")) -> list[Mount]:
    return parse(path.read_text())


def mount_for_path(mounts: list[Mount], path: str) -> Mount | None:
    """Return the mount that contains `path` (longest mountpoint prefix, last mounted wins).

    `path` must already be absolute and normalized; it is not resolved here so
    that callers can avoid touching automount points.
    """
    best: Mount | None = None
    best_len = -1
    for m in mounts:  # mountinfo is in mount order, so ">=" lets later (over-)mounts win
        mp = m.mountpoint.rstrip("/") or "/"
        if mp == "/" or path == mp or path.startswith(mp + "/"):
            if len(mp) >= best_len:
                best, best_len = m, len(mp)
    return best
