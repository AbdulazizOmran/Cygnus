"""Bounded readers for untrusted compressed streams."""

from __future__ import annotations

import bz2
import gzip
import io
import lzma

from cygnus.core.errors import UnsafeInputError


class CappedReader(io.RawIOBase):
    """Read-only stream that refuses to deliver more than `cap` bytes in total.

    Used underneath tarfile in streaming mode: tarfile then reads in small chunks, so a header
    claiming a multi-gigabyte name or pax record fails at the cap instead of allocating it.
    """

    def __init__(self, raw, cap: int):
        self._raw = raw
        self._cap = cap
        self.total = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        want = len(b)
        if self.total + want > self._cap:
            want = self._cap - self.total
            if want <= 0:
                raise UnsafeInputError(f"decompressed data exceeds the {self._cap // 2**20} MiB safety limit")
        data = self._raw.read(want)
        n = len(data)
        b[:n] = data
        self.total += n
        return n

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._cap - self.total + 1
        buf = bytearray(min(size, 1 << 20))
        out = bytearray()
        while len(out) < size:
            chunk = memoryview(buf)[: min(len(buf), size - len(out))]
            n = self.readinto(chunk)
            if not n:
                break
            out += chunk[:n]
        return bytes(out)


def decompressor(fileobj, compression: str):
    """Wrap a binary file object in a streaming decompressor for '', gz, xz, bz2 or zst."""
    if compression in ("", "none", "tar"):
        return fileobj
    if compression == "gz":
        return gzip.GzipFile(fileobj=fileobj, mode="rb")
    if compression == "xz":
        return lzma.LZMAFile(fileobj, mode="rb")
    if compression == "bz2":
        return bz2.BZ2File(fileobj, mode="rb")
    if compression == "zst":
        from compression import zstd

        return zstd.ZstdFile(fileobj, mode="rb")
    raise UnsafeInputError(f"unsupported compression {compression!r}")
