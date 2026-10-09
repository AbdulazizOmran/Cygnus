"""AppImage inspection without execution (architecture §2.6, §7.1).

Reads the ELF header for the payload offset, the `.upd_info`, `.sha256_sig` and
`.sig_key` sections, and — when squashfs-tools is installed — extracts only
metadata files (desktop entry, icon, AppStream) from the squashfs payload into a
private temporary directory.
"""

from __future__ import annotations

import contextvars
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from cygnus.core.desktop import entry as desktop_entry
from cygnus.core.detect import elf
from cygnus.core.errors import DetectionError, ToolMissingError
from cygnus.core.models import Candidate, PackageFormat, Severity
from cygnus.core.util import proc

SQUASHFS_MAGIC = b"hsqs"
DWARFS_MAGIC = b"DWARFS"
_LS_LINE = re.compile(r"^(?P<mode>[-dlcbps][rwxsStT-]{9})\s+\S+\s+(?P<size>\d+)\s+\S+\s+\S+\s+"
                      r"squashfs-root(?P<rest>/.*)$")
_MAX_LISTING = 4 * 1024 * 1024
_MAX_META_FILE = 4 * 1024 * 1024
METADATA_BUDGET = 90.0  # seconds for all unsquashfs calls reading one AppImage's metadata
_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("appimage_metadata_deadline", default=None)


def _timeout() -> float:
    """What is left of the metadata budget (each call is also limited to 60 s)."""
    deadline = _deadline.get()
    if deadline is None:
        return 60.0
    left = deadline - time.monotonic()
    if left <= 0:
        raise DetectionError("reading the AppImage's metadata takes implausibly long")
    return min(60.0, left)


def appimage_type(head: bytes) -> int | None:
    if len(head) >= 11 and head[:4] == elf.ELF_MAGIC and head[8:10] == b"AI":
        return head[10]
    return None


def parse_update_info(text: str) -> dict[str, str] | None:
    """Parse AppImage update information (AppImageSpec 'update information')."""
    text = text.strip("\0 \n")
    if not text:
        return None
    parts = text.split("|")
    kind = parts[0]
    if kind == "zsync" and len(parts) == 2:
        return {"type": kind, "url": parts[1]}
    if kind in ("gh-releases-zsync", "gh-releases-direct") and len(parts) == 5:
        return {"type": kind, "owner": parts[1], "repo": parts[2], "tag": parts[3], "filename": parts[4]}
    if kind == "pling-v1-zsync" and len(parts) == 3:
        return {"type": kind, "product_id": parts[1], "filename": parts[2]}
    return {"type": "unknown", "raw": text}


def _section_has_content(data: bytes) -> bool:
    return any(data)


def inspect(path: Path) -> Candidate:
    size = path.stat().st_size
    cand = Candidate(format=PackageFormat.APPIMAGE, source=str(path))
    with open(path, "rb") as fh:
        head = fh.read(16)
        ai_type = appimage_type(head)
        if ai_type is None:
            raise DetectionError("not an AppImage (missing 'AI' magic)")
        info = elf.read_elf(fh, size)
        cand.arch = info.arch
        cand.metadata["appimage_type"] = ai_type
        cand.metadata["runtime_arch"] = info.arch
        if ai_type == 1:
            cand.add("APPIMAGE_TYPE1", Severity.WARNING,
                     "Legacy type-1 AppImage (ISO 9660 payload); metadata extraction is not supported")
            return cand
        offset = info.end_of_section_headers
        cand.metadata["payload_offset"] = offset
        fh.seek(offset)
        magic = fh.read(6)
        if magic[:4] == SQUASHFS_MAGIC:
            cand.metadata["payload_format"] = "squashfs"
        elif magic == DWARFS_MAGIC:
            cand.metadata["payload_format"] = "dwarfs"
        else:
            cand.metadata["payload_format"] = "unknown"
            cand.add("APPIMAGE_PAYLOAD_UNKNOWN", Severity.WARNING, "Unrecognised payload format; metadata unavailable")

        upd = info.section(".upd_info")
        if upd is not None:
            parsed = parse_update_info(elf.read_section(fh, upd).decode("utf-8", "replace"))
            if parsed:
                cand.metadata["update_info"] = parsed
        sig, key = info.section(".sha256_sig"), info.section(".sig_key")
        signed = bool(
            sig and key
            and _section_has_content(elf.read_section(fh, sig))
            and _section_has_content(elf.read_section(fh, key))
        )
        cand.metadata["signature_present"] = signed  # present, not verified (see backends.appimage.verify_signature)
        if not signed:
            cand.add("APPIMAGE_UNSIGNED", Severity.INFO,
                     "No embedded signature; integrity relies on the download source")

    if cand.metadata.get("payload_format") == "squashfs":
        _read_metadata(path, cand)
    if not cand.name:
        cand.name = re.sub(r"(?i)[-_.]?(x86_64|amd64|aarch64|arm64|i686)?\.appimage$", "", path.name)
    return cand


def _ls(path: Path, offset: int, names: list[str] | None = None, max_depth: int | None = None
        ) -> dict[str, tuple[str, int, str | None]]:
    """List payload entries as {path: (mode, size, symlink_target)} with bounded output."""
    unsquashfs = proc.require("unsquashfs", "squashfs-tools")
    argv = [unsquashfs, "-o", str(offset), "-lls", "-no-wildcards"]
    if max_depth is not None:
        argv += ["-max-depth", str(max_depth)]
    argv += [str(path), *(names or [])]
    res = proc.run(argv, timeout=_timeout(), max_output=_MAX_LISTING)
    if res.truncated:
        raise DetectionError("the AppImage payload listing is implausibly large")
    if res.returncode != 0 and not res.stdout:
        raise DetectionError(f"unsquashfs could not list the payload: {res.stderr.strip()[:300]}")
    entries = {}
    for line in res.stdout.split("\n"):  # names may contain any other character
        m = _LS_LINE.match(line)
        if not m:
            continue
        path, target = m.group("rest"), None
        if m.group("mode").startswith("l"):  # only a symlink's line has " -> target"
            if path.count(" -> ") != 1:
                continue  # ambiguous: such a link is never followed
            path, target = path.split(" -> ")
        entries[path.lstrip("/")] = (m.group("mode"), int(m.group("size")), target)
    return entries


def _normalise_link(rel: str, target: str) -> str | None:
    if target.startswith("/"):
        return None  # absolute links would point at the host
    parts: list[str] = []
    for part in (PurePosixPath(rel).parent / target).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def _resolve(path: Path, offset: int, top: dict[str, tuple[str, int, str | None]], rel: str
             ) -> tuple[str, int] | None:
    """Follow symlinks *inside the image namespace* only; returns (regular file path, size)."""
    entry = top.get(rel)
    for _ in range(8):
        if entry is None:
            entry = _ls(path, offset, [rel]).get(rel)
            if entry is None:
                return None
        mode, size, target = entry
        if mode[0] == "-":
            return rel, size
        if mode[0] != "l" or target is None:
            return None
        nxt = _normalise_link(rel, target)
        if nxt is None:
            return None
        rel, entry = nxt, None
    return None


def _cat(path: Path, offset: int, rel: str) -> bytes | None:
    if any(c in rel for c in "*?[]\\"):
        return None
    unsquashfs = proc.require("unsquashfs", "squashfs-tools")
    res = proc.run([unsquashfs, "-o", str(offset), "-cat", "-no-wildcards", str(path), rel],
                   timeout=_timeout(), max_output=_MAX_META_FILE)
    if res.truncated or res.returncode != 0:
        return None
    return res.raw_stdout


def _read_metadata(path: Path, cand: Candidate) -> None:
    token = _deadline.set(time.monotonic() + METADATA_BUDGET)
    try:
        _read_metadata_within_budget(path, cand)
    finally:
        _deadline.reset(token)


def _read_metadata_within_budget(path: Path, cand: Candidate) -> None:
    offset = cand.metadata["payload_offset"]
    try:
        top = _ls(path, offset, max_depth=1)
        meta_dirs = _ls(path, offset, ["usr/share/metainfo", "usr/share/appdata"], max_depth=4)
    except ToolMissingError as exc:
        cand.add("APPIMAGE_METADATA_UNAVAILABLE", Severity.INFO, str(exc))
        return
    except (DetectionError, proc.CommandTimeout) as exc:
        cand.add("APPIMAGE_METADATA_UNAVAILABLE", Severity.WARNING, str(exc))
        return
    cand.metadata["payload_top_entries"] = len(top)

    wanted: dict[str, tuple[str, int]] = {}
    desktops = sorted(p for p in top if "/" not in p and p.endswith(".desktop"))
    if desktops and (r := _resolve(path, offset, top, desktops[0])):
        wanted["desktop"] = r
    if (r := _resolve(path, offset, top, ".DirIcon")):
        wanted["icon"] = r
    for p in sorted(meta_dirs):
        if re.fullmatch(r"usr/share/(metainfo|appdata)/[^/]+\.(metainfo|appdata)\.xml", p):
            if (r := _resolve(path, offset, meta_dirs, p)):
                wanted["appstream"] = r
                break
    if "desktop" not in wanted:
        cand.add("APPIMAGE_NO_DESKTOP_ENTRY", Severity.WARNING, "The AppImage contains no desktop entry")
    files: dict[str, bytes] = {}
    for key, (rel, size) in wanted.items():
        if size > _MAX_META_FILE:
            cand.add("APPIMAGE_METADATA_OVERSIZED", Severity.WARNING, f"{rel} is implausibly large; ignored")
            continue
        try:
            data = _cat(path, offset, rel)
        except proc.CommandTimeout:
            data = None
        if data is not None:
            files[key] = data

    if files.get("desktop") is not None:
        de = desktop_entry.parse(files["desktop"].decode("utf-8", "replace"))
        cand.metadata["desktop_entry"] = {k: v for k, v in de.groups.get(desktop_entry.MAIN_GROUP, [])}
        cand.metadata["desktop_actions"] = de.actions
        cand.metadata["desktop_action_groups"] = {
            a: dict(de.groups.get(f"Desktop Action {a}", [])) for a in de.actions[:20]}
        cand.identity["desktop_id"] = PurePosixPath(desktops[0]).name
        cand.name = de.get("Name") or cand.name
        cand.summary = de.get("Comment") or de.get("GenericName")
        cand.version = de.get("X-AppImage-Version") or cand.version
    if files.get("appstream") is not None:
        _apply_appstream(cand, files["appstream"])
    if files.get("icon") is not None:
        cand.metadata["icon_bytes"] = len(files["icon"])
        cand.metadata["icon_kind"] = "svg" if files["icon"].lstrip()[:5] in (b"<?xml", b"<svg ") else "png"


def _apply_appstream(cand: Candidate, data: bytes) -> None:
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError):
        cand.add("APPSTREAM_INVALID", Severity.INFO, "Embedded AppStream metadata is not valid XML")
        return
    if (cid := root.findtext("id")):
        cand.identity["appstream_id"] = cid.strip()
    if not cand.summary and (s := root.findtext("summary")):
        cand.summary = s.strip()
    dev = root.find("developer")
    vendor = (dev.findtext("name") if dev is not None else None) or root.findtext("developer_name")
    if vendor:
        cand.metadata["vendor"] = vendor.strip()
    rel = root.find("releases/release")
    if rel is not None and rel.get("version") and not cand.version:
        cand.version = rel.get("version")
    if (lic := root.findtext("project_license")):
        cand.metadata["license"] = lic.strip()
