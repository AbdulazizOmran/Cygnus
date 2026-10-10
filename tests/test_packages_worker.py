"""The libalpm worker's browsing operations (search, the installed list, a package's details): plain-text search, one entry per name,
caps, honest flags, and text that is safe to show. The last tests run the real worker against a scratch root."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

import builders
from cygnus.core.backends import alpm_worker as w


def _vercmp(a, b):
    import pyalpm

    return pyalpm.vercmp(a, b)


def _satisfier(pkgs, dep):  # names and provides, ignoring versions (libalpm's business)
    name = w._provided_name(dep)
    return next((p for p in pkgs if p.name == name or any(w._provided_name(x) == name for x in p.provides)), None)


class DB:
    def __init__(self, name, pkgs):
        self.name, self.pkgcache = name, pkgs
        for p in pkgs:
            p.db = self

    def get_pkg(self, name):
        return next((p for p in self.pkgcache if p.name == name), None)


def P(name, version="1.0-1", desc="", provides=(), groups=(), depends=(), optdepends=(), reason=0, required_by=(), optional_for=(),
      files=(), **extra):
    return SimpleNamespace(name=name, version=version, desc=desc, provides=list(provides), groups=list(groups), depends=list(depends),
                           optdepends=list(optdepends), conflicts=[], replaces=[], licenses=["MIT"], url="https://example.invalid",
                           packager="Test", builddate=1700000000, isize=1000, size=500, arch="x86_64", db=None, reason=reason,
                           installdate=1700000500, files=[(f, 0, 0) for f in files], has_scriptlet=False, md5sum="",
                           compute_requiredby=lambda: list(required_by), compute_optionalfor=lambda: list(optional_for), **extra)


class Local:
    def __init__(self, *pkgs):
        self.pkgcache = list(pkgs)

    def get_pkg(self, name):
        return next((p for p in self.pkgcache if p.name == name), None)


# -- search ------------------------------------------------------------------------------------------------------------------

def test_a_better_match_ranks_first():
    rank = lambda **kw: w.search_rank(kw.get("name", "x"), kw.get("desc", ""), kw.get("provides", ()), kw.get("groups", ()), ["tool"])
    assert w.search_rank("tool", "", [], [], ["tool"]) == 0  # the exact name
    ranks = [rank(name="tool-extra"), rank(name="my-tool"), rank(provides=["tool=1"]), rank(desc="a handy tool"), rank(groups=["tools"])]
    assert ranks == [1, 2, 3, 4, 4]
    assert rank(name="other") is None


def test_every_search_word_must_be_found_somewhere():
    assert w.search_rank("alpha", "a fast shell", [], [], ["alpha", "shell"]) == 4  # one in the name, one in the description
    assert w.search_rank("alpha", "a fast shell", [], [], ["alpha", "editor"]) is None
    assert w.search_rank("alpha-shell", "", [], [], ["alpha", "shell"]) == 1


def test_a_search_is_plain_text_never_a_pattern():
    # libalpm's own db.search takes regular expressions: nothing typed in a search box may be one
    for term in ("(.*", "a.c", "[", "a|b", "^abc", "abc$", "\\"):
        assert w.search_rank("abc", "", [], [], [term]) is None, term
    assert w.search_rank("c++-tools", "", [], [], ["c++"]) == 1
    assert w.search_rank("a.c-lib", "", [], [], ["a.c"]) == 1


def test_search_ignores_case():
    assert w.search_rank("Fire-Fox", "Fast Browser", [], [], ["fire-fox"]) == 0
    assert w.search_rank("x", "Fast Browser", [], [], ["browser"]) == 4


def test_a_name_that_appears_in_two_repositories_is_listed_once_from_the_first():
    first = DB("one", [P("collide", "1.0-1", "tool")])
    second = DB("two", [P("collide", "9.0-1", "tool"), P("other-tool", "1-1")])
    found = w.search_packages([first, second], Local(), ["tool"], _vercmp)
    # other-tool has the word in its name (rank 2); collide only in its description (rank 4), and only the first repository's collide counts
    assert [(r["name"], r["version"], r["repo"]) for r in found["results"]] == [("other-tool", "1-1", "two"), ("collide", "1.0-1", "one")]
    assert found["total"] == 2


def test_results_are_ordered_best_first_then_short_names_then_alphabetically():
    db = DB("r", [P("zzz-editor-extra"), P("editor"), P("a-editor"), P("editor-b"), P("editor-a")])
    found = w.search_packages([db], Local(), ["editor"], _vercmp)
    # exact name; then names that start with it (alphabetical at equal length); then names that merely contain it (shorter first)
    assert [r["name"] for r in found["results"]] == ["editor", "editor-a", "editor-b", "a-editor", "zzz-editor-extra"]


def test_a_search_is_capped_and_says_how_many_matched():
    db = DB("r", [P(f"tool{i:04d}") for i in range(30)])
    found = w.search_packages([db], Local(), ["tool"], _vercmp, limit=10)
    assert len(found["results"]) == 10 and found["total"] == 30 and found["truncated"] is True
    whole = w.search_packages([db], Local(), ["tool"], _vercmp, limit=30)
    assert len(whole["results"]) == 30 and whole["truncated"] is False


def test_results_say_what_is_installed_and_what_has_an_update():
    db = DB("r", [P("old", "2.0-1", "tool"), P("same", "1.0-1", "tool"), P("fresh", "1.0-1", "tool"), P("newer-here", "1.0-1", "tool")])
    local = Local(P("old", "1.0-1", reason=0), P("same", "1.0-1", reason=1), P("newer-here", "2.0-1"))
    by_name = {r["name"]: r for r in w.search_packages([db], local, ["tool"], _vercmp)["results"]}
    assert by_name["old"]["installed_version"] == "1.0-1" and by_name["old"]["update"] is True and by_name["old"]["reason"] == "explicit"
    assert by_name["same"]["update"] is False and by_name["same"]["reason"] == "dependency"
    assert by_name["fresh"]["installed_version"] is None and by_name["fresh"]["reason"] is None and by_name["fresh"]["update"] is False
    assert by_name["newer-here"]["update"] is False  # the installed one is newer: that is not an update


def test_text_from_a_package_database_is_made_safe_to_show():
    assert w._text("a\x1b[31mred\x1b[0m\nline‮flipped") == "a [31mred [0m line flipped"
    assert len(w._text("x" * 5000)) == w.TEXT_LIMIT and w._text("x" * 5000).endswith("…")
    assert w._text(None) == ""
    db = DB("r", [P("evil", desc="Does \x1b]0;title\x07 things‮")])
    result = w.search_packages([db], Local(), ["evil"], _vercmp)["results"][0]
    assert all(c.isprintable() for c in result["desc"] + result["name"])


def test_bad_search_requests_are_refused_not_crashed():
    h = SimpleNamespace(get_syncdbs=lambda: [], get_localdb=lambda: Local())
    assert w.op_search(h, {"terms": "firefox"})["ok"] is False  # text, not a list
    assert w.op_search(h, {"terms": [1, 2]})["ok"] is False
    assert w.op_search(h, {"terms": []})["ok"] is False
    assert w.op_search(h, {"terms": ["   "]})["ok"] is False
    assert w.op_search(h, {"terms": ["x"] * (w.MAX_TERMS + 1)})["ok"] is False
    assert w.op_search(h, {"terms": ["x" * (w.MAX_TERM_LENGTH + 1)]})["ok"] is False
    assert w.op_search(h, {"terms": ["fine", "words"]})["ok"] is True


# -- the installed list --------------------------------------------------------------------------------------------------------

def test_an_installed_package_whose_repository_version_is_the_same_or_older_is_not_an_update():
    db = DB("r", [P("same", "1.0-1"), P("older-there", "1.0-1"), P("newer-there", "2.0-1")])
    local = Local(P("same", "1.0-1"), P("older-there", "2.0-1"), P("newer-there", "1.0-1"))
    by_name = {e["name"]: e for e in w.installed_packages(local, [db], _vercmp)}
    assert [by_name[n]["update"] for n in ("same", "older-there", "newer-there")] == [False, False, True]


def test_details_say_how_the_installed_version_compares_with_the_repositorys():
    db = DB("r", [P("a", "2.0-1"), P("b", "2.0-1"), P("c", "2.0-1"), P("d", "2.0-1")])
    local = Local(P("a", "1.0-1"), P("b", "2.0-1"), P("c", "3.0-1"))
    cmp = {n: w.package_details(n, None, local, [db], _satisfier, _vercmp).get("version_cmp") for n in "abcd"}
    assert cmp == {"a": -1, "b": 0, "c": 1, "d": None}  # d is not installed: nothing to compare


def test_the_installed_list_says_reason_repository_update_and_what_is_no_longer_needed():
    db = DB("r", [P("kept", "2.0-1"), P("lib", "1.0-1")])
    local = Local(P("kept", "1.0-1", reason=0), P("lib", "1.0-1", reason=1, required_by=["kept"]),
                  P("leftover", "1.0-1", reason=1, required_by=[]), P("handmade", "3-1", reason=0))
    by_name = {e["name"]: e for e in w.installed_packages(local, [db], _vercmp)}
    assert by_name["kept"]["update"] is True and by_name["kept"]["update_to"] == "2.0-1" and by_name["kept"]["repo"] == "r"
    assert "unneeded" not in by_name["kept"]  # chosen by you: never "no longer needed"
    assert by_name["lib"]["unneeded"] is False  # something still needs it
    assert by_name["leftover"]["unneeded"] is True and by_name["leftover"]["repo"] is None
    assert by_name["handmade"]["repo"] is None and by_name["handmade"]["reason"] == "explicit"
    assert [e["name"] for e in w.installed_packages(local, [db], _vercmp)] == sorted(by_name)


# -- details --------------------------------------------------------------------------------------------------------------------

def test_dependencies_say_whether_they_are_installed_available_or_missing():
    have = P("have", "1-1", provides=["libhave.so=1"])
    offered = P("offered", "1-1")
    app = P("app", "1-1", depends=["have", "libhave.so=1", "offered", "nowhere"], optdepends=["have: extra things", "absent"])
    db = DB("r", [app, offered])
    d = w.package_details("app", None, Local(have), [db], _satisfier)
    states = {x["dep"]: (x["state"], x.get("by")) for x in d["depends"]}
    assert states == {"have": ("installed", "have"), "libhave.so=1": ("installed", "have"), "offered": ("available", "offered"),
                      "nowhere": ("missing", None)}
    assert d["depends_missing"] == 1
    assert d["optdepends"] == [{"dep": "have", "why": "extra things", "installed": True}, {"dep": "absent", "why": "", "installed": False}]
    assert d["installed"] is False and d["repo"] == "r" and "files" not in d  # not installed: no files, nothing required by


def test_an_installed_package_shows_what_needs_it_and_its_files():
    db = DB("r", [P("app", "2-1")])
    have = P("app", "1-1", reason=1, required_by=["a", "b"], optional_for=["c"], files=["usr/", "usr/bin/app"])
    d = w.package_details("app", None, Local(have), [db], _satisfier)
    assert d["installed"] is True and d["installed_version"] == "1-1" and d["version"] == "2-1"  # the repository's metadata, the installed version
    assert d["reason"] == "dependency" and d["required_by"] == ["a", "b"] and d["optional_for"] == ["c"]
    assert d["files"] == ["usr/", "usr/bin/app"] and d["files_total"] == 2 and d["has_install_script"] is False


def test_a_package_no_repository_offers_is_described_from_the_installed_one():
    have = P("handmade", "3-1", desc="mine", depends=["glibc"])
    d = w.package_details("handmade", None, Local(have, P("glibc")), [DB("r", [])], _satisfier)
    assert d["found"] and d["repo"] is None and d["installed"] and d["desc"] == "mine"


def test_an_unknown_package_is_not_found_and_is_never_matched_by_what_it_provides():
    db = DB("r", [P("real", provides=["virtual"])])
    assert w.package_details("virtual", None, Local(), [db], _satisfier) == {"found": False}  # details are by exact name
    assert w.package_details("nothing", None, Local(), [db], _satisfier) == {"found": False}


def test_the_repository_asked_for_is_the_one_described():
    one, two = DB("one", [P("collide", "1.0-1")]), DB("two", [P("collide", "9.0-1")])
    assert w.package_details("collide", None, Local(), [one, two], _satisfier)["version"] == "1.0-1"
    assert w.package_details("collide", "two", Local(), [one, two], _satisfier)["version"] == "9.0-1"
    assert w.package_details("collide", "three", Local(), [one, two], _satisfier) == {"found": False}


def test_long_lists_and_file_lists_are_capped_and_say_how_much_was_left_out(monkeypatch):
    monkeypatch.setattr(w, "LIST_LIMIT", 5)
    monkeypatch.setattr(w, "FILES_LIMIT", 7)
    app = P("app", depends=[f"d{i}" for i in range(9)], optdepends=[f"o{i}: x" for i in range(8)])
    have = P("app", required_by=[f"r{i}" for i in range(12)], optional_for=[f"p{i}" for i in range(6)],
             files=[f"f{i}" for i in range(20)])
    d = w.package_details("app", None, Local(have), [DB("r", [app])], _satisfier)
    assert len(d["depends"]) == 5 and d["depends_more"] == 4
    assert len(d["optdepends"]) == 5 and d["optdepends_more"] == 3
    assert len(d["required_by"]) == 5 and d["required_by_more"] == 7 and d["optional_for_more"] == 1
    assert len(d["files"]) == 7 and d["files_total"] == 20


def test_details_text_is_cleaned_too():
    d = w.package_details("evil\x1b", None, Local(), [DB("r", [P("evil\x1b", desc="x‮y", files=[])])], _satisfier)
    assert d["found"] and "\x1b" not in d["name"] and "‮" not in d["desc"]
    have = P("evil", files=["usr/\x1b[2Jbin", "ok"])
    shown = w.package_details("evil", None, Local(have), [DB("r", [])], _satisfier)["files"]
    assert shown == ["usr/ [2Jbin", "ok"]


def test_bad_details_requests_are_refused_not_crashed():
    h = SimpleNamespace(get_syncdbs=lambda: [], get_localdb=lambda: Local())
    for req in ({}, {"name": ""}, {"name": 5}, {"name": "x", "repo": 5}):
        assert w.op_details(h, req)["ok"] is False


def test_the_operations_are_registered():
    assert {"search", "installed", "details"} <= set(w.OPS)


# -- the real worker, in a scratch root ------------------------------------------------------------------------------------------

def _scratch(tmp_path):
    """Two repositories (one name offered by both) and a local database: `alpha` 1.0 installed (the repository has 2.0), `libbeta`
    installed for it, `leftover` installed for nothing, `handmade` offered by no repository."""
    repos = {"repo1": tmp_path / "r1", "repo2": tmp_path / "r2"}
    for r in repos.values():
        r.mkdir()
    builders.build_pkg(repos["repo1"], name="alpha", version="2.0-1", depends=("libbeta",), extra_pkginfo="provides = alpha-api=2\n")
    builders.build_pkg(repos["repo1"], name="libbeta", version="1.0-1", depends=())
    builders.build_pkg(repos["repo1"], name="gamma", version="1.0-1", depends=("missinglib",))
    builders.build_pkg(repos["repo1"], name="collide", version="1.0-1", depends=())
    builders.build_pkg(repos["repo2"], name="collide", version="9.0-1", depends=())
    builders.build_pkg(repos["repo2"], name="delta", version="1.0-1", depends=())
    dbpath = tmp_path / "db"
    (dbpath / "sync").mkdir(parents=True)
    for repo, folder in repos.items():
        subprocess.run(["repo-add", "-q", str(dbpath / "sync" / f"{repo}.db.tar.gz"),
                        *sorted(str(p) for p in folder.glob("*.pkg.tar.zst"))], check=True, capture_output=True)
    local = dbpath / "local"
    local.mkdir()
    (local / "ALPM_DB_VERSION").write_text("9\n")

    def install(name, version, reason, depends=(), files=()):
        folder = local / f"{name}-{version}"
        folder.mkdir()
        deps = "".join(f"{d}\n" for d in depends)
        (folder / "desc").write_text(f"%NAME%\n{name}\n\n%VERSION%\n{version}\n\n%DESC%\nInstalled {name}\n\n%ARCH%\nx86_64\n\n"
                                     f"%INSTALLDATE%\n1700000000\n\n%REASON%\n{reason}\n\n%SIZE%\n4096\n\n"
                                     + (f"%DEPENDS%\n{deps}\n" if depends else ""))
        (folder / "files").write_text("%FILES%\n" + "".join(f"{f}\n" for f in files))

    install("alpha", "1.0-1", 0, depends=["libbeta"], files=["usr/", "usr/bin/", "usr/bin/alpha"])
    install("libbeta", "1.0-1", 1)
    install("leftover", "1.0-1", 1)
    install("handmade", "3-1", 0)
    (tmp_path / "root").mkdir()
    return {"dbpath": str(dbpath), "arch": ["x86_64"], "repos": ["repo1", "repo2"], "root": str(tmp_path / "root")}


def _run(base, **request):
    out = subprocess.run([sys.executable, "-I", w.__file__], input=json.dumps({**base, **request}), capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def scratch(tmp_path):
    return _scratch(tmp_path)


@pytest.mark.needs_tool("bsdtar", "zstd", "repo-add")
def test_the_real_worker_searches_the_installed_list_and_describes_a_package(scratch):
    found = _run(scratch, op="search", terms=["alpha"])
    assert [r["name"] for r in found["results"]][0] == "alpha"
    alpha = found["results"][0]
    assert alpha["version"] == "2.0-1" and alpha["installed_version"] == "1.0-1" and alpha["update"] is True and alpha["repo"] == "repo1"
    assert _run(scratch, op="search", terms=["alpha-api"])["results"][0]["name"] == "alpha"  # by what it provides
    assert _run(scratch, op="search", terms=["(.*"])["total"] == 0
    both = [r for r in _run(scratch, op="search", terms=["collide"])["results"]]
    assert [(r["name"], r["repo"], r["version"]) for r in both] == [("collide", "repo1", "1.0-1")]  # the first repository wins

    listing = {e["name"]: e for e in _run(scratch, op="installed")["packages"]}
    assert set(listing) == {"alpha", "libbeta", "leftover", "handmade"}
    assert listing["alpha"]["update"] and listing["alpha"]["update_to"] == "2.0-1" and "unneeded" not in listing["alpha"]
    assert listing["libbeta"]["unneeded"] is False and listing["leftover"]["unneeded"] is True
    assert listing["handmade"]["repo"] is None and listing["leftover"]["repo"] is None and listing["libbeta"]["repo"] == "repo1"

    d = _run(scratch, op="details", name="alpha")
    assert d["found"] and d["installed_version"] == "1.0-1" and d["version"] == "2.0-1"
    assert d["depends"] == [{"dep": "libbeta", "state": "installed", "by": "libbeta"}]
    assert d["files"] == ["usr/", "usr/bin/", "usr/bin/alpha"] and d["files_total"] == 3
    libbeta = _run(scratch, op="details", name="libbeta")
    assert libbeta["required_by"] == ["alpha"] and libbeta["reason"] == "dependency"
    gamma = _run(scratch, op="details", name="gamma")
    assert gamma["installed"] is False and gamma["depends_missing"] == 1
    assert _run(scratch, op="details", name="collide", repo="repo2")["version"] == "9.0-1"
    assert _run(scratch, op="details", name="nothing")["found"] is False
    assert _run(scratch, op="details", name="alpha-api")["found"] is False  # a provided name is not a package
