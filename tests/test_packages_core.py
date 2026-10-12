"""Repository packages by name: what is checked before the helper is asked, what the page is told, and that nothing here can be turned
into a change the helper's own rules would refuse."""

import copy
import os

import pytest

from cygnus.core import packages
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import Plan
from cygnus.gui import fixes
from cygnus.helper import actions

CONFIG = pm.PacmanConfig(arch=("x86_64",), dbpath="/nonexistent-db", cachedirs=(), gpgdir="", repos=("core",), servers={},
                         hold=("held-pkg",))


def info(name="foo", *, installed=False, version="1.0-1", installed_version=None, repo="extra", required_by=(), required_by_more=0, **extra):
    return {**extra, "ok": True, "found": True, "name": name, "version": version, "repo": repo, "installed": installed,
            "installed_version": installed_version if installed else None, "depends": [], "optdepends": [],
            "required_by": list(required_by), "required_by_more": required_by_more, "optional_for": [], "files": [], "files_total": 0}


class Worker:
    """Answers like the libalpm worker, from a table of what is known; remembers what it was asked."""

    def __init__(self, table=None, **answers):
        self.table, self.answers, self.asked = table or {}, answers, []

    def __call__(self, config, request):
        self.asked.append(request)
        if request["op"] == "removal_facts":  # what the real worker derives from the installed database, here from the same table
            out = {}
            for n in request["names"]:
                t = self.table.get(n)
                out[n] = ({"installed": True, "reason": "explicit", "required_by": list(t["required_by"]), "required_by_more": t["required_by_more"]}
                          if t and t["installed"] else {"installed": False})
            return {"ok": True, "packages": out}
        if request["op"] == "details":
            return copy.deepcopy(self.table.get(request["name"], {"ok": True, "found": False}))  # a fresh answer each time, as from a process
        return self.answers[request["op"]]


# -- the words and names that are accepted --------------------------------------------------------------------------------

def test_a_search_is_split_into_lower_case_words():
    assert packages.query_terms("  Fire  FOX\tbrowser ") == ["fire", "fox", "browser"]
    assert packages.query_terms("ab") == ["ab"]


@pytest.mark.parametrize("bad", ["", " ", "a", "   a  ", "x " * 9, "y" * 101, None, 5, ["a", "b"]])
def test_a_search_that_is_too_short_or_too_long_or_not_text_is_refused(bad):
    with pytest.raises(packages.PackagesError):
        packages.query_terms(bad)


def test_package_names_are_checked_before_anything_else_happens():
    assert packages.names_checked(["b", "a", "b"]) == ["b", "a"]  # once each, in the order given
    for bad in ([], "foo", ["foo", 5], ["Foo"], ["-S"], ["--overwrite=*"], ["a b"], ["foo\n"], [""], ["x" * 101], ["foo/bar"], ["a", None],
                [f"p{i}" for i in range(packages.MAX_NAMES + 1)]):
        with pytest.raises(packages.PackagesError):
            packages.names_checked(bad)
    assert len(packages.names_checked([f"p{i}" for i in range(packages.MAX_NAMES)])) == packages.MAX_NAMES


def test_the_name_rule_is_the_helpers():
    assert packages.PKG_NAME.pattern == actions.PKG_NAME.pattern


# -- how old the package lists are ---------------------------------------------------------------------------------------------

def test_the_age_of_the_package_lists_is_when_pacman_last_put_one_there(tmp_path):
    sync = tmp_path / "sync"
    sync.mkdir()
    for name in ("core.db", "extra.db"):
        (sync / name).write_bytes(b"x")
        os.utime(sync / name, (1_000, 1_000))  # a list's own date is the mirror's (libalpm sets it from Last-Modified) ...
    os.utime(sync, (7_000, 7_000))  # ... the folder's is when this computer last refreshed
    config = pm.PacmanConfig(arch=("x86_64",), dbpath=str(tmp_path), cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    assert packages.lists_age(config) == {"refreshed": 7_000}


def test_a_folder_with_no_lists_in_it_has_no_age(tmp_path):
    sync = tmp_path / "sync"
    sync.mkdir()
    (sync / "core.db.sig").write_bytes(b"x")  # a signature is not a list
    (sync / "linked.db").symlink_to(sync / "core.db.sig")  # a link is not a list either
    config = pm.PacmanConfig(arch=("x86_64",), dbpath=str(tmp_path), cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    assert packages.lists_age(config) == {"refreshed": None}


def test_no_package_lists_is_not_an_error(tmp_path):
    config = pm.PacmanConfig(arch=("x86_64",), dbpath=str(tmp_path / "missing"), cachedirs=(), gpgdir="", repos=(), servers={}, hold=())
    assert packages.lists_age(config) == {"refreshed": None}


# -- reading ----------------------------------------------------------------------------------------------------------------------

def test_a_search_asks_the_worker_for_the_words_and_adds_how_old_the_lists_are():
    worker = Worker(search={"ok": True, "results": [{"name": "foo"}], "total": 1, "truncated": False})
    out = packages.search("Foo BAR", config=CONFIG, worker=worker)
    assert worker.asked == [{"op": "search", "terms": ["foo", "bar"]}]
    assert out["results"] == [{"name": "foo"}] and out["total"] == 1 and out["terms"] == ["foo", "bar"] and set(out["lists"]) == {"refreshed"}


def test_a_search_too_short_never_reaches_the_worker():
    worker = Worker()
    with pytest.raises(packages.PackagesError):
        packages.search("a", config=CONFIG, worker=worker)
    assert worker.asked == []


def test_a_worker_that_died_or_refused_is_told_in_words():
    def died(config, request):
        raise pm.WorkerCrash("libalpm worker died (exit -11)")

    with pytest.raises(packages.PackagesError, match="could not be read"):
        packages.search("firefox", config=CONFIG, worker=died)
    with pytest.raises(packages.PackagesError, match="no good"):
        packages.installed(config=CONFIG, worker=lambda c, r: {"ok": False, "error": {"message": "no good"}})
    with pytest.raises(packages.PackagesError, match="could not be read"):
        packages.installed(config=CONFIG, worker=lambda c, r: {"ok": False})


def test_the_installed_list_marks_protected_packages_and_counts_updates():
    rows = [{"name": "glibc", "update": False}, {"name": "held-pkg", "update": True}, {"name": "ncdu", "update": True},
            {"name": "linux-cachyos", "update": False}]
    out = packages.installed(config=CONFIG, worker=Worker(installed={"ok": True, "packages": rows}))
    assert {p["name"]: p["protected"] for p in out["packages"]} == {"glibc": True, "held-pkg": True, "ncdu": False, "linux-cachyos": True}
    assert out["updates"] == 2


def test_the_installed_list_also_says_which_packages_cygnus_manages_as_applications(monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {"chrome-like": "Chrome Like"})
    rows = [{"name": "chrome-like", "update": False}, {"name": "ncdu", "update": False}]
    out = packages.installed(config=CONFIG, worker=Worker(installed={"ok": True, "packages": rows}))
    assert {p["name"]: p["managed"] for p in out["packages"]} == {"chrome-like": True, "ncdu": False}


def test_details_of_a_package_nothing_has_is_an_error_in_words():
    with pytest.raises(packages.PackagesError, match="no package called 'nothing'"):
        packages.details("nothing", config=CONFIG, worker=Worker())
    for bad in ("", None, 5, "x" * 201):
        with pytest.raises(packages.PackagesError):
            packages.details(bad, config=CONFIG, worker=Worker())
    with pytest.raises(packages.PackagesError):
        packages.details("foo", 5, config=CONFIG, worker=Worker())


def test_details_say_why_a_package_cannot_be_installed_or_removed():
    table = {"foo": info("foo"), "held-pkg": info("held-pkg", installed=True, installed_version="1.0-1")}
    worker = Worker(table)
    foo = packages.details("foo", config=CONFIG, worker=worker)
    assert foo["blocked"] == {"install": None, "remove": "It is not installed."} and foo["protected"] is False
    held = packages.details("held-pkg", config=CONFIG, worker=worker)
    assert held["protected"] is True and "protected system package" in held["blocked"]["remove"]


@pytest.mark.parametrize("case, expected_install, expected_remove", [
    (dict(installed=True, installed_version="1.0-1"), "already installed", None),
    (dict(installed=True, installed_version="0.9-1"), "newer version is available", None),
    (dict(installed=True, installed_version="1.0-1", repo=None), "no repository offers it", None),
    (dict(installed=True, installed_version="2.0-1", version_cmp=1), "newer than the one the repository offers", None),
    (dict(installed=True, installed_version="0.9-1", version_cmp=-1), "newer version is available", None),
    (dict(installed=True, installed_version="1.0-1", version_cmp=0), "already installed", None),
    (dict(installed=False, repo=None), "No repository offers it", "not installed"),
    (dict(installed=False), None, "not installed"),
    (dict(installed=True, installed_version="1.0-1", required_by=["a", "b"]), "already installed", "Other packages need it: a, b"),
    (dict(installed=True, installed_version="1.0-1", required_by=["a"], required_by_more=5), "already installed", "and 5 more"),
])
def test_why_a_package_cannot_be_installed_or_removed(case, expected_install, expected_remove):
    why = packages.why_not(info("foo", **case), CONFIG)
    for got, wanted in ((why["install"], expected_install), (why["remove"], expected_remove)):
        assert (got is None) if wanted is None else (got is not None and wanted in got), (got, wanted)


@pytest.mark.parametrize("name", ["glibc", "systemd", "linux-cachyos", "linux-firmware", "held-pkg"])
def test_a_protected_package_is_never_offered_for_removal(name):
    why = packages.why_not(info(name, installed=True, installed_version="1.0-1"), CONFIG)
    assert "protected" in why["remove"]


# -- the checks before an install or a removal ----------------------------------------------------------------------------------------

def test_an_install_is_checked_name_by_name():
    worker = Worker({"fresh": info("fresh"), "have": info("have", installed=True, installed_version="1.0-1"), "offline": info("offline", repo=None)})
    assert packages.check_install(["fresh"], config=CONFIG, worker=worker) == ["fresh"]
    with pytest.raises(packages.PackagesError) as caught:
        packages.check_install(["fresh", "have", "offline", "nothing"], config=CONFIG, worker=worker)
    text = str(caught.value)
    assert "have: It is already installed." in text and "offline: No repository offers it." in text and "nothing: No repository offers it." in text
    assert "fresh:" not in text  # what is fine is not complained about


def test_an_install_with_a_bad_name_never_reaches_the_worker():
    worker = Worker()
    with pytest.raises(packages.PackagesError):
        packages.check_install(["--overwrite=*"], config=CONFIG, worker=worker)
    assert worker.asked == []


def test_a_removal_is_refused_for_what_is_missing_protected_or_needed():
    worker = Worker({"gone": info("gone"), "glibc": info("glibc", installed=True, installed_version="1.0-1"),
                     "lib": info("lib", installed=True, installed_version="1.0-1", required_by=["app", "other"]),
                     "huge": info("huge", installed=True, installed_version="1.0-1", required_by=["a"], required_by_more=300),
                     "ok": info("ok", installed=True, installed_version="1.0-1")})
    assert packages.check_removal(["ok"], config=CONFIG, worker=worker) == ["ok"]
    with pytest.raises(packages.PackagesError) as caught:
        packages.check_removal(["gone", "glibc", "lib", "huge", "ok"], config=CONFIG, worker=worker)
    text = str(caught.value)
    assert "gone: It is not installed." in text and "glibc: This is a protected system package" in text
    assert "lib: Other packages need it: app, other." in text and "huge:" in text and "ok:" not in text


def test_a_removal_is_checked_with_one_question_to_the_worker_however_many_packages():
    table = {f"p{i}": info(f"p{i}", installed=True, installed_version="1.0-1") for i in range(150)}
    worker = Worker(table)
    names = list(table)
    assert packages.check_removal(names, config=CONFIG, worker=worker, limit=packages.MAX_BATCH) == names
    assert [r["op"] for r in worker.asked] == ["removal_facts"] and worker.asked[0]["names"] == names
    with pytest.raises(packages.PackagesError, match="at most 20"):
        packages.check_removal(names, config=CONFIG, worker=worker)  # the ordinary limit, unless a batch is meant
    with pytest.raises(packages.PackagesError, match="at most 200"):
        packages.check_removal([f"q{i}" for i in range(201)], config=CONFIG, worker=worker, limit=packages.MAX_BATCH)


def test_packages_removed_together_may_need_each_other():
    worker = Worker({"app": info("app", installed=True, installed_version="1.0-1"),
                     "lib": info("lib", installed=True, installed_version="1.0-1", required_by=["app"])})
    assert packages.check_removal(["app", "lib"], config=CONFIG, worker=worker) == ["app", "lib"]
    with pytest.raises(packages.PackagesError, match="lib: Other packages need it: app"):
        packages.check_removal(["lib"], config=CONFIG, worker=worker)  # app stays, and needs it


def test_something_needed_by_a_package_that_stays_and_one_that_goes_is_still_refused():
    worker = Worker({"lib": info("lib", installed=True, installed_version="1.0-1", required_by=["app", "stays"]),
                     "app": info("app", installed=True, installed_version="1.0-1")})
    with pytest.raises(packages.PackagesError, match="lib: Other packages need it: stays"):
        packages.check_removal(["lib", "app"], config=CONFIG, worker=worker)


def test_more_dependents_than_the_list_holds_always_blocks_even_if_the_listed_ones_are_removed_too():
    worker = Worker({"lib": info("lib", installed=True, installed_version="1.0-1", required_by=["app"], required_by_more=1),
                     "app": info("app", installed=True, installed_version="1.0-1")})
    with pytest.raises(packages.PackagesError):
        packages.check_removal(["lib", "app"], config=CONFIG, worker=worker)


# -- packages Cygnus manages as applications -----------------------------------------------------------------------------------------------

def _registry_with(tmp_path):
    from cygnus.gui import service

    path = str(tmp_path / "registry.db")
    service.record_package_install("chrome-like", "1.0-1", origin="converted", source={"file": "/x.pkg.tar.zst"}, registry_path=path)
    service.record_package_install("handmade", "2-1", origin="local", source={"file": "/y.pkg.tar.zst"}, registry_path=path)
    return path


def test_the_packages_cygnus_manages_as_applications_are_found(tmp_path):
    assert packages.managed_packages(_registry_with(tmp_path)) == {"chrome-like": "chrome-like", "handmade": "handmade"}
    assert packages.managed_packages(str(tmp_path / "empty.db")) == {}


def test_a_registry_that_cannot_be_read_is_an_empty_answer_not_a_crash(tmp_path):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database" * 100)
    assert packages.managed_packages(str(broken)) == {}


def test_a_package_cygnus_manages_is_not_removed_from_here(monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {"chrome-like": "Chrome Like"})
    worker = Worker({"chrome-like": info("chrome-like", installed=True, installed_version="1.0-1"),
                     "plain": info("plain", installed=True, installed_version="1.0-1")})
    d = packages.details("chrome-like", config=CONFIG, worker=worker)
    assert d["managed_as"] == "Chrome Like" and "Applications" in d["blocked"]["remove"] and "Chrome Like" in d["blocked"]["remove"]
    assert packages.details("plain", config=CONFIG, worker=worker)["managed_as"] is None
    with pytest.raises(packages.PackagesError, match="chrome-like: Cygnus manages this as Chrome Like"):
        packages.check_removal(["chrome-like", "plain"], config=CONFIG, worker=worker)
    assert packages.check_removal(["plain"], config=CONFIG, worker=worker) == ["plain"]


def test_the_name_of_a_managed_application_is_made_safe_before_it_is_shown(monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {"foo": "Evil\x1b[31m App\u202e" + "x" * 300})
    shown = packages.details("foo", config=CONFIG, worker=Worker({"foo": info("foo", installed=True, installed_version="1.0-1")}))["managed_as"]
    assert all(c.isprintable() for c in shown) and len(shown) <= 100 and shown.startswith("Evil [31m App")


def test_a_package_that_is_not_installed_is_never_called_managed(monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {"gone": "Gone"})
    d = packages.details("gone", config=CONFIG, worker=Worker({"gone": info("gone")}))
    assert d["managed_as"] is None and d["blocked"]["remove"] == "It is not installed."


def test_protected_comes_before_managed_in_what_the_person_is_told():
    why = packages.why_not({**info("glibc", installed=True, installed_version="1.0-1"), "managed_as": "Odd"}, CONFIG)
    assert "protected" in why["remove"]


def test_removing_a_managed_package_is_refused_before_the_helper_is_asked(helper, monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {"ok": "Okay App"})
    with pytest.raises(packages.PackagesError, match="Cygnus manages this as Okay App"):
        fixes.plan_remove_by_name(["ok"])
    assert helper.calls == [] and fixes._PENDING == {}


# -- what the helper refuses, as the page needs to recognise it ----------------------------------------------------------------------------

class _Ledger:
    def __init__(self, owns):
        self.owns = owns

    def owns_package(self, name):
        return self.owns


def _helper_plan(analysis=None, owns=True, **req):
    return actions.plan_packages(req, caller_uid=1000, caller_sender=":1.1", ledger=_Ledger(owns), config=CONFIG,
                                 analyse=lambda targets, **kw: analysis or pm.PacmanAnalysis(targets=targets),
                                 sync_fresh=lambda: "/nonexistent")


def test_the_helpers_own_refusals_are_recognised():
    upgrading = pm.PacmanAnalysis(targets=["foo"], to_add=[{"name": "foo", "version": "2"}], issues=[pm._partial_upgrade_issue(
        [{"name": "bar", "installed_version": "1", "version": "2"}], [])])
    with pytest.raises(actions.PlanRefused) as stale:
        _helper_plan(upgrading, install_repo=["foo"])
    assert packages.refusal(str(stale.value)) == "update_first"
    with pytest.raises(actions.PlanRefused) as not_ours:
        _helper_plan(owns=False, remove=["ncdu"])
    assert packages.refusal(str(not_ours.value)) == "not_installed_by_cygnus"
    assert _helper_plan(owns=False, remove=["ncdu"], confirm_not_installed_by_cygnus=True).commands  # and with the confirmation it is planned
    with pytest.raises(actions.PlanRefused) as protected:
        _helper_plan(remove=["glibc"])
    assert packages.refusal(str(protected.value)) is None  # any other refusal is shown as it is


def test_both_refusals_have_words_for_the_page():
    assert set(packages.REFUSAL_TEXT) == {"update_first", "not_installed_by_cygnus"}
    assert "Updates page" in packages.REFUSAL_TEXT["update_first"]


# -- planning through the helper ------------------------------------------------------------------------------------------------------

class FakeClient:
    calls: list = []
    summary: dict = {}
    refuse: Exception | None = None

    def plan_packages(self, **kw):
        FakeClient.calls.append(kw)
        if FakeClient.refuse is not None:
            raise FakeClient.refuse
        return Plan("p1", FakeClient.summary, "Do it")


@pytest.fixture
def helper(monkeypatch):
    FakeClient.calls, FakeClient.summary, FakeClient.refuse = [], {}, None
    monkeypatch.setattr(fixes, "HelperClient", FakeClient)
    monkeypatch.setattr(pm, "read_config", lambda: CONFIG)
    table = {"fresh": info("fresh"), "ok": info("ok", installed=True, installed_version="1.0-1"),
             "lib": info("lib", installed=True, installed_version="1.0-1", required_by=["app"]),
             "glibc": info("glibc", installed=True, installed_version="1.0-1"), "have": info("have", installed=True, installed_version="1.0-1")}
    monkeypatch.setattr(pm, "run_worker", Worker(table))
    yield FakeClient
    fixes._PENDING.clear()


def test_installing_by_name_asks_for_an_ordinary_explicit_install_and_lists_what_it_does(helper):
    helper.summary = {"install": [{"name": "fresh", "version": "1.0-1", "repo": "extra"}, {"name": "libx", "version": "2", "reason": "dependency"}],
                      "upgrade": [], "remove": [], "download_bytes": 4096}
    out = fixes.plan_install_packages(["fresh", "fresh"])
    assert helper.calls == [{"install_repo": ["fresh"]}]  # explicit (never "as a dependency"), once
    assert out["kind"] == "helper" and out["token"] in fixes._PENDING and out["action"] == "Install" and out["title"] == "Install fresh"
    assert out["installs"] == [{"name": "fresh", "to": "1.0-1", "reason": ""}, {"name": "libx", "to": "2", "reason": "dependency"}]
    assert out["upgrades"] == [] and out["removals"] == [] and out["download_bytes"] == 4096


def test_installing_what_cannot_be_installed_never_asks_the_helper(helper):
    for bad in (["have"], ["nothing"], ["--overwrite=*"], [], "fresh", ["fresh", 5]):
        with pytest.raises(packages.PackagesError):
            fixes.plan_install_packages(bad)
    assert helper.calls == [] and fixes._PENDING == {}


def test_a_system_with_updates_waiting_is_told_to_update_first_and_no_plan_is_kept(helper):
    helper.refuse = actions.PlanRefused("PKG_PARTIAL_UPGRADE_RISK: Your system needs to be upgraded first")
    out = fixes.plan_install_packages(["fresh"])
    assert out["refused"] == "update_first" and "Updates page" in out["text"] and "token" not in out
    assert fixes._PENDING == {}


def test_any_other_refusal_of_an_install_is_raised_as_it_is(helper):
    helper.refuse = actions.PlanRefused("PKG_CONFLICT: fresh conflicts with your installed other")
    with pytest.raises(actions.PlanRefused, match="PKG_CONFLICT"):
        fixes.plan_install_packages(["fresh"])


def test_removing_by_name_asks_for_exactly_that_and_lists_what_goes(helper):
    helper.summary = {"install": [], "upgrade": [], "remove": [{"name": "ok", "reason": "requested"}]}
    out = fixes.plan_remove_by_name(["ok"])
    assert helper.calls == [{"remove": ["ok"], "confirm_not_installed_by_cygnus": False}]
    assert out["action"] == "Remove" and out["removals"] == [{"name": "ok", "reason": "requested"}] and "stays installed" in out["security_note"]


def test_a_removal_the_page_checks_is_never_planned_when_something_needs_the_package_or_it_is_protected(helper):
    for names in (["lib"], ["glibc"], ["nothing"], ["lib", "ok"]):
        with pytest.raises(packages.PackagesError):
            fixes.plan_remove_by_name(names)
    assert helper.calls == []


def test_a_package_cygnus_did_not_install_is_asked_about_before_it_is_planned(helper):
    helper.refuse = actions.PlanRefused("these packages were not installed by Cygnus: ok; the request must confirm their removal explicitly")
    out = fixes.plan_remove_by_name(["ok"])
    assert out["refused"] == "not_installed_by_cygnus" and out["names"] == ["ok"] and "token" not in out
    assert helper.calls == [{"remove": ["ok"], "confirm_not_installed_by_cygnus": False}] and fixes._PENDING == {}


def test_the_confirmation_is_passed_on_only_when_it_is_exactly_true(helper):
    for value in ("yes", 1, "true", None, [True]):
        helper.calls.clear()
        helper.refuse = actions.PlanRefused("these packages were not installed by Cygnus: ok")
        out = fixes.plan_remove_by_name(["ok"], value)
        assert out["refused"] == "not_installed_by_cygnus" and helper.calls == [{"remove": ["ok"], "confirm_not_installed_by_cygnus": False}], value
    helper.calls.clear()
    helper.refuse = None
    fixes.plan_remove_by_name(["ok"], True)
    assert helper.calls == [{"remove": ["ok"], "confirm_not_installed_by_cygnus": True}]


def test_a_refusal_after_the_person_confirmed_is_not_asked_again(helper):
    helper.refuse = actions.PlanRefused("these packages were not installed by Cygnus: ok")
    with pytest.raises(actions.PlanRefused):
        fixes.plan_remove_by_name(["ok"], True)  # never loops back to the question


def test_any_other_refusal_of_a_removal_is_raised_as_it_is(helper):
    helper.refuse = actions.PlanRefused("protected system packages cannot be removed with Cygnus: ok")
    with pytest.raises(actions.PlanRefused, match="protected"):
        fixes.plan_remove_by_name(["ok"])


# -- the names of every package the repositories have ---------------------------------------------------------------------------------------------
def test_the_repositories_names_are_asked_of_the_worker_once_and_returned_as_a_set():
    asked = []

    def worker(config, request):
        asked.append(request)
        return {"ok": True, "names": ["bash", "zsh", "bash"]}

    assert packages.repo_names(config=CONFIG, worker=worker) == frozenset({"bash", "zsh"})
    assert asked == [{"op": "repo_names"}]


@pytest.mark.parametrize("answer", [{"ok": True, "names": "bash"}, {"ok": True, "names": [1, 2]}, {"ok": True, "names": ["bash", None]}, {"ok": True, "names": {"bash": 1}},
                                    {"ok": True, "names": None}])
def test_an_answer_that_is_not_a_list_of_names_is_refused_in_words(answer):
    with pytest.raises(packages.PackagesError, match="did not expect"):
        packages.repo_names(config=CONFIG, worker=lambda c, r: answer)


def test_a_worker_that_fails_or_dies_is_said_in_words():
    with pytest.raises(packages.PackagesError, match="no good"):
        packages.repo_names(config=CONFIG, worker=lambda c, r: {"ok": False, "error": {"message": "no good"}})

    def died(config, request):
        raise pm.WorkerCrash("it died")

    with pytest.raises(packages.PackagesError, match="could not be read"):
        packages.repo_names(config=CONFIG, worker=died)
