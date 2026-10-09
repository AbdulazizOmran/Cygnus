"""Regression tests for the Phase 1 adversarial review: hostile inputs must fail fast and cleanly."""

import io
import os
import sqlite3
import struct
import tarfile
import time

import pytest

import builders
from cygnus.core.desktop import entry
from cygnus.core.detect import appimage as ai_detect
from cygnus.core.detect import detect_file
from cygnus.core.detect import flatpak as fp_detect
from cygnus.core.detect.deb import parse_dependencies
from cygnus.core.errors import DetectionError, StorageError
from cygnus.core.models import Candidate, PackageFormat
from cygnus.core.registry import db as regdb
from cygnus.core.registry import open_registry
from cygnus.core.storage import blockdev, fstab, locations, mountinfo
from cygnus.core.storage import probe as probe_mod
from cygnus.core.storage.discovery import discover
from cygnus.core.util import proc


def fast(fn, limit=2.0):
    t = time.monotonic()
    result = fn()
    assert time.monotonic() - t < limit, "took too long: possible algorithmic blow-up"
    return result


# -- parsers ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("evil", ["a" + " " * 50_000 + "!", "a (" + " " * 50_000 + "!", "a (>=" + " " * 50_000,
                                  "a [" + "x" * 50_000])
def test_deb_dependency_regex_is_linear(evil):
    fast(lambda: parse_dependencies(evil), 0.5)


def test_deb_dependency_field_too_long():
    with pytest.raises(DetectionError):
        parse_dependencies("a, " * 40_000)


def _longlink_header(claimed: int) -> bytes:
    h = bytearray(512)
    h[0:13] = b"././@LongLink"
    h[100:108] = b"0000644\0"
    for off in (108, 116):
        h[off:off + 8] = b"0000000\0"
    h[124:136] = f"{claimed:011o}\0".encode()
    h[136:148] = b"00000000000\0"
    h[156:157] = b"L"
    h[257:265] = b"ustar  \0"
    h[148:156] = b" " * 8
    h[148:156] = f"{sum(h):06o}\0 ".encode()
    return bytes(h)


def test_pkg_longname_bomb_is_capped(tmp_path):
    from compression import zstd

    stream = _longlink_header(1_500_000_000) + b"\0" * (40 * 1024 * 1024)
    path = tmp_path / "bomb-1-1-x86_64.pkg.tar.zst"
    path.write_bytes(zstd.compress(stream))
    with pytest.raises(DetectionError):
        fast(lambda: detect_file(path), 10)


def test_truncated_gzip_header_does_not_crash(tmp_path):
    path = tmp_path / "x.pkg.tar.gz"
    path.write_bytes(b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\x03")
    with pytest.raises(DetectionError):
        detect_file(path)


def test_deb_control_member_flood(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for i in range(500):
            info = tarfile.TarInfo(f"./x{i}")
            tar.addfile(info, io.BytesIO(b""))
    path = tmp_path / "flood.deb"
    path.write_bytes(builders._ar([("debian-binary", b"2.0\n"), ("control.tar.gz", buf.getvalue()),
                                   ("data.tar.gz", builders._tar_bytes({}, "w:gz"))]))
    with pytest.raises(DetectionError, match="many members"):
        detect_file(path)


def test_rpm_duplicate_tags_rejected(tmp_path):
    path = tmp_path / "dup.rpm"
    lead = b"\xed\xab\xee\xdb\x03\x00" + struct.pack(">hh", 0, 1) + b"x".ljust(66, b"\0") + struct.pack(">hh", 1, 5) \
        + b"\0" * 16
    sig = builders._rpm_header([(1000, builders.INT32, [1])])
    sig += b"\0" * ((8 - len(sig) % 8) % 8)
    hdr = builders._rpm_header([(1049, builders.STRING_ARRAY, ["a"])] * 50 + [(1000, builders.STRING, "x")])
    path.write_bytes(lead + sig + hdr)
    with pytest.raises(DetectionError, match="repeats tag"):
        fast(lambda: detect_file(path))


def test_rpm_real_world_types(tmp_path):
    """Epoch is INT32 and Summary is I18NSTRING in real RPMs."""
    lead = b"\xed\xab\xee\xdb\x03\x00" + struct.pack(">hh", 0, 1) + b"x".ljust(66, b"\0") + struct.pack(">hh", 1, 5) \
        + b"\0" * 16
    sig = builders._rpm_header([(1000, builders.INT32, [1])])
    sig += b"\0" * ((8 - len(sig) % 8) % 8)
    hdr = builders._rpm_header([(1000, builders.STRING, "openssl"), (1001, builders.STRING, "3.2"),
                                (1002, builders.STRING, "1"), (1003, builders.INT32, [2]),
                                (1004, 9, ["Secure sockets", "Sichere Sockets"]), (1022, builders.STRING, "x86_64")])
    path = tmp_path / "e.rpm"
    path.write_bytes(lead + sig + hdr)
    cand = detect_file(path)
    assert cand.version == "2:3.2-1" and cand.summary == "Secure sockets"


def test_rpm_wrong_declared_types_do_not_crash(tmp_path):
    lead = b"\xed\xab\xee\xdb\x03\x00" + struct.pack(">hh", 0, 1) + b"x".ljust(66, b"\0") + struct.pack(">hh", 1, 5) \
        + b"\0" * 16
    sig = builders._rpm_header([(1000, builders.INT32, [1])])
    sig += b"\0" * ((8 - len(sig) % 8) % 8)
    hdr = builders._rpm_header([(1000, builders.STRING, "x"), (1001, builders.STRING, "1"),
                                (1002, builders.STRING, "1"), (1022, builders.STRING_ARRAY, ["x86_64", "y"]),
                                (1048, builders.STRING, "notints"), (1049, builders.STRING_ARRAY, ["a"]),
                                (1024, builders.INT32, [5])])
    path = tmp_path / "t.rpm"
    path.write_bytes(lead + sig + hdr)
    detect_file(path)  # must not raise anything but DetectionError; here it should simply parse


def test_truncated_elf64_header(tmp_path):
    path = tmp_path / "short.AppImage"
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\0" * 49)
    with pytest.raises(DetectionError, match="truncated"):
        detect_file(path)


@pytest.mark.needs_tool("mksquashfs", "unsquashfs")
def test_appimage_oversized_desktop_is_not_extracted(tmp_path):
    big = builders.DESKTOP + "#" * (5 * 1024 * 1024)
    cand = fast(lambda: detect_file(builders.build_appimage(tmp_path, desktop=big)), 30)
    assert "desktop_entry" not in cand.metadata
    assert any(f.code == "APPIMAGE_METADATA_OVERSIZED" for f in cand.findings)


def test_appimage_cat_refuses_glob_names(monkeypatch):
    called = []
    monkeypatch.setattr(proc, "run", lambda *a, **k: called.append(a))
    assert ai_detect._cat(__import__("pathlib").Path("/x"), 0, "*") is None and not called


@pytest.mark.parametrize("xml", [b'<?xml version="1.0" encoding="x-bogus"?><component/>',
                                 b'<?xml version="1.0" encoding="utf-7"?><component/>'])
def test_appstream_bad_encodings(xml):
    cand = Candidate(format=PackageFormat.APPIMAGE, source="x")
    ai_detect._apply_appstream(cand, xml)
    assert any(f.code == "APPSTREAM_INVALID" for f in cand.findings)
    import gzip

    fp_detect._apply_appstream(Candidate(format=PackageFormat.FLATPAK_BUNDLE, source="x"), gzip.compress(xml))


def test_flatpakref_default_section_cannot_inject_gpg_key(tmp_path):
    path = tmp_path / "x.flatpakref"
    path.write_text("[Flatpak Ref]\nName=org.x.App\nUrl=https://x.invalid/repo/\n\n[DEFAULT]\nGPGKey=bm90YWtleQ==\n")
    cand = detect_file(path)
    assert cand.metadata["has_gpg_key"] is False
    assert any(f.code == "FLATPAKREF_NO_GPG" for f in cand.findings)


def test_flatpakref_garbage_key_is_not_a_key(tmp_path):
    path = tmp_path / "x.flatpakref"
    path.write_text("[Flatpak Ref]\nName=org.x.App\nGPGKey=notakey\n")
    assert detect_file(path).metadata["has_gpg_key"] is False


def test_desktop_entry_set_cannot_inject_keys():
    de = entry.parse("[Desktop Entry]\nName=Hello\\nExec=sh -c evil\nExec=hello %U\n")
    de.set("Name", de.get("Name"))
    again = entry.parse(de.serialize())
    assert again.get("Exec") == "hello %U"
    assert sum(1 for k, _ in again.groups[entry.MAIN_GROUP] if k == "Exec") == 1
    with pytest.raises(ValueError):
        de.set_raw("Name", "x\nExec=evil")


# -- process runner ----------------------------------------------------------------------------------
def test_process_output_is_bounded_while_reading():
    res = fast(lambda: proc.run(["sh", "-c", "yes x | head -c 100000000"], max_output=1_000_000), 10)
    assert res.truncated and not res.ok and len(res.stdout) <= 1_000_000


def test_process_timeout_raises():
    with pytest.raises(proc.CommandTimeout):
        proc.run(["sleep", "5"], timeout=0.5)


# -- storage -------------------------------------------------------------------------------------------
MI = """\
24 1 0:22 /@ / rw shared:1 - btrfs /dev/nvme0n1p2 rw,subvol=/@
60 24 0:22 /@home /home rw shared:2 - btrfs /dev/nvme0n1p2 rw,subvol=/@home
63 24 0:40 / /tmp rw,nosuid,nodev shared:3 - tmpfs tmpfs rw
64 60 0:50 / /home/u/Vaults/Work rw,nosuid,nodev shared:4 - fuse.gocryptfs gocryptfs rw
65 24 0:51 / /mnt/data rw shared:5 - autofs systemd-1 rw,fd=50
66 24 0:90 / /mnt/nas rw shared:6 - nfs4 nas:/export rw
"""
DEVS = [blockdev.BlockDevice(path="/dev/nvme0n1p2", name="nvme0n1p2", dev=None, type="part", fstype="btrfs",
                             label=None, uuid="ROOT", partuuid=None, size=1, rotational=False, removable=False,
                             hotplug=False, transport="nvme", model=None, serial=None, read_only=False)]


def test_paths_on_tmpfs_or_fuse_vaults_are_refused():
    mounts = mountinfo.parse(MI)
    def no_statvfs(p):
        raise OSError
    cands = discover(mounts, DEVS, [], home="/home/u", statvfs=no_statvfs)
    for path in ("/tmp/apps", "/home/u/Vaults/Work/Apps"):
        with pytest.raises(StorageError, match="cannot hold applications"):
            locations.candidate_for_path(path, cands, mounts)
    cand, view = locations.candidate_for_path("/home/u/Apps", cands, mounts)
    assert cand.fs_uuid == "ROOT" and view.root == "/@home"


def test_absent_automount_is_never_touched(monkeypatch):
    mounts = mountinfo.parse(MI)
    entries = fstab.parse("UUID=HDD1 /mnt/data ntfs3 nofail,x-systemd.automount 0 0\n")
    monkeypatch.setattr(blockdev, "device_present", lambda u: False)
    touched = []
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: touched.append(p) or os.stat("/"))
    with pytest.raises(StorageError, match="not connected"):
        locations.safe_realpath("/mnt/data/apps", mounts, entries)
    assert "/mnt/data" not in touched


def test_safe_realpath_follows_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    mounts = mountinfo.parse("1 0 0:1 / / rw - btrfs /dev/x rw\n")
    assert locations.safe_realpath(str(tmp_path / "link"), mounts, []) == str(real)


def test_network_shares_are_never_statted():
    def statvfs(p):
        assert p != "/mnt/nas", "statvfs on a network share could hang"
        raise OSError
    discover(mountinfo.parse(MI), DEVS, [], home="/home/u", statvfs=statvfs)


def test_probe_cleanup_never_touches_the_parent(tmp_path, monkeypatch):
    loc = tmp_path / "loc"
    loc.mkdir()
    loc.chmod(0o755)
    real_unlink = os.unlink

    def failing_unlink(path, *a, **k):
        if os.fspath(path) == "mm":
            raise PermissionError(1, "injected")
        return real_unlink(path, *a, **k)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    result = probe_mod.probe(loc, ostree=False)
    monkeypatch.setattr(os, "unlink", real_unlink)
    assert oct(loc.stat().st_mode & 0o777) == oct(0o755)
    assert result.cleaned_up is False


def test_probe_on_missing_dir_reports_no_cleanup_needed(tmp_path):
    result = probe_mod.probe(tmp_path / "missing", ostree=False)
    assert not result.writable and result.cleaned_up and "mkdir" in result.errors


# -- registry ------------------------------------------------------------------------------------------
def test_concurrent_migration_is_safe(tmp_path):
    path = tmp_path / "race.db"
    a = open_registry(path, migrate=False)
    b = open_registry(path, migrate=False)
    assert b.schema_version == 0
    a.migrate()
    assert b.migrate() == []  # re-checked under the lock: nothing left to do
    assert b.schema_version == regdb.MIGRATIONS[-1][0]


def test_corrupt_registry_is_a_registry_error(tmp_path):
    path = tmp_path / "bad.db"
    path.write_text("this is not sqlite")
    with pytest.raises(Exception) as exc:
        open_registry(path)
    assert not isinstance(exc.value, sqlite3.Error)


def test_flatpak_metadata_is_never_cut_short():
    from cygnus.core.detect.flatpak import candidate_from_metadata
    from cygnus.core.errors import DetectionError

    padded = "[Application]\nname=org.x.Y\nruntime=org.x.Platform/x86_64/1\n# " + "x" * 300_000 + "\n" \
             "[Context]\nfilesystems=host;\n"
    with pytest.raises(DetectionError, match="implausibly large"):
        candidate_from_metadata(ref="app/org.x.Y/x86_64/stable", remote="r", metadata_text=padded)
    with pytest.raises(DetectionError, match="not a Flatpak ref"):
        candidate_from_metadata(ref="app/org.x.Y/x86_64", remote="r", metadata_text="")


def test_rpm_dependency_lists_must_line_up():
    from cygnus.core.detect import rpm
    from cygnus.core.errors import DetectionError

    with pytest.raises(DetectionError, match="do not match up"):
        rpm._deps(["a", "b", "c"], [0], ["", "", ""], skip_internal=False)
    assert rpm._deps(["a", "b"], None, None, skip_internal=False) == ["a", "b"]


@pytest.mark.parametrize("field", [b"1_0       ", b"+5        ", b" 10       ", b"0x10      ", b"          "])
def test_ar_sizes_are_plain_decimal(tmp_path, field):
    from cygnus.core.detect import deb
    from cygnus.core.errors import DetectionError

    member = b"debian-binary   " + b"0           0     0     100644  " + field + b"`\n" + b"2.0\n"
    data = b"!<arch>\n" + member
    with pytest.raises(DetectionError):
        deb.read_ar_members(io.BytesIO(data), len(data))


def test_appimage_listing_keeps_names_with_arrows(monkeypatch, tmp_path):
    from cygnus.core.detect import appimage

    listing = ("-rw-r--r-- user/user 10 2024-01-01 00:00 squashfs-root/Evil.desktop -> Good.desktop\n"
               "lrwxrwxrwx user/user 3 2024-01-01 00:00 squashfs-root/AppRun -> usr/bin/app\n"
               "lrwxrwxrwx user/user 3 2024-01-01 00:00 squashfs-root/a -> b -> c\n")
    monkeypatch.setattr(appimage.proc, "run", lambda argv, timeout, max_output: type(
        "R", (), {"stdout": listing, "stderr": "", "returncode": 0, "truncated": False})())
    monkeypatch.setattr(appimage.proc, "require", lambda tool, pkg: tool)
    entries = appimage._ls(tmp_path / "x.AppImage", 0)
    assert entries["Evil.desktop -> Good.desktop"][2] is None
    assert entries["AppRun"][2] == "usr/bin/app" and "a" not in entries and "a -> b" not in entries


def test_appimage_metadata_reading_has_one_overall_time_limit(monkeypatch):
    from cygnus.core.detect import appimage
    from cygnus.core.errors import DetectionError

    token = appimage._deadline.set(time.monotonic() - 1)  # the budget is used up
    try:
        with pytest.raises(DetectionError, match="implausibly long"):
            appimage._timeout()
    finally:
        appimage._deadline.reset(token)
    assert appimage._timeout() == 60.0  # outside a metadata read: the per-call limit only
