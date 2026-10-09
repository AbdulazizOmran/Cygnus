"""Conversion of real-world shapes found by surveying vendor packages: install scripts that only tidy up when uninstalling, empty
/var folders, and the menu icon and command link a vendor's install script would have made."""

import io
import subprocess
import tarfile

import pytest

import builders
from cygnus.core.backends import foreign
from cygnus.core.detect import detect_file
from cygnus.core.ops import convert
from test_translate import png  # a real PNG of a given size

pytestmark = pytest.mark.needs_tool("makepkg", "fakeroot", "bsdtar", "zstd")


def _deb(tmp_path, entries, scripts=None, name="acme"):
    """A deb whose data holds `entries`: (path, "file", bytes) | (path, "dir", None) | (path, "symlink", target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, kind, value in entries:
            info = tarfile.TarInfo("./" + path)
            if kind == "dir":
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type, info.linkname = tarfile.SYMTYPE, value
                tar.addfile(info)
            else:
                info.size, info.mode = len(value), 0o644 if kind == "plain" else 0o755
                tar.addfile(info, io.BytesIO(value))
    control = {"./control": (f"Package: {name}\nVersion: 1.0-1\nArchitecture: amd64\nMaintainer: T <t@x.invalid>\n"
                             "Installed-Size: 1\nDepends: \nDescription: test\n").encode()}
    for sname, body in (scripts or {}).items():
        control[f"./{sname}"] = body.encode()
    path = tmp_path / f"{name}_1.0-1_amd64.deb"
    path.write_bytes(builders._ar([("debian-binary", b"2.0\n"), ("control.tar.xz", builders._tar_bytes(control, "w:xz")),
                                   ("data.tar.gz", buf.getvalue())]))
    return detect_file(str(path))


def _verdict(cand, **kw):
    return foreign.analyse(cand, satisfy=lambda deps: {}, sonames_on_host=set(), glibc_on_host=(2, 40), **kw)


def _listing(pkg):
    return subprocess.run(["bsdtar", "-tvf", str(pkg)], capture_output=True, text=True, check=True).stdout


def _entries(tmp_path):
    logo = tmp_path / "logo.png"
    png(logo, 32)
    return [("opt/acme/acme", "file", b"#!/bin/sh\n"), ("opt/acme/logo_32.png", "file", logo.read_bytes()),
            ("usr/bin/acme-stable", "file", b"#!/bin/sh\n"), ("usr/share/doc/acme/a", "file", b"a\n"),
            ("usr/share/applications/acme.desktop", "file", b"[Desktop Entry]\nName=Acme\nExec=/usr/bin/acme-stable\nIcon=acme\n")]


POSTINST = """#!/bin/sh
set -e
for size in 32; do
  xdg-icon-resource install --size "${size}" "/opt/acme/logo_${size}.png" "acme"
done
update-alternatives --install /usr/bin/acme acme /usr/bin/acme-stable 50
x="$(echo done)"
"""


def test_the_vendors_icon_and_command_are_added_and_listed_before_anything_is_confirmed(tmp_path):
    cand = _deb(tmp_path, _entries(tmp_path), {"postinst": POSTINST})
    verdict = _verdict(cand)
    assert {(a["kind"], a["path"]) for a in verdict.additions} == {
        ("link", "usr/bin/acme"), ("icon", "usr/share/icons/hicolor/32x32/apps/acme.png")}
    pkg, notes = convert.convert(cand, verdict, tmp_path / "out", accept_unread_scripts=True)
    listing = _listing(pkg)
    assert "usr/bin/acme -> /usr/bin/acme-stable" in listing and "usr/share/icons/hicolor/32x32/apps/acme.png" in listing
    assert any(n.startswith("added the command acme") for n in notes) and any("menu icon acme" in n for n in notes)


def test_a_path_that_another_installed_package_owns_is_left_alone(tmp_path):
    cand = _deb(tmp_path, _entries(tmp_path), {"postinst": POSTINST})
    owner = lambda path: "other" if path == "/usr/bin/acme" else None  # noqa: E731
    verdict = _verdict(cand, owner=owner)
    assert [a["kind"] for a in verdict.additions] == ["icon"]
    pkg, _ = convert.convert(cand, verdict, tmp_path / "out", accept_unread_scripts=True, owner=owner)
    assert "usr/bin/acme ->" not in _listing(pkg)


def test_a_package_without_install_scripts_gets_nothing_added(tmp_path):
    cand = _deb(tmp_path, _entries(tmp_path))
    assert _verdict(cand).additions == []


# -- uninstall-time cleanup --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("script", ["rm -f /usr/bin/acme\nrm $PROFILE\nrm -rf /opt/acme/cache\nrmdir /opt/acme\n"])
@pytest.mark.parametrize("name", ["prerm", "postrm"])
def test_cleanup_in_an_uninstall_script_never_blocks_a_conversion(tmp_path, name, script):
    cand = _deb(tmp_path, _entries(tmp_path), {name: "#!/bin/sh\n" + script})
    verdict = _verdict(cand)
    assert verdict.strategy == "convert" and verdict.scripts.blocking == [] and verdict.scripts.unknown == []
    assert "cleanup" in verdict.scripts.categories


def test_the_same_removals_in_an_install_script_still_block(tmp_path):
    cand = _deb(tmp_path, _entries(tmp_path), {"postinst": "#!/bin/sh\nrm -rf /opt/acme/cache\n"})
    assert _verdict(cand).strategy == "review"  # an install script that deletes things is not just tidying up


def test_other_commands_in_an_uninstall_script_are_judged_as_before(tmp_path):
    cand = _deb(tmp_path, _entries(tmp_path), {"postrm": "#!/bin/sh\nuserdel acme\nrm -f /usr/bin/acme\n"})
    assert _verdict(cand).strategy == "review"  # userdel is not a removal of a file; it stays unknown


# -- /var --------------------------------------------------------------------------------------------------------------
def test_a_var_that_holds_only_empty_folders_is_left_out_instead_of_refusing_the_package(tmp_path):
    entries = _entries(tmp_path) + [("var", "dir", None), ("var/log", "dir", None), ("var/log/acme", "dir", None)]
    cand = _deb(tmp_path, entries)
    verdict = _verdict(cand)
    assert verdict.strategy == "convert" and verdict.payload.unsupported_roots == []
    pkg, notes = convert.convert(cand, verdict, tmp_path / "out")
    assert "/var left out (it held only empty folders; the program creates them when it runs)" in notes
    assert "var/" not in _listing(pkg)


@pytest.mark.parametrize("extra", [("var/lib/acme/data.db", "file", b"x"), ("var/lib/acme/link", "symlink", "/etc/passwd")])
def test_a_var_with_anything_in_it_is_still_refused(tmp_path, extra):
    cand = _deb(tmp_path, _entries(tmp_path) + [("var/lib/acme", "dir", None), extra])
    verdict = _verdict(cand)
    assert verdict.strategy == "refuse" and verdict.payload.unsupported_roots == ["var"]


# -- empty folders and plain permissions the vendor's script sets ------------------------------------------------------
def test_a_folder_the_script_makes_and_a_permission_it_sets_are_reproduced_and_listed(tmp_path):
    entries = [*_entries(tmp_path), ("opt/acme/tool", "plain", b"#!/bin/sh\n")]
    scripts = {"postinst": "#!/bin/sh\nmkdir -p /opt/acme/logs\nchmod 755 /opt/acme/tool\nchmod +x /opt/acme/acme\n"
                           "x=$(echo hi)\n"}
    cand = _deb(tmp_path, entries, scripts)
    verdict = _verdict(cand)
    assert {(a["kind"], a["path"]) for a in verdict.additions} >= {("dir", "opt/acme/logs"), ("mode", "opt/acme/tool")}
    pkg, notes = convert.convert(cand, verdict, tmp_path / "out", accept_unread_scripts=True)
    listing = {line.split()[-1].rstrip("/"): line.split()[0] for line in _listing(pkg).splitlines()}
    assert listing["opt/acme/logs"].startswith("d") and listing["opt/acme/tool"] == "-rwxr-xr-x"
    assert any("empty folder /opt/acme/logs" in n for n in notes) and any("permission 755 on /opt/acme/tool" in n for n in notes)


def test_nothing_that_depends_on_a_condition_or_sets_special_bits_is_reproduced(tmp_path):
    entries = [*_entries(tmp_path), ("opt/acme/tool", "plain", b"x"), ("opt/acme/sandbox", "plain", b"x")]
    scripts = {"postinst": """#!/bin/sh
if [ -d /opt/acme ]; then
  mkdir -p /opt/acme/conditional
fi
setup() {
  mkdir -p /opt/acme/in-a-function
}
test -d /opt/acme || mkdir -p /opt/acme/after-or
chmod 4755 /opt/acme/sandbox
chmod 2755 /opt/acme/tool
chmod u+s /opt/acme/tool
x=$(echo hi)
"""}
    verdict = _verdict(_deb(tmp_path, entries, scripts))
    assert [a for a in verdict.additions if a["kind"] in ("dir", "mode")] == []
    shown = " ".join(i for g in verdict.script_effects for i in g["items"])
    assert "/opt/acme/conditional" in shown and "only if a condition holds" in shown  # still told to the person


def test_a_script_that_makes_something_writable_by_everyone_is_never_copied(tmp_path):
    entries = [*_entries(tmp_path), ("opt/acme/tool", "plain", b"#!/bin/sh\n"), ("etc/acme/acme.conf", "plain", b"x=1\n")]
    scripts = {"postinst": "#!/bin/sh\nchmod 0777 /opt/acme/tool\nchmod a+w /etc/acme/acme.conf\nchmod g+w /opt/acme/acme\n"
                           "x=$(echo hi)\n"}
    cand = _deb(tmp_path, entries, scripts)
    verdict = _verdict(cand)
    assert [a for a in verdict.additions if a["kind"] == "mode"] == []  # planned for nobody...
    pkg, _ = convert.convert(cand, verdict, tmp_path / "out", accept_unread_scripts=True)
    modes = {line.split()[-1].rstrip("/"): line.split()[0] for line in _listing(pkg).splitlines()}
    assert modes["opt/acme/tool"] == "-rw-r--r--" and modes["etc/acme/acme.conf"] == "-rw-r--r--"  # ...and not made so


def test_whatever_the_users_umask_nothing_in_the_package_is_writable_by_others_or_setgid(tmp_path):
    import os

    root = tmp_path / "root"
    (root / "opt/acme/setgid").mkdir(parents=True)
    for name, mode in (("open.txt", 0o666), ("ok", 0o755), ("setuid", 0o4755)):
        (root / "opt/acme" / name).write_bytes(b"x\n")
        os.chmod(root / "opt/acme" / name, mode)  # as an extraction with no umask would have left them
    os.chmod(root / "opt/acme/setgid", 0o2775)
    convert._normalise(root)
    modes = {p.name: p.stat().st_mode & 0o7777 for p in (root / "opt/acme").iterdir()}
    assert modes == {"open.txt": 0o644, "ok": 0o755, "setuid": 0o755, "setgid": 0o775}
