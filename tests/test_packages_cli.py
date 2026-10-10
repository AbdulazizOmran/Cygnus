"""`cygnus packages …`: reads print what the worker found; install and remove go through the helper's plans, and the question about a
package Cygnus did not install is never answered by --yes."""

import builtins
import copy

import pytest

from cygnus.cli import main as cli
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import Plan
from cygnus.helper import actions

CONFIG = pm.PacmanConfig(arch=("x86_64",), dbpath="/nonexistent-db", cachedirs=(), gpgdir="", repos=("core",), servers={}, hold=())


def _details(name="foo", **over):
    base = {"ok": True, "found": True, "name": name, "version": "1.0-1", "arch": "x86_64", "desc": f"The {name} tool", "url": "https://example.invalid",
            "licenses": ["MIT"], "groups": [], "packager": "Someone", "build_date": 1700000000, "repo": "extra", "package_size": 2 * 2**20,
            "installed_size": 5 * 2**20, "depends": [{"dep": "glibc", "state": "installed", "by": "glibc"},
                                                      {"dep": "libx", "state": "available", "by": "libx", "repo": "extra"},
                                                      {"dep": "ghost", "state": "missing"}],
            "depends_more": 0, "optdepends": [{"dep": "bar", "why": "for bars", "installed": False}], "optdepends_more": 0,
            "installed": False, "installed_version": None, "provides": ["foo-api"], "conflicts": [], "replaces": [], "depends_missing": 1}
    return {**base, **over}


INSTALLED = _details("foo", installed=True, installed_version="1.0-1", reason="explicit", install_date=1700001000, required_by=["a", "b"],
                     required_by_more=3, optional_for=["c"], optional_for_more=0, files=["usr/", "usr/bin/foo"], files_total=40,
                     has_install_script=True, depends_missing=0)


class Worker:
    def __init__(self, **answers):
        self.answers, self.asked = answers, []

    def __call__(self, config, request):
        self.asked.append(request)
        if request["op"] == "removal_facts":
            table = self.answers["details"]
            return {"ok": True, "packages": {n: ({"installed": True, "reason": "explicit", "required_by": list(table[n].get("required_by", [])),
                                                  "required_by_more": table[n].get("required_by_more", 0)}
                                                 if n in table and table[n]["installed"] else {"installed": False}) for n in request["names"]}}
        answer = self.answers[request["op"]]
        return copy.deepcopy(answer[request["name"]] if request["op"] == "details" else answer)


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setattr(pm, "read_config", lambda: CONFIG)
    worker = Worker(details={"foo": _details("foo"), "have": INSTALLED, "held": _details("held", installed=True, installed_version="1.0-1", reason="explicit",
                                                                                          required_by=[], required_by_more=0, files=[], files_total=0, optional_for=[],
                                                                                          optional_for_more=0, has_install_script=False, install_date=1),
                             "glibc": _details("glibc", installed=True, installed_version="1.0-1", reason="dependency", required_by=[],
                                               required_by_more=0, files=[], files_total=0, optional_for=[], optional_for_more=0,
                                               has_install_script=False, install_date=1)},
                    search={"ok": True, "results": [
                        {"name": "foo", "version": "1.0-1", "repo": "extra", "desc": "The foo tool", "installed_version": None, "reason": None, "update": False},
                        {"name": "foobar", "version": "2-1", "repo": "core", "desc": "", "installed_version": "1-1", "reason": "explicit", "update": True}],
                        "total": 250, "truncated": True},
                    installed={"ok": True, "packages": [
                        {"name": "chosen", "version": "1-1", "desc": "", "reason": "explicit", "size": 2048, "installed": 1, "repo": "extra", "update": False},
                        {"name": "needed", "version": "1-1", "desc": "", "reason": "dependency", "size": 3 * 2**20, "installed": 1, "repo": "extra",
                         "update": False, "unneeded": False},
                        {"name": "leftover", "version": "1-1", "desc": "", "reason": "dependency", "size": 1, "installed": 1, "repo": "extra",
                         "update": False, "unneeded": True},
                        {"name": "newer", "version": "1-1", "desc": "", "reason": "explicit", "size": 1, "installed": 1, "repo": "core", "update": True,
                         "update_to": "2-1"},
                        {"name": "handmade", "version": "9-1", "desc": "", "reason": "explicit", "size": 1, "installed": 1, "repo": None, "update": False}]})
    monkeypatch.setattr(pm, "run_worker", worker)
    return worker


def run(capsys, *argv):
    code = cli.main(["packages", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_search_prints_what_matched_and_how_much_was_left_out(system, capsys):
    code, out, _ = run(capsys, "search", "Foo", "tool")
    assert code == 0 and system.asked == [{"op": "search", "terms": ["foo", "tool"]}]
    assert "extra/foo 1.0-1\n    The foo tool" in out
    assert "core/foobar 2-1  [installed 1-1, update available]" in out
    assert "250 match; the first 2 are shown" in out and "package lists refreshed" in out


def test_a_search_that_is_too_short_is_an_error_not_a_scan(system, capsys):
    code, out, err = run(capsys, "search", "a")
    assert code == 2 and "at least 2 characters" in err and system.asked == []


def test_info_shows_what_a_package_needs_and_which_of_that_is_missing(system, capsys):
    code, out, _ = run(capsys, "info", "foo")
    assert code == 0
    assert "Depends on     : glibc (installed), libx (will be installed), ghost (NOT AVAILABLE)" in out
    assert "Optional       : bar - for bars" in out and "Download size  : 2.0 MiB" in out
    assert "Cannot remove from here: It is not installed." in out and "Cannot install" not in out


def test_info_of_an_installed_package_shows_what_needs_it_and_says_when_it_runs_a_script(system, capsys):
    code, out, _ = run(capsys, "info", "have")
    assert code == 0
    assert "Required by    : a, b … and 3 more" in out and "Optional for   : c" in out
    assert "Install script : yes" in out and "Files          : 40 (--files lists them)" in out
    assert "Cannot install from here: It is already installed." in out
    code, out, _ = run(capsys, "info", "have", "--files")
    assert "  usr/bin/foo" in out and "… 38 more of 40" in out


def test_info_of_a_protected_package_says_it_cannot_be_removed(system, capsys):
    code, out, _ = run(capsys, "info", "glibc")
    assert code == 0 and "Cannot remove from here: This is a protected system package" in out


def test_info_of_an_unknown_package_is_an_error(system, capsys):
    system.answers["details"]["nothing"] = {"ok": True, "found": False}
    code, out, err = run(capsys, "info", "nothing")
    assert code == 2 and "no package called 'nothing'" in err


@pytest.mark.parametrize("flags, names", [
    ([], ["chosen", "needed", "leftover", "newer", "handmade"]),
    (["--explicit"], ["chosen", "newer", "handmade"]),
    (["--deps"], ["needed", "leftover"]),
    (["--unneeded"], ["leftover"]),
    (["--updates"], ["newer"]),
    (["--foreign"], ["handmade"]),
    (["--explicit", "--updates"], ["newer"]),
    (["--deps", "--explicit"], []),
])
def test_the_installed_list_can_be_narrowed(system, capsys, flags, names):
    code, out, _ = run(capsys, "installed", *flags)
    assert code == 0
    listed = [line.split()[0] for line in out.splitlines() if line and not line.startswith(("(", "0 of", "5 of", "1 of", "2 of", "3 of", "4 of"))]
    assert listed == names
    assert f"{len(names)} of 5 installed packages; 1 have updates" in out


def test_the_installed_list_marks_what_is_no_longer_needed_and_what_no_repository_has(system, capsys):
    _, out, _ = run(capsys, "installed")
    assert "(no longer needed)" in next(l for l in out.splitlines() if l.startswith("leftover"))
    assert "(no repository)" in next(l for l in out.splitlines() if l.startswith("handmade"))
    assert "-> 2-1" in next(l for l in out.splitlines() if l.startswith("newer"))


# -- install and remove ----------------------------------------------------------------------------------------------------------------

class Client:
    plans: list = []
    refuse: list = []

    def plan_packages(self, **kw):
        Client.plans.append(kw)
        if Client.refuse:
            raise Client.refuse.pop(0)
        return Plan("p1", {"install": [{"name": "foo", "version": "1.0-1", "reason": ""}], "upgrade": [], "remove": [{"name": "have", "reason": "requested"}],
                           "download_bytes": 3 * 2**20}, "Do it")


@pytest.fixture
def helper(monkeypatch, system):
    Client.plans, Client.refuse = [], []
    commits = []
    monkeypatch.setattr("cygnus.core.privilege.HelperClient", Client)
    monkeypatch.setattr(cli, "_helper_commit", lambda client, plan, yes: commits.append((plan.plan_id, yes)) or True)
    return commits


def test_installing_goes_through_the_helpers_plan_and_the_usual_question(helper, capsys):
    code, out, _ = run(capsys, "install", "foo")
    assert code == 0 and Client.plans == [{"install_repo": ["foo"]}] and helper == [("p1", False)]
    assert "foo" in out and "Download: 3 MiB" in out
    helper.clear()
    run(capsys, "install", "foo", "-y")
    assert helper == [("p1", True)]


def test_installing_what_is_not_offered_or_already_there_never_asks_the_helper(helper, capsys):
    code, out, err = run(capsys, "install", "have")
    assert code == 2 and "already installed" in err and Client.plans == [] and helper == []


def test_a_system_with_updates_waiting_is_told_to_upgrade_first(helper, capsys):
    Client.refuse = [actions.PlanRefused("PKG_PARTIAL_UPGRADE_RISK: Your system needs to be upgraded first")]
    code, out, _ = run(capsys, "install", "foo")
    assert code == 1 and "cygnus upgrade" in out and helper == []


def test_other_refusals_of_an_install_are_shown_as_they_are(helper, capsys):
    Client.refuse = [actions.PlanRefused("PKG_CONFLICT: foo conflicts with your installed other")]
    code, _, err = run(capsys, "install", "foo")
    assert code == 2 and "PKG_CONFLICT" in err and helper == []


def test_removing_what_something_needs_or_what_is_protected_never_asks_the_helper(helper, capsys):
    system_table = helper  # (the fixture also set up the worker)
    code, _, err = run(capsys, "remove", "glibc")
    assert code == 2 and "protected" in err and Client.plans == [] and system_table == []


def test_a_package_cygnus_did_not_install_is_asked_about_and_yes_does_not_answer_it(helper, monkeypatch, capsys):
    refusal = actions.PlanRefused("these packages were not installed by Cygnus: have; the request must confirm their removal explicitly")
    # --yes answers the password question, never this one: with nothing typed (end of input) the answer is no
    Client.refuse = [refusal]

    def end_of_input(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", end_of_input)
    code, out, _ = run(capsys, "remove", "held", "-y")
    assert code == 1 and "cancelled" in out and "--not-installed-by-cygnus" in out
    assert Client.plans == [{"remove": ["held"], "confirm_not_installed_by_cygnus": False}] and helper == []


def test_typing_yes_to_that_question_plans_again_with_the_confirmation(helper, monkeypatch, capsys):
    Client.refuse = [actions.PlanRefused("these packages were not installed by Cygnus: held")]
    answers = iter(["y"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    code, out, _ = run(capsys, "remove", "held")
    assert code == 0
    assert Client.plans == [{"remove": ["held"], "confirm_not_installed_by_cygnus": False},
                            {"remove": ["held"], "confirm_not_installed_by_cygnus": True}] and helper == [("p1", False)]


def test_typing_no_to_that_question_stops(helper, monkeypatch, capsys):
    Client.refuse = [actions.PlanRefused("these packages were not installed by Cygnus: held")]
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    code, out, _ = run(capsys, "remove", "held")
    assert code == 1 and len(Client.plans) == 1 and helper == []


def test_the_flag_answers_it_in_advance(helper, monkeypatch, capsys):
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("nothing is asked when the flag is given"))
    code, _, _ = run(capsys, "remove", "held", "--not-installed-by-cygnus")
    assert code == 0 and Client.plans == [{"remove": ["held"], "confirm_not_installed_by_cygnus": True}]


def test_another_refusal_of_a_removal_is_shown_not_turned_into_the_question(helper, monkeypatch, capsys):
    Client.refuse = [actions.PlanRefused("protected system packages cannot be removed with Cygnus: held")]
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("not this question"))
    code, _, err = run(capsys, "remove", "held")
    assert code == 2 and "protected" in err and helper == []
