"""Where the cache cleaning and the removal of everything no longer needed are used: the GUI service and plan, the terminal, and the page."""

import builtins
import re

import pytest

from cygnus.cli import main as cli
from cygnus.core import cacheclean, packages
from cygnus.core.backends import pacman as pm
from cygnus.core.privilege import Plan
from cygnus.gui import fixes, service
from cygnus.helper import actions
from tests.test_qml_text import QML, _function

CONFIG = pm.PacmanConfig(arch=("x86_64",), dbpath="/nonexistent", cachedirs=(), gpgdir="", repos=(), servers={}, hold=("held",))
SHOWN = {"files": 6, "bytes": 3 * 2**20, "packages": 3, "looked_at": 40, "examples": ["foo 1.0-1", "bar 2-1", "baz 3-1"], "folders": ["/var/cache/pacman/pkg"],
         "problems": [], "keep": 2, "uninstalled": False}


class Client:
    calls: list = []
    plans: list = []

    def plan_cache_clean(self, keep, uninstalled):
        Client.calls.append((keep, uninstalled))
        return Plan("c1", {"files": 6, "bytes": 3 * 2**20, "packages": 3, "keep": keep, "uninstalled": uninstalled, "folders": ["/var/cache/pacman/pkg"],
                           "examples": ["foo 1.0-1"], "problems": []}, "Delete 6 cached package files (3 MiB)")

    def plan_packages(self, **kw):
        Client.plans.append(kw)
        return Plan("p1", {"install": [], "upgrade": [], "remove": [{"name": n, "reason": "requested"} for n in kw["remove"]]}, "Remove " + ", ".join(kw["remove"]))


@pytest.fixture
def client(monkeypatch):
    Client.calls, Client.plans = [], []
    monkeypatch.setattr(fixes, "HelperClient", Client)
    monkeypatch.setattr("cygnus.core.privilege.HelperClient", Client)
    monkeypatch.setattr(pm, "read_config", lambda: CONFIG)
    yield
    fixes._PENDING.clear()


def checking_preview(cfg, keep, uninstalled):
    """The real preview's first step (it refuses a request that is not the two choices) with a canned answer after it."""
    keep, uninstalled = cacheclean.check_request(keep, uninstalled)
    return {**SHOWN, "keep": keep, "uninstalled": uninstalled}


# -- the GUI's plan and preview ------------------------------------------------------------------------------------------------------------

def test_the_gui_plan_sends_only_the_two_choices_and_shows_what_the_helper_chose(client):
    out = fixes.plan_cache_clean(3, True)
    assert Client.calls == [(3, True)] and out["token"] in fixes._PENDING and out["action"] == "Delete the files"
    assert out["cache"] == {"files": 6, "bytes": 3 * 2**20, "packages": 3, "keep": 3, "uninstalled": True, "folders": ["/var/cache/pacman/pkg"],
                            "examples": ["foo 1.0-1"], "problems": []}
    assert "installed version of every package always stays" in out["security_note"] and out["message"].startswith("Delete 6")


@pytest.mark.parametrize("keep, uninstalled", [(-1, False), (11, False), (True, False), ("2", False), (None, True), (2, "yes"), (2, 1)])
def test_a_request_that_is_not_the_two_choices_never_reaches_the_helper(monkeypatch, keep, uninstalled):
    monkeypatch.setattr(fixes, "HelperClient", lambda: pytest.fail("the helper must not be asked"))
    with pytest.raises(fixes.CygnusError):
        fixes.plan_cache_clean(keep, uninstalled)


def test_the_preview_is_the_cleaners_and_a_refusal_is_a_message(monkeypatch):
    monkeypatch.setattr(pm, "read_config", lambda: CONFIG)
    monkeypatch.setattr(cacheclean, "preview", checking_preview)
    assert service.cache_preview(1, True)["keep"] == 1
    with pytest.raises(fixes.CygnusError, match="from 0 to 10"):
        service.cache_preview(99, False)


def test_removing_a_whole_batch_goes_through_one_plan_and_one_question(client, monkeypatch):
    from tests.test_packages_core import Worker, info

    table = {f"p{i}": info(f"p{i}", installed=True, installed_version="1.0-1") for i in range(60)}
    monkeypatch.setattr(pm, "run_worker", Worker(table))
    out = fixes.plan_remove_by_name(list(table))  # more than the 20 of an install: everything no longer needed can be one batch
    assert Client.plans[0]["remove"] == list(table) and len(out["removals"]) == 60


# -- the terminal ---------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def terminal(client, monkeypatch):
    commits = []
    monkeypatch.setattr(cli, "_helper_commit", lambda c, plan, yes: commits.append((plan.plan_id, yes)) or True)
    monkeypatch.setattr(cacheclean, "preview", checking_preview)
    return commits


def test_cache_only_shows_what_would_go(terminal, capsys):
    assert cli.main(["packages", "cache", "--keep", "1"]) == 0
    out = capsys.readouterr().out
    assert "6 cached files (3.0 MiB) of 3 package versions would go" in out and "the 1 newest older one." in out and "foo 1.0-1" in out
    assert Client.calls == [] and terminal == []


def test_clean_shows_it_then_asks_the_helper_for_exactly_the_two_choices_and_the_usual_question(terminal, capsys):
    assert cli.main(["packages", "clean", "--keep", "3", "--uninstalled"]) == 0
    assert Client.calls == [(3, True)] and terminal == [("c1", False)]
    assert "lose all their cached versions" in capsys.readouterr().out
    assert cli.main(["packages", "clean", "-y"]) == 0 and terminal[-1] == ("c1", True) and Client.calls[-1] == (2, False)


def test_clean_with_nothing_to_remove_does_not_ask_anyone(terminal, monkeypatch, capsys):
    monkeypatch.setattr(cacheclean, "preview", lambda cfg, keep, uninstalled: {**SHOWN, "files": 0, "bytes": 0, "packages": 0, "examples": []})
    assert cli.main(["packages", "clean"]) == 0 and Client.calls == [] and terminal == []
    assert "0 cached files" in capsys.readouterr().out


@pytest.mark.parametrize("flags", [["--keep", "11"], ["--keep", "-1"]])
def test_a_keep_out_of_range_is_an_error_not_a_request(terminal, capsys, flags):
    for command in ("cache", "clean"):
        assert cli.main(["packages", command, *flags]) == 2 and "from 0 to 10" in capsys.readouterr().err
    assert Client.calls == [] and terminal == []


def installed_fixture(monkeypatch, rows, managed=None):
    monkeypatch.setattr(packages, "installed", lambda **kw: {"packages": rows, "updates": 0, "lists": {"refreshed": None}})
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: managed or {})


def row(name, unneeded=True, protected=False, size=1000, odd_name=False):
    return {"name": name, "version": "1-1", "desc": "", "reason": "dependency", "size": size, "installed": 1, "repo": "extra", "update": False,
            "unneeded": unneeded, "protected": protected, "odd_name": odd_name}


def test_orphans_lists_what_nothing_needs_and_leaves_out_what_is_protected_or_an_application(terminal, monkeypatch, capsys):
    installed_fixture(monkeypatch, [row("a"), row("b", unneeded=False), row("held", protected=True), row("app"), row("c", size=2 * 2**20)], managed={"app": "My App"})
    assert cli.main(["packages", "orphans"]) == 0
    out = capsys.readouterr().out
    listed = [line.split()[0] for line in out.splitlines() if line and not line.startswith(("(", "2 packages"))]
    assert listed == ["a", "c"]  # not "b" (still needed), "held" (protected) or "app" (an application of Cygnus's)
    assert "2 packages were installed for another package" in out and "remove --unneeded" in out


def test_remove_unneeded_removes_exactly_those_through_the_usual_question(terminal, monkeypatch, capsys):
    from tests.test_packages_core import Worker, info

    installed_fixture(monkeypatch, [row("a"), row("b"), row("keep", unneeded=False), row("held", protected=True), row("app")], managed={"app": "My App"})
    monkeypatch.setattr(pm, "run_worker", Worker({n: info(n, installed=True, installed_version="1-1") for n in ("a", "b")}))
    answers = iter(["y"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    # Cygnus did not install them: the helper asks for the explicit confirmation, which --yes never gives
    seen = []

    def plan_packages(self, **kw):
        seen.append(kw)
        if not kw["confirm_not_installed_by_cygnus"]:
            raise actions.PlanRefused("these packages were not installed by Cygnus: a, b")
        return Plan("p1", {"install": [], "upgrade": [], "remove": [{"name": n, "reason": "requested"} for n in kw["remove"]]}, "Remove a, b")

    monkeypatch.setattr(Client, "plan_packages", plan_packages)
    assert cli.main(["packages", "remove", "--unneeded", "-y"]) == 0
    assert [kw["remove"] for kw in seen] == [["a", "b"], ["a", "b"]] and [kw["confirm_not_installed_by_cygnus"] for kw in seen] == [False, True]
    out = capsys.readouterr().out
    assert "a, b" in out and "keep" not in out.split("nothing needs them now:")[1].split("\n")[0] and terminal == [("p1", True)]


def test_remove_needs_names_or_unneeded_and_not_both(terminal, monkeypatch, capsys):
    installed_fixture(monkeypatch, [row("a")])
    assert cli.main(["packages", "remove"]) == 2 and "or use --unneeded" in capsys.readouterr().err
    assert cli.main(["packages", "remove", "foo", "--unneeded"]) == 2 and "not both" in capsys.readouterr().err
    assert Client.plans == [] and terminal == []


def test_remove_unneeded_with_nothing_left_says_so(terminal, monkeypatch, capsys):
    installed_fixture(monkeypatch, [row("a", unneeded=False), row("held", protected=True)])
    assert cli.main(["packages", "remove", "--unneeded"]) == 0 and "Nothing is left over" in capsys.readouterr().out and Client.plans == []


# -- the page ---------------------------------------------------------------------------------------------------------------------------------

def _code(text: str) -> str:
    return "\n".join(re.sub(r"\s//(?![/:]).*$", "", line) if not line.lstrip().startswith("//") else "" for line in text.splitlines())


PAGE = _code((QML / "PackagesPage.qml").read_text())


def test_packages_can_only_be_ticked_on_the_no_longer_needed_list_and_the_ticks_do_not_outlive_it():
    assert "readonly property bool selecting: mode === \"installed\" && filterIndex === 3" in PAGE
    filters = PAGE[PAGE.index("readonly property var filters"):PAGE.index("function installedShown()")]
    assert filters.split("{")[4].lstrip().startswith('label: qsTr("No longer needed")')  # index 3 is the no-longer-needed filter (the 0th "{" opens the array)
    assert "onSelectingChanged: picked = ({})" in PAGE
    assert "visible: page.selecting" in PAGE[PAGE.index("QQC2.CheckBox {"):][:200]


def test_the_no_longer_needed_list_leaves_out_what_could_not_be_removed_from_here():
    filters = PAGE[PAGE.index("readonly property var filters"):PAGE.index("function installedShown()")]
    assert "test: p => !!p.unneeded && !p.protected && !p.managed && !p.odd_name" in filters  # not a protected package, an application of Cygnus's, or an odd name


def test_what_is_sent_is_the_ticked_packages_that_are_still_no_longer_needed():
    assert "return shown.filter(p => !!p.unneeded && !p.protected && !p.managed && !p.odd_name && !!picked[p.name]).map(p => p.name);" in PAGE
    body = _function(PAGE, "    function planRemoveSelected(confirmedNotMine)")
    assert "const names = pickedNames();" in body and "backend.planRemovePackagesByName(JSON.stringify(names), confirmedNotMine)" in body
    assert "commitFix" not in body and body.index("confirmDialog.open()") > body.index("backend.planRemovePackagesByName")


def test_the_cache_is_only_cleaned_through_the_window_with_the_plan_taken_before_it_closes():
    assert PAGE.count("backend.commitFix(") == 1
    run = _function(PAGE, "    function runPlan(plan)")
    assert "backend.commitFix(plan.token)" in run and run.index("confirmPlan = null") < run.index("backend.commitFix")
    window = PAGE[PAGE.index("id: confirmDialog"):PAGE.index("id: warnDialog")]
    action = window[window.index('objectName: "listConfirmAction"'):]
    assert action.index("const plan = page.confirmPlan;") < action.index("confirmDialog.close();") < action.index("page.runPlan(plan);")
    assert PAGE.count("page.runPlan(") == 1
    plan = _function(PAGE, "    function planCache()")
    assert "backend.planCacheClean(cacheKeep.value, cacheUninstalled.checked)" in plan and "commitFix" not in plan


def test_the_question_about_packages_cygnus_did_not_install_needs_its_own_tick_here_too():
    body = _function(PAGE, "    function planRemoveSelected(confirmedNotMine)")
    refused = body[body.index("if (r.result.refused)"):body.index("confirmPlan = r.result;")]
    assert "warnTicked = false" in refused and "warnDialog.open()" in refused
    window = PAGE[PAGE.index("id: warnDialog"):]
    assert "enabled: page.warnTicked" in window and "page.planRemoveSelected(true)" in window and "onClosed: page.warnTicked = false" in window
    assert PAGE.count("planRemoveSelected(true)") == 1 and PAGE.count("planRemoveSelected(false)") == 1
    assert "property bool warnTicked: false" in PAGE


def test_a_cache_preview_answered_late_never_replaces_a_newer_one_and_the_settings_come_from_the_window():
    body = _function(PAGE, "    function previewCache()")
    assert body.index("const seq = ++cacheSeq;") < body.index("backend.cachePreview(cacheKeep.value, cacheUninstalled.checked)")
    answer = body[body.index("backend.cachePreview"):]
    assert answer.index("if (seq !== cacheSeq)") < answer.index("cacheShown = r.result")
    assert "from: 0" in PAGE and "to: 10" in PAGE and "onValueChanged: if (cacheDialog.opened)\n                        page.previewCache()" in PAGE
    assert "onToggled: page.previewCache()" in PAGE


def test_the_clean_button_waits_for_a_preview_that_says_something_would_go():
    action = PAGE[PAGE.index('objectName: "cacheAction"'):][:400]
    assert "page.cacheShown && page.cacheShown.files > 0" in action and 'page.busyText === ""' in action


def test_text_in_the_banners_is_made_safe():
    for line in PAGE.splitlines():
        m = re.match(r"\s*(?:page\.)?(doneText|errorText) = (.+);", line)
        if m:
            assert m.group(2) == '""' or m.group(2).startswith("root.plain("), line


# -- what the reviewers found in round 15 -----------------------------------------------------------------------------------------------

def test_a_blank_line_of_progress_never_ends_busy():
    for page in (PAGE, _code((QML / "PackagePage.qml").read_text())):
        run = _function(page, "    function runPlan(plan)")
        handler = run[run.index("function (line) {"):]
        assert 'if (line.trim() !== "")' in handler and "page.busyText = line;" in handler  # busyText is what keeps a second change from starting


def test_a_cancelled_change_keeps_the_ticks_and_a_finished_one_drops_only_what_is_gone():
    assert "page.picked = ({})" not in PAGE[PAGE.index("function onPackagesChanged()"):][:200]
    load = _function(PAGE, "    function loadInstalled()")
    assert "if (picked[p.name] && !!p.unneeded)" in load and "picked = still;" in load


def test_what_is_counted_and_what_is_sent_are_the_ticked_packages_on_the_screen():
    assert "return shown.filter(" in _function(PAGE, "    function pickedNames()")
    assert "shown.forEach(p => next[p.name] = true);" in _function(PAGE, "    function pickAll()")  # select all and the count use the same list


def test_the_page_does_not_let_more_than_the_helper_accepts_be_sent():
    limit = int(re.search(r"readonly property int batchLimit: (\d+)", PAGE).group(1))
    assert limit == packages.MAX_BATCH == 200
    button = PAGE[PAGE.index('text: page.pickedNames().length > page.batchLimit'):][:520]
    assert "page.pickedNames().length <= page.batchLimit" in button and "at most " in button
    assert "names.length > batchLimit" in _function(PAGE, "    function planRemoveSelected(confirmedNotMine)")


def test_the_cache_settings_cannot_leave_the_range_the_helper_takes():
    spin = PAGE[PAGE.index("id: cacheKeep"):][:260]
    assert re.search(r"from: 0\s+to: 10\s+value: 2", spin) and cacheclean.MAX_KEEP == 10 and cacheclean.DEFAULT_KEEP == 2


def test_the_reason_a_button_is_disabled_is_a_label_that_is_shown_when_there_is_one():
    one = _code((QML / "PackagePage.qml").read_text())
    label = one[one.index("text: !page.info ? \"\" : page.info.installed ? (page.info.blocked.remove"):]
    block = one[:one.index(label[:60])][-400:]
    assert "visible: text !== \"\"" in block


@pytest.mark.parametrize("answer, expected", [("y", True), ("yes", True), ("YES", True), (" y ", True), ("n", False), ("", False), ("ye", False), ("yeah", False)])
def test_only_an_explicit_yes_counts_and_the_end_of_input_is_a_no(monkeypatch, answer, expected):
    monkeypatch.setattr(builtins, "input", lambda prompt="": answer)
    assert cli._answer_yes("?") is expected

    def end_of_input(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", end_of_input)
    assert cli._answer_yes("?") is False


def test_a_package_with_a_name_the_helper_does_not_take_is_left_out_of_a_batch_and_said(terminal, monkeypatch, capsys):
    from tests.test_packages_core import Worker, info

    installed_fixture(monkeypatch, [row("a"), row("ttf-MSFonts", odd_name=True), row("b")])
    monkeypatch.setattr(pm, "run_worker", Worker({n: info(n, installed=True, installed_version="1-1") for n in ("a", "b")}))
    assert cli.main(["packages", "orphans"]) == 0
    out = capsys.readouterr().out
    assert "2 packages were installed" in out and "Left out" in out and "ttf-MSFonts" in out
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    Client.plans = []

    def plan_packages(self, **kw):
        Client.plans.append(kw)
        return Plan("p1", {"install": [], "upgrade": [], "remove": []}, "Remove")

    monkeypatch.setattr(Client, "plan_packages", plan_packages)
    assert cli.main(["packages", "remove", "--unneeded", "--not-installed-by-cygnus"]) == 0
    assert Client.plans[0]["remove"] == ["a", "b"] and "ttf-MSFonts" in capsys.readouterr().out  # the batch goes without it, and says so


def test_the_installed_list_marks_names_the_helper_does_not_take(monkeypatch):
    monkeypatch.setattr(packages, "managed_packages", lambda registry_path=None: {})
    rows = [{"name": "ncdu", "update": False}, {"name": "ttf-MSFonts", "update": False}]
    out = packages.installed(config=CONFIG, worker=lambda c, r: {"ok": True, "packages": rows})
    assert {p["name"]: p["odd_name"] for p in out["packages"]} == {"ncdu": False, "ttf-MSFonts": True}
