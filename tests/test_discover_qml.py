"""Rules the Discover screens (and every page's messages) must keep, read from the QML itself: text that reaches a message is made safe, a message
that can be closed does not depend on a binding closing would remove, and "Show more" continues the list that is on screen."""

import re
from pathlib import Path

import pytest

QML = Path(__file__).resolve().parent.parent / "cygnus/gui/qml"
PAGES = ("DiscoverPage.qml", "DiscoverAppPage.qml")


def _inline_messages(text):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s*Kirigami\.InlineMessage\s*\{\s*$", line):
            depth, j, body = 1, i + 1, []
            while j < len(lines) and depth:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth >= 1:
                    body.append(lines[j])
                j += 1
            yield i + 1, "\n".join(body)


@pytest.mark.parametrize("name", PAGES)
def test_every_error_text_is_made_safe_before_it_reaches_a_message(name):
    """A message reads markup: what goes into errorText (error text from the backend, names from data files) goes through root.plain()."""
    code = "\n".join(re.sub(r"\s//(?![/:]).*$", "", l) for l in (QML / name).read_text().splitlines())
    assignments = re.findall(r"(?:errorText|aurError|refreshError) = (.+?);", code)
    assert assignments
    assert all(a == '""' or a.startswith("root.plain(") for a in assignments), assignments


def test_the_list_of_unreadable_data_files_is_made_safe_before_it_is_shown():
    code = (QML / "DiscoverPage.qml").read_text()
    block = code[code.index("Some application lists could not be read"):][:200]
    assert "root.plain(" in code[code.index("Some application lists could not be read") - 120:code.index("Some application lists could not be read")]
    assert "problems.join" in code[code.index("Some application lists could not be read"):][:300] or "problems.join" in block


def test_a_message_that_can_be_closed_is_never_bound_to_a_condition():
    """Closing a Kirigami message sets its visible to false, which removes a binding: the next error would never show. A message with a close
    button therefore sets visible by hand (visible: false to start with), as the pages that have one do. (Checked on the Discover pages, whose
    errors come again and again; the Install page's one-time notice is set once when the page is made.)"""
    offenders = []
    for f in sorted(QML / name for name in PAGES):
        for line_no, body in _inline_messages(f.read_text()):
            closable = re.search(r"showCloseButton:\s*true", body)
            bound = re.search(r"^\s*visible:\s*(?!false\b)(?!true\b)\S", body, re.M)
            if closable and bound:
                offenders.append(f"{f.name}:{line_no}")
    assert offenders == []


def test_show_more_continues_what_was_asked_not_what_is_typed_since():
    code = (QML / "DiscoverPage.qml").read_text()
    search = code[code.index("function search(more)"):code.index("function searchAur()")]
    assert "query: resultQuery" in search and "group: resultGroup" in search and "kind: resultKind" in search
    assert "backend.discoverSearch(asked.query, asked.group, asked.kind, offset)" in search
    assert "resultQuery = asked.query" in search  # remembered only when the answer is the one still wanted (after the sequence check)
    assert search.index("if (seq !== searchSeq)") < search.index("resultQuery = asked.query")


def test_the_makers_download_buttons_are_one_per_kind_and_hand_over_through_the_backend():
    code = (QML / "DiscoverAppPage.qml").read_text()
    assert "backend.fetchReleaseDownload(source.key, format)" in code
    assert 'model: card.modelData.kind === "release" ? card.modelData.formats : []' in code  # a button exists only for a kind the project has
    assert 'objectName: "releaseButton_" + modelData' in code and "page.fetchRelease(card.modelData, modelData)" in code
    assert 'root.openPage("install"' in code[code.index("function fetchRelease"):code.index("function releaseButtonText")]  # handed over, never installed from here


def test_the_makers_download_says_it_is_not_watched_for_updates_and_how_it_was_checked():
    code = (QML / "DiscoverAppPage.qml").read_text()
    assert "Cygnus does not watch it for updates" in code
    notice = code[code.index("function fetchRelease"):code.index("function releaseButtonText")]
    assert "checksum GitHub publishes for it" in notice and "could not check it against a checksum" in notice and "nothing is installed until you confirm" in notice


def test_the_release_page_can_be_opened_from_the_card_and_only_when_it_is_an_https_address():
    code = (QML / "DiscoverAppPage.qml").read_text()
    block = code[code.index('objectName: "releasePageButton"'):][:420]
    assert 'card.modelData.page.indexOf("https://") === 0' in block and "Qt.openUrlExternally(card.modelData.page)" in block


def test_the_source_filter_offers_the_makers_downloads():
    code = (QML / "DiscoverPage.qml").read_text()
    assert 'value: "release"' in code and 'release: qsTr("Maker\'s own download")' in code


def test_the_aur_is_asked_only_by_the_button_never_by_typing():
    """The words in the box are sent to aur.archlinux.org, which a search of the local lists never does: only a press may do it."""
    code = (QML / "DiscoverPage.qml").read_text()
    calls = [m.start() for m in re.finditer(r"backend\.discoverAur\(", code)]
    assert len(calls) == 1 and "function searchAur()" in code[:calls[0]][-600:]
    uses = [m.start() for m in re.finditer(r"searchAur\(\)", code)]
    assert len(uses) == 2  # its definition, and the one place that calls it
    caller = code[uses[1] - 80:uses[1] + 20]
    assert "onClicked" in caller
    assert 'objectName: "aurSearchButton"' in code and "searchField.text.trim().length >= 2" in code


def test_the_online_catalogues_are_downloaded_when_the_page_is_opened_or_asked_and_never_by_reading():
    code = (QML / "DiscoverPage.qml").read_text()
    assert code.count("backend.catalogueRefresh(") == 1
    assert "Component.onCompleted: load(true)" in code and "onTriggered: page.load(false)" in code  # the Reload button reads, it does not download
    assert "onClicked: page.refreshCatalogues(true)" in code
    load = code[code.index("function load(withCatalogues)"):code.index("function setGroup(")]
    assert "if (withCatalogues && overview.catalogues_decided && overview.catalogues.some(c => c.on && c.stale))" in load  # not before the person has said yes
    assert "backend.catalogueDecide(accept)" in code and "visible: !!page.overview && !page.overview.catalogues_decided" in code
    assert "backend.catalogueSetOn(id, on)" in code and "onToggled: page.setCatalogue(" in code


def test_every_download_button_hands_over_to_the_install_page_and_nothing_is_installed_from_the_card():
    code = (QML / "DiscoverAppPage.qml").read_text()
    for name, backend_call in (("fetchAppimage", "backend.fetchAppimageDownload(source.repo)"), ("fetchDebian", "backend.fetchDebianDownload(source.package)"),
                               ("fetchFedora", "backend.fetchFedoraDownload(source.package)")):
        body = code[code.index(f"function {name}("):]
        body = body[:body.index("\n    }\n")]
        assert backend_call in body and 'root.openPage("install"' in body and "backend.install" not in body
        assert "nothing is installed until you confirm" in body.lower()
    assert "backend.install" not in code


def test_a_program_from_another_system_says_it_may_not_work_before_its_button():
    code = (QML / "DiscoverAppPage.qml").read_text()
    warning = code[code.index('objectName: "foreignWarning"'):][:1800]
    assert "This may not work: it was built for Fedora." in warning and "This may not work: it was built for Debian." in warning
    assert "Cygnus does not fetch them" in warning


def _blocks(text, opener):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{opener}\s*\{{\s*$", line):
            depth, j, body = 1, i + 1, []
            while j < len(lines) and depth:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth >= 1:
                    body.append(lines[j])
                j += 1
            yield i + 1, "\n".join(body)


@pytest.mark.parametrize("name", PAGES)
def test_every_label_that_shows_something_from_a_list_is_plain_text(name):
    """Labels read markup by default: a name, summary or package from a list somebody else wrote must never be shown as markup (a label whose text mentions
    list data, the entry, the AUR's answer or the catalogue rows has textFormat: Text.PlainText)."""
    offenders = []
    for opener in (r"QQC2\.Label", r"Kirigami\.Heading"):
        for line_no, body in _blocks((QML / name).read_text(), opener):
            text = re.search(r"^\s*text:(.*)$", body, re.M)
            if text and re.search(r"modelData|entry\.|aurCard|page\.entry|overview\.|catalogueRow|aurResult", text.group(1)) and "textFormat: Text.PlainText" not in body:
                offenders.append(f"{name}:{line_no}")
    assert offenders == []


def test_every_download_button_is_disabled_while_another_download_is_going():
    code = (QML / "DiscoverAppPage.qml").read_text()
    for name in ("appimageButton", "debianButton", "fedoraButton", "debButton", "rpmButton", "flatpakButton", "repoButton"):
        block = code[code.index(f'objectName: "{name}"'):][:420]
        assert 'enabled: page.busyText === ""' in block, name
    assert 'enabled: page.busyText === ""' in code[code.index('objectName: "releaseButton_" + modelData'):][:200]


def test_the_catalogue_switches_and_the_update_button_exist_only_once_the_question_is_answered():
    code = (QML / "DiscoverPage.qml").read_text()
    assert "model: page.overview && page.overview.catalogues_decided ? page.overview.catalogues : []" in code
    button = code[code.index('objectName: "catalogueUpdateButton"'):][:400]
    assert "page.overview.catalogues_decided" in button
    assert "page.load(false); // the switch shows what is saved" in code  # a failed switch is put back to what is saved


def test_a_download_asked_for_while_another_is_running_is_asked_again_when_it_is_done():
    code = (QML / "DiscoverPage.qml").read_text()
    body = code[code.index("function refreshCatalogues(force)"):code.index("function setCatalogue(")]
    assert "refreshAgain = true" in body and "if (refreshAgain)" in body and "r.result.busy" in body


def test_an_overview_that_arrives_after_a_newer_one_was_asked_for_is_dropped():
    code = (QML / "DiscoverPage.qml").read_text()
    body = code[code.index("function load(withCatalogues)"):code.index("function setGroup(")]
    assert "const seq = ++loadSeq" in body and "if (seq !== loadSeq)" in body


def test_a_busy_answer_makes_the_page_ask_again_in_a_moment():
    code = (QML / "DiscoverPage.qml").read_text()
    assert "retryRefresh.restart()" in code[code.index("function refreshCatalogues(force)"):code.index("function setCatalogue(")]
    timer = code[code.index("id: retryRefresh"):][:140]
    assert "interval: 5000" in timer and "page.refreshCatalogues(false)" in timer
