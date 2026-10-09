"""Arch package (.pkg.tar.*) inspection: reads .PKGINFO without extracting files."""

from __future__ import annotations

import tarfile
from pathlib import Path

from cygnus.core.errors import DetectionError
from cygnus.core.models import Candidate, PackageFormat, Severity
from cygnus.core.util.limits import CappedReader, decompressor

_MULTI = {"depend", "optdepend", "makedepend", "checkdepend", "conflict", "provides", "replaces",
          "backup", "license", "group", "xdata"}
_MAX_PKGINFO = 1024 * 1024
_MAX_LEADING_MEMBERS = 16
# Metadata members precede the payload; real packages need well under 1 MiB to reach them.
_MAX_LEADING_BYTES = 32 * 1024 * 1024

_COMPRESSION_MAGIC = [
    (b"\x28\xb5\x2f\xfd", "zst"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x1f\x8b", "gz"),
    (b"BZh", "bz2"),
]


def compression_of(head: bytes) -> str | None:
    for magic, name in _COMPRESSION_MAGIC:
        if head.startswith(magic):
            return name
    if len(head) >= 262 and head[257:262] == b"ustar":
        return ""  # uncompressed tar
    return None


def parse_pkginfo(text: str) -> dict[str, list[str] | str]:
    """Read .PKGINFO the way libalpm does: only "\n" ends a line (str.splitlines would also split
    on \r, \x1c and others, letting a package show one name here and install another), and keys
    are taken literally (" pkgname" is not "pkgname")."""
    info: dict[str, list[str] | str] = {}
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(" = ")
        if not sep:
            continue
        if key in _MULTI:
            info.setdefault(key, [])
            info[key].append(value)  # type: ignore[union-attr]
        else:
            info[key] = value
    return info


def read_leading_members(path: Path, compression: str) -> tuple[str | None, list[str]]:
    """Stream the archive and return (.PKGINFO text, names of leading dot-files).

    Decompressed bytes are capped, so hostile headers (huge GNU long names, pax records or
    dot-file members) fail at the cap instead of exhausting memory or CPU.
    """
    pkginfo: str | None = None
    dotfiles: list[str] = []
    try:
        with open(path, "rb") as raw:
            capped = CappedReader(decompressor(raw, compression), _MAX_LEADING_BYTES)
            with tarfile.open(fileobj=capped, mode="r|") as tar:
                for i, member in enumerate(tar):
                    name = member.name.removeprefix("./")
                    if not name.startswith("."):
                        break  # metadata members always precede payload files
                    dotfiles.append(name)
                    if name == ".PKGINFO" and member.isfile():
                        if member.size > _MAX_PKGINFO:
                            raise DetectionError(".PKGINFO is implausibly large")
                        f = tar.extractfile(member)
                        pkginfo = f.read(_MAX_PKGINFO).decode("utf-8", "replace") if f else None
                    if i >= _MAX_LEADING_MEMBERS:
                        break
    except DetectionError:
        raise
    except MemoryError as exc:
        raise DetectionError("package archive is malformed (excessive memory use)") from exc
    except Exception as exc:  # noqa: BLE001 - tarfile/decompressors raise many types on hostile input
        raise DetectionError(f"cannot read package archive: {exc}") from exc
    return pkginfo, dotfiles


def inspect(path: Path, compression: str) -> Candidate:
    pkginfo_text, dotfiles = read_leading_members(path, compression)
    if pkginfo_text is None:
        raise DetectionError("archive has no .PKGINFO: not an Arch package")
    info = parse_pkginfo(pkginfo_text)
    cand = Candidate(format=PackageFormat.LOCAL_PKG, source=str(path))
    cand.name = info.get("pkgname")  # type: ignore[assignment]
    cand.version = info.get("pkgver")  # type: ignore[assignment]
    cand.arch = info.get("arch")  # type: ignore[assignment]
    cand.summary = info.get("pkgdesc")  # type: ignore[assignment]
    cand.depends = list(info.get("depend", []))
    cand.optional_depends = list(info.get("optdepend", []))
    cand.conflicts = list(info.get("conflict", []))
    cand.provides = list(info.get("provides", []))
    try:
        cand.installed_size = int(info.get("size", ""))  # type: ignore[arg-type]
    except ValueError:
        pass
    cand.metadata.update({
        "compression": compression or "none",
        "url": info.get("url"),
        "packager": info.get("packager"),
        "builddate": info.get("builddate"),
        "pkgbase": info.get("pkgbase"),
        "replaces": info.get("replaces", []),
        "backup": info.get("backup", []),
        "license": info.get("license", []),
        "dotfiles": dotfiles,
    })
    if ".INSTALL" in dotfiles:
        cand.add("PKG_HAS_INSTALL_SCRIPT", Severity.INFO,
                 "The package runs an install script (.INSTALL) as root during installation")
    sig = path.with_name(path.name + ".sig")
    cand.metadata["detached_signature"] = sig.is_file()
    if not sig.is_file():
        cand.add("PKG_UNSIGNED_LOCAL", Severity.WARNING,
                 "No signature file next to the package; its origin must be verified another way")
    if not cand.name or not cand.version:
        raise DetectionError(".PKGINFO lacks pkgname/pkgver")
    return cand
