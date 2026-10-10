"""The Packages screens: a late search answer never replaces a newer one, nothing is installed or removed without the confirmation
window, the question about a package Cygnus did not install needs its own tick, and what the page asks the helper for is the name the
backend reported, never what was typed. (Static rules over the QML: there is no live-window driver here.)"""

import re

from tests.test_qml_text import QML, _function


def _code(text: str) -> str:
    """The QML without its // comments (a guard that is only mentioned in a comment is not a guard)."""
    return "\n".join(re.sub(r"\s//(?![/:]).*$", "", line) if not line.lstrip().startswith("//") else "" for line in text.splitlines())


LIST = _code((QML / "PackagesPage.qml").read_text())
ONE = _code((QML / "PackagePage.qml").read_text())
MAIN = _code((QML / "Main.qml").read_text())


def test_a_search_answered_late_never_replaces_a_newer_one():
    body = _function(LIST, "    function search()")
    assert "const seq = ++searchSeq;" in body
    answer = body[body.index("backend.packagesSearch"):]
    assert answer.index("if (seq !== searchSeq)") < answer.index("results = r.result.results")  # checked before anything is taken from the answer
    assert "return;" in answer[answer.index("if (seq !== searchSeq)"):][:80]


def test_a_search_too_short_asks_nothing_and_clears_the_results():
    body = _function(LIST, "    function search()")
    short = body[body.index("if (text.length < 2)"):body.index("searching = true;")]
    assert "results = []" in short and "return;" in short and "backend." not in short
    assert body.index("const seq = ++searchSeq;") < body.index("if (text.length < 2)")  # so an answer to an older, longer search is dropped too


def test_typing_waits_a_moment_before_searching():
    assert "onTextChanged: debounce.restart()" in LIST
    assert "autoAccept: false" in LIST  # Kirigami's SearchField otherwise emits accepted() at every key, and that searches at once
    interval = int(re.search(r"id: debounce\s+interval: (\d+)", LIST).group(1))
    assert 250 <= interval <= 1000


def test_the_search_only_runs_on_the_available_tab():
    assert 'if (mode !== "available")\n            return;' in _function(LIST, "    function search()")[:200]


def test_what_is_asked_of_the_helper_is_the_name_the_backend_reported_never_what_was_typed():
    assert "backend.planInstallPackages(JSON.stringify([info.name]))" in ONE
    assert "backend.planRemovePackagesByName(JSON.stringify([info.name])" in ONE
    assert "searchField" not in ONE and "query" not in ONE


def test_the_details_page_opens_the_package_the_list_reported():
    body = _function(LIST, "    function open(p)")
    assert "packageName: p.name" in body
    assert 'repo: mode === "available" ? p.repo : ""' in body


def test_nothing_is_committed_except_from_the_confirmation_window():
    assert ONE.count("backend.commitFix(") == 1
    run = _function(ONE, "    function runPlan(plan)")
    assert "backend.commitFix(plan.token)" in run
    assert run.index("if (busyText !== \"\" || !plan)") < run.index("backend.commitFix")  # a second click uses no plan twice
    assert run.index("confirmPlan = null") < run.index("backend.commitFix")
    assert ONE.count("page.runPlan(") == 1
    window = ONE[ONE.index("id: confirmDialog"):ONE.index("id: warnDialog")]
    assert "page.runPlan(plan)" in window and "enabled: !!page.confirmPlan" in window


def test_the_plan_is_taken_before_the_window_is_closed():
    """Closing the window clears the plan (onClosed); with no closing animation that happens inside close(), so a plan read after it
    would be gone and Install would silently do nothing."""
    action = ONE[ONE.index('objectName: "confirmAction"'):ONE.index("id: warnDialog")]
    assert action.index("const plan = page.confirmPlan;") < action.index("confirmDialog.close();") < action.index("page.runPlan(plan);")
    assert "onClosed: if (page.busyText === \"\")\n            page.confirmPlan = null" in ONE


def test_a_plan_is_shown_before_it_can_be_run():
    for name in ("planInstall", "planRemove"):
        body = _function(ONE, f"    function {name}(")  if name == "planRemove" else _function(ONE, f"    function {name}()")
        assert "confirmDialog.open()" in body and "runPlan" not in body and "commitFix" not in body


def test_the_question_about_a_package_cygnus_did_not_install_needs_its_own_tick():
    body = _function(ONE, "    function planRemove(confirmedNotMine)")
    refused = body[body.index("if (r.result.refused)"):body.index("confirmPlan = r.result;")]
    assert "warnTicked = false" in refused and "warnDialog.open()" in refused  # the tick starts clear every time
    window = ONE[ONE.index("id: warnDialog"):]
    assert "enabled: page.warnTicked" in window and "page.planRemove(true)" in window
    assert "onClosed: page.warnTicked = false" in window
    assert "checked: page.warnTicked" in window and "onToggled: page.warnTicked = checked" in window
    assert ONE.count("planRemove(true)") == 1 and ONE.count("planRemove(false)") == 1  # nothing else confirms on the person's behalf
    assert "planRemove(false)" in ONE[ONE.index("text: \"Remove…\""):ONE.index("text: \"Remove…\"") + 200]
    assert "property bool warnTicked: false" in ONE


def test_install_and_remove_are_only_offered_when_the_checks_allow_them():
    install = ONE[ONE.index('text: "Install…"') - 300:ONE.index('text: "Install…"')]
    assert "visible: !!(page.info && !page.info.installed)" in install
    assert "enabled: !!(page.info && !page.info.blocked.install) && page.busyText === \"\"" in install
    remove = ONE[ONE.index('text: "Remove…"') - 300:ONE.index('text: "Remove…"')]
    assert "visible: !!(page.info && page.info.installed)" in remove
    assert "enabled: !!(page.info && !page.info.blocked.remove) && page.busyText === \"\"" in remove


def test_the_reason_a_button_is_disabled_is_shown():
    assert "page.info.blocked.remove" in ONE and "page.info.blocked.install" in ONE
    assert "Some of what it needs is not available" in ONE


def test_a_partial_upgrade_answer_points_at_the_updates_page():
    body = _function(ONE, "    function planInstall()")
    assert "if (r.result.refused)" in body and "stayText = r.result.text" in body
    assert 'root.openPage("updates")' in ONE and "visible: page.stayText !== \"\"" in ONE


def test_text_that_reaches_a_message_is_made_safe_first():
    for page in (LIST, ONE):
        for line in page.splitlines():
            m = re.match(r"\s*(?:page\.)?errorText = (.+);", line)
            if m:
                assert m.group(1) == '""' or m.group(1).startswith("root.plain("), line
        assert "text: page.errorText" in page


def test_the_list_reloads_when_a_change_to_the_packages_was_made_even_if_the_page_that_asked_was_left():
    """The commit is answered by the backend, not by the page that asked: leaving the package page while it runs must not leave the list
    stale. So the list listens to the backend's own signal (see test_the_backend_says_when_a_change_was_made...)."""
    handler = LIST[LIST.index("target: backend"):][:300]
    assert "function onPackagesChanged()" in handler and "page.loadInstalled()" in handler and "page.search()" in handler
    assert "packagesDirty" not in LIST and "packagesDirty" not in ONE and "packagesDirty" not in MAIN


def test_the_pages_can_be_reached():
    assert 'text: "Packages"' in MAIN and "root.showPage(packagesPage)" in MAIN
    assert "packages: packagesPage" in MAIN and "package: packagePage" in MAIN
    assert "PackagesPage {}" in MAIN and "PackagePage {}" in MAIN


def test_the_page_is_busy_while_it_works_or_a_window_is_open():
    # Main.qml's isBusy() reads these: a link handed to Cygnus must not replace a page in the middle of a change
    for text in (LIST, ONE):
        assert "property string busyText" in text
    assert "property var confirmPlan" in ONE
    busy = _function(MAIN, "    function isBusy(page: var): bool")
    assert "page.busyText !== undefined && page.busyText !== \"\"" in busy
    assert "page.confirmPlan !== undefined && page.confirmPlan !== null" in busy  # an open confirmation window counts


def test_text_shown_in_a_message_that_reads_markup_is_made_safe_first():
    """Kirigami's InlineMessage reads markup: whatever reaches it from data must go through root.plain() (or be a plain literal)."""
    for page in (LIST, ONE):
        for line in page.splitlines():
            m = re.match(r"\s*(?:page\.)?(doneText|errorText|stayText) = (.+);", line)
            if m and m.group(1) != "stayText":
                assert m.group(2) == '""' or m.group(2).startswith("root.plain(") or re.fullmatch(r'"[^"+]*"', m.group(2)), line
    assert "root.plain(page.stayText)" in ONE  # the one that is shown as markup is escaped where it is shown


def test_the_updates_waiting_message_has_a_way_to_the_updates_page():
    block = LIST[LIST.index("visible: page.updatesPending > 0"):][:900]
    assert 'root.openPage("updates")' in block and "Update the whole system" in block


def test_the_filters_only_narrow_the_list_they_never_ask_anything():
    body = _function(LIST, "    function installedShown()")
    assert "backend." not in body and "installedAll.filter(" in body
