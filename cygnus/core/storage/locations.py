"""Storage-location service: register, resolve and refresh locations (architecture §6).

A location is identified by filesystem UUID + the filesystem subtree visible at its
mountpoint (view root) + a subpath. Its current absolute path is re-derived from
mountinfo on every resolution, so a changed mountpoint does not break it, and an
absent drive is reported as offline rather than touched.
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pathlib import PurePosixPath

from cygnus.core.errors import StorageError
from cygnus.core.registry.db import Registry, StorageLocation
from cygnus.core.storage import blockdev, fstab, mountinfo
from cygnus.core.storage.discovery import LocationClass, MountView, StorageCandidate, scan
from cygnus.core.storage.probe import ProbeResult, probe


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    location: StorageLocation
    online: bool
    path: str | None  # absolute path when online
    reason: str | None = None


def _check_not_absent_automount(path: str, mounts: list[mountinfo.Mount],
                                fstab_entries: list[fstab.FstabEntry]) -> None:
    """Refuse to touch a path whose automount point has no device behind it (would block ~90 s)."""
    m = mountinfo.mount_for_path(mounts, path)
    if m is None or m.fstype != "autofs":
        return
    entry = next((e for e in fstab_entries if e.mountpoint.rstrip("/") == m.mountpoint.rstrip("/")), None)
    if entry is None or not entry.uuid:
        raise StorageError(f"{m.mountpoint} is an automount point whose drive cannot be identified; "
                           "mount it first")
    if not blockdev.device_present(entry.uuid):
        raise StorageError(f"the drive for {m.mountpoint} is not connected")


def safe_realpath(path: str, mounts: list[mountinfo.Mount] | None = None,
                  fstab_entries: list[fstab.FstabEntry] | None = None) -> str:
    """Resolve symlinks component by component, never stepping into an absent automount. ".." is
    applied to what is already resolved, as the kernel does (never to the text of the path, which
    would be wrong after a symbolic link)."""
    mounts = mounts if mounts is not None else mountinfo.read()
    fstab_entries = fstab_entries if fstab_entries is not None else fstab.read()
    if not path.startswith("/"):
        path = os.path.join(os.getcwd(), path)
    pending = [p for p in path.split("/") if p]
    current, hops = "/", 0
    while pending:
        part = pending.pop(0)
        if part == ".":
            continue
        if part == "..":
            current = os.path.dirname(current)  # `current` holds no links, so its parent is the real one
            continue
        nxt = os.path.join(current, part)
        _check_not_absent_automount(nxt, mounts, fstab_entries)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError as exc:
            raise StorageError(f"{nxt} does not exist") from exc
        if os.path.islink(nxt):
            hops += 1
            if hops > 40:
                raise StorageError(f"too many symbolic links resolving {path}")
            target = os.readlink(nxt)
            if target.startswith("/"):
                current = "/"
            pending = [p for p in target.split("/") if p] + pending  # relative: from the link's folder
            continue
        del st
        current = nxt
    return current


def candidate_for_path(path: str, candidates: list[StorageCandidate] | None = None,
                       mounts: list[mountinfo.Mount] | None = None) -> tuple[StorageCandidate, MountView]:
    """Find the candidate whose *actual* containing mount holds `path` (already resolved)."""
    path = os.path.normpath(path)
    mounts = mounts if mounts is not None else mountinfo.read()
    candidates = candidates if candidates is not None else scan()
    real = mountinfo.mount_for_path(mounts, path)
    if real is None:
        raise StorageError(f"{path} is not on a mounted filesystem")
    for cand in candidates:
        for view in cand.views:
            if view.mountpoint == real.mountpoint and view.root == real.root:
                return cand, view
    raise StorageError(f"{path} is on {real.fstype} ({real.mountpoint}), which cannot hold applications")


def validate_apps_dir(rel: str) -> str:
    pp = PurePosixPath(rel)
    if not rel or pp.is_absolute() or any(part == ".." for part in pp.parts):
        raise StorageError("the applications folder must be a relative path inside the location")
    return str(pp)


MAX_LABEL = 64


def validate_label(label: str) -> str:
    """A location's name is shown in dialogs and written into launchers and notifications."""
    label = (label or "").strip()
    if not label:
        raise StorageError("give the location a name")
    if len(label) > MAX_LABEL:
        raise StorageError(f"the location name is longer than {MAX_LABEL} characters")
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in label):
        raise StorageError("the location name must not contain control characters such as line breaks")
    if any(unicodedata.category(c) in ("Cf", "Zl", "Zp", "Co", "Cn", "Cs") for c in label):
        raise StorageError("the location name must not contain invisible or direction-changing characters")
    return label


def location_class_from_probe(base: LocationClass, result: ProbeResult | None) -> LocationClass:
    if result is None or base in (LocationClass.SYSTEM, LocationClass.NETWORK):
        return base
    if not result.caps.get("exec_allowed") or not result.caps.get("symlinks"):
        return LocationClass.LIMITED
    return base


def register(
    registry: Registry,
    path: str,
    label: str,
    *,
    default: bool = False,
    apps_dir_name: str = "Applications",  # relative to `path`
    run_probe: bool = True,
    candidates: list[StorageCandidate] | None = None,
    mounts: list[mountinfo.Mount] | None = None,
    realpath=None,
) -> tuple[StorageLocation, ProbeResult | None]:
    """Register `path` (an existing directory) as an application storage location.

    The path is resolved safely (symlinks followed, absent automounts never touched) and the
    location is classified by the filesystem that *actually* holds it.
    """
    apps_dir_name = validate_apps_dir(apps_dir_name)
    label = validate_label(label)
    realpath = realpath or (lambda p, m: safe_realpath(p, m))
    mounts = mounts if mounts is not None else mountinfo.read()
    path = realpath(path, mounts)
    if not os.path.isdir(path):
        raise StorageError(f"{path} is not an existing directory")
    cand, view = candidate_for_path(path, candidates, mounts)
    if not cand.eligible:
        raise StorageError(f"{cand.display_name} cannot hold applications: {cand.ineligible_reason}")
    if cand.fs_uuid is None:
        raise StorageError(f"{cand.display_name} has no stable filesystem UUID")
    rel = os.path.relpath(path, view.mountpoint)
    subpath = "" if rel == "." else rel
    # Prefer the canonical mount (e.g. /mnt/data over /run/media/<user>/DATA) when it shows the same subtree.
    canonical = next((v for v in cand.views if v.mountpoint == cand.canonical_mount), None)
    if canonical is not None and canonical.root == view.root:
        view = canonical
        path = os.path.join(view.mountpoint, subpath) if subpath else view.mountpoint

    result = probe(path) if run_probe else None
    if result is not None and not result.writable:
        raise StorageError(f"{path} is not writable by you: {result.errors.get('mkdir', '')}")
    loc = StorageLocation(
        id="",
        label=label,
        fs_uuid=cand.fs_uuid,
        fs_type=cand.fs_type,
        location_class=str(location_class_from_probe(cand.location_class, result)),
        canonical_mount=view.mountpoint,
        view_root=view.root,
        subpath=subpath,
        removable=cand.removable,
        rotational=cand.rotational,
        capabilities=result.caps if result else {},
        probed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if result else None,
        probe_boot_id=result.boot_id if result else None,
        user_apps_dir=apps_dir_name,  # relative to the location root
        is_default=default,
    )
    return registry.add_location(loc), result


def resolve(loc: StorageLocation, candidates: list[StorageCandidate] | None = None) -> ResolvedLocation:
    """Find where a registered location currently lives, without triggering automounts."""
    if loc.fs_uuid and not blockdev.device_present(loc.fs_uuid):
        return ResolvedLocation(loc, False, None, "the drive is not connected")
    candidates = candidates if candidates is not None else scan()
    for cand in candidates:
        if cand.fs_uuid != loc.fs_uuid:
            continue
        views = [v for v in cand.views if v.root == loc.view_root]
        if not views:
            continue
        preferred = [v for v in views if v.mountpoint == loc.canonical_mount]
        view = (preferred or sorted(views, key=lambda v: (v.mountpoint.startswith("/run/media/"), len(v.mountpoint))))[0]
        path = os.path.join(view.mountpoint, loc.subpath) if loc.subpath else view.mountpoint
        if view.read_only:
            return ResolvedLocation(loc, False, path, "the drive is mounted read-only")
        return ResolvedLocation(loc, True, path)
    return ResolvedLocation(loc, False, None, "the drive is connected but not mounted")


def apps_dir(loc: StorageLocation, resolved: ResolvedLocation) -> Path | None:
    """Absolute user-visible applications folder of an online location."""
    if not resolved.online or resolved.path is None:
        return None
    return Path(resolved.path) / validate_apps_dir(loc.user_apps_dir or "Applications")
