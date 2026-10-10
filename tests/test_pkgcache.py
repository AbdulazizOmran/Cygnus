"""Going back to an older version from pacman's cache: only real package files of exactly that package, ordered by libalpm's rules, found
again at the moment of use, planned through the helper's ordinary local-package plan, and never recorded as an application."""

import argparse
import builtins
import hashlib
import os

import pyalpm
import pytest

from cygnus.cli import main as cli
from cygnus.core import packages, pkgcache
from cygnus.core.backends import alpm_worker as w
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import Plan
from cygnus.gui import fixes
from tests.test_qml_text import QML, _function


def config(cache, *extra):
    return pm.PacmanConfig(arch=("x86_64", "x86_64_v3"), dbpath="/nonexistent", cachedirs=(str(cache) + "/", *extra), gpgdir="", repos=(),
                           servers={}, hold=("held",), local_file_sig_level=("PackageOptional", "PackageTrustedOnly"))  # package files may be unsigned


class Vercmp:
    """The worker's vercmp, in process (libalpm's own function); counts how often it is asked."""

    def __init__(self):
        self.asked, self.ops = [], []

    def __call__(self, cfg, request):
        self.ops.append(request["op"])
        if request["op"] == "sort_versions":
            from functools import cmp_to_key

            return {"ok": True, "orders": [sorted(g, key=cmp_to_key(pyalpm.vercmp), reverse=True) for g in request["groups"]]}
        assert request["op"] == "vercmp"
        self.asked.append(request["pairs"])
        return {"ok": True, "results": [pyalpm.vercmp(a, b) for a, b in request["pairs"]]}


@pytest.fixture(autouse=True)
def as_root(monkeypatch):
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid())  # the scratch folders are the test's own: it stands in for root


@pytest.fixture
def cache(tmp_path):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for name in ("foo-1.0-1-x86_64.pkg.tar.zst", "foo-1.1-1-x86_64.pkg.tar.zst", "foo-1.2-1-x86_64.pkg.tar.zst", "foo-0.9-1-any.pkg.tar.xz",
                 "foo-2.0-1-x86_64.pkg.tar.zst", "foo-1:0.1-1-x86_64.pkg.tar.zst", "foo-bar-1.0-1-x86_64.pkg.tar.zst",
                 "foo-1.1-1-armv7h.pkg.tar.zst", "foo-1.0-1-x86_64.pkg.tar.zst.part", "foo-1.1-1-x86_64.pkg.tar.zst.sig",
                 "foo-1.0-1-any.pkg.tar.zst", "bar-1.0-1-x86_64.pkg.tar.zst", "notes.txt"):
        (folder / name).write_bytes(name.encode())
    (folder / "foo-0.4-1-x86_64.pkg.tar.zst").mkdir()  # a folder with a package's name
    (folder / "download-abc123").mkdir()
    target = tmp_path / "elsewhere.pkg.tar.zst"
    target.write_bytes(b"x")
    (folder / "foo-0.5-1-x86_64.pkg.tar.zst").symlink_to(target)  # a link is never a cached file
    return folder


# -- names -------------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    ("foo-1.0-1-x86_64.pkg.tar.zst", ("foo", "1.0-1", "x86_64")),
    ("ttf-fira-sans-1:4.301-3-any.pkg.tar.zst", ("ttf-fira-sans", "1:4.301-3", "any")),
    ("lib32-glibc-2.40+r5-1-x86_64_v3.pkg.tar.xz", ("lib32-glibc", "2.40+r5-1", "x86_64_v3")),
    ("python-3.14-1.1-x86_64.pkg.tar", ("python", "3.14-1.1", "x86_64")),
    ("foo-1.0-1-x86_64.pkg.tar.zst.sig", None), ("foo-1.0-1-x86_64.pkg.tar.zst.part", None), ("foo-1.0-x86_64.pkg.tar.zst", None),
    ("Foo-1.0-1-x86_64.pkg.tar.zst", None), ("-s-1.0-1-x86_64.pkg.tar.zst", None), ("foo-1.0-1-x86_64.tar.zst", None), ("", None),
    ("foo-1.0 -1-x86_64.pkg.tar.zst", None), ("foo-1.0-1-x86 64.pkg.tar.zst", None), ("a" * 300 + "-1-1-any.pkg.tar.zst", None),
])
def test_a_cache_file_name_is_read_into_name_version_and_architecture(filename, expected):
    assert pkgcache.parse_filename(filename) == expected


def test_only_regular_files_of_exactly_that_package_and_an_accepted_architecture_are_cached_versions(cache):
    found = sorted((f.version, f.arch) for f in pkgcache.cached_files("foo", config(cache)))
    assert found == [("0.9-1", "any"), ("1.0-1", "any"), ("1.0-1", "x86_64"), ("1.1-1", "x86_64"), ("1.2-1", "x86_64"), ("1:0.1-1", "x86_64"),
                     ("2.0-1", "x86_64")]  # not foo-bar, not armv7h, not .part/.sig, not the folder, not the link
    assert all(os.path.isfile(f.path) and not os.path.islink(f.path) for f in pkgcache.cached_files("foo", config(cache)))


def test_a_folder_that_is_not_roots_alone_is_not_used_for_a_downgrade_either(cache, monkeypatch):
    cfg = config(cache)
    assert pkgcache.cached_files("foo", cfg)  # as root's own folder it is used
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid() + 4242)  # someone else's folder
    assert pkgcache.cached_files("foo", cfg) == []
    monkeypatch.setattr(pkgcache, "ROOT_UID", os.getuid())
    os.chmod(cache, 0o775)
    assert pkgcache.cached_files("foo", cfg) == []  # writable by the group
    os.chmod(cache, 0o757)
    assert pkgcache.cached_files("foo", cfg) == []  # writable by everyone


def test_the_scan_of_one_folder_stops_at_the_cap(cache, monkeypatch):
    monkeypatch.setattr(pkgcache, "MAX_CACHE_ENTRIES", 3)
    assert len(os.listdir(cache)) > 10 and len(pkgcache.cached_files("foo", config(cache))) <= 4


def test_a_cache_folder_that_is_missing_or_relative_is_skipped(cache):
    assert pkgcache.cached_files("foo", config("/nonexistent-cache")) == []
    assert [f.version for f in pkgcache.cached_files("foo", pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=("pkg/",), gpgdir="",
                                                                          repos=(), servers={}, hold=()))] == []


# -- older versions ---------------------------------------------------------------------------------------------------------------------------

def test_older_versions_are_the_cached_ones_below_the_installed_version_newest_first(cache):
    vercmp = Vercmp()
    versions = [f.version for f in pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=vercmp)]
    assert versions == ["1.1-1", "1.0-1", "0.9-1"]  # not 1.2-1 itself, not 2.0-1, and not the epoch version 1:0.1-1 (an epoch outranks everything)
    assert len(vercmp.asked) == 1  # one question to libalpm for all of it


def test_an_epoch_decides_before_the_version(cache):
    versions = [f.version for f in pkgcache.older_versions("foo", "1:5.0-1", config=config(cache), worker=Vercmp())]
    assert versions[0] == "1:0.1-1" and "2.0-1" in versions and versions.index("1:0.1-1") < versions.index("2.0-1")


def test_the_same_version_cached_for_two_architectures_is_listed_once_with_the_preferred_one(cache):
    found = {f.version: f.arch for f in pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=Vercmp())}
    assert found["1.0-1"] == "x86_64"  # over "any", which pacman's configured list does not name


def test_nothing_is_offered_for_a_package_that_is_not_installed_or_not_in_the_cache(cache):
    assert pkgcache.older_versions("foo", None, config=config(cache), worker=Vercmp()) == []
    assert pkgcache.older_versions("foo", "", config=config(cache), worker=Vercmp()) == []
    assert pkgcache.older_versions("nothing", "1.0-1", config=config(cache), worker=Vercmp()) == []
    assert pkgcache.older_versions("-s", "1.0-1", config=config(cache), worker=Vercmp()) == []
    assert pkgcache.older_versions("../foo", "1.0-1", config=config(cache), worker=Vercmp()) == []


def test_a_long_history_is_cut_and_still_compared_in_one_question(tmp_path):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for i in range(200):
        (folder / f"big-1.{i}-1-x86_64.pkg.tar.zst").write_bytes(b"x")
        os.utime(folder / f"big-1.{i}-1-x86_64.pkg.tar.zst", (1000 + i, 1000 + i))
    vercmp = Vercmp()
    found = pkgcache.older_versions("big", "9.0-1", config=config(folder), worker=vercmp)
    assert len(found) == pkgcache.MAX_LISTED and len(vercmp.asked) == 1 and len(vercmp.asked[0]) <= w.MAX_PAIRS
    assert [f.version for f in found][:2] == ["1.199-1", "1.198-1"]  # the most recently cached were the ones compared; newest first


def test_a_worker_that_failed_is_said_in_words(cache):
    def died(cfg, request):
        raise pm.WorkerCrash("libalpm worker died")

    with pytest.raises(pkgcache.DowngradeError, match="could not be compared"):
        pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=died)
    with pytest.raises(pkgcache.DowngradeError, match="could not be compared"):
        pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=lambda c, r: {"ok": False})


def test_the_workers_comparison_is_libalpms():
    pairs = [["1.0-1", "1.0-2"], ["1:0.1-1", "9.9-1"], ["1.0-1", "1.0-1"], ["1.0rc1-1", "1.0-1"]]
    assert w.op_vercmp(None, {"pairs": pairs}) == {"ok": True, "results": [pyalpm.vercmp(a, b) for a, b in pairs]}
    for bad in ({}, {"pairs": "x"}, {"pairs": [["a"]]}, {"pairs": [["a", 1]]}, {"pairs": [["a", "b"]] * (w.MAX_PAIRS + 1)}):
        assert w.op_vercmp(None, bad)["ok"] is False


def test_find_reaches_every_older_version_not_only_the_listed_ones(tmp_path):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for i in range(1, 20):
        (folder / f"foo-{i}.0-1-x86_64.pkg.tar.zst").write_bytes(b"x")
    cfg = config(folder)
    listed = [f.version for f in pkgcache.older_versions("foo", "20.0-1", config=cfg, worker=Vercmp())]
    assert len(listed) == pkgcache.MAX_LISTED and listed[0] == "19.0-1" and "1.0-1" not in listed
    assert pkgcache.find("foo", "1.0-1", "20.0-1", config=cfg, worker=Vercmp()).version == "1.0-1"  # beyond the listed ones, still reachable
    assert len(pkgcache.older_versions("foo", "20.0-1", config=cfg, worker=Vercmp(), limit=None)) == 19


def test_the_oldest_of_a_very_long_history_is_still_reachable(tmp_path):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for i in range(1, 151):
        path = folder / f"foo-0.{i}-1-x86_64.pkg.tar.zst"
        path.write_bytes(b"x")
        os.utime(path, (10_000 - i, 10_000 - i))  # the oldest version was cached first: it has the oldest file date
    cfg = config(folder)
    vercmp = Vercmp()
    found = pkgcache.find("foo", "0.1-1", "9.0-1", config=cfg, worker=vercmp)
    assert found.version == "0.1-1"
    everything = pkgcache.older_versions("foo", "9.0-1", config=cfg, worker=Vercmp(), limit=None)
    assert len(everything) == 150 and everything[0].version == "0.150-1" and everything[-1].version == "0.1-1"
    assert vercmp.ops == ["vercmp", "sort_versions"]  # the versions against the installed one, then their order: two questions


def test_the_comparisons_are_asked_in_chunks_the_worker_accepts(tmp_path, monkeypatch):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for i in range(10):
        (folder / f"foo-1.{i}-1-x86_64.pkg.tar.zst").write_bytes(b"x")
    monkeypatch.setattr(pkgcache, "COMPARE_CHUNK", 3)
    vercmp = Vercmp()
    found = pkgcache.older_versions("foo", "2.0-1", config=config(folder), worker=vercmp, limit=None)
    assert len(found) == 10 and [len(q) for q in vercmp.asked] == [3, 3, 3, 1] and vercmp.ops[-1] == "sort_versions"
    assert all(len(q) <= 5000 for q in vercmp.asked)


def test_only_the_most_recently_cached_versions_are_considered_beyond_the_limit(tmp_path, monkeypatch):
    folder = tmp_path / "pkg"
    folder.mkdir()
    os.chmod(folder, 0o755)
    for i in range(8):
        path = folder / f"foo-1.{i}-1-x86_64.pkg.tar.zst"
        path.write_bytes(b"x")
        os.utime(path, (1000 + i, 1000 + i))
    monkeypatch.setattr(pkgcache, "MAX_VERSIONS", 3)
    versions = [f.version for f in pkgcache.older_versions("foo", "2.0-1", config=config(folder), worker=Vercmp(), limit=None)]
    assert versions == ["1.7-1", "1.6-1", "1.5-1"]


def test_an_order_the_worker_gets_wrong_or_cannot_give_is_said_not_trusted(cache):
    def odd(cfg, request):
        if request["op"] == "sort_versions":
            return {"ok": True, "orders": [["9.9-9"]]}  # not about the versions asked for
        return {"ok": True, "results": [-1 for _ in request["pairs"]]}

    with pytest.raises(pkgcache.DowngradeError, match="could not be ordered"):
        pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=odd)

    def died(cfg, request):
        if request["op"] == "sort_versions":
            raise pm.WorkerCrash("died")
        return {"ok": True, "results": [-1 for _ in request["pairs"]]}

    with pytest.raises(pkgcache.DowngradeError, match="could not be ordered"):
        pkgcache.older_versions("foo", "1.2-1", config=config(cache), worker=died)


def test_find_returns_the_file_again_and_only_for_an_older_version(cache):
    cfg = config(cache)
    found = pkgcache.find("foo", "1.1-1", "1.2-1", config=cfg, worker=Vercmp())
    assert found.version == "1.1-1" and found.path == str(cache / "foo-1.1-1-x86_64.pkg.tar.zst")
    for version in ("1.2-1", "2.0-1", "9.9-9", "1.1-1; rm -rf /", "../x", "", None, 5):
        with pytest.raises(pkgcache.DowngradeError):
            pkgcache.find("foo", version, "1.2-1", config=cfg, worker=Vercmp())
    with pytest.raises(pkgcache.DowngradeError):
        pkgcache.find("foo", "1.1-1", None, config=cfg, worker=Vercmp())


# -- planning through the helper --------------------------------------------------------------------------------------------------------------

def details_for(installed="1.2-1", protected=False, is_installed=True):
    def details(name, repo=None, *, config=None, worker=None):
        return {"name": name, "installed": is_installed, "installed_version": installed if is_installed else None, "protected": protected}
    return details


class Client:
    def __init__(self, summary=None):
        self.calls = []
        self.summary = summary

    def plan_packages(self, **kw):
        self.calls.append(kw)
        if self.summary is not None:
            return Plan("p1", self.summary, "Install foo from a local file")
        (path, sha), = kw["local_files"]
        name, version, _arch = pkgcache.parse_filename(os.path.basename(path))  # what libalpm would read from the file
        return Plan("p1", {"local": [{"name": name, "version": version, "sha256": sha}], "install": [], "upgrade": [], "remove": []}, "msg")


@pytest.fixture
def planning(monkeypatch, cache):
    monkeypatch.setattr(packages, "details", details_for())
    monkeypatch.setattr(pm, "run_worker", Vercmp())
    monkeypatch.setattr(pm, "read_config", lambda: config(cache))
    yield
    fixes._PENDING.clear()


def test_the_plan_is_for_the_file_found_in_the_cache_with_its_own_hash(planning, cache):
    client = Client()
    plan, installed = pkgcache.plan_downgrade(client, "foo", "1.1-1")
    (path, sha), = client.calls[0]["local_files"]
    assert path == str(cache / "foo-1.1-1-x86_64.pkg.tar.zst") and sha == hashlib.sha256(b"foo-1.1-1-x86_64.pkg.tar.zst").hexdigest()
    assert installed == "1.2-1" and plan.plan_id == "p1"
    assert set(client.calls[0]) == {"local_files", "claim_ownership"} and client.calls[0]["claim_ownership"] is False  # nothing else is asked for


@pytest.mark.parametrize("sig_level, local_level, required", [
    ((), (), True),  # nothing set: pacman's default is Required
    (("PackageRequired", "PackageTrustedOnly", "DatabaseOptional"), (), True),  # Arch's default
    (("PackageRequired", "PackageTrustedOnly"), ("PackageOptional", "PackageTrustedOnly"), False),  # CachyOS: package files may be unsigned
    (("PackageOptional",), (), False), (("PackageNever",), (), False), (("Optional", "TrustedOnly"), (), False),
    (("Required", "DatabaseOptional"), (), True), (("DatabaseOptional",), (), True),  # a database level says nothing about packages
    (("PackageOptional",), ("PackageRequired",), True),  # LocalFileSigLevel wins for package files
    (("PackageRequired",), ("Optional",), False),
])
def test_it_is_known_whether_pacman_here_insists_on_a_signature_for_a_package_file(sig_level, local_level, required):
    cfg = pm.PacmanConfig(arch=("x86_64",), dbpath="/x", cachedirs=(), gpgdir="", repos=(), servers={}, hold=(), sig_level=sig_level,
                          local_file_sig_level=local_level)
    assert pm.package_signature_required(cfg) is required


RECORDED_DUMP = """[options]
RootDir = /
DBPath = /var/lib/pacman/
CacheDir = /var/cache/pacman/pkg/
HookDir = /etc/pacman.d/hooks/
GPGDir = /etc/pacman.d/gnupg/
LogFile = /var/log/pacman.log
HoldPkg = pacman
HoldPkg = glibc
Architecture = x86_64
Architecture = x86_64_v2
Architecture = x86_64_v3
CleanMethod = KeepInstalled
SigLevel = PackageRequired
SigLevel = PackageTrustedOnly
SigLevel = DatabaseOptional
SigLevel = DatabaseTrustedOnly
LocalFileSigLevel = PackageOptional
LocalFileSigLevel = PackageTrustedOnly
[cachyos-v3]
Usage = All
Server = https://mirror.example/cachyos-v3
[core]
Include = /etc/pacman.d/mirrorlist
"""


def test_the_levels_are_read_from_a_real_pacman_conf_dump():
    cfg = pm.parse_pacman_conf(RECORDED_DUMP)  # `pacman-conf` writes every flag on a line of its own, fully spelt out
    assert cfg.arch == ("x86_64", "x86_64_v2", "x86_64_v3") and cfg.hold == ("pacman", "glibc") and cfg.hookdirs == ("/etc/pacman.d/hooks/",)
    assert cfg.sig_level == ("PackageRequired", "PackageTrustedOnly", "DatabaseOptional", "DatabaseTrustedOnly")
    assert cfg.local_file_sig_level == ("PackageOptional", "PackageTrustedOnly") and pm.package_signature_required(cfg) is False
    arch_default = RECORDED_DUMP.replace("LocalFileSigLevel = PackageOptional\nLocalFileSigLevel = PackageTrustedOnly\n", "")
    assert pm.package_signature_required(pm.parse_pacman_conf(arch_default)) is True  # no LocalFileSigLevel: SigLevel decides


def test_the_signature_levels_are_read_from_pacmans_configuration():
    cfg = pm.parse_pacman_conf("[options]\nSigLevel = PackageRequired\nSigLevel = PackageTrustedOnly\nLocalFileSigLevel = PackageOptional\n"
                               "HookDir = /a/\nHookDir = /b/\n[core]\nServer = https://x\n")
    assert cfg.sig_level == ("PackageRequired", "PackageTrustedOnly") and cfg.local_file_sig_level == ("PackageOptional",)
    assert cfg.hookdirs == ("/a/", "/b/") and pm.parse_pacman_conf("[options]\n").hookdirs == ("/etc/pacman.d/hooks/",)


def test_a_downgrade_is_refused_in_words_where_pacman_would_refuse_an_unsigned_file(planning, monkeypatch, cache):
    strict = pm.PacmanConfig(arch=("x86_64", "x86_64_v3"), dbpath="/nonexistent", cachedirs=(str(cache) + "/",), gpgdir="", repos=(), servers={},
                             hold=(), sig_level=("PackageRequired", "PackageTrustedOnly"))
    client = Client()
    with pytest.raises(pkgcache.DowngradeError, match="requires a signature"):
        pkgcache.plan_downgrade(client, "foo", "1.1-1", config=strict)
    assert client.calls == []


def test_the_file_is_hashed_without_following_a_link(tmp_path):
    from cygnus.core.errors import CygnusError

    real = tmp_path / "real.pkg.tar.zst"
    real.write_bytes(b"package")
    link = tmp_path / "link.pkg.tar.zst"
    link.symlink_to(real)
    assert pkgcache._sha256(str(real)) == hashlib.sha256(b"package").hexdigest()
    with pytest.raises(CygnusError, match="link"):
        pkgcache._sha256(str(link))  # a file swapped for a link after it was found is refused, not read


def test_nothing_is_planned_for_what_is_not_installed_or_protected_or_not_cached(planning, monkeypatch):
    client = Client()
    monkeypatch.setattr(packages, "details", details_for(is_installed=False))
    with pytest.raises(pkgcache.DowngradeError, match="not installed"):
        pkgcache.plan_downgrade(client, "foo", "1.1-1")
    monkeypatch.setattr(packages, "details", details_for(protected=True))
    with pytest.raises(pkgcache.DowngradeError, match="protected"):
        pkgcache.plan_downgrade(client, "foo", "1.1-1")
    monkeypatch.setattr(packages, "details", details_for())
    for version in ("2.0-1", "7.7-7"):
        with pytest.raises(pkgcache.DowngradeError, match="not in your package cache"):
            pkgcache.plan_downgrade(client, "foo", version)
    assert client.calls == []


def test_a_file_whose_contents_are_not_its_name_is_not_planned(planning):
    for summary in ({"local": [{"name": "evil", "version": "1.1-1"}]}, {"local": [{"name": "foo", "version": "9.9-9"}]}, {"local": []},
                    {"local": [{"name": "foo", "version": "1.1-1"}, {"name": "x", "version": "1"}]}, {}):
        with pytest.raises(pkgcache.DowngradeError, match="nothing was planned"):
            pkgcache.plan_downgrade(Client(summary), "foo", "1.1-1")


def test_the_gui_plan_names_the_downgrade_lists_what_comes_with_it_and_records_no_application(planning, monkeypatch):
    monkeypatch.setattr(fixes, "HelperClient", lambda: Client())
    out = fixes.plan_downgrade("foo", "1.1-1")
    assert out["downgrade"] == {"name": "foo", "from": "1.2-1", "to": "1.1-1"} and out["action"] == "Install the older version"
    assert out["token"] in fixes._PENDING and out["title"] == "Go back to foo 1.1-1" and "UNSIGNED" in out["security_note"]
    assert out["token"] not in fixes._ON_SUCCESS  # a repository package stays a system package: no entry in the application list


def test_the_gui_plan_takes_a_name_and_a_version_never_a_path(planning, monkeypatch):
    monkeypatch.setattr(fixes, "HelperClient", lambda: pytest.fail("the helper must not be asked"))
    for name, version in (("foo", "/etc/passwd"), ("foo", "../../x"), ("../foo", "1.1-1"), ("foo", "1.1-1/../../x")):
        with pytest.raises((pkgcache.DowngradeError, packages.PackagesError)):
            fixes.plan_downgrade(name, version)


# -- the terminal --------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def terminal(planning, monkeypatch):
    monkeypatch.setattr(packages, "details", lambda name, repo=None, **kw: details_for()(name))
    commits = []
    client = Client()
    monkeypatch.setattr("cygnus.core.privilege.HelperClient", lambda: client)
    monkeypatch.setattr(cli, "_helper_commit", lambda c, plan, yes: commits.append((plan.plan_id, yes)) or True)
    return client, commits


def test_versions_lists_the_older_cached_ones(terminal, capsys):
    assert cli.main(["packages", "versions", "foo"]) == 0
    out = capsys.readouterr().out
    assert "foo 1.2-1 is installed" in out and out.index("1.1-1") < out.index("1.0-1") < out.index("0.9-1") and "downgrade foo --to VERSION" in out


def test_downgrade_goes_to_the_newest_older_version_unless_told_and_asks_first(terminal, capsys):
    client, commits = terminal
    assert cli.main(["packages", "downgrade", "foo"]) == 0
    assert client.calls[0]["local_files"][0][0].endswith("foo-1.1-1-x86_64.pkg.tar.zst") and commits == [("p1", False)]
    out = capsys.readouterr().out
    assert "1.2-1" in out and "1.1-1" in out and "(older)" in out and "does not check a package file's signature" in out
    client.calls.clear()
    assert cli.main(["packages", "downgrade", "foo", "--to", "1.0-1", "-y"]) == 0
    assert client.calls[0]["local_files"][0][0].endswith("foo-1.0-1-x86_64.pkg.tar.zst") and commits[-1] == ("p1", True)


def test_downgrade_refuses_in_words_when_nothing_is_there_to_go_back_to(terminal, monkeypatch, capsys):
    client, commits = terminal
    monkeypatch.setattr(packages, "details", lambda name, repo=None, **kw: details_for(is_installed=False)(name))
    assert cli.main(["packages", "downgrade", "foo"]) == 2 and "not installed" in capsys.readouterr().err
    monkeypatch.setattr(packages, "details", lambda name, repo=None, **kw: details_for(installed="0.1-1")(name))
    assert cli.main(["packages", "downgrade", "foo"]) == 2 and "no older version" in capsys.readouterr().err
    monkeypatch.setattr(packages, "details", lambda name, repo=None, **kw: details_for(protected=True)(name))
    assert cli.main(["packages", "downgrade", "foo"]) == 2
    assert client.calls == [] and commits == []


# -- the screen ---------------------------------------------------------------------------------------------------------------------------------

PAGE = (QML / "PackagePage.qml").read_text()


def test_older_versions_are_only_looked_for_an_installed_unprotected_package_and_a_late_answer_for_another_one_is_dropped():
    body = _function(PAGE, "    function loadOlder()")
    assert "!info.installed || info.protected" in body and body.index("return;") < body.index("backend.packageOlderVersions")
    assert "info.name === name" in body[body.index("backend.packageOlderVersions"):]


def test_going_back_asks_for_the_package_and_the_version_that_was_listed_and_shows_the_plan_first():
    body = _function(PAGE, "    function planDowngrade(version)")
    assert "backend.planDowngradePackage(info.name, version)" in body and "confirmDialog.open()" in body and "commitFix" not in body
    assert 'onClicked: page.planDowngrade(modelData.version)' in PAGE
