"""Debian package (.deb) inspection with a bounds-checked ar reader; dpkg is not required."""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

from cygnus.core.errors import DetectionError
from cygnus.core.models import Candidate, PackageFormat, Severity
from cygnus.core.util.limits import CappedReader, decompressor

AR_MAGIC = b"!<arch>\n"
_MAX_CONTROL_TAR = 32 * 1024 * 1024
_MAX_SCRIPT = 512 * 1024
_MAX_CONTROL_DECOMPRESSED = 64 * 1024 * 1024
_MAX_CONTROL_MEMBERS = 64  # dpkg control archives hold about ten files
_MAX_FIELD = 64 * 1024
MAINTAINER_SCRIPTS = ("preinst", "postinst", "prerm", "postrm", "config", "triggers")
DEB_ARCH_TO_ARCH = {"amd64": "x86_64", "arm64": "aarch64", "i386": "i686", "armhf": "armv7h", "all": "any"}


@dataclass(frozen=True, slots=True)
class ArMember:
    name: str
    offset: int
    size: int


def read_ar_members(fh, file_size: int) -> list[ArMember]:
    fh.seek(0)
    if fh.read(8) != AR_MAGIC:
        raise DetectionError("not an ar archive")
    members, pos = [], 8
    while pos + 60 <= file_size and len(members) < 64:
        fh.seek(pos)
        header = fh.read(60)
        if header[58:60] != b"`\n":
            raise DetectionError("corrupt ar member header")
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        field = header[48:58].decode("ascii", "replace").rstrip(" ")
        if not re.fullmatch(r"[0-9]+", field):  # decimal digits padded with spaces, as dpkg reads it
            raise DetectionError("corrupt ar member size")
        size = int(field)
        data_off = pos + 60
        if size < 0 or data_off + size > file_size:
            raise DetectionError("ar member extends beyond end of file")
        members.append(ArMember(name, data_off, size))
        pos = data_off + size + (size & 1)
    return members


def parse_control(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key: str | None = None
    for line in text.split("\n"):
        if not line.strip():
            if fields:
                break  # first paragraph only
            continue
        if line[0] in " \t" and key:
            fields[key] += "\n" + line.strip()
            continue
        k, sep, v = line.partition(":")
        if sep:
            key = k.strip()
            fields[key] = v.strip()
    return fields


# Applied to an already-stripped alternative; no two adjacent quantifiers can match the same text,
# so matching is linear (a previous version backtracked cubically on long runs of spaces).
_DEP_ITEM = re.compile(r"^(?P<name>[a-z0-9][a-z0-9+.\-]*)(?::(?P<qual>[a-z0-9-]+))?"
                       r"(?:\s*\(\s*(?P<op><<|<=|=|>=|>>)\s*(?P<ver>[^()\s]+)\s*\))?"
                       r"(?:\s*\[(?P<arch>[^\[\]]+)\])?$")


def parse_dependencies(value: str | None) -> list[list[dict[str, str]]]:
    """Parse a Depends-style field into AND-groups of OR-alternatives."""
    if not value:
        return []
    if len(value) > _MAX_FIELD:
        raise DetectionError("dependency field is implausibly long")
    groups = []
    for group in value.replace("\n", " ").split(","):
        alts = []
        for alt in group.split("|"):
            alt = alt.strip()
            m = _DEP_ITEM.match(alt)
            if m:
                alts.append({k: v.strip() for k, v in m.groupdict().items() if v})
            elif alt.strip():
                alts.append({"name": alt.strip(), "unparsed": "1"})
        if alts:
            groups.append(alts)
    return groups


def _format_dep(group: list[dict[str, str]]) -> str:
    def one(d: dict[str, str]) -> str:
        s = d["name"]
        if "op" in d:
            s += f" ({d['op']} {d['ver']})"
        return s
    return " | ".join(one(d) for d in group)


def inspect(path: Path) -> Candidate:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        members = read_ar_members(fh, size)
        names = [m.name for m in members]
        if not names or names[0] != "debian-binary":
            raise DetectionError("ar archive is not a Debian package")
        fh.seek(members[0].offset)
        format_version = fh.read(min(members[0].size, 16)).decode("ascii", "replace").strip()
        control_member = next((m for m in members if m.name.startswith("control.tar")), None)
        data_member = next((m for m in members if m.name.startswith("data.tar")), None)
        if control_member is None or data_member is None:
            raise DetectionError("Debian package lacks control or data archive")
        if control_member.size > _MAX_CONTROL_TAR:
            raise DetectionError("control archive is implausibly large")
        fh.seek(control_member.offset)
        control_bytes = fh.read(control_member.size)

    control_text, scripts, control_files, oversized = None, {}, [], []
    compression = control_member.name.removeprefix("control.tar").lstrip(".") or ""
    try:
        capped = CappedReader(decompressor(io.BytesIO(control_bytes), compression), _MAX_CONTROL_DECOMPRESSED)
        with tarfile.open(fileobj=capped, mode="r|") as tar:
            for count, member in enumerate(tar):
                if count >= _MAX_CONTROL_MEMBERS:
                    raise DetectionError("control archive has implausibly many members")
                name = member.name.removeprefix("./")
                if not name or not member.isfile():
                    continue
                control_files.append(name)
                if member.size > _MAX_SCRIPT:
                    if name in MAINTAINER_SCRIPTS:
                        oversized.append(name)  # recorded: a script nobody read must never pass as harmless
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                data = f.read(_MAX_SCRIPT).decode("utf-8", "replace")
                if name == "control":
                    control_text = data
                elif name in MAINTAINER_SCRIPTS:
                    scripts[name] = data
    except DetectionError:
        raise
    except MemoryError as exc:
        raise DetectionError("control archive is malformed (excessive memory use)") from exc
    except Exception as exc:  # noqa: BLE001 - tarfile/decompressors raise many types on hostile input
        raise DetectionError(f"cannot read control archive: {exc}") from exc
    if control_text is None:
        raise DetectionError("Debian package has no control file")

    ctl = parse_control(control_text)
    cand = Candidate(format=PackageFormat.DEB, source=str(path))
    cand.name = ctl.get("Package")
    cand.version = ctl.get("Version")
    deb_arch = ctl.get("Architecture")
    cand.arch = DEB_ARCH_TO_ARCH.get(deb_arch or "", deb_arch)
    desc = ctl.get("Description", "")
    cand.summary = desc.split("\n", 1)[0] or None
    deps = parse_dependencies(ctl.get("Pre-Depends")) + parse_dependencies(ctl.get("Depends"))
    cand.depends = [_format_dep(g) for g in deps]
    cand.optional_depends = [_format_dep(g) for g in parse_dependencies(ctl.get("Recommends"))
                             + parse_dependencies(ctl.get("Suggests"))]
    cand.conflicts = [_format_dep(g) for g in parse_dependencies(ctl.get("Conflicts"))
                      + parse_dependencies(ctl.get("Breaks"))]
    cand.provides = [_format_dep(g) for g in parse_dependencies(ctl.get("Provides"))]
    try:
        cand.installed_size = int(ctl.get("Installed-Size", "")) * 1024
    except ValueError:
        pass
    cand.metadata.update({
        "deb_format": format_version,
        "deb_architecture": deb_arch,
        "maintainer": ctl.get("Maintainer"),
        "homepage": ctl.get("Homepage"),
        "section": ctl.get("Section"),
        "control_files": control_files,
        "data_compression": data_member.name.removeprefix("data.tar").lstrip(".") or "none",
        "depends_structured": deps,
        "maintainer_scripts": scripts,
        "uninspected_scripts": oversized,
    })
    if scripts:
        cand.add("DEB_MAINTAINER_SCRIPTS", Severity.WARNING,
                 "The package contains maintainer scripts (" + ", ".join(sorted(scripts))
                 + "); they will be analysed, never run blindly", scripts=sorted(scripts))
    cand.add("FOREIGN_PACKAGE_FORMAT", Severity.INFO,
             "Debian packages are built for Debian/Ubuntu; Cygnus will look for a native alternative first")
    if not cand.name or not cand.version:
        raise DetectionError("control file lacks Package/Version")
    return cand
