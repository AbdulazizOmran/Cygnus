"""What the online catalogues of other systems' programs (Debian's, Fedora's) have in common: reading a compressed text with a limit, the patterns a
kept field must match, and the AUR's list of package names (a program the AUR has is left out of those catalogues: the Arch way is preferred)."""

from __future__ import annotations

import gzip
import io
import lzma
import re
from collections.abc import Iterable, Iterator

from typing import Any

from cygnus.core.errors import CygnusError
from cygnus.core.util.text import plain

AUR_NAMES = "https://aur.archlinux.org/packages.gz"
AUR_HOST = "aur.archlinux.org"
CHUNK = 1 << 16

MAX_NAME, MAX_SUMMARY = 120, 300

CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,29}\Z")
APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\Z")
HTTPS = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def aur_host_ok(host: str) -> bool:
    return host == AUR_HOST


def _reader(data: bytes, kind: str):
    if kind == "xz":
        return lzma.LZMAFile(io.BytesIO(data))
    if kind == "gz":
        return gzip.GzipFile(fileobj=io.BytesIO(data))
    if kind == "zst":
        from compression import zstd

        return zstd.ZstdFile(io.BytesIO(data))
    raise ValueError(kind)


def _damaged(who: str) -> CygnusError:
    return CygnusError(f"a list from {who} could not be unpacked (it may be damaged)")


def chunks(data: bytes, kind: str, limit: int, who: str) -> Iterator[bytes]:
    """The bytes of a compressed file ("xz", "gz" or "zst"), decompressed in pieces. More than `limit` bytes, or damaged data, is an error in words,
    so a small download cannot become an enormous text."""
    if not data:
        raise _damaged(who)  # nothing came: there is no such thing as an empty list that was sent complete
    total = 0
    try:
        with _reader(data, kind) as reader:
            while True:
                piece = reader.read(CHUNK)
                if not piece:
                    return
                total += len(piece)
                if total > limit:
                    raise CygnusError(f"a list from {who} is larger than Cygnus will read")
                yield piece
    except CygnusError:
        raise
    except Exception as exc:  # noqa: BLE001 - every decompressor has its own errors (lzma.LZMAError, zstd.ZstdError, gzip.BadGzipFile, EOFError, OSError)
        raise _damaged(who) from exc


MAX_LINE = 1024 * 1024  # a real list has lines of a few kilobytes; a longer one is not a list


def text_lines(data: bytes, kind: str, limit: int, who: str) -> Iterator[str]:
    """The lines of a compressed text file, decoded leniently, line ends removed. A line longer than MAX_LINE is an error in words (and reading never
    gets slower the longer a line is: each piece is searched for a line end once)."""
    parts: list[bytes] = []
    held = 0
    for piece in chunks(data, kind, limit, who):
        start = 0
        while True:
            end = piece.find(b"\n", start)
            if end < 0:
                break
            parts.append(piece[start:end])
            yield b"".join(parts).decode("utf-8", "replace").rstrip("\r")
            parts, held, start = [], 0, end + 1
        tail = piece[start:]
        if tail:
            parts.append(tail)
            held += len(tail)
            if held > MAX_LINE:
                raise CygnusError(f"a list from {who} has a line far longer than expected, so it was not read")
    if parts:
        yield b"".join(parts).decode("utf-8", "replace").rstrip("\r")


def aur_names(lines: Iterable[str]) -> set[str]:
    """The names of the packages in the AUR (one per line, with a comment line first)."""
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def several(app: dict[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """A package that holds several applications is one entry that is named after the package (naming it after one of them would be arbitrary), says
    which programs it installs, and keeps the first one's id and categories. `names` are the distinct names of its applications."""
    names = list(names)
    shown = ", ".join(names[:4]) + (", …" if len(names) > 4 else "")
    return {**app, "name": plain(app["package"], MAX_NAME), "summary": plain(f"Installs {len(names)} programs: {shown}", MAX_SUMMARY)}


MAX_PER_PACKAGE = 200  # applications of one package that are looked at: a package with more is not one a person is looking for


class Group:
    """The first application of a package and the distinct names and ids of all of them (up to MAX_PER_PACKAGE), without a list scan per application."""

    __slots__ = ("app", "names", "ids")

    def __init__(self, app: dict[str, Any], name: str, app_id: str):
        self.app, self.names, self.ids = app, {name: None}, {app_id: None}

    def add(self, name: str, app_id: str) -> None:
        if len(self.names) < MAX_PER_PACKAGE:
            self.names.setdefault(name, None)
        if len(self.ids) < MAX_PER_PACKAGE:
            self.ids.setdefault(app_id, None)

    def entry(self) -> dict[str, Any]:
        base = several(self.app, self.names) if len(self.names) > 1 else self.app
        return {**base, "ids": list(self.ids)[:8]}
