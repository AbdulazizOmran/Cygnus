"""Portable archives: recognised by what is inside, read once with every size capped, and refused with a reason when anything in them
is unsafe. Nothing here extracts."""

import struct

import pytest

from cygnus.core.detect import archive as ad
from cygnus.core.detect import detect_file
from cygnus.core.errors import DetectionError
from cygnus.core.models import PackageFormat
from tests import builders as b


def codes(cand):
    return {f.code for f in cand.findings}


def errors(cand):
    return {f.code for f in cand.findings if f.severity.value == "error"}


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst", ""])
def test_a_portable_program_is_recognised_in_every_compression_and_whatever_the_file_is_called(tmp_path, compression):
    for name in ("Discord.tar.gz", "Discord.tar", "Discord.bin", "discord-0.0.45.tgz"):  # the person's own file was gzip named .tar
        cand = detect_file(b.build_tarball(tmp_path / name, b.portable_app_entries(), compression=compression))
        assert cand.format is PackageFormat.ARCHIVE and errors(cand) == set()
        m = cand.metadata
        assert (cand.name, m["root"], m["main"], m["icon"]["path"]) == ("Discord", "Discord", "discord", "discord.png")
        assert list(m["scripts"]) == ["postinst.sh"] and "ARCHIVE_SCRIPTS" in codes(cand)
        assert m["desktop_entry"]["Exec"] == "/usr/bin/discord --url -- %u" and cand.identity["desktop_id"] == "discord.desktop"
        assert cand.installed_size == m["total_bytes"] > 0 and not m["has_symlinks"]


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "zst", ""])
def test_a_real_arch_package_is_still_an_arch_package_in_every_compression(tmp_path, compression):
    assert detect_file(b.build_pkg(tmp_path, compression=compression)).format is PackageFormat.LOCAL_PKG


def test_a_package_by_name_without_package_information_is_called_damaged_not_offered_for_unpacking(tmp_path):
    for name in ("thing-1.0-1-x86_64.pkg.tar.zst", "thing.pkg.tar.gz", "thing.pkg.tar", "thing.pkg"):
        path = b.build_tarball(tmp_path / name, b.portable_app_entries(), compression="zst" if name.endswith("zst") else "gz")
        with pytest.raises(DetectionError, match="named like an Arch package"):
            detect_file(path)


def test_a_dot_pkg_is_judged_by_what_it_holds_and_the_other_kinds_are_named(tmp_path):
    arch = b.build_tarball(tmp_path / "a.pkg", [(".PKGINFO", "file", "pkgname = x\npkgver = 1-1\narch = any\n", 0o644),
                                                 ("usr/bin/x", "file", b.ELF_STUB, 0o755)], compression="zst")
    assert detect_file(arch).format is PackageFormat.LOCAL_PKG  # an Arch package whatever its name
    mac = tmp_path / "mac.pkg"
    mac.write_bytes(b"xar!" + struct.pack(">HHQQI", 28, 1, 100, 200, 1) + b"\0" * 300)
    with pytest.raises(DetectionError, match="macOS"):
        detect_file(mac)
    junk = tmp_path / "junk.pkg"
    junk.write_bytes(b"not an archive at all" * 50)
    with pytest.raises(DetectionError, match="not an Arch package"):
        detect_file(junk)


def test_a_freebsd_package_called_dot_pkg_is_named_as_one(tmp_path):
    path = b.build_tarball(tmp_path / "thing-1.0.pkg", [("+MANIFEST", "file", "{}", 0o644), ("usr/local/bin/x", "file", b.ELF_STUB, 0o755)], compression="xz")
    with pytest.raises(DetectionError, match="FreeBSD"):
        detect_file(path)


def test_a_freebsd_package_is_said_to_be_for_freebsd(tmp_path):
    cand = detect_file(b.build_tarball(tmp_path / "thing.pkg.txz.tar.xz", [("+MANIFEST", "file", "{}", 0o644), ("+COMPACT_MANIFEST", "file", "{}", 0o644),
                                                                           ("usr/local/bin/x", "file", b.ELF_STUB, 0o755)], compression="xz"))
    assert "ARCHIVE_FREEBSD_PACKAGE" in errors(cand)


HOSTILE = {
    "an absolute path": ([("/etc/cron.d/evil", "file", "x", 0o644)] + b.portable_app_entries(), "ARCHIVE_UNSAFE_PATH"),
    "a path that climbs out": ([("../../.bashrc", "file", "x", 0o644)] + b.portable_app_entries(), "ARCHIVE_UNSAFE_PATH"),
    "a climbing path in the middle": ([("Discord/../../x", "file", "x", 0o644)] + b.portable_app_entries(), "ARCHIVE_UNSAFE_PATH"),
    "a hard link": (b.portable_app_entries(extra=[("Discord/h", "hardlink", "Discord/discord", 0o755)]), "ARCHIVE_HARDLINK"),
    "a device file": (b.portable_app_entries(extra=[("Discord/dev", "chr", None, 0o666)]), "ARCHIVE_SPECIAL_FILE"),
    "a block device": (b.portable_app_entries(extra=[("Discord/sda", "blk", None, 0o660)]), "ARCHIVE_SPECIAL_FILE"),
    "a pipe": (b.portable_app_entries(extra=[("Discord/pipe", "fifo", None, 0o644)]), "ARCHIVE_SPECIAL_FILE"),
    "a link with an absolute target": (b.portable_app_entries(extra=[("Discord/etc", "symlink", "/etc", 0o777)]), "ARCHIVE_UNSAFE_LINK"),
    "a link that climbs out": (b.portable_app_entries(extra=[("Discord/up", "symlink", "../../..", 0o777)]), "ARCHIVE_UNSAFE_LINK"),
    "a link out of the stripped folder": (b.portable_app_entries(extra=[("Discord/lib", "symlink", "../Discord/discord", 0o777)]), "ARCHIVE_UNSAFE_LINK"),
    "the same file twice": (b.portable_app_entries(extra=[("Discord/discord", "file", "#!/bin/sh\nrm -rf ~\n", 0o755)]), "ARCHIVE_DUPLICATE"),
    "a file written through a link": (b.portable_app_entries(extra=[("Discord/link", "symlink", "sub", 0o777), ("Discord/link/evil", "file", "x", 0o644)]),
                                       "ARCHIVE_THROUGH_LINK"),
}


@pytest.mark.parametrize("name", list(HOSTILE))
def test_anything_unsafe_in_an_archive_is_a_reason_to_refuse_it_and_is_named(tmp_path, name):
    entries, code = HOSTILE[name]
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries))
    assert code in errors(cand)


def test_a_link_inside_the_folder_is_fine_and_is_noted_because_some_drives_cannot_hold_links(tmp_path):
    entries = b.portable_app_entries(extra=[("Discord/lib", "dir", None, 0o755), ("Discord/lib/libx.so.1", "file", b.ELF_STUB, 0o755),
                                            ("Discord/lib/libx.so", "symlink", "libx.so.1", 0o777), ("Discord/data", "symlink", "lib/../lib", 0o777)])
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries))
    assert errors(cand) == set() and cand.metadata["has_symlinks"] is True


def test_the_archive_may_not_be_bigger_than_the_caps_however_it_is_made(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ad, "MAX_ENTRIES", 5)
        cand = detect_file(b.build_tarball(tmp_path / "many.tar.gz", b.portable_app_entries()))
        assert "ARCHIVE_TOO_MANY" in errors(cand)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ad, "MAX_BYTES", 5000)
        cand = detect_file(b.build_tarball(tmp_path / "big.tar.gz", b.portable_app_entries(extra=[("Discord/blob", "file", b"x" * 20000, 0o644)])))
        assert "ARCHIVE_TOO_BIG" in errors(cand)


def test_a_small_file_that_unpacks_into_far_too_much_is_stopped_by_the_stream_cap_alone(tmp_path, monkeypatch):
    # the total of the sizes is far under its limit here, so only the cap on the decompressed stream can stop this one
    monkeypatch.setattr(ad, "MAX_BYTES", 10**9)
    monkeypatch.setattr(ad, "_STREAM_SLACK", -(10**9) + 20_000)  # the stream may not be more than 20,000 bytes
    path = b.build_tarball(tmp_path / "bomb.tar.gz", [("x/big", "file", b"\0" * 5_000_000, 0o644)])
    assert "ARCHIVE_TOO_BIG" in errors(detect_file(path))


@pytest.mark.parametrize("data", [b"\x1f\x8b\x08\x00garbage-after-a-gzip-header", b"\x28\xb5\x2f\xfd" + b"\xff" * 64, b"\xfd7zXZ\x00" + b"\x01" * 64, b"BZh9" + b"\x00" * 40])
def test_a_damaged_archive_is_a_plain_error_never_a_crash(tmp_path, data):
    path = tmp_path / "broken.tar.gz"
    path.write_bytes(data)
    with pytest.raises(DetectionError):
        detect_file(path)


def test_a_truncated_archive_is_refused(tmp_path):
    good = b.build_tarball(tmp_path / "good.tar.xz", b.portable_app_entries(extra=[("Discord/big", "file", bytes(range(256)) * 400, 0o644)]), compression="xz")
    cut = tmp_path / "cut.tar.xz"
    cut.write_bytes(good.read_bytes()[:-60])
    with pytest.raises(DetectionError):
        detect_file(cut)


def test_source_code_a_system_tree_nothing_to_run_and_an_empty_archive_are_each_named(tmp_path):
    src = detect_file(b.build_tarball(tmp_path / "s.tar.gz", [("proj-1.0", "dir", None, 0o755), ("proj-1.0/configure", "file", "#!/bin/sh\n", 0o755),
                                                              ("proj-1.0/Makefile.in", "file", "x", 0o644), ("proj-1.0/main.c", "file", "int main(){}", 0o644)]))
    assert "ARCHIVE_SOURCE_CODE" in errors(src)
    rootfs = detect_file(b.build_tarball(tmp_path / "r.tar.gz", [("usr/bin/tool", "file", b.ELF_STUB, 0o755), ("etc/tool.conf", "file", "x", 0o644)]))
    assert "ARCHIVE_SYSTEM_LAYOUT" in errors(rootfs)
    opt = detect_file(b.build_tarball(tmp_path / "o.tar.gz", [("opt/tool/tool", "file", b.ELF_STUB, 0o755)]))
    assert "ARCHIVE_SYSTEM_LAYOUT" in errors(opt)
    data = detect_file(b.build_tarball(tmp_path / "d.tar.gz", [("docs", "dir", None, 0o755), ("docs/readme.txt", "file", "hello", 0o644), ("docs/run.dat", "file", "x", 0o755)]))
    assert "ARCHIVE_NO_PROGRAM" in errors(data)
    empty = detect_file(b.build_tarball(tmp_path / "e.tar.gz", []))
    assert "ARCHIVE_EMPTY" in errors(empty)


def test_an_unpacked_appimage_folder_is_a_program_even_though_it_has_usr(tmp_path):
    entries = [("app", "dir", None, 0o755), ("app/AppRun", "file", "#!/bin/sh\nexec \"$(dirname \"$0\")/usr/bin/app\"\n", 0o755), ("app/usr", "dir", None, 0o755),
               ("app/usr/bin", "dir", None, 0o755), ("app/usr/bin/app", "file", b.ELF_STUB, 0o755), ("app/app.desktop", "file", "[Desktop Entry]\nName=App\nExec=app\n", 0o644)]
    cand = detect_file(b.build_tarball(tmp_path / "app.tar.gz", entries))
    assert errors(cand) == set() and cand.metadata["main"] in ("usr/bin/app", "AppRun")


def test_special_privilege_marks_are_reported_and_install_scripts_found(tmp_path):
    entries = b.portable_app_entries(extra=[("Discord/chrome-sandbox", "file", b.ELF_STUB, 0o4755)])
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries))
    assert cand.metadata["special_modes"] == ["chrome-sandbox"] and "ARCHIVE_SPECIAL_MODES" in codes(cand)
    assert cand.metadata["main"] == "discord"  # its own helper is never chosen for the person


# -- which program to start --------------------------------------------------------------------------------------------------------------
def prog(kind="elf"):
    return {"kind": kind, "size": 1000}


@pytest.mark.parametrize("exec_line, programs, expected", [
    ("/usr/bin/discord --url -- %u", {"discord": prog("script"), "updater_bootstrap": prog()}, "discord"),
    ("/opt/Thing/thing %F", {"thing": prog(), "bin/thing": prog()}, "thing"),
    ("/usr/share/thing/bin/thing", {"thing": prog(), "bin/thing": prog()}, "bin/thing"),  # the longest matching tail wins
    ("env GTK_USE_PORTAL=1 /usr/bin/thing %U", {"thing": prog(), "other": prog()}, "thing"),
    ("thing", {"sub/thing": prog(), "deep/er/thing": prog()}, "sub/thing"),
])
def test_the_menu_entry_that_came_with_the_archive_decides_which_program_it_means(exec_line, programs, expected):
    meta = {"executables": programs, "desktop_entries": [{"path": "thing.desktop", "fields": {"Exec": exec_line}}], "root": "thing"}
    assert ad.choose_main(meta)[0] == expected


def test_without_a_menu_entry_the_likely_program_is_picked_only_when_it_is_plain_and_otherwise_the_person_chooses():
    one = {"executables": {"thing": prog(), "chrome-sandbox": prog(), "crashpad_handler": prog(), "libx.so.1": prog()}, "root": "thing"}
    assert ad.choose_main(one)[0] == "thing"
    named = {"executables": {"bin/idea.sh": prog("script"), "bin/fsnotifier": prog(), "bin/other": prog()}, "root": "idea"}
    assert ad.choose_main(named)[0] == "bin/idea.sh"
    many = {"executables": {"bin/a": prog(), "bin/b": prog(), "bin/c": prog()}, "root": "tools"}
    chosen, candidates = ad.choose_main(many)
    assert chosen is None and candidates == ["bin/a", "bin/b", "bin/c"]
    assert ad.choose_main({"executables": {}}) == (None, [])
    assert ad.choose_main({"executables": {"postinst.sh": prog("script"), "install.sh": prog("script")}}) == (None, [])  # install scripts are never the program


def test_name_and_version_are_guessed_from_the_names_and_never_from_guesswork_alone():
    assert ad._name_guess("discord-0.0.45.tar.gz", "") == "discord"
    assert ad._name_guess("Telegram-5.1.2-linux-x64.tar.xz", "Telegram") == "Telegram"
    assert ad._name_guess("firefox-130.0.tar.bz2", "firefox") == "firefox"
    assert ad._name_guess("thing_linux_x86_64.tar.gz", "") == "thing"
    assert ad._version_guess("discord-0.0.45.tar.gz") == "0.0.45" and ad._version_guess("Discord.tar.gz", "Discord") is None
    assert ad._version_guess("blender-4.2.1-linux-x64.tar.xz") == "4.2.1" and ad._version_guess("app-1.2rc3.tgz") == "1.2rc3"


def test_a_big_archive_is_read_quickly_and_in_one_pass(tmp_path):
    import time

    entries = [("big", "dir", None, 0o755)] + [(f"big/dir{i % 50}/file{i}.dat", "file", b"x" * 50, 0o644) for i in range(30_000)] \
        + [("big/big", "file", b.ELF_STUB, 0o755)]
    path = b.build_tarball(tmp_path / "big.tar.gz", entries)
    started = time.monotonic()
    cand = detect_file(path)
    assert time.monotonic() - started < 15 and cand.metadata["entries"] == 30_002 and errors(cand) == set()


def test_an_archive_may_not_carry_the_marker_file_cygnus_uses_to_recognise_its_folders(tmp_path):
    entries = b.portable_app_entries(extra=[("Discord/.cygnus-installation", "file", '{"installation": "x"}', 0o644)])
    assert "ARCHIVE_RESERVED_NAME" in errors(detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries)))


# -- what the second review found ---------------------------------------------------------------------------------------------------------------
def test_a_name_with_a_hundred_thousand_folders_is_refused_at_once_not_after_minutes(tmp_path):
    import time

    for segments in (20_000, 100_000):
        path = b.build_tarball(tmp_path / f"deep{segments}.tar.gz", [("Prog", "dir", None, 0o755), ("Prog/run", "file", b.ELF_STUB, 0o755),
                                                                   ("Prog/" + "a/" * segments + "f", "file", "x", 0o644)])
        started = time.monotonic()
        cand = detect_file(path)
        assert time.monotonic() - started < 3 and "ARCHIVE_NAME_TOO_LONG" in errors(cand), segments


def test_a_path_a_file_system_can_hold_is_fine_and_one_it_cannot_is_not(tmp_path):
    ok = "/".join(["d" * 100] * 30)  # 3,029 bytes
    assert errors(detect_file(b.build_tarball(tmp_path / "ok.tar.gz", b.portable_app_entries(extra=[(f"Discord/{ok}/f", "file", "x", 0o644)])))) == set()
    for name in ("x" * 256, "a/" + "y" * 300 + "/f", "/".join(["e" * 200] * 20)):
        cand = detect_file(b.build_tarball(tmp_path / "bad.tar.gz", b.portable_app_entries(extra=[(f"Discord/{name}", "file", "x", 0o644)])))
        assert "ARCHIVE_NAME_TOO_LONG" in errors(cand), name[:20]


@pytest.mark.parametrize("kind", ["gnu", "pax"])
def test_a_header_that_claims_to_be_enormous_is_refused_without_reading_it_into_memory(tmp_path, kind):
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    fmt = tarfile.GNU_FORMAT if kind == "gnu" else tarfile.PAX_FORMAT
    with tarfile.open(fileobj=raw, mode="w", format=fmt) as tar:
        ok = tarfile.TarInfo("Prog/run")
        ok.size, ok.mode = 4, 0o755
        tar.addfile(ok, io.BytesIO(b"\x7fELF"))
        if kind == "gnu":
            big = tarfile.TarInfo("Prog/" + "n" * (6 * 1024 * 1024))  # a long-name header of 6 MiB
        else:
            big = tarfile.TarInfo("Prog/x")
            big.pax_headers = {"comment": "c" * (6 * 1024 * 1024)}  # an extended header of 6 MiB
        big.size = 1
        tar.addfile(big, io.BytesIO(b"x"))
    path = tmp_path / "huge.tar.gz"
    path.write_bytes(gzip.compress(raw.getvalue(), 1))
    assert "ARCHIVE_BAD_HEADER" in errors(detect_file(path))  # refused for what the header says, not after reading it (tarfile reads it whole)


@pytest.mark.parametrize("entries, code", [
    ([("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/sub", "file", "x", 0o644), ("Prog/sub/y", "file", "y", 0o644)], "ARCHIVE_THROUGH_LINK"),
    ([("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/sub/x", "file", "x", 0o644), ("Prog/sub", "file", "y", 0o644)], "ARCHIVE_DUPLICATE"),
    ([("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/sub/x", "file", "x", 0o644), ("Prog/sub", "symlink", ".", 0o777)], "ARCHIVE_DUPLICATE"),
    ([("bin", "file", "x", 0o644), ("bin/run", "file", b.ELF_STUB, 0o755), ("readme", "file", "r", 0o644)], "ARCHIVE_THROUGH_LINK"),
    ([("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/a/b/c", "file", "x", 0o644), ("Prog/a", "file", "y", 0o644)], "ARCHIVE_DUPLICATE"),
])
def test_a_file_or_link_above_entries_that_are_inside_it_is_refused_when_the_archive_is_looked_at_not_when_it_fails_to_unpack(tmp_path, entries, code):
    assert code in errors(detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries)))


def test_a_folder_listed_after_its_contents_or_twice_is_fine(tmp_path):
    entries = [("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/sub/x", "file", "x", 0o644), ("Prog/sub", "dir", None, 0o755), ("Prog/sub", "dir", None, 0o755)]
    assert errors(detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries))) == set()


@pytest.mark.parametrize("links, bad", [
    ({"A": ".", "B": "A/../x"}, True),                      # A is a link to the top, so ".." from there leaves
    ({"d/e": "..", "f": "d/e/.."}, True),                   # looks inside by its text, leaves through d/e
    ({"A": "B", "B": "A"}, True),                           # a loop
    ({f"l{i}": f"l{i + 1}" for i in range(60)}, True),      # a chain far too long to follow
    ({"A": "run"}, False), ({"a": "b", "b": "c", "c": "run"}, False), ({"lib/liba.so": "../lib/liba.so.1"}, False),
    ({"d/up": ".."}, False), ({"d/up": "../run"}, False), ({"x": "./run"}, False),
])
def test_a_link_is_judged_by_where_it_really_leads_through_the_other_links(tmp_path, links, bad):
    entries = [("Prog", "dir", None, 0o755), ("Prog/run", "file", b.ELF_STUB, 0o755), ("Prog/d", "dir", None, 0o755),
               ("Prog/lib", "dir", None, 0o755), ("Prog/lib/liba.so.1", "file", b.ELF_STUB, 0o755)]
    entries += [(f"Prog/{name}", "symlink", target, 0o777) for name, target in links.items() if not name.startswith("d/e")]
    if "d/e" in links:
        entries += [("Prog/d/e", "symlink", "..", 0o777), ("Prog/f", "symlink", links["f"], 0o777)]
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries))
    assert ("ARCHIVE_UNSAFE_LINK" in errors(cand)) is bad, links


def test_so_many_links_to_follow_are_refused_rather_than_taking_for_ever(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "MAX_LINK_WORK", 20)
    entries = [("Prog", "dir", None, 0o755), ("Prog/run", "file", b.ELF_STUB, 0o755)] + [(f"Prog/l{i}", "symlink", "run", 0o777) for i in range(40)]
    assert "ARCHIVE_UNSAFE_LINK" in errors(detect_file(b.build_tarball(tmp_path / "x.tar.gz", entries)))


def test_an_arch_style_install_file_is_an_install_script_and_is_never_offered_as_the_program(tmp_path):
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", b.portable_app_entries(extra=[("Discord/.INSTALL", "file", "#!/bin/sh\nsystemctl x\n", 0o755)])))
    assert ".INSTALL" in cand.metadata["scripts"] and ".INSTALL" not in cand.metadata["main_candidates"] and "ARCHIVE_SCRIPTS" in codes(cand)


def test_names_in_the_messages_cannot_carry_a_terminal_escape(tmp_path):
    evil = "Discord/\x1b[2J\x1b]0;pwned\x07evil"
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", b.portable_app_entries(extra=[(evil, "file", "#!/bin/sh\n", 0o4755), ("Discord/\x1b[31mpostinst", "file", "#!/bin/sh\n", 0o755)])))
    for finding in cand.findings:
        assert "\x1b" not in finding.message and "\x07" not in finding.message, finding.message


@pytest.mark.parametrize("exec_line", ["/usr/bin/postinst.sh", "/opt/x/install.sh --now", "/usr/bin/.INSTALL"])
def test_a_menu_entry_cannot_make_an_install_script_the_program(tmp_path, exec_line):
    entries = [("T", "dir", None, 0o755), ("T/real", "file", b.ELF_STUB, 0o755), ("T/postinst.sh", "file", "#!/bin/sh\n", 0o755),
               ("T/install.sh", "file", "#!/bin/sh\n", 0o755), ("T/.INSTALL", "file", "#!/bin/sh\n", 0o755),
               ("T/t.desktop", "file", f"[Desktop Entry]\nName=T\nExec={exec_line}\n", 0o644)]
    cand = detect_file(b.build_tarball(tmp_path / "t.tar.gz", entries))
    assert cand.metadata["main"] == "real"


def test_the_only_program_is_found_however_deep_it_is(tmp_path):
    cand = detect_file(b.build_tarball(tmp_path / "x.tar.gz", [("Tool", "dir", None, 0o755), ("Tool/a/b/c/prog", "file", b.ELF_STUB, 0o755)]))
    assert cand.metadata["main"] == "a/b/c/prog" and errors(cand) == set()
    two = detect_file(b.build_tarball(tmp_path / "y.tar.gz", [("Tool", "dir", None, 0o755), ("Tool/a/b/c/p1", "file", b.ELF_STUB, 0o755), ("Tool/a/b/c/p2", "file", b.ELF_STUB, 0o755)]))
    assert two.metadata["main"] is None and two.metadata["main_candidates"] == ["a/b/c/p1", "a/b/c/p2"]  # two far-off programs: the person chooses


def test_an_archive_whose_first_member_is_a_big_dot_file_is_still_read_as_a_program_archive(tmp_path):
    entries = [(".cache-blob", "file", b"\0" * (40 * 1024 * 1024), 0o644), ("Prog", "dir", None, 0o755), ("Prog/run", "file", b.ELF_STUB, 0o755)]
    cand = detect_file(b.build_tarball(tmp_path / "blob.tar.gz", entries))  # a package has .PKGINFO in its first kilobytes: this is not one
    assert cand.format is PackageFormat.ARCHIVE


@pytest.mark.parametrize("name", ["tool-1.0.pkg.tar.lz4", "tool.pkg.tar.lzo", "tool.pkg.tar.lrz", "tool.pkg.tar.Z"])
def test_an_arch_package_in_a_compression_cygnus_cannot_read_says_so(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"\x04\x22\x4d\x18" + b"\0" * 300)
    with pytest.raises(DetectionError, match="cannot read"):
        detect_file(path)


def test_global_records_that_add_up_to_more_than_a_real_archive_has_are_refused(tmp_path):
    import gzip
    import io
    import tarfile

    def record(key: str, value: str) -> bytes:
        body = f" {key}={value}\n".encode()
        length = len(body) + len(str(len(body)))
        length = len(body) + len(str(length))  # the length counts its own digits
        return f"{length}".encode() + body

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for i in range(3):  # each under the limit for one header, together over it
            data = record(f"k{i}", "v" * 450_000)
            info = tarfile.TarInfo(f"GlobalHead.{i}")
            info.type, info.size = tarfile.XGLTYPE, len(data)
            tar.addfile(info, io.BytesIO(data))
        ok = tarfile.TarInfo("Prog/run")
        ok.size, ok.mode = 4, 0o755
        tar.addfile(ok, io.BytesIO(b"\x7fELF"))
    path = tmp_path / "global.tar.gz"
    path.write_bytes(gzip.compress(raw.getvalue(), 1))
    assert "ARCHIVE_BAD_HEADER" in errors(detect_file(path))
    one = tmp_path / "one.tar.gz"  # an ordinary global record (git archive writes one) is fine
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "0" * 40}) as tar:
        ok = tarfile.TarInfo("Prog/run")
        ok.size, ok.mode = 4, 0o755
        tar.addfile(ok, io.BytesIO(b"\x7fELF"))
    one.write_bytes(gzip.compress(raw.getvalue(), 1))
    assert errors(detect_file(one)) == set()


def test_the_read_limit_on_the_stream_refuses_a_read_that_is_too_big_or_unbounded_and_lets_small_ones_through(tmp_path):
    # the second line of defence, for versions of tarfile that read a long header whole: it is the stream itself that is limited
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("a")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    raw.seek(0)
    with tarfile.open(fileobj=raw, mode="r|") as tar:
        ad.limit_headers(tar)
        assert len(tar.fileobj.read(512)) == 512
        for size in (ad.MAX_HEADER + 1, -1, None):
            with pytest.raises(ad.HeaderTooBig):
                tar.fileobj.read(size)
