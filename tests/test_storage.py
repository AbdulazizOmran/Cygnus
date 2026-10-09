import os
from types import SimpleNamespace

import pytest

from cygnus.core.errors import StorageError
from cygnus.core.registry import open_registry
from cygnus.core.storage import blockdev, fstab, locations, mountinfo
from cygnus.core.storage.discovery import LocationClass, discover
from cygnus.core.storage.probe import PROBE_PREFIX, probe

# Modeled on the development machine: btrfs SSD with subvolumes, an NTFS HDD mounted twice
# (fstab + udisks, one superblock), a second NTFS partition, /boot, tmpfs and a FUSE AppImage mount.
MOUNTINFO = """\
24 1 0:22 /@ / rw,noatime shared:1 - btrfs /dev/nvme0n1p2 rw,compress=zstd:1,ssd,subvol=/@
60 24 0:22 /@home /home rw,noatime shared:30 - btrfs /dev/nvme0n1p2 rw,compress=zstd:1,ssd,subvol=/@home
61 24 0:22 /@cache /var/cache rw,noatime shared:31 - btrfs /dev/nvme0n1p2 rw,subvol=/@cache
62 24 259:1 / /boot rw,relatime shared:32 - vfat /dev/nvme0n1p1 rw,fmask=0077
63 24 0:40 / /tmp rw,nosuid,nodev shared:33 - tmpfs tmpfs rw
70 24 8:1 / /mnt/data rw,relatime shared:40 - ntfs3 /dev/sda1 rw,uid=1000,gid=1000,acl
71 24 8:1 / /run/media/me/My\\040Data rw,nosuid,nodev,relatime shared:41 - ntfs3 /dev/sda1 rw,uid=1000,gid=1000,acl
72 24 8:2 / /mnt/photos rw,relatime shared:42 - ntfs3 /dev/sda2 rw,uid=1000
73 24 0:80 / /tmp/.mount_helium rw,nosuid,nodev shared:50 - fuse.helium.AppImage helium.AppImage ro
74 24 8:17 / /mnt/usb ro,relatime shared:60 - exfat /dev/sdb1 ro
75 24 0:90 / /mnt/nas rw,relatime shared:70 - nfs4 nas:/export rw
76 24 8:33 / /mnt/scratch rw,noexec,relatime shared:80 - ext4 /dev/sdc1 rw
"""


def dev(path, uuid, fstype, label=None, rota=False, tran=None, rm=False):
    return blockdev.BlockDevice(path=path, name=path.rsplit("/", 1)[-1], dev=None, type="part", fstype=fstype,
                                label=label, uuid=uuid, partuuid=None, size=10**12, rotational=rota,
                                removable=rm, hotplug=rm, transport=tran, model=None, serial=None,
                                read_only=False)


DEVICES = [
    dev("/dev/nvme0n1p2", "ssd-uuid", "btrfs", tran="nvme"),
    dev("/dev/nvme0n1p1", "BOOT-UUID", "vfat", tran="nvme"),
    dev("/dev/sda1", "HDD1", "ntfs", label="DATA", rota=True, tran="sata"),
    dev("/dev/sda2", "HDD2", "ntfs", label="Photos", rota=True, tran="sata"),
    dev("/dev/sdb1", "USB1", "exfat", label="STICK", tran="usb", rm=True),
    dev("/dev/sdc1", "EXT1", "ext4", label="scratch", rota=True),
]
FSTAB = fstab.parse("UUID=HDD1 /mnt/data ntfs3 uid=1000,nofail,x-systemd.automount 0 0\n# comment\n")


def fake_statvfs(_path):
    return SimpleNamespace(f_bavail=100, f_frsize=4096, f_blocks=1000)


@pytest.fixture
def candidates():
    return discover(mountinfo.parse(MOUNTINFO), DEVICES, FSTAB, home="/home/me", statvfs=fake_statvfs)


MOUNTS = mountinfo.parse(MOUNTINFO)


def reg_loc(reg, path, label, candidates, **kw):
    return locations.register(reg, path, label, run_probe=False, candidates=candidates, mounts=MOUNTS,
                              realpath=lambda p, m: os.path.normpath(p), **kw)


def by_uuid(cands, uuid):
    return next(c for c in cands if c.fs_uuid == uuid)


def test_mountinfo_unescapes_and_parses_options():
    m = mountinfo.parse(MOUNTINFO)[6]
    assert m.mountpoint == "/run/media/me/My Data"
    assert m.has("nosuid") and m.fstype == "ntfs3" and m.super_options["uid"] == "1000"
    assert m.dev == (8, 1)


def test_mount_for_path_prefers_longest_and_later_mounts():
    mounts = mountinfo.parse(MOUNTINFO)
    assert mountinfo.mount_for_path(mounts, "/home/me/x").mountpoint == "/home"
    assert mountinfo.mount_for_path(mounts, "/mnt/data/apps").mountpoint == "/mnt/data"
    assert mountinfo.mount_for_path(mounts, "/etc").mountpoint == "/"
    assert mountinfo.mount_for_path(mounts, "/mnt/database").mountpoint == "/"


def test_pseudo_and_fuse_mounts_are_excluded(candidates):
    uuids = {c.fs_uuid for c in candidates}
    assert "BOOT-UUID" not in uuids
    assert all(c.fs_type not in ("tmpfs", "fuse.helium.AppImage") for c in candidates)


def test_system_candidate_uses_home(candidates):
    ssd = by_uuid(candidates, "ssd-uuid")
    assert ssd.is_system and ssd.location_class == LocationClass.SYSTEM
    assert ssd.canonical_mount == "/home"
    assert {v.root for v in ssd.views} == {"/@", "/@home", "/@cache"}
    assert ssd.display_name == "System (SSD)"


def test_double_mounted_ntfs_is_merged_with_fstab_canonical(candidates):
    hdd = by_uuid(candidates, "HDD1")
    assert hdd.canonical_mount == "/mnt/data"
    assert len(hdd.views) == 2
    assert hdd.location_class == LocationClass.USER_OWNED
    assert hdd.automount and hdd.display_name == "DATA (HDD)"
    assert "nosuid" not in hdd.mount_flags  # canonical mount's flags, not the udisks mount's
    assert any("several places" in n for n in hdd.notes)
    assert any("nosuid" in n for n in hdd.notes)


def test_ineligible_locations(candidates):
    assert not by_uuid(candidates, "USB1").eligible  # read-only
    assert "noexec" in by_uuid(candidates, "EXT1").ineligible_reason
    nas = next(c for c in candidates if c.fs_type == "nfs4")
    assert nas.location_class == LocationClass.NETWORK and not nas.eligible


def test_free_space_reported(candidates):
    assert by_uuid(candidates, "HDD1").free_bytes == 100 * 4096


def test_fstab_parse():
    e = FSTAB[0]
    assert e.uuid == "HDD1" and e.automount and e.nofail and e.mountpoint == "/mnt/data"


def test_lsblk_json_flattening():
    data = {"blockdevices": [{
        "name": "sda", "path": "/dev/sda", "maj:min": "8:0", "type": "disk", "rota": True, "rm": False,
        "hotplug": False, "tran": "sata", "model": "ST1000LM049 ", "serial": "X1", "ro": False,
        "children": [{"name": "sda1", "path": "/dev/sda1", "maj:min": "8:1", "type": "part", "fstype": "ntfs",
                      "label": "DATA", "uuid": "01DC", "rota": None, "ro": False,
                      "mountpoints": ["/mnt/data", None]}]}]}
    devs = blockdev.parse(data)
    part = next(d for d in devs if d.name == "sda1")
    assert part.rotational and part.transport == "sata" and part.model == "ST1000LM049"
    assert part.dev == (8, 1) and part.mountpoints == ("/mnt/data",)


def test_device_present_rejects_path_tricks():
    assert not blockdev.device_present("../../etc")
    assert not blockdev.device_present("")


# -- probe ----------------------------------------------------------------------------------------
def test_probe_on_tmp_dir_and_cleanup(tmp_path):
    result = probe(tmp_path, ostree=False)
    assert result.writable and result.cleaned_up
    for cap in ("write_read_fsync", "chmod_persists", "exec_allowed", "symlinks", "hardlinks", "atomic_rename"):
        assert result.caps[cap] is True, cap
    assert result.supports_appimages
    assert not any(p.name.startswith(PROBE_PREFIX) for p in tmp_path.iterdir())


@pytest.mark.needs_tool("ostree")
def test_probe_ostree(tmp_path):
    result = probe(tmp_path, ostree=True)
    assert result.caps["ostree_bare_user_only"] is True
    assert result.supports_flatpak and result.cleaned_up


def test_probe_unwritable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        if os.access(ro, os.W_OK):
            pytest.skip("running with privileges that ignore permissions")
        result = probe(ro, ostree=False)
        assert not result.writable and "mkdir" in result.errors
    finally:
        ro.chmod(0o755)


def test_probe_requires_absolute_path():
    with pytest.raises(StorageError):
        probe("relative/dir")


# -- registration and resolution ------------------------------------------------------------------
def test_register_and_resolve_via_duplicate_mount(candidates, monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    reg = open_registry(":memory:")
    loc, _ = reg_loc(reg, "/run/media/me/My Data/Apps", "HDD", candidates, default=True)
    assert loc.canonical_mount == "/mnt/data" and loc.subpath == "Apps" and loc.view_root == "/"
    assert loc.location_class == "user-owned" and loc.is_default
    rv = locations.resolve(loc, candidates)
    assert rv.online and rv.path == "/mnt/data/Apps"
    assert str(locations.apps_dir(loc, rv)) == "/mnt/data/Apps/Applications"


def test_resolve_offline_drive_without_touching_it(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    reg = open_registry(":memory:")
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    loc, _ = reg_loc(reg, "/mnt/photos", "Photos", candidates)
    monkeypatch.setattr(blockdev, "device_present", lambda u: False)
    rv = locations.resolve(loc, candidates)
    assert not rv.online and rv.reason == "the drive is not connected"


def test_resolve_after_mountpoint_change(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    reg = open_registry(":memory:")
    loc, _ = reg_loc(reg, "/mnt/photos/Apps", "Photos", candidates)
    moved = discover(mountinfo.parse(MOUNTINFO.replace("/mnt/photos", "/srv/photos")), DEVICES, FSTAB,
                     home="/home/me", statvfs=fake_statvfs)
    rv = locations.resolve(loc, moved)
    assert rv.online and rv.path == "/srv/photos/Apps"


def test_register_refuses_ineligible(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    reg = open_registry(":memory:")
    with pytest.raises(StorageError, match="noexec"):
        reg_loc(reg, "/mnt/scratch", "Scratch", candidates)


def test_system_location_in_home(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    reg = open_registry(":memory:")
    loc, _ = reg_loc(reg, "/home/me", "SSD", candidates)
    assert (loc.canonical_mount, loc.view_root, loc.subpath) == ("/home", "/@home", "me")
    rv = locations.resolve(loc, candidates)
    assert rv.path == "/home/me" and str(locations.apps_dir(loc, rv)) == "/home/me/Applications"


def test_apps_dir_cannot_escape_location(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    reg = open_registry(":memory:")
    with pytest.raises(StorageError):
        reg_loc(reg, "/mnt/photos", "P", candidates, apps_dir_name="../../etc")
    loc, _ = reg_loc(reg, "/mnt/photos", "P", candidates, apps_dir_name="..Apps")  # a legitimate name
    assert str(locations.apps_dir(loc, locations.resolve(loc, candidates))) == "/mnt/photos/..Apps"


def test_malformed_mountinfo_lines_give_one_clear_error():
    import pytest

    from cygnus.core.storage import mountinfo

    for bad in ["24 1 0022 / / rw shared:1 - btrfs /dev/sda rw", "24 1 0:22:3 / / rw shared:1 - btrfs /dev/sda rw",
                "x 1 0:22 / / rw shared:1 - btrfs /dev/sda rw", "24 1 0:22 / / rw"]:
        with pytest.raises(ValueError, match="malformed mountinfo line"):
            mountinfo.parse_line(bad)


def test_register_refuses_unsafe_labels(candidates, monkeypatch):
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    monkeypatch.setattr(blockdev, "device_present", lambda u: True)
    reg = open_registry(":memory:")
    for bad in ["HDD\ntouch /tmp/x #", "HD\rD", "a\x00b", "\x1b[31mred", "", "   ", "x" * 65]:
        with pytest.raises(StorageError):
            reg_loc(reg, "/home/me", bad, candidates)
    loc, _ = reg_loc(reg, "/home/me", "  Fast SSD  ", candidates)
    assert loc.label == "Fast SSD"


def test_mountinfo_keeps_unescaped_control_characters_in_paths():
    text = ("24 1 0:22 / /mnt/a\rb rw shared:1 - ext4 /dev/sda1 rw\n"
            "25 1 0:23 / /mnt/c\x0cd e rw shared:2 - ext4 /dev/sdb1 rw\n")
    assert [m.mountpoint for m in mountinfo.parse(text)] == ["/mnt/a\rb", "/mnt/c\x0cd e"]


def test_fstab_fields_are_split_on_blanks_only():
    from cygnus.core.storage import fstab

    [e] = fstab.parse("UUID=1 /mnt/a\rb ext4 defaults 0 2\n")
    assert e.mountpoint == "/mnt/a\rb" and e.fstype == "ext4"


def test_safe_realpath_applies_dotdot_after_following_links(tmp_path):
    (tmp_path / "real/deep").mkdir(parents=True)
    (tmp_path / "real/other").mkdir()
    (tmp_path / "other").mkdir()
    os.symlink(tmp_path / "real/deep", tmp_path / "link")
    os.symlink("deep/../other", tmp_path / "real/rel")
    got = locations.safe_realpath(str(tmp_path / "link/../other"), mounts=[], fstab_entries=[])
    assert got == os.path.realpath(tmp_path / "link/../other") == str(tmp_path / "real/other")
    assert locations.safe_realpath(str(tmp_path / "real/rel"), mounts=[], fstab_entries=[]) == str(tmp_path / "real/other")
    assert locations.safe_realpath(str(tmp_path / "./real/./deep/.."), mounts=[], fstab_entries=[]) == str(tmp_path / "real")


# -- review round 4 ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("label", ["‮evil", "a​b", "x y", "x y", "﻿bom", "ab", "a⁨b"])
def test_a_location_name_may_not_hide_or_reverse_text(label):
    from cygnus.core.errors import StorageError
    from cygnus.core.storage import locations

    with pytest.raises(StorageError):
        locations.validate_label(label)


@pytest.mark.parametrize("label", ["HDD", "My Games 2", "Externe Festplatte", "ディスク", "SSD (fast)"])
def test_ordinary_location_names_are_still_fine(label):
    from cygnus.core.storage import locations

    assert locations.validate_label(f"  {label} ") == label


def test_a_hash_inside_an_fstab_field_is_not_a_comment():
    from cygnus.core.storage import fstab

    entries = fstab.parse("UUID=1234 /mnt/data#1 ntfs3 nosuid,nodev 0 0\n"
                          "   # a real comment\n"
                          "UUID=5678 /mnt/b ext4 defaults 0 2 # trailing words are extra fields\n")
    assert [(e.spec, e.mountpoint) for e in entries] == [("UUID=1234", "/mnt/data#1"), ("UUID=5678", "/mnt/b")]
