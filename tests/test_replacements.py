"""Repository replacements are computed exactly like libalpm does (literal names, repository order)."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import builders
from cygnus.core.backends import alpm_worker
from cygnus.core.backends.alpm_worker import UnresolvableDependency, compute_replacements, replacer_dependencies


def _vercmp(a, b):
    import pyalpm

    return pyalpm.vercmp(a, b)


class DB:
    def __init__(self, name, pkgs):
        self.name, self.pkgcache = name, pkgs

    def get_pkg(self, name):
        return next((p for p in self.pkgcache if p.name == name), None)


def P(name, version="1-1", replaces=(), provides=(), depends=()):
    return SimpleNamespace(name=name, version=version, replaces=list(replaces), provides=list(provides),
                           depends=list(depends))


def test_provides_never_count_as_being_replaced():
    # This machine (2026-10-07): nvidia-utils provides nvidia-libgl; nvidia-580xx-utils replaces nvidia-libgl.
    local = [P("nvidia-utils", "615.71.09-1", provides=["nvidia-libgl"]), P("eza", "0.23.5-2", provides=["exa"]),
             P("hdf5", "2.2.0-1", provides=["hdf5-java"])]
    cachyos = DB("cachyos", [P("nvidia-580xx-utils", "580.178.04-1", replaces=["nvidia-libgl"]),
                             P("eza-git", replaces=["exa"])])
    extra = DB("extra", [P("hdf5-openmpi", replaces=["hdf5-java"]), P("nvidia-utils", "615.71.09-1"),
                         P("eza", "0.23.5-2"), P("hdf5", "2.2.0-1")])
    assert compute_replacements(local, [cachyos, extra], _vercmp) == []


def test_a_literal_replacement_is_found_with_versions():
    local = [P("oldtool", "1.0-1"), P("newer", "3.0-1")]
    db = DB("extra", [P("tool", replaces=["oldtool<2"]), P("x", replaces=["newer<2"])])
    assert compute_replacements(local, [db], _vercmp) == [{"install": "tool", "remove": "oldtool", "repo": "extra"}]


def test_the_first_repository_carrying_the_package_decides():
    local = [P("foo")]
    first = DB("core", [P("foo", "2-1")])  # still carries foo itself
    second = DB("extra", [P("foo-ng", replaces=["foo"])])
    assert compute_replacements(local, [first, second], _vercmp) == []
    assert compute_replacements(local, [second, first], _vercmp) == [
        {"install": "foo-ng", "remove": "foo", "repo": "extra"}]


# -- what a replacement pulls in (helper finding 13) ---------------------------------------------------------------
def _satisfier(pkgs, dep):  # names and provides only; versions are libalpm's business
    name = dep.split(">")[0].split("<")[0].split("=")[0]
    return next((p for p in pkgs if p.name == name or name in p.provides), None)


def _repo(*pkgs):
    return lambda dep: _satisfier(list(pkgs), dep)


def test_a_replacers_new_dependencies_are_listed_in_the_order_pacman_resolves_them():
    new = P("newtool", replaces=["oldtool"], depends=["libnew", "glibc"])
    libnew = P("libnew", depends=["libnewer"])
    libnewer = P("libnewer")
    pulled = replacer_dependencies([P("glibc"), P("oldtool")], [], {"oldtool"}, [new],
                                   _repo(libnew, libnewer), _satisfier)
    assert [(p.name, by) for p, by in pulled] == [("libnew", "newtool"), ("libnewer", "libnew")]


def test_dependencies_already_installed_or_already_in_the_transaction_are_not_listed_again():
    new = P("newtool", depends=["glibc", "libup", "libnew"])
    libnew = P("libnew")
    assert replacer_dependencies([P("glibc")], [P("libup")], set(), [new], _repo(libnew), _satisfier) == [(libnew, "newtool")]


def test_a_dependency_on_the_package_being_replaced_is_met_by_the_replacer_providing_it():
    new = P("newtool", replaces=["oldtool"], provides=["oldtool"], depends=["oldtool"])
    assert replacer_dependencies([P("oldtool")], [], {"oldtool"}, [new], _repo(), _satisfier) == []
    plain = P("newtool", replaces=["oldtool"], depends=["oldtool"])  # no provides: oldtool will be gone
    with pytest.raises(UnresolvableDependency, match="newtool needs oldtool"):
        replacer_dependencies([P("oldtool")], [], {"oldtool"}, [plain], _repo(), _satisfier)


def test_a_dependency_nobody_provides_is_reported_not_skipped():
    with pytest.raises(UnresolvableDependency) as caught:
        replacer_dependencies([], [], set(), [P("newtool", depends=["ghost>=2"])], _repo(), _satisfier)
    assert caught.value.dependency == "ghost>=2"


def test_two_replacers_needing_the_same_library_list_it_once():
    lib = P("libshared")
    pulled = replacer_dependencies([], [], set(), [P("a", depends=["libshared"]), P("b", depends=["libshared"])],
                                   _repo(lib), _satisfier)
    assert [p.name for p, _ in pulled] == ["libshared"]


# The same thing with the real libalpm, in a scratch root (nothing of this machine is read or written).
@pytest.mark.needs_tool("bsdtar", "zstd", "repo-add")
def test_a_full_upgrade_through_real_libalpm_lists_what_a_replacement_brings_with_it(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    builders.build_pkg(repo, name="newtool", version="2.0-1", arch="x86_64", depends=("libnew",),
                       extra_pkginfo="replaces = oldtool\n")
    builders.build_pkg(repo, name="libnew", version="1.0-1", arch="x86_64", depends=())
    dbpath = tmp_path / "db"
    (dbpath / "sync").mkdir(parents=True)
    (dbpath / "local" / "oldtool-1.0-1").mkdir(parents=True)
    (dbpath / "local" / "ALPM_DB_VERSION").write_text("9\n")
    (dbpath / "local" / "oldtool-1.0-1" / "desc").write_text(
        "%NAME%\noldtool\n\n%VERSION%\n1.0-1\n\n%ARCH%\nx86_64\n\n%REASON%\n0\n")
    (dbpath / "local" / "oldtool-1.0-1" / "files").write_text("%FILES%\n")
    packages = sorted(str(p) for p in repo.glob("*.pkg.tar.zst"))
    subprocess.run(["repo-add", "-q", str(dbpath / "sync" / "testrepo.db.tar.gz"), *packages], check=True,
                   capture_output=True)
    request = {"op": "resolve", "sysupgrade": True, "targets": [], "dbpath": str(dbpath), "arch": "x86_64",
               "repos": ["testrepo"], "root": str(tmp_path / "root")}
    (tmp_path / "root").mkdir()
    out = subprocess.run([sys.executable, "-I", alpm_worker.__file__], input=json.dumps(request), capture_output=True,
                         text=True, timeout=60)
    answer = json.loads(out.stdout)
    assert answer["ok"] is True, answer
    added = {p["name"]: p for p in answer["to_add"]}
    assert added["newtool"]["replaces_installed"] == "oldtool"
    assert added["libnew"]["pulled_in_by"] == "newtool"  # libalpm alone would never have listed this
    assert [r["name"] for r in answer["to_remove"]] == ["oldtool"]
