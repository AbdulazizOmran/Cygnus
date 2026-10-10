"""Route an input file to the right inspector based on its content."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from cygnus.core.detect import appimage, archive, deb, flatpak, pkg, rpm
from cygnus.core.errors import DetectionError
from cygnus.core.models import Candidate, PackageFormat

_HEAD = 512


def sniff_format(head: bytes, name: str = "") -> PackageFormat | None:
    """Best-effort identification from the first bytes (and, for text formats, the name)."""
    if appimage.appimage_type(head) is not None:
        return PackageFormat.APPIMAGE
    if head.startswith(flatpak.BUNDLE_MAGIC):
        return PackageFormat.FLATPAK_BUNDLE
    if head.startswith(deb.AR_MAGIC):
        return PackageFormat.DEB
    if head.startswith(rpm.RPM_LEAD_MAGIC):
        return PackageFormat.RPM
    if pkg.compression_of(head) is not None:
        return PackageFormat.LOCAL_PKG  # confirmed by the presence of .PKGINFO
    text = head.lstrip()
    if text.startswith(b"[Flatpak Ref]"):
        return PackageFormat.FLATPAK_REF_FILE
    if text.startswith(b"[Flatpak Repo]"):
        return PackageFormat.FLATPAK_REPO_FILE
    lowered = name.lower()
    if lowered.endswith(".flatpakref"):
        return PackageFormat.FLATPAK_REF_FILE
    if lowered.endswith(".flatpakrepo"):
        return PackageFormat.FLATPAK_REPO_FILE
    return None


def detect_file(path: str | Path) -> Candidate:
    """Identify a user-selected file and extract its declared metadata (read-only)."""
    path = Path(path).expanduser()
    try:
        st = os.stat(path)  # following a user-chosen symlink is fine; the target must be a regular file
    except OSError as exc:
        raise DetectionError(f"cannot access {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise DetectionError(f"{path} is not a regular file")
    try:
        path = path.resolve()
        with open(path, "rb") as fh:
            head = fh.read(_HEAD)
    except OSError as exc:
        raise DetectionError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        return _inspect(path, head, st)
    except DetectionError:
        raise
    except MemoryError as exc:
        raise DetectionError(f"{path.name}: malformed file (excessive memory use)") from exc
    except Exception as exc:  # noqa: BLE001 - no parser may crash the caller on hostile input
        raise DetectionError(f"{path.name}: malformed or unsupported file ({type(exc).__name__}: {exc})") from exc


_ARCH_NAME = re.compile(r"(?i)\.pkg(?:\.tar(?:\.(?:zst|xz|gz|bz2|lz4|lzo|lrz|z))?)?$")


_UNREADABLE_PKG = re.compile(r"(?i)\.pkg\.tar\.(?:lz4|lzo|lrz|z)$")


def _named_like_arch_package(name: str) -> bool:
    return _ARCH_NAME.search(name) is not None


def _inspect(path: Path, head: bytes, st: os.stat_result) -> Candidate:
    if head.startswith(b"xar!"):
        raise DetectionError(f"{path.name} is a macOS installer package, which only works on macOS")
    fmt = sniff_format(head, path.name)
    if fmt is None:
        if _UNREADABLE_PKG.search(path.name):
            raise DetectionError(f"{path.name} is compressed in a way Cygnus cannot read (lz4, lzo, lrz or Z): unpack and compress it "
                                 "again as .zst or .xz, or install the package with pacman -U")
        if _named_like_arch_package(path.name):
            raise DetectionError(f"{path.name} is not an Arch package (it is not an archive Cygnus can read); a .pkg file "
                                 "can also be a macOS or FreeBSD installer, which cannot be used here")
        raise DetectionError(f"{path.name}: unrecognised file type")
    if fmt is PackageFormat.APPIMAGE:
        cand = appimage.inspect(path)
    elif fmt is PackageFormat.FLATPAK_BUNDLE:
        cand = flatpak.inspect_bundle(path)
    elif fmt is PackageFormat.DEB:
        cand = deb.inspect(path)
    elif fmt is PackageFormat.RPM:
        cand = rpm.inspect(path)
    elif fmt is PackageFormat.LOCAL_PKG:
        compression = pkg.compression_of(head)
        try:
            cand = pkg.inspect(path, compression or "")
        except pkg.NotAnArchPackage:
            cand = archive.inspect(path, compression or "")  # any other tar archive: a portable program, or something to refuse
            if _named_like_arch_package(path.name):  # by its name a package: say what it really is when that can be told
                if any(f.code == "ARCHIVE_FREEBSD_PACKAGE" for f in cand.findings):
                    raise DetectionError(f"{path.name} is a FreeBSD package, which only works on FreeBSD") from None
                raise DetectionError(f"{path.name} is named like an Arch package but has no .PKGINFO: it is damaged, "
                                     "or it is something else with the wrong name") from None
    else:
        if st.st_size > 256 * 1024:
            raise DetectionError("key file is implausibly large")
        cand = flatpak.inspect_ref_file(path, path.read_text(encoding="utf-8", errors="replace"))
    cand.metadata.setdefault("file_size", st.st_size)
    return cand
