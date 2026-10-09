"""Filesystem helpers: atomic writes and bounded reads."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path


def open_regular(path, *, follow_symlinks: bool = True) -> int:
    """Open a file for reading and return the descriptor, refusing anything that is not a regular file.

    Opened non-blocking and checked with fstat, so a FIFO or device chosen by mistake (or on purpose)
    cannot make the caller wait for a writer that never comes."""
    from cygnus.core.errors import CygnusError

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | (0 if follow_symlinks else os.O_NOFOLLOW)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP and not follow_symlinks:
            raise CygnusError(f"{path} is a link to another file, not the file itself; choose the file it points to") from exc
        raise CygnusError(f"cannot open {path}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise CygnusError(f"{path} is not a regular file")
    return fd


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write via a temp file in the same directory, fsync, then rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
