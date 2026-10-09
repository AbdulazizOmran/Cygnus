"""The AI parts of the pages are not driven by a test window; these checks pin down the rules their logic has to keep (round 11):
a typed key is read in one place only, a cancelled dialog keeps nothing, the switch never touches the key, errors are shown where they
can be seen, and the page shows what is saved."""

import re
from pathlib import Path

QML = Path(__file__).resolve().parent.parent / "cygnus/gui/qml"
SETTINGS = (QML / "SettingsPage.qml").read_text()
APP = (QML / "AppPage.qml").read_text()


def _function(text: str, name: str) -> str:
    start = text.index(f"    function {name}(")
    end = text.index("\n    }\n", start)
    return text[start:end]


def test_a_key_is_handed_to_the_backend_from_the_dialogs_save_and_nowhere_else():
    assert SETTINGS.count("backend.aiSetKey(") == 1 and "backend.aiSetKey(" in _function(SETTINGS, "saveAiDialog")
    reads = [m.start() for m in re.finditer(r"aiKey\.text(?!\s*=[^=])", SETTINGS)]  # reads, not assignments
    save = SETTINGS.index("    function saveAiDialog(")
    save_end = SETTINGS.index("\n    }\n", save)
    assert reads and all(save < r < save_end for r in reads)


def test_the_switch_and_the_assistant_choice_never_carry_a_key():
    switch = SETTINGS[SETTINGS.index("id: aiSwitch"):SETTINGS.index("id: aiProvider")]
    assert "aiKey" not in switch and "changeAi(" in switch
    assert "aiKey" not in _function(SETTINGS, "changeAi") and "aiKey" not in _function(SETTINGS, "pickProvider")


def test_a_closed_dialog_keeps_no_typed_key_and_goes_back_to_the_saved_assistant():
    dialog = SETTINGS[SETTINGS.index("id: aiDialog"):SETTINGS.index("id: partsDialog")]
    closed = dialog[dialog.index("onClosed:"):dialog.index("customFooterActions")]
    assert 'aiKey.text = ""' in closed and "syncProviderChoice()" in closed and 'aiPending = ""' in closed


def test_the_dialog_closes_only_when_everything_was_saved_and_says_what_went_wrong_inside_itself():
    save = _function(SETTINGS, "saveAiDialog")
    assert save.count("aiDialog.close()") == 2  # after the settings alone, and after the key as well
    failed = save[save.index("if (!r.ok) {"):save.index("aiPending = \"\"")]
    assert "aiDialogError" in failed and "close()" not in failed
    assert "InlineMessage" in SETTINGS[SETTINGS.index("id: aiDialog"):SETTINGS.index("id: partsDialog")]


def test_the_page_shows_the_saved_assistant_and_removes_the_key_of_that_one():
    assert "readonly property string aiProviderId: aiState ? aiState.provider" in SETTINGS
    assert "backend.aiClearKey(aiProviderId)" in SETTINGS


def test_a_failed_suggestion_is_shown_inside_the_window_that_covers_the_page():
    act = _function(APP, "actOnSuggestion")
    failed = act[act.index("if (!r.ok) {"):]
    assert "needsError" in failed and "showError" not in failed.split("needsDialog.close()")[0]
    dialog = APP[APP.index("id: needsDialog"):APP.index("id: fixDialog")]
    assert "needsError" in dialog and "InlineMessage" in dialog and "onClosed: page.needsError" in dialog


def test_nothing_in_the_suggestions_window_opens_an_address_that_did_not_come_from_a_fetched_page():
    opened = re.findall(r"Qt\.openUrlExternally\(([^)]*)\)", APP)
    assert sorted(opened) == ["modelData.citation_url", "page.fixPlan.urls[0]"]  # the page it came from; a store page the helper-side plan re-checked
