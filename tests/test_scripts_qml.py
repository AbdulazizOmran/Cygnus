"""The screens for running install scripts: nothing is ticked by default or carried from one program to the next, a box can only be
ticked while the script's full text is on screen, and what is sent is exactly what was ticked. (Static rules over the QML: there is
no live-window driver here.)"""

import re
from pathlib import Path

from tests.test_qml_text import QML, _function



def _code(text: str) -> str:
    """The QML without its // comments (a guard that is only mentioned in a comment is not a guard)."""
    return "\n".join(re.sub(r"\s//(?![/:]).*$", "", line) if not line.lstrip().startswith("//") else "" for line in text.splitlines())


INSTALL = _code((QML / "InstallPage.qml").read_text())
APP = _code((QML / "AppPage.qml").read_text())
MAIN = _code((QML / "Main.qml").read_text())


def test_nothing_is_ticked_for_a_new_file_and_nothing_queued_is_carried_over():
    body = _function(INSTALL, "    function analyse()")
    guard = "if ((!fileUrl && !appSpec) || installing)\n            return;"
    assert body.index(guard) < 120  # the one early return of analyse() comes first and is the busy guard: nothing returns before the resets
    after_guard = body[body.index(guard) + len(guard):]
    for reset in ("scriptChoice = ({})", "vendorShown = false", "pendingAdmin = []", "runVendor.checked = false"):
        assert reset in after_guard, reset
    assert "property var scriptChoice: ({})" in INSTALL  # and the starting state is nothing


def test_only_install_scripts_that_were_ticked_are_sent_and_with_the_checksum_that_was_shown():
    body = _function(INSTALL, "    function chosenScripts()")
    assert 'i.phase === "install"' in body and "scriptChoice[i.name]" in body and "sha256: i.sha256" in body and "as: scriptChoice[i.name]" in body
    install = _function(INSTALL, "    function install()")
    assert "page.archiveMain, JSON.stringify(page.chosenScripts())" in install
    assert "acceptScripts.checked, page.vendorChosen" in install  # the deb/rpm choice is the box ticked while the text is shown, nothing else


def test_a_box_can_only_be_ticked_while_the_text_is_on_screen():
    # the archive's boxes live inside the list that is only filled while the scripts are shown ...
    repeater = INSTALL[INSTALL.index("model: page.scriptsShown && page.plan && page.plan.scripts ? Object.keys(page.plan.scripts.texts) : []"):]
    assert "id: runBox" in repeater[:repeater.index("RowLayout {\n                        Layout.fillWidth: true\n                        visible: !!(page.plan && page.plan.installable_after_review)")]
    # ... and the deb/rpm box is disabled until they are shown
    vendor_box = INSTALL[INSTALL.index("id: runVendor"):][:400]
    assert "enabled: page.vendorShown" in vendor_box
    for box in ("id: runBox", "id: runVendor"):
        assert INSTALL.count(box) == 1


def test_the_boxes_and_names_from_the_package_are_shown_as_plain_text():
    for box_id in ("runBox", "runVendor"):
        block = INSTALL[INSTALL.index(f"id: {box_id}"):][:900]
        assert "contentItem: QQC2.Label" in block and "textFormat: Text.PlainText" in block  # a name could otherwise be read as markup
    block = APP[APP.index("id: removalBox"):][:900]
    assert "textFormat: Text.PlainText" in block


def test_a_cancelled_confirmation_drops_the_scripts_that_were_waiting():
    closed = INSTALL[INSTALL.index("onClosed: {\n            if (!confirmed)"):][:600]
    assert "page.pendingAdmin = []" in closed
    queue = _function(INSTALL, "    function nextAdminScript()")
    assert "pendingAdmin = pendingAdmin.slice(1)" in queue and "backend.planScriptAdmin(s.installationId, s.name, s.sha256)" in queue
    assert 'return "continue"' in queue  # the program was already announced as installed: no second "installed" banner
    failed = queue[queue.index("if (!p.ok)"):][:200]
    assert "pendingAdmin = []" in failed


def test_a_script_that_failed_is_reported_and_never_hidden_behind_the_installed_banner():
    body = _function(INSTALL, "    function reportScripts(results)")
    assert "failed.length" in body and "showAttention" in body and "did not finish" in body
    finish = _function(INSTALL, "    function finishArchive(r)")
    assert finish.index("page.reportScripts(") < finish.index("page.runAdminScripts(")  # what ran as you is told first, then the helper is asked


def test_the_confirmation_names_what_it_is_for():
    assert "confirmPlan.dialog_title" in INSTALL and "confirmPlan.action" in INSTALL


def test_removal_scripts_start_unticked_for_every_program_and_the_text_comes_with_the_box():
    body = _function(APP, "    function openUninstall()")
    assert body.count("removalChoice = ({})") == 2 and body.count("removalScripts = []") == 2  # for every kind of program, not only archives
    plain = body[:body.index("if (busyText")]
    assert plain.index("removalChoice = ({})") < plain.index("uninstallDialog.open()")
    assert body.index("removalChoice = ({})", len(plain)) < body.rindex("uninstallDialog.open()")
    assert 'x.phase === "remove" && x.state === "ok"' in body  # only removal scripts that are still exactly as recorded are offered
    row = APP[APP.index("id: removalRow"):][:1500]
    assert row.index("QQC2.TextArea") < row.index("id: removalBox")  # the text above the box
    uninstall = APP[APP.index("const chosen = page.chosenRemovalScripts();"):][:500]
    assert "chosen.length ? backend.uninstallArchive(" in uninstall and ": backend.uninstall(page.installationId, deleteFile.visible && deleteFile.checked)" in uninstall


def test_the_scripts_dialog_runs_nothing_but_by_a_button_and_only_for_scripts_that_are_unchanged():
    dialog = APP[APP.index("id: scriptsDialog"):][:3800]
    assert 'visible: scriptRow.modelData.state === "ok"' in dialog
    assert "page.runScript(scriptRow.modelData.name, scriptRow.modelData.sha256, false)" in dialog
    assert "page.runScript(scriptRow.modelData.name, scriptRow.modelData.sha256, true)" in dialog
    assert len(re.findall(r"backend\.runArchiveScript\(", APP)) == 1 and len(re.findall(r"backend\.planScriptAdmin\(", APP)) == 1
    run = _function(APP, "    function runScript(name, sha256, admin)")
    assert run.index("if (admin)") < run.index("backend.runArchiveScript(") and "fixDialog.open()" in run  # administrator: the usual confirmation first


def test_a_late_answer_cannot_open_a_confirmation_over_another_file_that_was_opened_since():
    body = _function(INSTALL, "    function nextAdminScript()")
    before_ask = body[:body.index("page.ask(")]
    assert "const seq = analysisSeq" in before_ask
    answer = body[body.index("function (p)"):]
    assert answer.index("if (seq !== analysisSeq)") < answer.index("confirmPlan = p.result")


def test_the_choice_shown_is_always_the_choice_that_is_stored():
    howbox = INSTALL[INSTALL.index("id: howBox"):][:400]
    assert 'currentIndex: page.scriptChoice[scriptView.modelData] === "admin" ? 1 : 0' in howbox  # not left at "as me" when it is "admin"
    assert "onActivated: page.setScriptChoice(" in howbox


def test_the_scripts_that_cannot_be_offered_are_listed_with_why():
    assert "page.plan.scripts.not_runnable" in INSTALL


def test_hiding_the_text_takes_the_choice_back_so_nothing_that_cannot_be_seen_runs():
    assert "onScriptsShownChanged: {\n        if (!scriptsShown)\n            scriptChoice = ({});" in INSTALL
    assert "onVendorShownChanged: {\n        if (!vendorShown)\n            runVendor.checked = false;" in INSTALL
    assert "readonly property bool vendorChosen: runVendor.checked && vendorShown" in INSTALL
    chosen = _function(INSTALL, "    function chosenScripts()")
    assert chosen.index("if (!scriptsShown)\n            return [];") < chosen.index("items.filter")  # even a stale choice sends nothing
    assert "page.vendorChosen" in _function(INSTALL, "    function install()") and "page.vendorChosen))) &&" in INSTALL  # the button, too
    assert "runVendor.checked" not in INSTALL.replace("runVendor.checked && vendorShown", "").replace("runVendor.checked = false", "").replace("checked: runVendor.checked", "")


def test_what_will_run_is_written_next_to_the_install_button():
    summary = _function(INSTALL, "    function scriptSummary()")
    assert "After installing, Cygnus will run: " in summary and "as administrator, asking for your password" in summary and "vendorChosen" in summary
    label = INSTALL[INSTALL.index("text: page.scriptSummary()") - 400:INSTALL.index("text: page.scriptSummary()") + 60]
    assert "textFormat: Text.PlainText" in label  # script names come from the archive


def test_a_cancelled_or_failed_queue_says_which_scripts_were_not_run():
    closed = INSTALL[INSTALL.index("onClosed: {\n            if (!confirmed)"):][:700]
    assert closed.index("Not run (cancelled)") < closed.index("page.pendingAdmin = []")
    queue = _function(INSTALL, "    function nextAdminScript()")
    assert 'page.log += "Not run: "' in queue and "could not be run as administrator" in queue


def test_removal_scripts_are_not_run_from_the_install_scripts_dialog():
    dialog = APP[APP.index("id: scriptsDialog"):][:4200]
    assert 'visible: scriptRow.modelData.state === "ok" && scriptRow.modelData.phase === "install"' in dialog  # the buttons
    assert 'scriptRow.modelData.phase !== "install"' in dialog and "It is offered when you remove the program." in dialog


def test_a_script_that_did_not_finish_during_a_removal_is_said_before_the_page_goes():
    callback = APP[APP.index("const chosen = page.chosenRemovalScripts();"):][:900]
    assert callback.index("r.result.attention") < callback.index("root.showPage(appsPage)") and "root.notify(" in callback


def test_the_main_window_says_what_only_it_can_say_for_a_page_that_was_left():
    late = _function(MAIN, "    function lateAnswer(what, r)")
    assert "result.late_attention" in late and "result.attention" in late and "notify(" in late
