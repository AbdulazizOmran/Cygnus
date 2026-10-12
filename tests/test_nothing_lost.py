"""Nothing of the vendor's package is lost or changed on the way, except what Cygnus says it leaves out. Two guards:
the unpacked tree must hold every entry of the vendor's file, and the built package must hold exactly the tree."""

import os
import tarfile
import io
import subprocess
from pathlib import Path

import pytest

from cygnus.core.backends import foreign
from cygnus.core.errors import CygnusError
from cygnus.core.ops import convert
from test_conversion_extras import _deb, _listing, _verdict

pytestmark = pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")

# the kinds of file makepkg's own defaults delete or rename, plus folders the program may need to exist
KEPT_FILES = ["opt/acme/lib/libacme.la", "opt/acme/lib/libacme.a", "opt/acme/docs/notes.pod", "usr/share/info/dir",
              "usr/share/man/man1/acme.1", "usr/share/acme/a.txt", "usr/share/acme/b.txt", "usr/share/acme/c.txt"]
EMPTY_FOLDERS = ["opt/acme/plugins", "opt/acme/logs", "opt/acme/data/cache"]


def _entries():
    return [("opt/acme/acme", "file", b"#!/bin/sh\n"), *[(p, "file", b"content of " + p.encode()) for p in KEPT_FILES],
            *[(p, "dir", None) for p in EMPTY_FOLDERS]]


def test_libtool_pod_info_man_and_empty_folders_come_through_exactly_as_the_vendor_shipped_them(tmp_path):
    cand = _deb(tmp_path, _entries())
    pkg, _ = convert.convert(cand, _verdict(cand), tmp_path / "out")
    names = {line.split()[-1].rstrip("/") for line in _listing(pkg).splitlines()}
    for path in ["opt/acme/acme", *KEPT_FILES, *EMPTY_FOLDERS]:
        assert path in names, f"/{path} was lost"
    assert "usr/share/man/man1/acme.1.gz" not in names  # the man page keeps its name and its text
    shipped = subprocess.run(["bsdtar", "-xOf", str(pkg), "usr/share/man/man1/acme.1"], capture_output=True).stdout
    assert shipped == b"content of usr/share/man/man1/acme.1"


# -- the guard on what the packaging tool made --------------------------------------------------------------------------
def _package(path: Path, entries):
    """A package-shaped archive: (name, kind, data) with kind file | dir | link | hard (data is the link target)."""
    with tarfile.open(path, "w:zst") as tar:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, data
                tar.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, data
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return path


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "opt/acme/empty").mkdir(parents=True)
    (root / "opt/acme/app").write_bytes(b"12345")
    os.symlink("app", root / "opt/acme/alias")
    os.link(root / "opt/acme/app", root / "opt/acme/hard")
    return root


BASE = [(".PKGINFO", "file", b"pkgname = x\n"), (".MTREE", "file", b"#mtree\n"), ("opt/", "dir", None), ("opt/acme/", "dir", None),
        ("opt/acme/empty/", "dir", None), ("opt/acme/app", "file", b"12345"), ("opt/acme/alias", "link", "app"),
        ("opt/acme/hard", "hard", "opt/acme/app")]


def test_a_package_that_holds_exactly_the_tree_is_accepted_whatever_the_metadata_files(tree, tmp_path):
    assert convert.compare_with_tree(tree, _package(tmp_path / "p.pkg.tar.zst", BASE)) == []


@pytest.mark.parametrize("change, expected", [
    (lambda e: [x for x in e if x[0] != "opt/acme/app"], "/opt/acme/app is missing"),
    (lambda e: [x for x in e if x[0] != "opt/acme/empty/"], "/opt/acme/empty is missing"),  # an empty folder counts
    (lambda e: [*e, ("opt/acme/extra", "file", b"x")], "/opt/acme/extra was added"),
    (lambda e: [(n, "file", b"12345") if n == "opt/acme/alias" else (n, k, d) for n, k, d in e], "/opt/acme/alias became a file"),
    (lambda e: [(n, k, b"123") if n == "opt/acme/app" else (n, k, d) for n, k, d in e], "/opt/acme/app changed size (5 became 3)"),
    (lambda e: [(n, "dir", None) if n == "opt/acme/app" else (n, k, d) for n, k, d in e], "/opt/acme/app became a folder"),
    (lambda e: [(n, k, "elsewhere") if n == "opt/acme/alias" else (n, k, d) for n, k, d in e],
     "/opt/acme/alias points somewhere else (app became elsewhere)"),  # same kind, same name, another target
])
def test_anything_that_differs_from_the_tree_is_reported(tree, tmp_path, change, expected):
    problems = convert.compare_with_tree(tree, _package(tmp_path / "p.pkg.tar.zst", change(BASE)))
    assert any(expected in p for p in problems), problems


def test_a_conversion_whose_package_differs_from_the_tree_is_refused_and_leaves_nothing_behind(tmp_path, monkeypatch):
    cand = _deb(tmp_path, _entries())
    monkeypatch.setattr(convert, "compare_with_tree", lambda root, pkg: ["/opt/acme/acme is missing"])
    with pytest.raises(CygnusError, match="does not hold exactly what Cygnus meant to ship.*/opt/acme/acme is missing"):
        convert.convert(cand, _verdict(cand), tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.pkg.tar.*"))


# -- the guard on unpacking --------------------------------------------------------------------------------------------
def test_entries_missing_after_unpacking_are_found_from_the_listing(tmp_path):
    (tmp_path / "usr/bin").mkdir(parents=True)
    (tmp_path / "usr/bin/kept").write_bytes(b"x")
    os.symlink("kept", tmp_path / "usr/bin/alias")
    listing = "\n".join([
        "drwxr-xr-x  0 root   root        0 Oct  8 22:19 ./usr/bin/",
        "-rwxr-xr-x  0 root   root        1 Oct  8 22:19 ./usr/bin/kept",
        "lrwxrwxrwx  0 root   root        0 Oct  8 22:19 ./usr/bin/alias -> kept",
        "hrwxr-xr-x  0 root   root        0 Oct  8 22:19 ./usr/bin/kept link to ./usr/bin/hard",
        "-rw-r--r--  0 root   root        1 Oct  8 22:19 ./usr/bin/lost one",
        "-rw-r--r--  0 root   root        1 Oct  8 22:19 /usr/bin/also lost",
        "-rw-r--r--  0 root   root        1 Oct  8 22:19 ../../etc/escape",
        "lrwxrwxrwx  0 root   root        0 Oct  8 22:19 ./usr/bin/gone -> kept"])
    assert foreign.not_unpacked(listing, tmp_path) == ["/usr/bin/lost one", "/usr/bin/also lost", "/../../etc/escape", "/usr/bin/gone"]


def test_a_file_the_unpacker_quietly_skipped_stops_the_conversion(tmp_path, monkeypatch):
    cand = _deb(tmp_path, _entries())
    real = foreign.proc.run

    def run(argv, **kw):
        result = real(argv, **kw)
        if "-x" in argv:  # an unpacker that leaves a file out without failing
            (Path(argv[argv.index("-C") + 1]) / "opt/acme/acme").unlink()
        return result

    monkeypatch.setattr(foreign.proc, "run", run)
    (tmp_path / "dest").mkdir()
    with pytest.raises(foreign.PayloadError, match="could not be unpacked.*/opt/acme/acme"):
        foreign.extract_payload(cand, tmp_path / "dest")


# -- folders nobody can enter must not hide anything from the analysis ---------------------------------------------------
from test_missing_libraries import elf  # noqa: E402,F401 - real compiled programs (a program that needs a missing library)


def _deb_with_closed_folder(tmp_path, program: bytes):
    import builders
    from cygnus.core.detect import detect_file

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, mode in (("opt", 0o755), ("opt/hello", 0o755), ("opt/hello/closed", 0o600)):  # nobody can enter the last
            info = tarfile.TarInfo("./" + name)
            info.type, info.mode = tarfile.DIRTYPE, mode
            tar.addfile(info)
        for name, data in (("opt/hello/closed/program", program), ("usr/share/hello/a.txt", b"a\n"), ("usr/share/hello/b.txt", b"b\n"),
                           ("usr/share/hello/c.txt", b"c\n")):
            info = tarfile.TarInfo("./" + name)
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    control = {"./control": b"Package: hello\nVersion: 1\nArchitecture: amd64\nMaintainer: T <t@x.invalid>\nInstalled-Size: 1\n"
                            b"Depends: \nDescription: t\n"}
    path = tmp_path / "hello_1_amd64.deb"
    path.write_bytes(builders._ar([("debian-binary", b"2.0\n"), ("control.tar.xz", builders._tar_bytes(control, "w:xz")),
                                   ("data.tar.gz", buf.getvalue())]))
    return detect_file(str(path))


@pytest.mark.needs_tool("gcc", "readelf")
def test_a_program_under_a_folder_nobody_can_enter_is_still_analysed(tmp_path, elf):
    # the analysis used to skip whatever it could not open: this program's missing library went unnoticed
    verdict = _verdict(_deb_with_closed_folder(tmp_path, elf["needs_ghost"]))
    assert "libghost.so.5" in verdict.unresolved_sonames and verdict.strategy == "refuse"


def test_a_listing_with_an_owner_name_that_shifts_the_columns_is_refused_not_misread():
    odd = "-rw-r--r--  0 build user root  4294967296 Oct  8 22:19 ./opt/big"  # the size would be read from the wrong column
    with pytest.raises(foreign.PayloadError, match="cannot read reliably"):
        foreign.parse_listing(odd)


def test_real_listing_lines_of_every_kind_are_read():
    lines = "\n".join([
        "drwxr-xr-x  0 root   root        0 Oct  8 22:19 ./usr/",
        "-rwxr-xr-x  0 0      0   143539796 Jan  1  1970 ./opt/big file.bin",
        "lrwxrwxrwx  0 root   root        0 Oct  8 22:19 ./usr/bin/a -> b",
        "hrwxr-xr-x  0 root   root        0 Oct  8 22:19 ./usr/bin/h link to ./usr/bin/g",
        "crw-r--r--  0 root   root     1, 3 Oct  8 22:19 ./dev/null",
        "-rw-r--r--@ 0 root   root       12 Oct  8 22:19 ./with attribute marker"])
    assert foreign.parse_listing(lines) == [
        ("d", 0, "./usr/"), ("-", 143539796, "./opt/big file.bin"), ("l", 0, "./usr/bin/a -> b"),
        ("h", 0, "./usr/bin/h link to ./usr/bin/g"), ("c", 1, "./dev/null"), ("-", 12, "./with attribute marker")]


def test_the_size_bound_cannot_be_dodged_by_an_odd_owner_name(tmp_path, monkeypatch):
    cand = _deb(tmp_path, _entries())
    real = foreign.proc.run

    def run(argv, **kw):
        result = real(argv, **kw)
        if "-tvf" in argv and str(argv[-1]).endswith(".gz"):  # the listing names an owner with a space in it
            import dataclasses

            result = dataclasses.replace(result, stdout="\n".join(
                line.replace("  0 ", "  0 build user ", 1) if line[:1] == "-" else line for line in result.stdout.splitlines()))
        return result

    monkeypatch.setattr(foreign.proc, "run", run)
    with pytest.raises(foreign.PayloadError, match="cannot read reliably"):
        foreign.extract_payload(cand, tmp_path / "unpacked")


@pytest.mark.parametrize("line", [
    "lrwxrwxrwx  0 root   root        0 Oct  8 22:19 ./x -> y -> z",
    "hrwxr-xr-x  0 root   root        0 Oct  8 22:19 ./a link to b link to c"])
def test_a_link_whose_name_holds_the_separator_is_refused_not_cut_in_the_wrong_place(line):
    with pytest.raises(foreign.PayloadError, match="link Cygnus cannot read reliably"):
        foreign.parse_listing(line)


def test_only_a_newline_ends_a_listing_line():
    odd = "-rw-r--r--  0 root   root        5 Oct  8 22:19 ./a\x0cb\x85c"  # form feed and NEL are part of the name
    assert foreign.parse_listing(odd) == [("-", 5, "./a\x0cb\x85c")]


# -- the second and later names of a hard-linked file in an .rpm's cpio payload --------------------------------------------------------------------
def test_a_cpio_hard_link_listed_as_a_regular_file_with_link_to_is_unpacked_when_its_file_is_there(tmp_path):
    """Found on real Fedora packages: a cpio archive lists the later names of a hard-linked file as ordinary files with " link to <first name>" after
    the name (the type letter is "-", not "h"), and the guard took the whole text for the file's name and refused the package as incomplete."""
    (tmp_path / "usr/share/icons").mkdir(parents=True)
    (tmp_path / "usr/share/icons/first.svg").write_bytes(b"x")
    os.link(tmp_path / "usr/share/icons/first.svg", tmp_path / "usr/share/icons/second.svg")
    listing = "\n".join([
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/share/icons/second.svg link to ./usr/share/icons/first.svg",
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/share/icons/first.svg"])
    assert foreign.not_unpacked(listing, tmp_path) == []


def test_a_cpio_hard_link_whose_file_did_not_come_out_is_still_reported(tmp_path):
    (tmp_path / "usr/share/icons").mkdir(parents=True)
    (tmp_path / "usr/share/icons/first.svg").write_bytes(b"x")
    listing = "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/share/icons/second.svg link to ./usr/share/icons/first.svg"
    assert foreign.not_unpacked(listing, tmp_path) == ["/usr/share/icons/second.svg"]  # named without the " link to …", as a person knows the file


def test_a_file_that_really_has_link_to_in_its_name_is_found_by_its_whole_name(tmp_path):
    (tmp_path / "usr/share").mkdir(parents=True)
    (tmp_path / "usr/share/notes link to others.txt").write_bytes(b"x")
    assert foreign.not_unpacked("-rw-r--r--  1 0      0           1 May 29  2024 ./usr/share/notes link to others.txt", tmp_path) == []
    assert foreign.not_unpacked("-rw-r--r--  1 0      0           1 May 29  2024 ./usr/share/missing link to others.txt", tmp_path) == ["/usr/share/missing"]


def test_a_regular_entry_whose_name_holds_the_separator_twice_is_refused_not_cut_in_the_wrong_place():
    with pytest.raises(foreign.PayloadError, match="link Cygnus cannot read reliably"):
        foreign.parse_listing("-rw-r--r--  2 0      0           1 May 29  2024 ./a link to b link to c")


def test_a_cpio_hard_link_that_escapes_is_still_a_lost_entry(tmp_path):
    (tmp_path / "usr").mkdir()
    assert foreign.not_unpacked("-rw-r--r--  2 0      0           1 May 29  2024 ../etc/passwd link to ./usr/x", tmp_path) == ["/../etc/passwd"]


@pytest.mark.needs_tool("bsdtar")
def test_a_real_cpio_archive_with_hard_links_unpacks_and_passes_the_guard(tmp_path):
    """The real tool's listing, not a typed-in one: build a cpio payload like an .rpm's, list it and unpack it as extract_payload does."""
    import subprocess

    src = tmp_path / "src"
    (src / "usr/share/icons").mkdir(parents=True)
    (src / "usr/share/icons/first.svg").write_bytes(b"<svg/>")
    os.link(src / "usr/share/icons/first.svg", src / "usr/share/icons/second.svg")
    os.link(src / "usr/share/icons/first.svg", src / "usr/share/icons/third.svg")
    archive = tmp_path / "payload.cpio"
    names = subprocess.run(["find", ".", "-mindepth", "1"], cwd=src, capture_output=True, text=True, check=True).stdout
    subprocess.run(["bsdtar", "-cf", str(archive), "--format=newc", "-C", str(src), "-T", "-"], input=names, text=True, check=True, capture_output=True)
    listing = subprocess.run(["bsdtar", "-tvf", str(archive)], capture_output=True, text=True, check=True).stdout
    assert " link to " in listing  # the real tool does print the second and later names this way
    dest = tmp_path / "dest"
    dest.mkdir()
    subprocess.run(["bsdtar", "-x", "--no-same-owner", "--no-same-permissions", "-f", str(archive), "-C", str(dest)], check=True, capture_output=True)
    assert foreign.not_unpacked(listing, dest) == []
    assert sorted(p.name for p in (dest / "usr/share/icons").iterdir()) == ["first.svg", "second.svg", "third.svg"]


def test_a_name_that_leaves_the_folder_is_lost_even_when_something_of_that_name_exists(tmp_path):
    """Found by mutation review: the existing escape tests named files that did not exist, so removing the escape checks changed nothing."""
    dest = tmp_path / "dest"
    (dest / "usr").mkdir(parents=True)
    (dest / "x").write_bytes(b"x")
    (tmp_path / "outside").write_bytes(b"o")
    assert foreign.not_unpacked("-rw-r--r--  1 0      0           1 May 29  2024 ./usr/../x", dest) == ["/usr/../x"]
    assert foreign.not_unpacked("-rw-r--r--  1 0      0           1 May 29  2024 ../outside", dest) == ["/../outside"]
    assert foreign.not_unpacked("-rw-r--r--  1 0      0           1 May 29  2024 ./x/..", dest) == ["/x/.."]


def test_a_cpio_hard_link_counts_only_when_both_names_are_the_same_file_and_inside_the_folder(tmp_path):
    dest = tmp_path / "dest"
    (dest / "usr").mkdir(parents=True)
    (dest / "usr/a").write_bytes(b"a")
    (dest / "usr/b").write_bytes(b"b")  # exists, but is another file than the one the listing says it is linked to
    os.link(dest / "usr/a", dest / "usr/c")
    (tmp_path / "scratch").write_bytes(b"s")
    listing = "\n".join([
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/c link to ./usr/a",
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/b link to ./usr/a",
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/a link to ../../scratch",
        "-rw-r--r--  2 0      0           1 May 29  2024 ./usr/a link to ./usr/missing"])
    assert foreign.not_unpacked(listing, dest) == ["/usr/b", "/usr/a", "/usr/a"]  # only the first line is a real hard link


def test_a_package_list_hard_link_that_escapes_is_stopped_by_the_unpacker_and_the_guard_agrees(tmp_path):
    (tmp_path / "usr").mkdir()
    (tmp_path / "usr/a").write_bytes(b"a")
    assert foreign.not_unpacked("-rw-r--r--  2 0      0           1 May 29  2024 ./usr/a link to ../../../etc/passwd", tmp_path) == ["/usr/a"]
