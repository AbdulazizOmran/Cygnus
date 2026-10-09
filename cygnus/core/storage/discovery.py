"""Discover filesystems that could serve as application storage locations.

Sources are combined as described in the architecture (§6.1):
  * /proc/self/mountinfo is authoritative for what is mounted and with which options;
  * lsblk provides stable identity (filesystem UUID) and drive characteristics;
  * fstab is only a hint for choosing the canonical mountpoint.

Several mounts of one filesystem (e.g. an fstab mount plus a udisks mount of the
same NTFS partition, or btrfs subvolumes) are merged into one candidate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from cygnus.core.storage import blockdev, fstab, mountinfo
from cygnus.core.storage.blockdev import BlockDevice
from cygnus.core.storage.fstab import FstabEntry
from cygnus.core.storage.mountinfo import Mount


class LocationClass(StrEnum):
    SYSTEM = "system"          # the filesystem holding / (and usually /home)
    POSIX = "posix"            # Linux-native secondary filesystem
    USER_OWNED = "user-owned"  # ownership is forced by mount options or forgeable (NTFS, exFAT, FAT)
    NETWORK = "network"
    LIMITED = "limited"        # set by the probe: no symlinks or no exec


POSIX_FS = frozenset({"btrfs", "ext2", "ext3", "ext4", "xfs", "f2fs", "bcachefs", "zfs", "jfs", "nilfs2"})
USER_OWNED_FS = frozenset({"ntfs3", "ntfs", "fuseblk", "exfat", "vfat", "msdos", "hfsplus"})
NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "9p", "ceph", "glusterfs", "fuse.rclone"})
_EXCLUDED_MOUNTPOINTS = ("/boot", "/efi", "/boot/efi")
_EXCLUDED_PREFIXES = ("/proc", "/sys", "/dev", "/run/user", "/var/lib/flatpak", "/snap", "/tmp", "/var/tmp")


@dataclass(frozen=True, slots=True)
class MountView:
    mountpoint: str
    root: str  # subtree of the filesystem visible here (btrfs subvolume or bind root)
    options: frozenset[str]
    read_only: bool


@dataclass(slots=True, kw_only=True)
class StorageCandidate:
    fs_uuid: str | None
    fs_type: str
    label: str | None
    device: str | None
    model: str | None = None
    transport: str | None = None
    rotational: bool = False
    removable: bool = False
    size_bytes: int | None = None
    free_bytes: int | None = None
    views: list[MountView] = field(default_factory=list)
    canonical_mount: str | None = None
    is_system: bool = False
    location_class: LocationClass = LocationClass.POSIX
    mount_flags: frozenset[str] = frozenset()
    super_options: dict[str, str] = field(default_factory=dict)
    present: bool = True
    mounted: bool = True
    automount: bool = False
    eligible: bool = True
    ineligible_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def kind_label(self) -> str:
        if self.rotational:
            return "HDD"
        if self.transport == "nvme" or not self.rotational:
            return "SSD"
        return "Disk"

    @property
    def display_name(self) -> str:
        if self.is_system:
            return f"System ({self.kind_label})"
        name = self.label or (self.canonical_mount or self.device or "?")
        return f"{name} ({self.kind_label})"


def classify_fs(fs_type: str) -> LocationClass | None:
    if fs_type in POSIX_FS:
        return LocationClass.POSIX
    if fs_type in USER_OWNED_FS:
        return LocationClass.USER_OWNED
    if fs_type in NETWORK_FS:
        return LocationClass.NETWORK
    return None  # pseudo, virtual, FUSE app mounts, ...


def _is_excluded_mountpoint(mp: str) -> bool:
    if mp in _EXCLUDED_MOUNTPOINTS:
        return True
    return any(mp == p or mp.startswith(p + "/") for p in _EXCLUDED_PREFIXES)


def _choose_canonical(views: list[MountView], fstab_mps: set[str]) -> MountView:
    def rank(v: MountView) -> tuple[int, int, int, int]:
        return (
            0 if v.mountpoint in fstab_mps else 1,
            1 if v.read_only else 0,
            1 if v.mountpoint.startswith("/run/media/") else 0,
            len(v.mountpoint),
        )

    return sorted(views, key=rank)[0]


def discover(
    mounts: list[Mount],
    devices: list[BlockDevice],
    fstab_entries: list[FstabEntry],
    *,
    home: str | None = None,
    statvfs=os.statvfs,
) -> list[StorageCandidate]:
    home = home or str(Path.home())
    by_path = {d.path: d for d in devices}
    by_dev = {d.dev: d for d in devices if d.dev}
    fstab_by_uuid: dict[str, list[FstabEntry]] = {}
    for e in fstab_entries:
        if e.uuid:
            fstab_by_uuid.setdefault(e.uuid, []).append(e)

    groups: dict[str, dict] = {}
    for m in mounts:
        cls = classify_fs(m.fstype)
        if cls is None or _is_excluded_mountpoint(m.mountpoint):
            continue
        dev = by_path.get(m.source) or by_dev.get(m.dev)
        if dev is None and m.source.startswith("/dev/"):
            dev = by_path.get(os.path.realpath(m.source))
        key = (dev.uuid if dev and dev.uuid else None) or f"{m.fstype}:{m.source}"
        g = groups.setdefault(key, {"dev": dev, "mounts": [], "cls": cls})
        g["mounts"].append(m)

    candidates: list[StorageCandidate] = []
    for key, g in groups.items():
        dev: BlockDevice | None = g["dev"]
        ms: list[Mount] = g["mounts"]
        views = [MountView(m.mountpoint, m.root, m.options, m.read_only) for m in ms]
        is_system = any(m.mountpoint == "/" for m in ms)
        uuid = dev.uuid if dev else None
        fstab_mps = {e.mountpoint for e in fstab_by_uuid.get(uuid or "", [])}

        if is_system:
            home_views = [v for v in views if home == v.mountpoint or home.startswith(v.mountpoint.rstrip("/") + "/")]
            canonical = max(home_views, key=lambda v: len(v.mountpoint)) if home_views else _choose_canonical(views, fstab_mps)
        else:
            canonical = _choose_canonical(views, fstab_mps)
        canon_mount = next(m for m in ms if m.mountpoint == canonical.mountpoint)

        cand = StorageCandidate(
            fs_uuid=uuid,
            fs_type=ms[0].fstype,
            label=dev.label if dev else None,
            device=dev.path if dev else ms[0].source,
            model=dev.model if dev else None,
            transport=dev.transport if dev else None,
            rotational=dev.rotational if dev else False,
            removable=(dev.removable or dev.hotplug) if dev else False,
            size_bytes=dev.size if dev else None,
            views=views,
            canonical_mount=canonical.mountpoint,
            is_system=is_system,
            location_class=LocationClass.SYSTEM if is_system else g["cls"],
            mount_flags=frozenset(f for f in ("noexec", "nosuid", "nodev", "ro") if f in canonical.options),
            super_options=dict(canon_mount.super_options),
            automount=any(e.automount for e in fstab_by_uuid.get(uuid or "", [])),
        )
        if len({v.mountpoint for v in views}) > 1 and not is_system:
            cand.notes.append(
                "mounted at several places: " + ", ".join(sorted(v.mountpoint for v in views))
                + f"; using {canonical.mountpoint}"
            )
        try:
            if cand.location_class == LocationClass.NETWORK:
                raise OSError("network filesystems are not queried (a dead server would hang statvfs)")
            st = statvfs(canonical.mountpoint)
            cand.free_bytes = st.f_bavail * st.f_frsize
            if cand.size_bytes is None:
                cand.size_bytes = st.f_blocks * st.f_frsize
        except OSError:
            pass
        _apply_eligibility(cand)
        candidates.append(cand)

    candidates.sort(key=lambda c: (not c.is_system, c.display_name))
    return candidates


def _apply_eligibility(c: StorageCandidate) -> None:
    if c.location_class == LocationClass.NETWORK:
        c.eligible, c.ineligible_reason = False, "network filesystems are not supported as application storage"
    elif "ro" in c.mount_flags or all(v.read_only for v in c.views):
        c.eligible, c.ineligible_reason = False, "mounted read-only"
    elif "noexec" in c.mount_flags:
        c.eligible, c.ineligible_reason = False, "mounted with noexec: programs cannot run from it"
    if c.location_class == LocationClass.USER_OWNED:
        c.notes.append("can hold applications you run (AppImages, portable apps, Flatpak data); "
                       "cannot hold system packages")
        if "nosuid" not in c.mount_flags:
            c.notes.append("mounted without nosuid,nodev: consider adding them (see security model §2.5)")


def scan() -> list[StorageCandidate]:
    """Discover candidates on the running system (read-only)."""
    return discover(mountinfo.read(), blockdev.read(), fstab.read())
